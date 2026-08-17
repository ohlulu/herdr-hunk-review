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


class StateDirTestCase(unittest.TestCase):
    """Base: point HERDR_PLUGIN_STATE_DIR at a throwaway tempdir."""

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


class StateIOTests(StateDirTestCase):

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


def fake_git(responses):
    """Injected git runner: exact-args lookup; anything unlisted fails (None)."""

    def git(*args):
        return responses.get(args)

    return git


class ResolveBaseTests(unittest.TestCase):
    def test_upstream_tracking_other_branch_wins(self):
        git = fake_git(
            {
                ("rev-parse", "--abbrev-ref", "HEAD"): "feature",
                ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"): "origin/main",
            }
        )
        self.assertEqual(hr.resolve_base(git), "origin/main")

    def test_upstream_self_skipped_falls_to_origin_head(self):
        # AC-006: feature tracking origin/feature must not diff against itself.
        git = fake_git(
            {
                ("rev-parse", "--abbrev-ref", "HEAD"): "feature",
                ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"): "origin/feature",
                ("rev-parse", "--abbrev-ref", "origin/HEAD"): "origin/main",
            }
        )
        self.assertEqual(hr.resolve_base(git), "origin/main")

    def test_conventional_branch_order(self):
        git = fake_git(
            {
                ("rev-parse", "--abbrev-ref", "HEAD"): "wip",
                ("rev-parse", "--verify", "--quiet", "master"): "abc123",
                ("rev-parse", "--verify", "--quiet", "trunk"): "def456",
            }
        )
        self.assertEqual(hr.resolve_base(git), "master")

    def test_all_fail_returns_none(self):
        self.assertIsNone(hr.resolve_base(fake_git({})))


class BuildMenuTests(unittest.TestCase):
    def test_menu_with_base(self):
        # AC-004: merge-base row first and default.
        menu = hr.build_menu("origin/main")
        self.assertEqual(menu[0], ("merge-base", "Merge base (origin/main...HEAD)"))
        self.assertEqual(
            [key for key, _ in menu],
            [
                "merge-base",
                "uncommitted",
                "last-commit",
                "pick-commit",
                "pick-range",
                "branch-vs-branch",
            ],
        )

    def test_menu_without_base(self):
        # AC-005: no merge-base row; Uncommitted becomes first/default.
        menu = hr.build_menu(None)
        self.assertEqual(menu[0], ("uncommitted", "Uncommitted"))
        self.assertNotIn("merge-base", [key for key, _ in menu])
        self.assertEqual(len(menu), 5)


class TargetArgvTests(unittest.TestCase):
    def test_complete_argv_table(self):
        cases = [
            (
                {"key": "merge-base", "base": "origin/main"},
                ["hunk", "diff", "origin/main...HEAD"],
                ["diff", "origin/main...HEAD"],
            ),
            (
                {"key": "uncommitted"},
                ["hunk", "diff", "HEAD", "--watch"],
                ["diff", "HEAD"],
            ),
            ({"key": "last-commit"}, ["hunk", "show"], ["show"]),
            (
                {"key": "pick-commit", "sha": "abc123"},
                ["hunk", "show", "abc123"],
                ["show", "abc123"],
            ),
            (
                {"key": "pick-range", "old": "old1", "new": "new2"},
                ["hunk", "diff", "old1..new2"],
                ["diff", "old1..new2"],
            ),
            (
                # One mark: that commit against the worktree.
                {"key": "pick-range", "old": "abc123"},
                ["hunk", "diff", "abc123"],
                ["diff", "abc123"],
            ),
            (
                {"key": "branch-vs-branch", "base": "develop", "compare": "feature/x"},
                ["hunk", "diff", "develop...feature/x"],
                ["diff", "develop...feature/x"],
            ),
        ]
        for kwargs, want_exec, want_reload in cases:
            with self.subTest(**kwargs):
                key = kwargs.pop("key")
                got_exec, got_reload = hr.target_argv(key, **kwargs)
                self.assertEqual(got_exec, want_exec)
                self.assertEqual(got_reload, want_reload)

    def test_unknown_target_raises(self):
        with self.assertRaises(ValueError):
            hr.target_argv("bogus")


class PickRangeTests(unittest.TestCase):
    LOG = [
        "\x1b[33mccc333\x1b[m newest commit",
        "\x1b[33mbbb222\x1b[m middle commit",
        "\x1b[33maaa111\x1b[m oldest commit",
    ]

    def test_two_marks_map_to_old_new_by_log_position(self):
        # fzf --ansi outputs plain lines; order of marks must not matter.
        for marks in ("ccc333 newest commit\naaa111 oldest commit",
                      "aaa111 oldest commit\nccc333 newest commit"):
            with self.subTest(marks=marks):
                with mock.patch.object(hr, "git_log_lines", return_value=self.LOG), \
                     mock.patch.object(hr, "run_fzf", return_value=marks):
                    self.assertEqual(hr.pick_range_shas(), ("aaa111", "ccc333"))

    def test_single_mark_means_commit_vs_worktree(self):
        with mock.patch.object(hr, "git_log_lines", return_value=self.LOG), \
             mock.patch.object(hr, "run_fzf", return_value="bbb222 middle commit"):
            self.assertEqual(hr.pick_range_shas(), ("bbb222", None))

    def test_cancel_returns_none(self):
        with mock.patch.object(hr, "git_log_lines", return_value=self.LOG), \
             mock.patch.object(hr, "run_fzf", return_value=None):
            self.assertIsNone(hr.pick_range_shas())


class LaunchViewerTests(StateDirTestCase):
    def test_reuse_reloads_focuses_recorded_pane_and_skips_exec(self):
        # AC-007: focus goes to the RECORDED viewer pane, never the picker's
        # own HERDR_PANE_ID, and the mapping is left untouched.
        hr.write_json_state("panes.json", {"/repo": "w1:pOLD"})
        calls = []

        def hunk(*args):
            calls.append(("hunk",) + args)
            return "{}"

        def herdr(*args):
            calls.append(("herdr",) + args)
            return ""

        def execvp(prog, argv):
            calls.append(("exec", prog, tuple(argv)))

        with mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w1:pPICKER"}):
            rc = hr.launch_viewer(
                "/repo", ["hunk", "show"], ["show"],
                hunk=hunk, herdr=herdr, execvp=execvp,
            )
        self.assertEqual(rc, 0)
        self.assertIn(
            ("hunk", "session", "reload", "--repo", "/repo", "--", "show"), calls
        )
        self.assertIn(("herdr", "plugin", "pane", "focus", "w1:pOLD"), calls)
        self.assertNotIn("exec", [c[0] for c in calls])
        self.assertEqual(hr.read_json_state("panes.json", None), {"/repo": "w1:pOLD"})

    def test_exec_writes_own_pane_mapping_before_exec(self):
        # AC-008: no live session -> record repo -> own pane id, then exec
        # the DEC-005 uncommitted argv in place.
        exec_argv, reload_args = hr.target_argv("uncommitted")
        recorded = {}

        def execvp(prog, argv):
            recorded["mapping_at_exec"] = hr.read_json_state("panes.json", None)
            recorded["prog"] = prog
            recorded["argv"] = list(argv)

        with mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w1:pPICKER"}):
            rc = hr.launch_viewer(
                "/repo", exec_argv, reload_args,
                hunk=lambda *a: None, herdr=lambda *a: "", execvp=execvp,
            )
        self.assertEqual(rc, 0)
        self.assertEqual(recorded["prog"], "hunk")
        self.assertEqual(recorded["argv"], ["hunk", "diff", "HEAD", "--watch"])
        self.assertEqual(recorded["mapping_at_exec"], {"/repo": "w1:pPICKER"})

    def test_reuse_without_recorded_pane_skips_focus(self):
        herdr_calls = []

        rc = hr.launch_viewer(
            "/repo", ["hunk", "show"], ["show"],
            hunk=lambda *a: "{}",
            herdr=lambda *a: herdr_calls.append(a),
            execvp=lambda prog, argv: None,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(herdr_calls, [])
