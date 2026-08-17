#!/usr/bin/env python3
"""Pick a git review target, view it in hunk, send inline notes to an agent pane.

Subcommands:
  open-picker  action: open the picker pane in the current tab (REQ-001)
  picker       pane entrypoint: fzf target picker that becomes the hunk viewer
               (REQ-002..005)
  send-notes   action: deliver unsent user hunk notes to the resolved agent
               pane (REQ-006..008)

Design (DEC-001, DEC-002): single stdlib-only file. Pure decision functions
take injected runner callables; subprocess glue stays thin so tests never
shell out to real herdr/git/hunk.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

USAGE = "usage: hunk_review.py {open-picker|picker|send-notes}"

PANES_STATE = "panes.json"  # repo root -> viewer pane id (written on exec only)
SENT_STATE = "sent.json"  # hunk session id -> [delivered noteId, ...]

# ---------------------------------------------------------------------------
# State IO (JSON files under HERDR_PLUGIN_STATE_DIR, atomic temp + rename)
# ---------------------------------------------------------------------------


def state_dir():
    """Directory for plugin state files, created on first use."""
    root = os.environ.get("HERDR_PLUGIN_STATE_DIR") or os.path.join(
        tempfile.gettempdir(), "herdr-hunk-review-state"
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


def run_hunk(*args):
    return _run(["hunk", *args])


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


def resolve_base(git):
    """Merge-base ref for the review menu (REQ-003), or None.

    `git` is an injected runner: git(*args) -> stripped stdout, None on failure.
    `@{u}` is skipped when it is the current branch's own remote-tracking ref,
    which would diff to nothing (AC-006)."""
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    upstream = git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if upstream:
        own_tracking = (
            branch and "/" in upstream and upstream.split("/", 1)[1] == branch
        )
        if not own_tracking:
            return upstream
    origin_head = git("rev-parse", "--abbrev-ref", "origin/HEAD")
    if origin_head:
        return origin_head
    for name in ("main", "master", "trunk"):
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
    """DEC-005 table -> (exec_argv, reload_args).

    exec_argv execs hunk in the picker pane; reload_args go after `--` in
    `hunk session reload`. Only `uncommitted` watches (other targets do not
    change with the worktree), and its reload drops `--watch`."""
    if key == "merge-base":
        spec = f"{base}...HEAD"
        return ["hunk", "diff", spec], ["diff", spec]
    if key == "uncommitted":
        return ["hunk", "diff", "HEAD", "--watch"], ["diff", "HEAD"]
    if key == "last-commit":
        return ["hunk", "show"], ["show"]
    if key == "pick-commit":
        return ["hunk", "show", sha], ["show", sha]
    if key == "pick-range":
        # One mark: diff that commit against the worktree (REQ-002).
        spec = f"{old}..{new}" if new else old
        return ["hunk", "diff", spec], ["diff", spec]
    if key == "branch-vs-branch":
        spec = f"{base}...{compare}"
        return ["hunk", "diff", spec], ["diff", spec]
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


def format_prompt(worktree, notes):
    """DEC-010 fixed English template; one `filePath:line — body` row per note.

    Line number prefers newRange[0], falls back to oldRange[0], else the
    `:line` suffix is omitted. Multi-line bodies pass through verbatim."""
    lines = [f"Human inline review comments on your changes in {worktree}:", ""]
    for note in notes:
        location = note.get("filePath", "")
        line_no = None
        for range_key in ("newRange", "oldRange"):
            rng = note.get(range_key)
            if rng:
                line_no = rng[0]
                break
        if line_no is not None:
            location = f"{location}:{line_no}"
        lines.append(f"- {location} — {note.get('body', '')}")
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

    Returns selection stdout (multi-select: newline-joined) or None on
    cancel/Esc — fzf exits 130 on Esc, 1 on no match."""
    try:
        proc = subprocess.run(
            ["fzf", *fzf_args],
            input="\n".join(lines) + "\n",
            stdout=subprocess.PIPE,
            text=True,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
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


def pick_range_shas():
    """(old, new) shas from up to two marks; (sha, None) for a single mark.

    Order is derived from log position (git log is newest-first, DEC-006:
    diff old..new), not from fzf's mark output order."""
    lines = git_log_lines()
    if not lines:
        return None
    selected = run_fzf(lines, "--ansi", "--multi", "2")
    if selected is None:
        return None
    shas = [line.split()[0] for line in selected.splitlines()]
    if len(shas) == 1:
        return shas[0], None
    if len(shas) != 2:
        return None
    log_order = [strip_ansi(line).split()[0] for line in lines]
    try:
        older_first = sorted(shas, key=log_order.index, reverse=True)
    except ValueError:
        return None
    return older_first[0], older_first[1]


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


def launch_viewer(repo, exec_argv, reload_args, hunk=None, herdr=None, execvp=None):
    """DEC-007: reuse the live hunk session for repo, else exec into a viewer.

    Runners are injectable for tests; defaults hit the real subprocesses."""
    hunk = hunk or run_hunk
    herdr = herdr or run_herdr
    execvp = execvp or os.execvp

    if hunk("session", "get", "--repo", repo, "--json") is not None:
        # Live session: reload it in place, hand focus back to the recorded
        # viewer pane, and let this picker pane exit (AC-007). Never record
        # our own pane id here — that would point the mapping at a pane that
        # is about to close (DEC-007).
        if hunk("session", "reload", "--repo", repo, "--", *reload_args) is None:
            print("hunk session reload failed")
            wait_for_keypress()
            return 1
        old_pane = read_json_state(PANES_STATE, {}).get(repo)
        if old_pane:
            # Stale ids are harmless: focus failure is ignored by design.
            herdr("plugin", "pane", "focus", old_pane)
        return 0

    pane_id = os.environ.get("HERDR_PANE_ID")
    if pane_id:
        mapping = read_json_state(PANES_STATE, {})
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
        return fail_action("hunk-review: cannot determine focused pane cwd")
    out = run_herdr(
        "plugin", "pane", "open",
        "--plugin", "herdr-hunk-review",
        "--entrypoint", "picker",
        "--placement", "split",
        "--direction", "right",
        "--cwd", cwd,
        "--focus",
    )
    if out is None:
        return fail_action("hunk-review: failed to open picker pane")
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
    if selection is None:
        return 0  # Esc in any sub-picker also closes cleanly (DEC-006).
    exec_argv, reload_args = selection
    return launch_viewer(repo, exec_argv, reload_args)


def filter_unsent(notes, sent_ids):
    """Notes whose noteId has not been delivered yet (AC-014)."""
    sent = set(sent_ids or [])
    return [note for note in notes if note.get("noteId") not in sent]


def gc_sent(sent, live_session_ids):
    """Drop sent entries whose hunk session no longer exists (DEC-008)."""
    live = set(live_session_ids or [])
    return {sid: ids for sid, ids in sent.items() if sid in live}


def hunk_live_session_ids():
    """Session ids from `hunk session list --json`, or None on failure."""
    out = run_hunk("session", "list", "--json")
    if out is None:
        return None
    try:
        return [s.get("sessionId") for s in json.loads(out)["sessions"]]
    except (ValueError, KeyError, TypeError):
        return None


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


def cmd_send_notes(args):
    """Action: deliver unsent user hunk notes to the resolved agent pane
    (REQ-006..008, DEC-008..011)."""
    focused = None
    for pane in herdr_pane_list() or []:
        if pane.get("focused"):
            focused = pane
            break
    if focused is None:
        return fail_action("hunk-review: cannot determine focused pane")

    cwd = focused.get("cwd")
    repo = run_git("-C", cwd, "rev-parse", "--show-toplevel") if cwd else None
    if repo is None:
        return fail_action("hunk-review: focused pane is not in a git repository")

    out = run_hunk("session", "get", "--repo", repo, "--json")
    session_id = None
    if out is not None:
        try:
            session_id = json.loads(out)["session"]["sessionId"]
        except (ValueError, KeyError, TypeError):
            session_id = None
    if not session_id:
        # AC-011: without a live session there is nothing to collect.
        return fail_action(f"hunk-review: no live hunk session for {repo}")

    out = run_hunk(
        "session", "comment", "list", "--repo", repo, "--type", "user", "--json"
    )
    if out is None:
        return fail_action("hunk-review: failed to list hunk notes")
    try:
        notes = json.loads(out)["comments"]
    except (ValueError, KeyError, TypeError):
        return fail_action("hunk-review: unexpected hunk comment list output")

    sent = read_json_state(SENT_STATE, {})
    unsent = filter_unsent(notes, sent.get(session_id, []))
    if not unsent:
        run_herdr("notification", "show", "No new notes to send")  # AC-010
        return 0

    agent_pane, candidates = resolve_agent_for(focused, repo)
    if agent_pane is None:
        if candidates:
            return fail_action(
                "hunk-review: multiple agent panes match: "
                + ", ".join(candidates)
                + " — focus next to the target agent and retry"
            )
        return fail_action("hunk-review: no agent pane found")

    prompt = format_prompt(repo, unsent)
    # No --wait: fire-and-forget so the action returns immediately (DEC-009).
    if run_herdr("agent", "prompt", agent_pane, prompt) is None:
        return fail_action("hunk-review: failed to prompt agent")

    # Mark BEFORE comment rm: if rm fails the note stays visible in hunk but
    # must never be delivered twice (AC-014).
    delivered = [note.get("noteId") for note in unsent]
    sent.setdefault(session_id, []).extend(delivered)
    live = hunk_live_session_ids()
    if live is not None:
        # Keep the current session even if the list read races the daemon.
        sent = gc_sent(sent, set(live) | {session_id})
    write_json_state(SENT_STATE, sent)

    failed_rm = [
        note_id
        for note_id in delivered
        if run_hunk("session", "comment", "rm", "--repo", repo, note_id) is None
    ]
    if failed_rm:
        # Notify only; the sent-id record already guards against re-delivery.
        run_herdr(
            "notification",
            "show",
            f"hunk-review: failed to remove {len(failed_rm)} note(s) from hunk",
        )

    run_herdr("notification", "show", f"Sent {len(unsent)} note(s) to {agent_pane}")
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
