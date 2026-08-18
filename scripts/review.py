#!/usr/bin/env python3
"""Pick a git review target, view it in tuicr, send inline notes to an agent pane.

Subcommands:
  open-picker  action: open the picker pane in the current tab (REQ-001)
  picker       pane entrypoint: fzf target picker that becomes the tuicr viewer
               (REQ-002..005)
  send-notes   action: paste unsent review comments into the resolved agent
               pane's input as one editable draft (REQ-006..008)

Design (DEC-001, DEC-002): single stdlib-only file. Pure decision functions
take injected runner callables; subprocess glue stays thin so tests never
shell out to real herdr/git/tuicr.
"""

import fcntl
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

USAGE = "usage: review.py {open-picker|picker|send-notes}"

PANES_STATE = "panes.json"  # repo root -> viewer pane id (written on exec only)
SENT_STATE = "sent.json"  # tuicr session slug -> [delivered comment id, ...]
SEND_LOCK = "send.lock"  # cross-process claim for the whole send flow

# ---------------------------------------------------------------------------
# State IO (JSON files under HERDR_PLUGIN_STATE_DIR, atomic temp + rename)
# ---------------------------------------------------------------------------


def state_dir():
    """Directory for plugin state files, created on first use."""
    root = os.environ.get("HERDR_PLUGIN_STATE_DIR") or os.path.join(
        tempfile.gettempdir(), "herdr-review-state"
    )
    path = Path(root)
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json_state(name, default):
    """Read state file `name`; a missing or corrupt file yields `default`."""
    try:
        with open(state_dir() / name, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json_state(name, data):
    """Atomically replace state file `name` (temp file + os.replace)."""
    directory = state_dir()
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, directory / name)
    except BaseException:
        # Never leave a half-written temp file next to real state.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Subprocess runners (thin; pure decision functions receive these injected)
# ---------------------------------------------------------------------------


def _run(argv):
    """Run argv; return stripped stdout on exit 0, None on any failure."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def run_herdr(*args):
    return _run([os.environ.get("HERDR_BIN_PATH", "herdr"), *args])


def run_git(*args):
    return _run(["git", *args])


def run_tuicr(*args):
    return _run(["tuicr", *args])


# ---------------------------------------------------------------------------
# herdr helpers (thin glue over run_herdr)
# ---------------------------------------------------------------------------


def herdr_pane_list():
    """Panes array from `herdr pane list`, or None when herdr is unreachable."""
    out = run_herdr("pane", "list")
    if out is None:
        return None
    try:
        return json.loads(out)["result"]["panes"]
    except (ValueError, KeyError, TypeError):
        return None


def herdr_agent_list():
    """Agents array from `herdr agent list`, or None when herdr is unreachable."""
    out = run_herdr("agent", "list")
    if out is None:
        return None
    try:
        return json.loads(out)["result"]["agents"]
    except (ValueError, KeyError, TypeError):
        return None


def herdr_neighbor_id(pane_id, direction):
    """Neighbor pane id in one direction, or None (no neighbor / error)."""
    out = run_herdr("pane", "neighbor", "--direction", direction, "--pane", pane_id)
    if out is None:
        return None
    try:
        # Without a neighbor the envelope still returns rc 0 but omits
        # neighbor_pane_id, so .get() is the whole no-neighbor handling.
        return json.loads(out)["result"]["neighbor"].get("neighbor_pane_id")
    except (ValueError, KeyError, TypeError, AttributeError):
        return None



def herdr_send_input(pane_id, text):
    """Paste text into a pane's input WITHOUT submitting it; True on success.

    Speaks the daemon's newline-delimited JSON API directly because no herdr
    0.8 CLI verb covers this: `pane send-text` writes raw bytes (a chat TUI
    may treat embedded newlines as submissions), while `agent prompt` and
    `pane run` append Enter. `pane.send_input` with no keys goes through the
    server's bracketed-paste encoding, so multi-line text lands in the
    composer exactly like a human paste. HERDR_SOCKET_PATH is injected into
    plugin action environments by the server. Success requires the exact
    v0.8.0 envelope (echoed request id + result.type == "ok"): anything
    else — error response, empty object, protocol drift in a future herdr —
    fails visibly here instead of silently consuming the notes."""
    socket_path = os.environ.get("HERDR_SOCKET_PATH")
    if not socket_path:
        return False
    request_id = "herdr-review:send-input"
    request = json.dumps(
        {
            "id": request_id,
            "method": "pane.send_input",
            "params": {"pane_id": pane_id, "text": text},
        }
    )
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(5)
            conn.connect(socket_path)
            conn.sendall(request.encode("utf-8") + b"\n")
            with conn.makefile("rb") as reader:
                line = reader.readline()
    except OSError:
        return False
    try:
        response = json.loads(line.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return False
    if not isinstance(response, dict) or response.get("id") != request_id:
        return False
    result = response.get("result")
    return isinstance(result, dict) and result.get("type") == "ok"


def fail_action(message):
    """DEC-011: action-layer failure -> notification + stderr, exit 1."""
    print(message, file=sys.stderr)
    run_herdr("notification", "show", message)
    return 1


# ---------------------------------------------------------------------------
# Pure decision functions (injected data/runners only; no subprocess calls)
# ---------------------------------------------------------------------------


def resolve_review_cwd(context_json, pane_list):
    """Cwd for the review picker (DEC-004): action context first, then the
    focused pane from `herdr pane list`."""
    if isinstance(context_json, dict):
        cwd = context_json.get("focused_pane_cwd")
        if cwd:
            return cwd
    for pane in pane_list or []:
        if pane.get("focused") and pane.get("cwd"):
            return pane["cwd"]
    return None


def _split_ref(refname, remotes):
    """Full refname -> (short, branch_part, is_remote), or None to skip.

    branch_part strips the remote prefix by longest match against the
    configured remote names: remote names may themselves contain slashes
    (`git remote add team/origin` is legal), so a fixed first-segment split
    would misparse refs/remotes/team/origin/feature-b and let that remote's
    copy of the current branch escape the own-copy exclusion. Refs under an
    unconfigured layout fall back to the first-segment split. Parsing the
    full refname also keeps nested local names (feature/x) out of the
    remote-copy exclusion."""
    if refname.startswith("refs/heads/"):
        name = refname[len("refs/heads/"):]
        return name, name, False
    if not refname.startswith("refs/remotes/"):
        return None
    rest = refname[len("refs/remotes/"):]
    prefix = max(
        (r for r in remotes or [] if rest.startswith(r + "/")),
        key=len,
        default=None,
    )
    if prefix:
        return rest, rest[len(prefix) + 1:], True
    if "/" not in rest:
        return rest, rest, True
    return rest, rest.split("/", 1)[1], True


def _fork_ref_rows(output, branch, remotes):
    """Parse `for-each-ref` lines into candidate (refname, short, branch_part,
    is_remote, tip) rows.

    Skips the current branch, every remote's copy of it (their history is our
    own, so they would win the fork-point race and shrink the diff to the
    unpushed commits, AC-006), and remote HEAD alias rows. tip is None when
    the format carried no objectname column."""
    rows = []
    for line in (output or "").splitlines():
        parts = line.split()
        if not parts:
            continue
        refname = parts[0]
        tip = parts[1] if len(parts) > 1 else None
        split = _split_ref(refname, remotes)
        if split is None:
            continue
        short, branch_part, is_remote = split
        if branch_part == "HEAD" or (branch and branch_part == branch):
            continue
        rows.append((refname, short, branch_part, is_remote, tip))
    return rows


CONVENTIONAL_BRANCHES = ("main", "master", "trunk")


def resolve_fork_parent(git, branch):
    """Nearest branch this branch was forked from, or None.

    Stacked branches (feature-b forked from feature-a) must review against
    feature-a, not the repo default branch. Git records no parent-branch
    metadata, so this walks the first-parent chain to the first commit any
    other branch contains (the fork point), then labels it with the best
    containing ref. Fixed six git calls regardless of branch count.

    Refs that contain HEAD are children of this branch or twins at its tip:
    they can never be the fork parent, and leaving them in would collapse
    the fork point to HEAD (a stack's child at our tip made detection bail
    to the trunk fallback). Both the fork-point race and the labeling drop
    them. A child forked from a mid-chain commit after this branch advanced
    is indistinguishable from a parent by reachability alone; the menu row
    shows whatever won, and an explicit `--set-upstream-to` parent (REQ-003
    first step) overrides detection entirely."""
    remotes = (git("remote") or "").splitlines()
    refs_out = git("for-each-ref", "--format=%(refname)", "refs/heads", "refs/remotes")
    head_holders = git(
        "for-each-ref", "--format=%(refname)", "--contains", "HEAD",
        "refs/heads", "refs/remotes",
    )
    if not head_holders:
        # The current branch always contains HEAD, so an empty answer means
        # the probe failed — and without it a child could pose as parent.
        return None
    descendants = set(head_holders.split())
    candidates = [
        row[0]
        for row in _fork_ref_rows(refs_out, branch, remotes)
        if row[0] not in descendants
    ]
    if not candidates:
        return None
    count = git("rev-list", "--count", "--first-parent", "HEAD", "--not", *candidates)
    try:
        exclusive = int(count)
    except (TypeError, ValueError):
        return None
    if exclusive == 0:
        # Descendants were excluded above, so this means the candidate set
        # raced a ref update; treat as no parent rather than guess.
        return None
    fork_point = git("rev-parse", f"HEAD~{exclusive}")
    if not fork_point:
        # Entire history is exclusive (unrelated branches, shallow clone).
        return None
    containing = git(
        "for-each-ref",
        "--format=%(refname) %(objectname)",
        "--contains", fork_point,
        "refs/heads", "refs/remotes",
    )
    rows = [
        row
        for row in _fork_ref_rows(containing, branch, remotes)
        if row[0] not in descendants
    ]
    if not rows:
        return None
    # An unmoved local parent (tip == fork point) is the branch we forked
    # from, verbatim; conventional trunks beat siblings that merely contain
    # the fork point; locals beat their remote copies.
    best = min(
        rows,
        key=lambda row: (
            0 if row[4] == fork_point else 1,
            1 if row[3] else 0,
            0 if row[2] in CONVENTIONAL_BRANCHES else 1,
            row[1],
        ),
    )
    return best[1]


def resolve_base(git):
    """Merge-base ref for the review menu (REQ-003), or None.

    `git` is an injected runner: git(*args) -> stripped stdout, None on failure.
    Order: explicit non-own-tracking upstream, fork-parent detection (skipped
    on conventional trunk branches, which have no parent), origin/HEAD, then
    conventional names. `@{u}` is skipped when it is the current branch's own
    remote-tracking ref, which would diff to nothing (AC-006)."""
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    upstream = git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if upstream:
        own_tracking = (
            branch and "/" in upstream and upstream.split("/", 1)[1] == branch
        )
        if not own_tracking:
            return upstream
    origin_head = git("rev-parse", "--abbrev-ref", "origin/HEAD")
    on_trunk = branch in CONVENTIONAL_BRANCHES or (
        branch
        and origin_head
        and "/" in origin_head
        and origin_head.split("/", 1)[1] == branch
    )
    if branch and not on_trunk:
        fork_parent = resolve_fork_parent(git, branch)
        if fork_parent:
            return fork_parent
    if origin_head:
        return origin_head
    for name in CONVENTIONAL_BRANCHES:
        if git("rev-parse", "--verify", "--quiet", name) is not None:
            return name
    return None


def build_menu(base):
    """Target menu rows as (key, label), first row = default (REQ-002)."""
    rows = []
    if base:
        rows.append(("merge-base", f"Merge base ({base}...HEAD)"))
    rows.extend(
        [
            ("uncommitted", "Uncommitted"),
            ("last-commit", "Last commit"),
            ("pick-commit", "Pick commit"),
            ("pick-range", "Pick range"),
            ("branch-vs-branch", "Branch vs branch"),
        ]
    )
    return rows


def target_argv(key, base=None, sha=None, old=None, new=None, compare=None):
    """DEC-005 table -> exec_argv for tuicr.

    tuicr takes one `-r <revset>` instead of hunk's diff/show verbs, and `-w`
    for worktree state. `-w` alone is uncommitted-only; combined with `-r` it
    extends the range through the worktree (DEC-015)."""
    if key == "merge-base":
        return ["tuicr", "-r", f"{base}...HEAD"]
    if key == "uncommitted":
        # No watch equivalent in tuicr — the viewer is a snapshot (DEC-016).
        return ["tuicr", "-w"]
    if key == "last-commit":
        return ["tuicr", "-r", "HEAD"]
    if key == "pick-commit":
        return ["tuicr", "-r", sha]
    if key == "pick-range":
        # One mark: that commit through the worktree, so -w joins the range.
        if new:
            return ["tuicr", "-r", f"{old}..{new}"]
        return ["tuicr", "-r", f"{old}..HEAD", "-w"]
    if key == "branch-vs-branch":
        return ["tuicr", "-r", f"{base}...{compare}"]
    raise ValueError(f"unknown target: {key}")


def resolve_agent(focused_pane_id, focused_tab_id, agent_panes, neighbor_ids, repo, repo_root_of):
    """DEC-009 agent resolution -> (pane_id, []) or (None, candidate_ids).

    agent_panes: entries from `herdr agent list` (pane_id / tab_id / cwd).
    neighbor_ids: neighbor pane ids in probe order left/right/up/down;
    None entries (no neighbor) are skipped.
    repo_root_of: injected cwd -> repo-root resolver (None when not a repo)."""
    agent_ids = {p.get("pane_id") for p in agent_panes or []}
    if focused_pane_id in agent_ids:
        return focused_pane_id, []
    for neighbor in neighbor_ids or []:
        if neighbor and neighbor in agent_ids:
            return neighbor, []
    candidates = [
        p["pane_id"]
        for p in agent_panes or []
        if p.get("tab_id") == focused_tab_id and repo_root_of(p.get("cwd")) == repo
    ]
    if len(candidates) == 1:
        return candidates[0], []
    return None, candidates


def format_prompt(worktree, comments):
    """DEC-010 fixed English template; one `path:lines — content` row per comment.

    tuicr carries real ranges, so a multi-line comment renders `path:10-14`
    rather than collapsing to its first line. A file-level comment has no
    lines, a review-level comment has no path, and a classified comment keeps
    its `[type]` tag so the agent sees nit vs issue (DEC-020). Multi-line
    bodies pass through verbatim."""
    lines = [f"Human inline review comments on your changes in {worktree}:", ""]
    for comment in comments:
        location = comment.get("path") or "(review)"
        start = comment.get("start_line")
        end = comment.get("end_line")
        if start is not None:
            span = f"{start}-{end}" if end is not None and end != start else f"{start}"
            location = f"{location}:{span}"
        kind = comment.get("comment_type")
        tag = f"[{kind}] " if kind and kind != "none" else ""
        lines.append(f"- {location} — {tag}{comment.get('content', '')}")
    lines.append("")
    lines.append(
        "Address each comment and verify the result. "
        "If a comment is unclear, ask a focused question before proceeding."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Picker pane helpers
# ---------------------------------------------------------------------------


def wait_for_keypress():
    """Block until one key (tty) or one line (pipe) so the user can read the
    message shown in the pane before it closes (DEC-011)."""
    if sys.stdin.isatty():
        import termios
        import tty

        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            os.read(fd, 1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    else:
        sys.stdin.readline()


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text):
    return ANSI_RE.sub("", text)


def run_fzf(lines, *fzf_args):
    """Run fzf over lines (UI renders on /dev/tty even with stdio piped).

    Returns selection stdout (multi-select: newline-joined) or None on user
    cancel — fzf exits 130 on Esc/interrupt, 1 on no match. Anything else
    (missing binary, permission, unexpected status) raises RuntimeError so
    callers surface it instead of miming a cancel (DEC-011 fail-fast)."""
    try:
        proc = subprocess.run(
            ["fzf", *fzf_args],
            input="\n".join(lines) + "\n",
            stdout=subprocess.PIPE,
            text=True,
        )
    except OSError as error:
        raise RuntimeError(f"fzf failed to start: {error}")
    if proc.returncode in (1, 130):
        return None
    if proc.returncode != 0:
        raise RuntimeError(f"fzf exited with status {proc.returncode}")
    out = proc.stdout.strip("\n")
    return out if out else None


def git_log_lines():
    """Colored `git log --oneline -200` lines (DEC-006), or None."""
    out = run_git("log", "--oneline", "--color=always", "-200")
    if not out:
        return None
    return out.splitlines()


def pick_commit_sha():
    lines = git_log_lines()
    if not lines:
        return None
    selected = run_fzf(lines, "--ansi")
    if selected is None:
        return None
    # fzf --ansi strips color codes from the output line, so field 0 is the sha.
    return selected.split()[0]


WORKTREE_ROW = "(worktree)"


def pick_range_shas():
    """(old, new) shas via two fzf rounds; (old, None) means old..worktree.

    Round one picks the old end over the full log; round two lists only
    commits newer than it (log is newest-first) plus a leading `(worktree)`
    row as the default, so the range is old..new by construction. The
    original single-round `--multi 2` flow required knowing fzf's Tab
    marking, and Enter with one mark silently launched the single-commit
    fallback — which read as 'cannot select the second commit'."""
    lines = git_log_lines()
    if not lines:
        return None
    old_line = run_fzf(lines, "--ansi", "--prompt", "old> ")
    if old_line is None:
        return None
    # fzf --ansi strips color codes from the output line, so field 0 is the sha.
    old = old_line.split()[0]
    log_order = [strip_ansi(line).split()[0] for line in lines]
    try:
        newer = lines[: log_order.index(old)]
    except ValueError:
        return None
    new_line = run_fzf([WORKTREE_ROW, *newer], "--ansi", "--prompt", "new> ")
    if new_line is None:
        return None
    if new_line == WORKTREE_ROW:
        return old, None
    return old, new_line.split()[0]


def pick_branches():
    """(base, compare) via two fzf rounds over local + remote branches."""
    out = run_git(
        "branch", "-a", "--format=%(refname:short)", "--sort=-committerdate"
    )
    if out is None:
        return None
    # origin/HEAD is an alias row, not a reviewable branch (DEC-006).
    branches = [b for b in out.splitlines() if b and b != "origin/HEAD"]
    if not branches:
        return None
    chosen_base = run_fzf(branches, "--prompt", "base> ")
    if chosen_base is None:
        return None
    compare = run_fzf(branches, "--prompt", "compare> ")
    if compare is None:
        return None
    return chosen_base, compare


def pick_target(key, base):
    """Sub-picker flow for a menu key -> (exec_argv, reload_args) or None."""
    if key == "merge-base":
        return target_argv("merge-base", base=base)
    if key == "uncommitted":
        return target_argv("uncommitted")
    if key == "last-commit":
        return target_argv("last-commit")
    if key == "pick-commit":
        sha = pick_commit_sha()
        if sha is None:
            return None
        return target_argv("pick-commit", sha=sha)
    if key == "pick-range":
        pair = pick_range_shas()
        if pair is None:
            return None
        old, new = pair
        return target_argv("pick-range", old=old, new=new)
    if key == "branch-vs-branch":
        pair = pick_branches()
        if pair is None:
            return None
        chosen_base, compare = pair
        return target_argv("branch-vs-branch", base=chosen_base, compare=compare)
    return None


def launch_viewer(repo, exec_argv, herdr=None, execvp=None):
    """DEC-007: close any recorded viewer for repo, then exec into this pane.

    tuicr has no reload verb, so a target switch cannot mutate a running
    viewer in place. Closing the stale pane and letting the picker pane become
    the new viewer keeps the one-viewer-per-repo invariant of AC-007; tuicr
    persists comments per target slug, so revisiting a target restores its
    notes instead of losing them (DEC-017).

    Runners are injectable for tests; defaults hit the real subprocesses."""
    herdr = herdr or run_herdr
    execvp = execvp or os.execvp

    pane_id = os.environ.get("HERDR_PANE_ID")
    mapping = read_json_state(PANES_STATE, {})
    old_pane = mapping.get(repo)
    if old_pane and old_pane != pane_id:
        # Stale ids are harmless: close failure is ignored by design.
        herdr("plugin", "pane", "close", old_pane)

    if pane_id:
        mapping[repo] = pane_id
        write_json_state(PANES_STATE, mapping)
    execvp(exec_argv[0], exec_argv)
    return 0  # reached only when execvp is a test recorder


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_open_picker(args):
    """Action: open the picker pane in the current tab (REQ-001, DEC-004)."""
    context = None
    raw = os.environ.get("HERDR_PLUGIN_CONTEXT_JSON")
    if raw:
        try:
            context = json.loads(raw)
        except ValueError:
            context = None
    cwd = resolve_review_cwd(context, herdr_pane_list())
    if not cwd:
        return fail_action("herdr-review: cannot determine focused pane cwd")
    out = run_herdr(
        "plugin", "pane", "open",
        "--plugin", "herdr-review",
        "--entrypoint", "picker",
        "--placement", "split",
        "--direction", "right",
        "--cwd", cwd,
        "--focus",
    )
    if out is None:
        return fail_action("herdr-review: failed to open picker pane")
    return 0


def cmd_picker(args):
    """Pane entrypoint: guard, then target menu (REQ-001..005)."""
    repo = run_git("rev-parse", "--show-toplevel")
    if repo is None:
        # AC-002: tell the human, wait so they can read it, close cleanly.
        print("not a git repository")
        wait_for_keypress()
        return 0
    os.chdir(repo)
    try:
        base = resolve_base(run_git)
        menu = build_menu(base)
        # --layout=reverse renders input order top-down with the cursor on the
        # first row, matching REQ-002's menu order + default selection.
        label = run_fzf([label for _, label in menu], "--layout=reverse")
        if label is None:
            return 0  # Esc: close the pane with no residue (AC-003).
        key = {lbl: k for k, lbl in menu}.get(label)
        if key is None:
            return 0
        selection = pick_target(key, base)
    except RuntimeError as error:
        # Dependency/execution failure is not a cancel: show it, let the
        # human read it, then close (DEC-011).
        print(error)
        wait_for_keypress()
        return 1
    if selection is None:
        return 0  # Esc in any sub-picker also closes cleanly (DEC-006).
    return launch_viewer(repo, selection)


def filter_unsent(comments, sent_ids):
    """Comments whose id has not been delivered yet (AC-014)."""
    sent = set(sent_ids or [])
    return [c for c in comments if c.get("id") not in sent]


def gc_sent(sent, live_slugs):
    """Drop sent entries whose tuicr session no longer exists (DEC-008)."""
    live = set(live_slugs or [])
    return {slug: ids for slug, ids in sent.items() if slug in live}


def tuicr_sessions(repo=None, tuicr=None):
    """Session rows from `tuicr review list`, or None on failure.

    Documented agent-facing contract (tuicr docs/REVIEW_CLI.md): a JSON array
    of {slug, kind, path, updated_at, comment_count, active, ...}. A repo
    scopes the listing to that checkout; omitting it lists every session,
    which is what the sent-record GC must compare against."""
    tuicr = tuicr or run_tuicr
    selector = ("--repo", repo) if repo else ("--all",)
    out = tuicr("review", "list", *selector)
    if out is None:
        return None
    try:
        rows = json.loads(out)
    except ValueError:
        return None
    return rows if isinstance(rows, list) else None


def pick_session(sessions):
    """The session to collect notes from: the live one, else the newest.

    tuicr keeps a session file after the TUI exits, so notes written in a
    closed viewer stay recoverable instead of being stranded the way a dead
    hunk session stranded them. A pick that is not the running viewer is named
    in the notification, so the fallback is visible rather than silent
    (DEC-018)."""
    if not sessions:
        return None
    active = [s for s in sessions if s.get("active")]
    return max(active or sessions, key=lambda s: s.get("updated_at") or "")


def tuicr_comments(repo, slug, tuicr=None):
    """Local-draft comments of a session, newest last, or None on failure.

    `lifecycle_state` filters out comments already published to a forge, the
    way hunk's `--type user` filtered out agent notes (DEC-019)."""
    tuicr = tuicr or run_tuicr
    out = tuicr("review", "comments", "--session", slug, "--repo", repo)
    if out is None:
        return None
    try:
        rows = json.loads(out)
    except ValueError:
        return None
    if not isinstance(rows, list):
        return None
    return [c for c in rows if c.get("lifecycle_state") == "local_draft"]


def resolve_agent_for(focused_pane, repo):
    """Glue: gather herdr data + repo roots, then run resolve_agent (DEC-009)."""
    agents = herdr_agent_list() or []
    pane_id = focused_pane.get("pane_id")
    directions = ("left", "right", "up", "down")
    neighbor_ids = (
        [herdr_neighbor_id(pane_id, d) for d in directions] if pane_id else []
    )

    def repo_root_of(cwd):
        if not cwd:
            return None
        return run_git("-C", cwd, "rev-parse", "--show-toplevel")

    return resolve_agent(
        pane_id, focused_pane.get("tab_id"), agents, neighbor_ids, repo, repo_root_of
    )


def acquire_send_lock():
    """Claim the send flow across processes, or None when already claimed.

    The duplicate guard is read sent.json -> paste -> write sent.json; two
    overlapping invocations racing that window would each paste the same
    notes. flock serializes the whole flow and releases on process exit, so
    a crashed sender never wedges the next one. The caller must keep the
    returned file object referenced while sending."""
    handle = open(state_dir() / SEND_LOCK, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def cmd_send_notes(args):
    """Action: paste unsent tuicr review comments into the resolved agent
    pane's input as one editable draft (REQ-006..008, DEC-014, DEC-018..021)."""
    focused = None
    for pane in herdr_pane_list() or []:
        if pane.get("focused"):
            focused = pane
            break
    if focused is None:
        return fail_action("herdr-review: cannot determine focused pane")

    cwd = focused.get("cwd")
    repo = run_git("-C", cwd, "rev-parse", "--show-toplevel") if cwd else None
    if repo is None:
        return fail_action("herdr-review: focused pane is not in a git repository")

    sessions = tuicr_sessions(repo)
    if sessions is None:
        return fail_action("herdr-review: failed to list tuicr review sessions")
    session = pick_session(sessions)
    if session is None:
        # AC-011: no session file means nothing was ever reviewed here.
        return fail_action(f"herdr-review: no tuicr review session for {repo}")
    slug = session.get("slug")
    if not slug:
        return fail_action("herdr-review: unexpected tuicr review list output")

    send_lock = acquire_send_lock()  # held (referenced) until process exit
    if send_lock is None:
        run_herdr("notification", "show", "herdr-review: send already in progress")
        return 1

    comments = tuicr_comments(repo, slug)
    if comments is None:
        return fail_action("herdr-review: failed to read tuicr review comments")

    sent = read_json_state(SENT_STATE, {})
    unsent = filter_unsent(comments, sent.get(slug, []))
    if not unsent:
        run_herdr("notification", "show", "No new notes to send")  # AC-010
        return 0

    agent_pane, candidates = resolve_agent_for(focused, repo)
    if agent_pane is None:
        if candidates:
            return fail_action(
                "herdr-review: multiple agent panes match: "
                + ", ".join(candidates)
                + " — focus next to the target agent and retry"
            )
        return fail_action("herdr-review: no agent pane found")

    prompt = format_prompt(repo, unsent)
    # Draft, not submit: the human reviews/edits in the composer and presses
    # Enter themselves (DEC-014).
    if not herdr_send_input(agent_pane, prompt):
        return fail_action("herdr-review: failed to paste notes into agent pane")

    # tuicr has no CLI delete, so the sent-id record is the only duplicate
    # guard — comments stay in the viewer as the review record instead of
    # vanishing on send (DEC-021). Pasted = delivered: a draft the human later
    # discards is not re-sendable (documented in README).
    sent.setdefault(slug, []).extend(c.get("id") for c in unsent)
    live = tuicr_sessions()  # every repo: sent.json is global, unlike the pick
    if live is not None:
        # Keep the current slug even if the list read races a session rewrite.
        sent = gc_sent(sent, {s.get("slug") for s in live} | {slug})
    write_json_state(SENT_STATE, sent)

    # Naming the source only in the fallback case keeps the common
    # notification short while making a stale pick impossible to miss.
    source = "" if session.get("active") else f" from closed session {slug}"
    run_herdr(
        "notification",
        "show",
        f"Pasted {len(unsent)} note(s){source} into {agent_pane}"
        " — press Enter to send",
    )
    return 0


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def main(argv):
    if not argv:
        print(USAGE, file=sys.stderr)
        return 2
    handlers = {
        "open-picker": cmd_open_picker,
        "picker": cmd_picker,
        "send-notes": cmd_send_notes,
    }
    handler = handlers.get(argv[0])
    if handler is None:
        print(f"unknown subcommand: {argv[0]}", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 2
    return handler(argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
