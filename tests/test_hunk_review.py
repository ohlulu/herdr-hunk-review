"""Tests for scripts/hunk_review.py (stdlib unittest, no real subprocesses)."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import hunk_review as hr


class StateIOTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state_dir = Path(tmp.name)

        original = os.environ.get("HERDR_PLUGIN_STATE_DIR")
        os.environ["HERDR_PLUGIN_STATE_DIR"] = tmp.name

        def restore():
            if original is None:
                os.environ.pop("HERDR_PLUGIN_STATE_DIR", None)
            else:
                os.environ["HERDR_PLUGIN_STATE_DIR"] = original

        self.addCleanup(restore)

    def test_missing_file_returns_default(self):
        self.assertEqual(hr.read_json_state("panes.json", {}), {})

    def test_corrupt_file_returns_default(self):
        (self.state_dir / "sent.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(hr.read_json_state("sent.json", {"d": 1}), {"d": 1})

    def test_write_then_read_roundtrip(self):
        data = {"repo": "pane-1", "ids": ["a", "b"], "n": 3}
        hr.write_json_state("panes.json", data)
        self.assertEqual(hr.read_json_state("panes.json", None), data)

    def test_rewrite_replaces_content_and_leaves_no_temp_files(self):
        hr.write_json_state("sent.json", {"a": 1})
        hr.write_json_state("sent.json", {"b": 2})
        self.assertEqual(hr.read_json_state("sent.json", None), {"b": 2})
        self.assertEqual(os.listdir(self.state_dir), ["sent.json"])

    def test_failed_write_preserves_existing_state(self):
        hr.write_json_state("sent.json", {"a": 1})
        with mock.patch.object(hr.json, "dump", side_effect=ValueError("boom")):
            with self.assertRaises(ValueError):
                hr.write_json_state("sent.json", {"b": 2})
        # Old content survives the aborted replace; no temp litter either.
        self.assertEqual(hr.read_json_state("sent.json", None), {"a": 1})
        self.assertEqual(os.listdir(self.state_dir), ["sent.json"])


if __name__ == "__main__":
    unittest.main()


class ResolveReviewCwdTests(unittest.TestCase):
    def test_context_cwd_wins(self):
        ctx = {"focused_pane_cwd": "/repo/a"}
        panes = [{"pane_id": "w1:p1", "focused": True, "cwd": "/repo/b"}]
        self.assertEqual(hr.resolve_review_cwd(ctx, panes), "/repo/a")

    def test_falls_back_to_focused_pane(self):
        panes = [
            {"pane_id": "w1:p1", "focused": False, "cwd": "/repo/a"},
            {"pane_id": "w1:p2", "focused": True, "cwd": "/repo/b"},
        ]
        self.assertEqual(hr.resolve_review_cwd(None, panes), "/repo/b")
        self.assertEqual(hr.resolve_review_cwd({}, panes), "/repo/b")

    def test_no_focused_pane_returns_none(self):
        panes = [{"pane_id": "w1:p1", "focused": False, "cwd": "/repo/a"}]
        self.assertIsNone(hr.resolve_review_cwd(None, panes))
        self.assertIsNone(hr.resolve_review_cwd(None, None))


class OpenPickerTests(unittest.TestCase):
    def test_context_present_opens_pane_with_context_cwd(self):
        env = {"HERDR_PLUGIN_CONTEXT_JSON": json.dumps({"focused_pane_cwd": "/repo/a"})}
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(hr, "herdr_pane_list", return_value=[]), \
             mock.patch.object(hr, "run_herdr", return_value="") as run:
            rc = hr.cmd_open_picker([])
        self.assertEqual(rc, 0)
        run.assert_called_once_with(
            "plugin", "pane", "open",
            "--plugin", "herdr-hunk-review",
            "--entrypoint", "picker",
            "--placement", "split",
            "--direction", "right",
            "--cwd", "/repo/a",
            "--focus",
        )

    def test_no_context_falls_back_to_pane_list(self):
        panes = [{"pane_id": "w1:p2", "focused": True, "cwd": "/repo/b"}]
        with mock.patch.dict(os.environ, {"HERDR_PLUGIN_CONTEXT_JSON": ""}), \
             mock.patch.object(hr, "herdr_pane_list", return_value=panes), \
             mock.patch.object(hr, "run_herdr", return_value="") as run:
            rc = hr.cmd_open_picker([])
        self.assertEqual(rc, 0)
        args = run.call_args[0]
        self.assertEqual(args[args.index("--cwd") + 1], "/repo/b")

    def test_unresolvable_cwd_notifies_and_fails(self):
        with mock.patch.dict(os.environ, {"HERDR_PLUGIN_CONTEXT_JSON": ""}), \
             mock.patch.object(hr, "herdr_pane_list", return_value=None), \
             mock.patch.object(hr, "run_herdr", return_value="") as run, \
             contextlib.redirect_stderr(io.StringIO()):
            rc = hr.cmd_open_picker([])
        self.assertEqual(rc, 1)
        self.assertEqual(run.call_args[0][0], "notification")
        self.assertEqual(run.call_args[0][1], "show")


class PickerGuardTests(unittest.TestCase):
    def test_non_repo_prints_message_waits_and_exits_zero(self):
        buf = io.StringIO()
        with mock.patch.object(hr, "run_git", return_value=None), \
             mock.patch.object(hr, "wait_for_keypress") as wait, \
             contextlib.redirect_stdout(buf):
            rc = hr.cmd_picker([])
        self.assertEqual(rc, 0)
        self.assertIn("not a git repository", buf.getvalue())
        wait.assert_called_once()
