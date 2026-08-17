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


class ResolveAgentTests(unittest.TestCase):
    AGENTS = [
        {"pane_id": "w1:pA", "tab_id": "w1:t1", "cwd": "/repo"},
        {"pane_id": "w1:pB", "tab_id": "w1:t1", "cwd": "/repo/sub"},
        {"pane_id": "w1:pX", "tab_id": "w1:t2", "cwd": "/repo"},
    ]

    @staticmethod
    def repo_root_of(cwd):
        roots = {"/repo": "/repo", "/repo/sub": "/repo", "/elsewhere": "/elsewhere"}
        return roots.get(cwd)

    def test_focused_agent_pane_wins(self):
        pane, candidates = hr.resolve_agent(
            "w1:pA", "w1:t1", self.AGENTS, [], "/repo", self.repo_root_of
        )
        self.assertEqual(pane, "w1:pA")
        self.assertEqual(candidates, [])

    def test_left_neighbor_agent_wins(self):
        # AC-012: focused hunk pane, agent to the left.
        pane, candidates = hr.resolve_agent(
            "w1:pHUNK",
            "w1:t1",
            self.AGENTS,
            ["w1:pA", None, None, None],  # left/right/up/down probe order
            "/repo",
            self.repo_root_of,
        )
        self.assertEqual(pane, "w1:pA")

    def test_non_agent_neighbor_skipped_unique_same_repo_wins(self):
        pane, candidates = hr.resolve_agent(
            "w2:pHUNK",
            "w1:t2",
            self.AGENTS,
            ["w2:pNVIM", None, None, None],  # neighbor exists but is no agent
            "/repo",
            self.repo_root_of,
        )
        self.assertEqual(pane, "w1:pX")  # only same-tab same-repo agent
        self.assertEqual(candidates, [])

    def test_two_same_repo_agents_fail_with_candidates(self):
        # AC-013: no agent neighbor, two same-repo agents in the tab.
        pane, candidates = hr.resolve_agent(
            "w1:pHUNK", "w1:t1", self.AGENTS, [None, None, None, None],
            "/repo", self.repo_root_of,
        )
        self.assertIsNone(pane)
        self.assertEqual(candidates, ["w1:pA", "w1:pB"])

    def test_no_agents_anywhere_fails_empty(self):
        pane, candidates = hr.resolve_agent(
            "w1:pHUNK", "w1:t1", [], [None, None, None, None],
            "/repo", self.repo_root_of,
        )
        self.assertIsNone(pane)
        self.assertEqual(candidates, [])


class FormatPromptTests(unittest.TestCase):
    def test_line_prefers_new_range(self):
        notes = [
            {
                "noteId": "n1",
                "filePath": "src/app.py",
                "oldRange": [10, 12],
                "newRange": [11, 13],
                "body": "Rename this.",
            }
        ]
        prompt = hr.format_prompt("/repo", notes)
        self.assertIn("- src/app.py:11 — Rename this.", prompt)
        self.assertTrue(
            prompt.startswith(
                "Human inline review comments on your changes in /repo:\n\n"
            )
        )
        self.assertTrue(
            prompt.endswith(
                "Address each comment and verify the result. "
                "If a comment is unclear, ask a focused question before proceeding."
            )
        )

    def test_line_falls_back_to_old_range(self):
        notes = [
            {
                "noteId": "n2",
                "filePath": "lib/util.py",
                "oldRange": [7, 7],
                "newRange": None,
                "body": "Why removed?",
            }
        ]
        self.assertIn("- lib/util.py:7 — Why removed?", hr.format_prompt("/r", notes))

    def test_no_ranges_omits_line_suffix(self):
        notes = [
            {
                "noteId": "n3",
                "filePath": "README.md",
                "oldRange": None,
                "newRange": None,
                "body": "General: tighten intro.",
            }
        ]
        self.assertIn("- README.md — General: tighten intro.", hr.format_prompt("/r", notes))

    def test_multiline_body_verbatim(self):
        notes = [
            {
                "noteId": "n4",
                "filePath": "a.py",
                "oldRange": None,
                "newRange": [3, 4],
                "body": "First line.\nSecond line.",
            }
        ]
        self.assertIn("- a.py:3 — First line.\nSecond line.", hr.format_prompt("/r", notes))
