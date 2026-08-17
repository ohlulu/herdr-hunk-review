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
    print("picker: menu not implemented yet", file=sys.stderr)
    return 1


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
