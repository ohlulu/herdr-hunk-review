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


def launch_viewer(repo, exec_argv, reload_args):
    """Reuse a live hunk session or exec into a new viewer (T009/T010)."""
    print("viewer launch not implemented yet", file=sys.stderr)
    return 1


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


def cmd_send_notes(args):
    print("send-notes: not implemented yet", file=sys.stderr)
    return 1


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
