"""Tests for scripts/review.py (stdlib unittest).

Pure-logic tests inject fake runners and never shell out; the
ResolveForkParentGitIntegrationTests class is the deliberate exception and
builds real throwaway git repos, because graph-reachability behavior is
exactly where fake-runner assumptions go wrong."""

import contextlib
import fcntl
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import review as hr


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

    def test_strict_missing_file_still_returns_default(self):
        # Absence is a legitimate fresh state, even in strict mode.
        self.assertEqual(hr.read_json_state("sent.json", {}, strict=True), {})

    def test_strict_corrupt_file_raises_state_error(self):
        # sent.json is the only duplicate guard: a corrupt file must not
        # read as empty history (fail closed).
        (self.state_dir / "sent.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(hr.StateError) as ctx:
            hr.read_json_state("sent.json", {}, strict=True)
        self.assertIn("sent.json", str(ctx.exception))

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
            "--plugin", "herdr-review",
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


FORK_SHA = "bb3671a750deba4a775ef59b76ef6f22a2a92523"
ADVANCED_SHA = "9f2c11d9f2c11d9f2c11d9f2c11d9f2c11d9f2c"


class ResolveForkParentTests(unittest.TestCase):
    """Stacked branches must review against their fork parent, not the repo
    default branch."""

    def test_stacked_branch_resolves_fork_parent(self):
        # AC-017: both branches advanced after the fork, so the parent's tip
        # no longer equals the fork point and labeling must still find it.
        git = fake_git(
            {
                ("rev-parse", "--abbrev-ref", "HEAD"): "feature-b",
                ("for-each-ref", "--format=%(refname)", "refs/heads", "refs/remotes"):
                    "refs/heads/feature-a\nrefs/heads/feature-b\nrefs/heads/main",
                ("for-each-ref", "--format=%(refname)", "--contains", "HEAD",
                 "refs/heads", "refs/remotes"): "refs/heads/feature-b",
                ("rev-list", "--count", "--first-parent", "HEAD", "--not",
                 "refs/heads/feature-a", "refs/heads/main"): "3",
                ("rev-parse", "HEAD~3"): FORK_SHA,
                ("for-each-ref", "--format=%(refname) %(objectname)",
                 "--contains", FORK_SHA, "refs/heads", "refs/remotes"):
                    f"refs/heads/feature-a {ADVANCED_SHA}\n"
                    "refs/heads/feature-b 50680de50680de50680de50680de50680de5068",
            }
        )
        self.assertEqual(hr.resolve_base(git), "feature-a")

    def test_child_at_tip_is_excluded_from_race_and_labeling(self):
        # AC-019: a stack's child branch contains HEAD; without the descendant
        # exclusion it collapses the fork point to HEAD and detection bails to
        # the trunk fallback (the originally reported bug, reintroduced).
        git = fake_git(
            {
                ("rev-parse", "--abbrev-ref", "HEAD"): "feature-b",
                ("for-each-ref", "--format=%(refname)", "refs/heads", "refs/remotes"):
                    "refs/heads/feature-a\nrefs/heads/feature-b\n"
                    "refs/heads/feature-c\nrefs/heads/main",
                ("for-each-ref", "--format=%(refname)", "--contains", "HEAD",
                 "refs/heads", "refs/remotes"):
                    "refs/heads/feature-b\nrefs/heads/feature-c",
                ("rev-list", "--count", "--first-parent", "HEAD", "--not",
                 "refs/heads/feature-a", "refs/heads/main"): "3",
                ("rev-parse", "HEAD~3"): FORK_SHA,
                ("for-each-ref", "--format=%(refname) %(objectname)",
                 "--contains", FORK_SHA, "refs/heads", "refs/remotes"):
                    f"refs/heads/feature-a {FORK_SHA}\n"
                    "refs/heads/feature-b 50680de50680de50680de50680de50680de5068\n"
                    "refs/heads/feature-c 7c33aa17c33aa17c33aa17c33aa17c33aa17c33",
            }
        )
        self.assertEqual(hr.resolve_base(git), "feature-a")

    def test_head_probe_failure_aborts_detection(self):
        # Without the --contains HEAD answer a child could pose as parent;
        # fail closed into the fallback chain instead.
        git = fake_git(
            {
                ("rev-parse", "--abbrev-ref", "HEAD"): "feature",
                ("rev-parse", "--abbrev-ref", "origin/HEAD"): "origin/main",
                ("for-each-ref", "--format=%(refname)", "refs/heads", "refs/remotes"):
                    "refs/heads/feature\nrefs/heads/main",
            }
        )
        self.assertEqual(hr.resolve_base(git), "origin/main")

    def test_slash_remote_copy_of_current_branch_is_excluded(self):
        # `git remote add team/origin …` is legal; the own-copy exclusion must
        # strip the remote prefix by configured-remote longest match, not by
        # the first path segment.
        git = fake_git(
            {
                ("rev-parse", "--abbrev-ref", "HEAD"): "feature-b",
                ("remote",): "team/origin",
                ("for-each-ref", "--format=%(refname)", "refs/heads", "refs/remotes"):
                    "refs/heads/feature-b\nrefs/heads/main\n"
                    "refs/remotes/team/origin/HEAD\n"
                    "refs/remotes/team/origin/feature-a\n"
                    "refs/remotes/team/origin/feature-b",
                ("for-each-ref", "--format=%(refname)", "--contains", "HEAD",
                 "refs/heads", "refs/remotes"): "refs/heads/feature-b",
                ("rev-list", "--count", "--first-parent", "HEAD", "--not",
                 "refs/heads/main", "refs/remotes/team/origin/feature-a"): "2",
                ("rev-parse", "HEAD~2"): FORK_SHA,
                ("for-each-ref", "--format=%(refname) %(objectname)",
                 "--contains", FORK_SHA, "refs/heads", "refs/remotes"):
                    f"refs/remotes/team/origin/feature-a {FORK_SHA}\n"
                    "refs/remotes/team/origin/feature-b 50680de50680de50680de50680de50680de5068",
            }
        )
        self.assertEqual(hr.resolve_base(git), "team/origin/feature-a")

    def test_remote_only_parent_wins_and_own_remote_copy_is_excluded(self):
        # feature-a deleted locally; origin/feature-b (our own pushed copy)
        # must not shrink the fork point to the unpushed commits (AC-006).
        git = fake_git(
            {
                ("rev-parse", "--abbrev-ref", "HEAD"): "feature-b",
                ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"):
                    "origin/feature-b",
                ("rev-parse", "--abbrev-ref", "origin/HEAD"): "origin/main",
                ("remote",): "origin",
                ("for-each-ref", "--format=%(refname)", "refs/heads", "refs/remotes"):
                    "refs/heads/feature-b\nrefs/heads/main\n"
                    "refs/remotes/origin/HEAD\nrefs/remotes/origin/feature-a\n"
                    "refs/remotes/origin/feature-b\nrefs/remotes/origin/main",
                ("for-each-ref", "--format=%(refname)", "--contains", "HEAD",
                 "refs/heads", "refs/remotes"): "refs/heads/feature-b",
                ("rev-list", "--count", "--first-parent", "HEAD", "--not",
                 "refs/heads/main", "refs/remotes/origin/feature-a",
                 "refs/remotes/origin/main"): "4",
                ("rev-parse", "HEAD~4"): FORK_SHA,
                ("for-each-ref", "--format=%(refname) %(objectname)",
                 "--contains", FORK_SHA, "refs/heads", "refs/remotes"):
                    "refs/heads/feature-b 311f8b9311f8b9311f8b9311f8b9311f8b9311f\n"
                    f"refs/remotes/origin/feature-a {FORK_SHA}\n"
                    "refs/remotes/origin/feature-b 50680de50680de50680de50680de50680de5068",
            }
        )
        self.assertEqual(hr.resolve_base(git), "origin/feature-a")

    def test_unmoved_local_parent_beats_conventional_and_remote_rows(self):
        rows_out = (
            f"refs/heads/feature-a {FORK_SHA}\n"
            "refs/heads/main 4413b2d4413b2d4413b2d4413b2d4413b2d4413\n"
            f"refs/remotes/origin/feature-a {FORK_SHA}"
        )
        git = fake_git(
            {
                ("remote",): "origin",
                ("for-each-ref", "--format=%(refname)", "refs/heads", "refs/remotes"):
                    "refs/heads/feature-a\nrefs/heads/main\nrefs/remotes/origin/feature-a",
                ("for-each-ref", "--format=%(refname)", "--contains", "HEAD",
                 "refs/heads", "refs/remotes"): "refs/heads/feature-b",
                ("rev-list", "--count", "--first-parent", "HEAD", "--not",
                 "refs/heads/feature-a", "refs/heads/main",
                 "refs/remotes/origin/feature-a"): "2",
                ("rev-parse", "HEAD~2"): FORK_SHA,
                ("for-each-ref", "--format=%(refname) %(objectname)",
                 "--contains", FORK_SHA, "refs/heads", "refs/remotes"): rows_out,
            }
        )
        self.assertEqual(hr.resolve_fork_parent(git, "feature-b"), "feature-a")

    def test_trunk_branch_skips_fork_detection(self):
        calls = []
        responses = {
            ("rev-parse", "--abbrev-ref", "HEAD"): "main",
            ("rev-parse", "--abbrev-ref", "origin/HEAD"): "origin/main",
        }

        def git(*args):
            calls.append(args)
            return responses.get(args)

        self.assertEqual(hr.resolve_base(git), "origin/main")
        self.assertNotIn("for-each-ref", [args[0] for args in calls])
        self.assertNotIn(("remote",), calls)

    def test_all_candidates_contain_head_falls_back(self):
        # A twin branch at our tip contains HEAD: empty candidate set, no
        # parent, fall back to origin/HEAD (previously this surfaced as
        # rev-list counting zero exclusive commits).
        git = fake_git(
            {
                ("rev-parse", "--abbrev-ref", "HEAD"): "feature",
                ("rev-parse", "--abbrev-ref", "origin/HEAD"): "origin/main",
                ("for-each-ref", "--format=%(refname)", "refs/heads", "refs/remotes"):
                    "refs/heads/feature\nrefs/heads/twin",
                ("for-each-ref", "--format=%(refname)", "--contains", "HEAD",
                 "refs/heads", "refs/remotes"):
                    "refs/heads/feature\nrefs/heads/twin",
            }
        )
        self.assertEqual(hr.resolve_base(git), "origin/main")


@unittest.skipUnless(shutil.which("git"), "requires git on PATH")
class ResolveForkParentGitIntegrationTests(unittest.TestCase):
    """Real temp-repo graphs. The fake-runner fixtures above encode the
    author's call-shape assumptions — the child-at-tip bug slipped exactly
    there — so the graph scenarios also run against actual git."""

    @staticmethod
    def _git(repo, *args):
        proc = subprocess.run(
            ["git", "-C", repo, *args], capture_output=True, text=True
        )
        if proc.returncode != 0:
            raise AssertionError(f"git {args} failed: {proc.stderr}")
        return proc.stdout.strip()

    def _repo(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = tmp.name
        self._git(repo, "init", "-q", "-b", "main")
        self._git(repo, "config", "user.email", "t@t.t")
        self._git(repo, "config", "user.name", "t")
        return repo

    def _commits(self, repo, label, n):
        for i in range(1, n + 1):
            self._git(repo, "commit", "--allow-empty", "-qm", f"{label} {i}")

    def _stack(self, repo):
        """main(2) <- feature-a(2) <- feature-b(3); returns the fork point of
        feature-b (feature-a's tip at fork time)."""
        self._commits(repo, "main", 2)
        self._git(repo, "checkout", "-qb", "feature-a")
        self._commits(repo, "fa", 2)
        fork_point = self._git(repo, "rev-parse", "HEAD")
        self._git(repo, "checkout", "-qb", "feature-b")
        self._commits(repo, "fb", 3)
        return fork_point

    def _runner(self, repo):
        return lambda *args: hr._run(["git", "-C", repo, *args])

    def test_stacked_branch_with_advanced_parent(self):
        # AC-017: both branches advanced after the fork; the diff base must
        # stay the fork point, keeping parent commits out of the review.
        repo = self._repo()
        fork_point = self._stack(repo)
        self._git(repo, "checkout", "-q", "feature-a")
        self._commits(repo, "fa-later", 1)
        self._git(repo, "checkout", "-q", "feature-b")
        self.assertEqual(hr.resolve_base(self._runner(repo)), "feature-a")
        self.assertEqual(
            self._git(repo, "merge-base", "feature-a", "HEAD"), fork_point
        )

    def test_stack_child_at_tip_is_not_the_base(self):
        # AC-019: main <- a <- b <- c, standing on b. The child c contains
        # HEAD; before the descendant exclusion this collapsed detection and
        # fell back to main (the originally reported bug).
        repo = self._repo()
        self._stack(repo)
        self._git(repo, "checkout", "-qb", "feature-c")
        self._commits(repo, "fc", 2)
        self._git(repo, "checkout", "-q", "feature-b")
        self.assertEqual(hr.resolve_base(self._runner(repo)), "feature-a")

    def test_remote_only_parent(self):
        # AC-018: parent deleted locally, only origin/feature-a remains.
        repo = self._repo()
        self._stack(repo)
        bare = tempfile.TemporaryDirectory()
        self.addCleanup(bare.cleanup)
        subprocess.run(
            ["git", "init", "-q", "--bare", bare.name],
            check=True, capture_output=True,
        )
        self._git(repo, "remote", "add", "origin", bare.name)
        self._git(repo, "push", "-q", "origin", "main", "feature-a", "feature-b")
        self._git(repo, "branch", "-q", "--set-upstream-to=origin/feature-b")
        self._git(repo, "branch", "-qD", "feature-a")
        self._commits(repo, "fb-unpushed", 1)
        self.assertEqual(hr.resolve_base(self._runner(repo)), "origin/feature-a")

    def test_slash_remote_copy_of_current_branch_is_excluded(self):
        # A remote literally named team/origin: first-segment parsing would
        # read its feature-b copy as branch "origin/feature-b" and let it win
        # the fork race, shrinking the diff to the unpushed commits.
        repo = self._repo()
        self._stack(repo)
        bare = tempfile.TemporaryDirectory()
        self.addCleanup(bare.cleanup)
        subprocess.run(
            ["git", "init", "-q", "--bare", bare.name],
            check=True, capture_output=True,
        )
        self._git(repo, "remote", "add", "team/origin", bare.name)
        self._git(repo, "push", "-q", "team/origin", "feature-a", "feature-b")
        self._git(repo, "branch", "-qD", "feature-a")
        self._commits(repo, "fb-unpushed", 1)
        self.assertEqual(
            hr.resolve_base(self._runner(repo)), "team/origin/feature-a"
        )

    def test_trunk_branch_keeps_fallback(self):
        repo = self._repo()
        self._stack(repo)
        self._git(repo, "checkout", "-q", "main")
        self.assertEqual(hr.resolve_base(self._runner(repo)), "main")


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
                ["tuicr", "-r", "origin/main...HEAD"],
            ),
            # No watch equivalent in tuicr: the viewer is a snapshot.
            ({"key": "uncommitted"}, ["tuicr", "-w"]),
            ({"key": "last-commit"}, ["tuicr", "-r", "HEAD"]),
            ({"key": "pick-commit", "sha": "abc123"}, ["tuicr", "-r", "abc123"]),
            (
                {"key": "pick-range", "old": "old1", "new": "new2"},
                ["tuicr", "-r", "old1..new2"],
            ),
            (
                # One mark: that commit through the worktree, so -w joins the
                # range instead of standing alone.
                {"key": "pick-range", "old": "abc123"},
                ["tuicr", "-r", "abc123..HEAD", "-w"],
            ),
            (
                {"key": "branch-vs-branch", "base": "develop", "compare": "feature/x"},
                ["tuicr", "-r", "develop...feature/x"],
            ),
        ]
        for kwargs, want_exec in cases:
            with self.subTest(**kwargs):
                key = kwargs.pop("key")
                self.assertEqual(hr.target_argv(key, **kwargs), want_exec)

    def test_unknown_target_raises(self):
        with self.assertRaises(ValueError):
            hr.target_argv("bogus")


class RunFzfTests(unittest.TestCase):
    @staticmethod
    def _proc(returncode, stdout=""):
        return subprocess.CompletedProcess(
            args=["fzf"], returncode=returncode, stdout=stdout
        )

    def test_cancel_codes_return_none(self):
        for returncode in (1, 130):
            with self.subTest(returncode=returncode):
                with mock.patch.object(
                    hr.subprocess, "run", return_value=self._proc(returncode)
                ):
                    self.assertIsNone(hr.run_fzf(["a", "b"]))

    def test_selection_passes_through(self):
        with mock.patch.object(
            hr.subprocess, "run", return_value=self._proc(0, "picked\n")
        ):
            self.assertEqual(hr.run_fzf(["picked", "other"]), "picked")

    def test_unexpected_exit_raises(self):
        with mock.patch.object(hr.subprocess, "run", return_value=self._proc(2)):
            with self.assertRaises(RuntimeError):
                hr.run_fzf(["a"])

    def test_missing_binary_raises(self):
        with mock.patch.object(
            hr.subprocess, "run", side_effect=FileNotFoundError("fzf")
        ):
            with self.assertRaises(RuntimeError):
                hr.run_fzf(["a"])


class PickerFzfErrorTests(unittest.TestCase):
    def test_picker_surfaces_fzf_error_and_waits(self):
        # A broken fzf must not masquerade as Esc: message + keypress + rc 1.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        buf = io.StringIO()
        with mock.patch.object(hr, "run_git", return_value=tmp.name), \
             mock.patch.object(
                 hr, "run_fzf", side_effect=RuntimeError("fzf failed to start: boom")
             ), \
             mock.patch.object(hr, "wait_for_keypress") as wait, \
             contextlib.redirect_stdout(buf):
            rc = hr.cmd_picker([])
        self.assertEqual(rc, 1)
        self.assertIn("fzf failed to start", buf.getvalue())
        wait.assert_called_once()


class PickRangeTests(unittest.TestCase):
    """Two-round old>/new> flow; the retired --multi 2 round needed fzf Tab
    knowledge and read as "cannot select the second commit"."""

    LOG = [
        "\x1b[33mccc333\x1b[m newest commit",
        "\x1b[33mbbb222\x1b[m middle commit",
        "\x1b[33maaa111\x1b[m oldest commit",
    ]

    def _run(self, answers):
        """pick_range_shas() with scripted fzf answers; returns (result, calls)."""
        calls = []

        def fzf(lines, *args):
            calls.append((list(lines), args))
            return answers.pop(0)

        with mock.patch.object(hr, "git_log_lines", return_value=self.LOG), \
             mock.patch.object(hr, "run_fzf", side_effect=fzf):
            return hr.pick_range_shas(), calls

    def test_two_rounds_map_to_old_new(self):
        result, calls = self._run(["aaa111 oldest commit", "ccc333 newest commit"])
        self.assertEqual(result, ("aaa111", "ccc333"))
        self.assertEqual(calls[0][1], ("--ansi", "--prompt", "old> "))
        # Round two: only commits newer than old, worktree row first/default.
        self.assertEqual(calls[1][0], [hr.WORKTREE_ROW, *self.LOG[:2]])
        self.assertEqual(calls[1][1], ("--ansi", "--prompt", "new> "))

    def test_worktree_row_means_commit_vs_worktree(self):
        result, _ = self._run(["bbb222 middle commit", hr.WORKTREE_ROW])
        self.assertEqual(result, ("bbb222", None))

    def test_newest_old_offers_only_worktree_and_collapses(self):
        # Regression: `<tip>..HEAD` is an empty commit range tuicr rejects
        # even with -w, so a tip old end with the worktree row must signal
        # worktree-only instead of mapping to `tuicr -r <tip>..HEAD -w`.
        result, calls = self._run(["ccc333 newest commit", hr.WORKTREE_ROW])
        self.assertEqual(result, (None, None))
        self.assertEqual(calls[1][0], [hr.WORKTREE_ROW])

    def test_tip_to_worktree_pick_maps_to_uncommitted_argv(self):
        with mock.patch.object(hr, "pick_range_shas", return_value=(None, None)):
            self.assertEqual(hr.pick_target("pick-range", None), ["tuicr", "-w"])

    def test_cancel_in_first_round_returns_none(self):
        result, calls = self._run([None])
        self.assertIsNone(result)
        self.assertEqual(len(calls), 1)

    def test_cancel_in_second_round_returns_none(self):
        result, _ = self._run(["bbb222 middle commit", None])
        self.assertIsNone(result)


class LaunchViewerTests(StateDirTestCase):
    def test_reuse_closes_recorded_pane_then_execs_in_place(self):
        # AC-007: one viewer per repo. tuicr cannot reload, so the stale pane
        # is closed and this picker pane becomes the viewer, taking over the
        # mapping.
        hr.write_json_state("panes.json", {"/repo": "w1:pOLD"})
        calls = []

        def herdr(*args):
            calls.append(("herdr",) + args)
            return ""

        def execvp(prog, argv):
            calls.append(("exec", prog, tuple(argv)))

        with mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w1:pPICKER"}):
            rc = hr.launch_viewer(
                "/repo", ["tuicr", "-r", "HEAD"], herdr=herdr, execvp=execvp
            )
        self.assertEqual(rc, 0)
        self.assertIn(("herdr", "plugin", "pane", "close", "w1:pOLD"), calls)
        self.assertIn(("exec", "tuicr", ("tuicr", "-r", "HEAD")), calls)
        self.assertEqual(
            hr.read_json_state("panes.json", None), {"/repo": "w1:pPICKER"}
        )

    def test_exec_writes_own_pane_mapping_before_exec(self):
        # AC-008: record repo -> own pane id, then exec the uncommitted argv.
        exec_argv = hr.target_argv("uncommitted")
        recorded = {}

        def execvp(prog, argv):
            recorded["mapping_at_exec"] = hr.read_json_state("panes.json", None)
            recorded["prog"] = prog
            recorded["argv"] = list(argv)

        with mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w1:pPICKER"}):
            rc = hr.launch_viewer(
                "/repo", exec_argv, herdr=lambda *a: "", execvp=execvp
            )
        self.assertEqual(rc, 0)
        self.assertEqual(recorded["prog"], "tuicr")
        self.assertEqual(recorded["argv"], ["tuicr", "-w"])
        self.assertEqual(recorded["mapping_at_exec"], {"/repo": "w1:pPICKER"})

    def test_no_recorded_pane_skips_close(self):
        herdr_calls = []

        rc = hr.launch_viewer(
            "/repo", ["tuicr", "-r", "HEAD"],
            herdr=lambda *a: herdr_calls.append(a),
            execvp=lambda prog, argv: None,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(herdr_calls, [])

    def test_recorded_pane_equal_to_own_is_not_closed(self):
        # Re-picking from a pane that is already the recorded viewer must not
        # close the pane the exec is about to land in.
        hr.write_json_state("panes.json", {"/repo": "w1:pSAME"})
        herdr_calls = []

        with mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w1:pSAME"}):
            hr.launch_viewer(
                "/repo", ["tuicr", "-r", "HEAD"],
                herdr=lambda *a: herdr_calls.append(a),
                execvp=lambda prog, argv: None,
            )
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
        # AC-012: focused viewer pane, agent to the left.
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
    @staticmethod
    def comment(**kwargs):
        base = {
            "id": "c1", "path": None, "start_line": None, "end_line": None,
            "comment_type": "none", "lifecycle_state": "local_draft", "content": "",
        }
        base.update(kwargs)
        return base

    def test_single_line_comment(self):
        notes = [self.comment(path="src/app.py", start_line=11, end_line=11,
                              content="Rename this.")]
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

    def test_range_comment_keeps_both_bounds(self):
        # The capability hunk never had: a comment spanning lines 10-14.
        notes = [self.comment(path="lib/util.py", start_line=10, end_line=14,
                              content="Simplify this block.")]
        self.assertIn("- lib/util.py:10-14 — Simplify this block.",
                      hr.format_prompt("/r", notes))

    def test_old_side_comment_keeps_old_marker(self):
        # A deleted-line comment carries pre-change coordinates; dropping
        # tuicr's `[old]` marker would point the agent at the wrong line.
        notes = [self.comment(path="a.py", start_line=3, end_line=3,
                              side="old", content="Why was this removed?")]
        self.assertIn("- a.py:3 [old] — Why was this removed?",
                      hr.format_prompt("/r", notes))

    def test_old_side_range_comment_keeps_old_marker(self):
        notes = [self.comment(path="a.py", start_line=10, end_line=14,
                              side="old", content="Dead block.")]
        self.assertIn("- a.py:10-14 [old] — Dead block.",
                      hr.format_prompt("/r", notes))

    def test_new_side_comment_has_no_marker(self):
        notes = [self.comment(path="a.py", start_line=3, end_line=3,
                              side="new", content="Tighten.")]
        self.assertIn("- a.py:3 — Tighten.", hr.format_prompt("/r", notes))

    def test_comment_type_is_tagged(self):
        notes = [self.comment(path="a.py", start_line=3, end_line=3,
                              comment_type="nit", content="Spacing.")]
        self.assertIn("- a.py:3 — [nit] Spacing.", hr.format_prompt("/r", notes))

    def test_file_level_comment_omits_line_suffix(self):
        notes = [self.comment(path="README.md", content="General: tighten intro.")]
        self.assertIn("- README.md — General: tighten intro.",
                      hr.format_prompt("/r", notes))

    def test_review_level_comment_has_no_path(self):
        notes = [self.comment(content="Overall: ship it.")]
        self.assertIn("- (review) — Overall: ship it.", hr.format_prompt("/r", notes))

    def test_multiline_body_verbatim(self):
        notes = [self.comment(path="a.py", start_line=3, end_line=3,
                              content="First line.\nSecond line.")]
        self.assertIn("- a.py:3 — First line.\nSecond line.",
                      hr.format_prompt("/r", notes))


class FilterUnsentTests(unittest.TestCase):
    NOTES = [
        {"id": "c1", "path": "a.py", "start_line": 1, "content": "x"},
        {"id": "c2", "path": "b.py", "start_line": 3, "content": "y"},
    ]

    def test_excludes_recorded_ids(self):
        self.assertEqual(hr.filter_unsent(self.NOTES, ["c1"]), [self.NOTES[1]])

    def test_empty_sent_keeps_all(self):
        self.assertEqual(hr.filter_unsent(self.NOTES, []), self.NOTES)
        self.assertEqual(hr.filter_unsent(self.NOTES, None), self.NOTES)

    def test_gc_drops_dead_sessions(self):
        sent = {"slug-live": ["c1"], "slug-dead": ["c2"]}
        self.assertEqual(
            hr.gc_sent(sent, ["slug-live"]), {"slug-live": ["c1"]}
        )
        self.assertEqual(hr.gc_sent(sent, []), {})


class PickSessionTests(unittest.TestCase):
    def test_prefers_active_over_newer_inactive(self):
        rows = [
            {"slug": "stale", "active": False, "updated_at": "2026-09-09T00:00:00Z"},
            {"slug": "live", "active": True, "updated_at": "2026-01-01T00:00:00Z"},
        ]
        self.assertEqual(hr.pick_session(rows)["slug"], "live")

    def test_falls_back_to_newest_when_none_active(self):
        # A closed viewer keeps its session file, so unsent notes written there
        # stay recoverable (DEC-018).
        rows = [
            {"slug": "old", "active": False, "updated_at": "2026-01-01T00:00:00Z"},
            {"slug": "new", "active": False, "updated_at": "2026-09-09T00:00:00Z"},
        ]
        self.assertEqual(hr.pick_session(rows)["slug"], "new")

    def test_newest_among_several_active(self):
        rows = [
            {"slug": "a", "active": True, "updated_at": "2026-01-01T00:00:00Z"},
            {"slug": "b", "active": True, "updated_at": "2026-02-02T00:00:00Z"},
        ]
        self.assertEqual(hr.pick_session(rows)["slug"], "b")

    def test_empty_is_none(self):
        self.assertIsNone(hr.pick_session([]))
        self.assertIsNone(hr.pick_session(None))


class TuicrReadTests(unittest.TestCase):
    """Parsers for the tuicr review CLI. Malformed output must fail closed:
    returning None keeps the notes retryable, while a wrong guess would
    silently consume or mis-deliver them."""

    def test_sessions_scopes_by_repo_and_parses_rows(self):
        seen = []

        def tuicr(*args):
            seen.append(args)
            return '[{"slug": "s1", "active": true}]'

        rows = hr.tuicr_sessions("/repo", tuicr=tuicr)
        self.assertEqual(rows, [{"slug": "s1", "active": True}])
        self.assertEqual(seen[0], ("review", "list", "--repo", "/repo"))

    def test_sessions_without_repo_lists_every_session(self):
        # The sent-record GC is global, so it must not use a repo-scoped list.
        seen = []

        def tuicr(*args):
            seen.append(args)
            return "[]"

        hr.tuicr_sessions(tuicr=tuicr)
        self.assertEqual(seen[0], ("review", "list", "--all"))

    def test_sessions_failure_and_malformed_output_are_none(self):
        self.assertIsNone(hr.tuicr_sessions("/repo", tuicr=lambda *a: None))
        self.assertIsNone(hr.tuicr_sessions("/repo", tuicr=lambda *a: "not json"))
        self.assertIsNone(hr.tuicr_sessions("/repo", tuicr=lambda *a: '{"a": 1}'))

    def test_comments_keeps_only_local_drafts(self):
        rows = json.dumps([
            {"id": "c1", "lifecycle_state": "local_draft"},
            {"id": "c2", "lifecycle_state": "published"},
            {"id": "c3"},
        ])
        got = hr.tuicr_comments("/repo", "slug", tuicr=lambda *a: rows)
        self.assertEqual([c["id"] for c in got], ["c1"])

    def test_comments_passes_session_and_repo(self):
        seen = []

        def tuicr(*args):
            seen.append(args)
            return "[]"

        hr.tuicr_comments("/repo", "slug", tuicr=tuicr)
        self.assertEqual(
            seen[0], ("review", "comments", "--session", "slug", "--repo", "/repo")
        )

    def test_comments_failure_and_malformed_output_are_none(self):
        self.assertIsNone(hr.tuicr_comments("/r", "s", tuicr=lambda *a: None))
        self.assertIsNone(hr.tuicr_comments("/r", "s", tuicr=lambda *a: "nope"))
        self.assertIsNone(hr.tuicr_comments("/r", "s", tuicr=lambda *a: '{"a": 1}'))


def make_fake_herdr(record, agents, neighbors):
    """Low-level run_herdr fake emitting the observed CLI envelopes."""

    def herdr(*args):
        record.append(("herdr",) + args)
        if args[:2] == ("agent", "list"):
            return json.dumps({"result": {"agents": agents, "type": "agent_list"}})
        if args[:2] == ("pane", "neighbor"):
            direction = args[args.index("--direction") + 1]
            neighbor = {"pane_id": args[args.index("--pane") + 1]}
            if neighbors.get(direction):
                neighbor["neighbor_pane_id"] = neighbors[direction]
            return json.dumps(
                {"result": {"neighbor": neighbor, "type": "pane_neighbor"}}
            )
        if args[:2] == ("notification", "show"):
            return ""
        return ""

    return herdr


def make_fake_send_input(record, ok=True):
    """herdr_send_input fake; records (\"paste\", pane_id, text) calls."""

    def send_input(pane_id, text):
        record.append(("paste", pane_id, text))
        return ok

    return send_input


def make_fake_tuicr(record, sessions, comments):
    """Low-level run_tuicr fake emitting tuicr review-CLI JSON (DEC-018..021).

    sessions: rows as `tuicr review list` returns them, or None to simulate a
    failed listing. comments: rows as `tuicr review comments` returns them."""

    def tuicr(*args):
        record.append(("tuicr",) + args)
        if args[:2] == ("review", "list"):
            if sessions is None:
                return None
            return json.dumps(sessions)
        if args[:2] == ("review", "comments"):
            if comments is None:
                return None
            return json.dumps(comments)
        return ""

    return tuicr


class SendNotesTests(StateDirTestCase):
    PANES = [
        {"pane_id": "w1:pVIEW", "tab_id": "w1:t1", "cwd": "/repo", "focused": True},
        {"pane_id": "w1:pAGENT", "tab_id": "w1:t1", "cwd": "/repo", "focused": False},
    ]
    AGENTS = [{"pane_id": "w1:pAGENT", "tab_id": "w1:t1", "cwd": "/repo"}]
    SESSIONS = [
        {"slug": "repo@main/worktree", "active": True, "updated_at": "2026-01-02T00:00:00Z"}
    ]
    COMMENTS = [
        {"id": "c1", "path": "src/app.py", "start_line": 11, "end_line": 13,
         "comment_type": "issue", "lifecycle_state": "local_draft",
         "content": "Rename this."},
        {"id": "c2", "path": "README.md", "start_line": None, "end_line": None,
         "comment_type": "none", "lifecycle_state": "local_draft",
         "content": "Tighten intro."},
    ]

    @staticmethod
    def fake_git(*args):
        if args[:1] == ("-C",) and args[2:] == ("rev-parse", "--show-toplevel"):
            return "/repo" if args[1].startswith("/repo") else None
        return None

    def run_send(self, record, herdr, tuicr, paste=None):
        if paste is None:
            paste = make_fake_send_input(record)
        with mock.patch.object(hr, "herdr_pane_list", return_value=self.PANES), \
             mock.patch.object(hr, "run_herdr", herdr), \
             mock.patch.object(hr, "run_tuicr", tuicr), \
             mock.patch.object(hr, "herdr_send_input", paste), \
             mock.patch.object(hr, "run_git", self.fake_git), \
             contextlib.redirect_stderr(io.StringIO()):
            return hr.cmd_send_notes([])

    def test_happy_path_pastes_and_marks_without_deleting(self):
        # AC-009. tuicr has no CLI delete, so the comments survive the send and
        # the sent record is the only duplicate guard (DEC-021).
        record = []
        herdr = make_fake_herdr(record, self.AGENTS, {"left": "w1:pAGENT"})
        tuicr = make_fake_tuicr(record, self.SESSIONS, self.COMMENTS)
        rc = self.run_send(record, herdr, tuicr)
        self.assertEqual(rc, 0)

        paste_calls = [c for c in record if c[0] == "paste"]
        self.assertEqual(len(paste_calls), 1)
        self.assertEqual(paste_calls[0][1], "w1:pAGENT")
        # Range and comment type both survive into the prompt (DEC-020).
        self.assertIn("- src/app.py:11-13 — [issue] Rename this.", paste_calls[0][2])
        self.assertIn("- README.md — Tighten intro.", paste_calls[0][2])

        self.assertEqual(
            hr.read_json_state("sent.json", {}), {"repo@main/worktree": ["c1", "c2"]}
        )
        notifications = [c[3] for c in record if c[:3] == ("herdr", "notification", "show")]
        self.assertIn(
            "Pasted 2 note(s) into w1:pAGENT — press Enter to send", notifications
        )

    def test_no_session_notifies_and_skips_paste(self):
        # AC-011: nothing was ever reviewed in this repo.
        record = []
        herdr = make_fake_herdr(record, self.AGENTS, {})
        tuicr = make_fake_tuicr(record, [], [])
        rc = self.run_send(record, herdr, tuicr)
        self.assertEqual(rc, 1)
        self.assertEqual([c for c in record if c[0] == "paste"], [])
        notifications = [c[3] for c in record if c[:3] == ("herdr", "notification", "show")]
        self.assertTrue(any("no tuicr review session" in n for n in notifications))

    def test_session_listing_failure_is_reported(self):
        record = []
        herdr = make_fake_herdr(record, self.AGENTS, {})
        tuicr = make_fake_tuicr(record, None, [])
        rc = self.run_send(record, herdr, tuicr)
        self.assertEqual(rc, 1)
        self.assertEqual([c for c in record if c[0] == "paste"], [])
        notifications = [c[3] for c in record if c[:3] == ("herdr", "notification", "show")]
        self.assertTrue(any("failed to list" in n for n in notifications))

    def test_all_sent_notifies_no_new_notes(self):
        # AC-010.
        hr.write_json_state("sent.json", {"repo@main/worktree": ["c1", "c2"]})
        record = []
        herdr = make_fake_herdr(record, self.AGENTS, {"left": "w1:pAGENT"})
        tuicr = make_fake_tuicr(record, self.SESSIONS, self.COMMENTS)
        rc = self.run_send(record, herdr, tuicr)
        self.assertEqual(rc, 0)
        self.assertEqual([c for c in record if c[0] == "paste"], [])
        notifications = [c[3] for c in record if c[:3] == ("herdr", "notification", "show")]
        self.assertIn("No new notes to send", notifications)

    def test_second_run_after_send_sends_nothing(self):
        # AC-014: comments stay in tuicr after delivery, so only the sent
        # record prevents a re-paste.
        record = []
        herdr = make_fake_herdr(record, self.AGENTS, {"left": "w1:pAGENT"})
        tuicr = make_fake_tuicr(record, self.SESSIONS, self.COMMENTS)
        self.assertEqual(self.run_send(record, herdr, tuicr), 0)

        record2 = []
        herdr2 = make_fake_herdr(record2, self.AGENTS, {"left": "w1:pAGENT"})
        tuicr2 = make_fake_tuicr(record2, self.SESSIONS, self.COMMENTS)
        self.assertEqual(self.run_send(record2, herdr2, tuicr2), 0)
        self.assertEqual([c for c in record2 if c[0] == "paste"], [])
        notifications2 = [c[3] for c in record2 if c[:3] == ("herdr", "notification", "show")]
        self.assertIn("No new notes to send", notifications2)

    def test_corrupt_sent_state_fails_closed_without_paste(self):
        # A corrupt sent.json is the loss of the only duplicate guard
        # (AC-014): abort loudly instead of re-delivering history.
        (self.state_dir / "sent.json").write_text("{not json", encoding="utf-8")
        record = []
        herdr = make_fake_herdr(record, self.AGENTS, {"left": "w1:pAGENT"})
        tuicr = make_fake_tuicr(record, self.SESSIONS, self.COMMENTS)
        rc = self.run_send(record, herdr, tuicr)
        self.assertEqual(rc, 1)
        self.assertEqual([c for c in record if c[0] == "paste"], [])
        notifications = [c[3] for c in record if c[:3] == ("herdr", "notification", "show")]
        self.assertTrue(any("sent.json" in n for n in notifications))
        # The corrupt file stays in place for inspection, not overwritten.
        self.assertEqual(
            (self.state_dir / "sent.json").read_text(encoding="utf-8"), "{not json"
        )

    def test_published_comments_are_not_sent(self):
        # Forge-published comments are not the human's local draft notes.
        published = [dict(self.COMMENTS[0], lifecycle_state="published")]
        record = []
        herdr = make_fake_herdr(record, self.AGENTS, {"left": "w1:pAGENT"})
        tuicr = make_fake_tuicr(record, self.SESSIONS, published)
        rc = self.run_send(record, herdr, tuicr)
        self.assertEqual(rc, 0)
        self.assertEqual([c for c in record if c[0] == "paste"], [])

    def test_closed_session_fallback_is_named_in_notification(self):
        # DEC-018: falling back to a non-running session must be visible, or a
        # stale pick would paste week-old notes with no signal.
        sessions = [
            {"slug": "repo@main/worktree", "active": False,
             "updated_at": "2026-01-01T00:00:00Z"}
        ]
        record = []
        herdr = make_fake_herdr(record, self.AGENTS, {"left": "w1:pAGENT"})
        tuicr = make_fake_tuicr(record, sessions, self.COMMENTS)
        self.assertEqual(self.run_send(record, herdr, tuicr), 0)
        notifications = [c[3] for c in record if c[:3] == ("herdr", "notification", "show")]
        self.assertTrue(
            any("from closed session repo@main/worktree" in n for n in notifications)
        )

    def test_active_session_notification_omits_source(self):
        record = []
        herdr = make_fake_herdr(record, self.AGENTS, {"left": "w1:pAGENT"})
        tuicr = make_fake_tuicr(record, self.SESSIONS, self.COMMENTS)
        self.assertEqual(self.run_send(record, herdr, tuicr), 0)
        notifications = [c[3] for c in record if c[:3] == ("herdr", "notification", "show")]
        self.assertIn(
            "Pasted 2 note(s) into w1:pAGENT — press Enter to send", notifications
        )

    def test_live_session_wins_over_newer_stale_one(self):
        # DEC-018: the running viewer is the target even when another session
        # file was touched more recently.
        sessions = [
            {"slug": "stale", "active": False, "updated_at": "2026-09-09T00:00:00Z"},
            {"slug": "live", "active": True, "updated_at": "2026-01-01T00:00:00Z"},
        ]
        record = []
        herdr = make_fake_herdr(record, self.AGENTS, {"left": "w1:pAGENT"})
        tuicr = make_fake_tuicr(record, sessions, self.COMMENTS)
        self.assertEqual(self.run_send(record, herdr, tuicr), 0)
        asked = [c for c in record if c[:2] == ("tuicr", "review") and c[2] == "comments"]
        self.assertEqual(asked[0][4], "live")

    def test_concurrent_send_blocked_by_lock(self):
        # Race guard: a second invocation while one holds the claim must not
        # paste into the agent a second time.
        holder = open(self.state_dir / "send.lock", "w")
        self.addCleanup(holder.close)
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)

        record = []
        herdr = make_fake_herdr(record, self.AGENTS, {"left": "w1:pAGENT"})
        tuicr = make_fake_tuicr(record, self.SESSIONS, self.COMMENTS)
        rc = self.run_send(record, herdr, tuicr)
        self.assertEqual(rc, 1)
        self.assertEqual([c for c in record if c[0] == "paste"], [])
        notifications = [c[3] for c in record if c[:3] == ("herdr", "notification", "show")]
        self.assertTrue(any("already in progress" in n for n in notifications))
        # Nothing was marked: the notes stay claimable by the lock holder.
        self.assertEqual(hr.read_json_state("sent.json", {}), {})

    def test_ambiguous_agents_notification_lists_candidates(self):
        # AC-013 at the orchestration layer.
        agents = [
            {"pane_id": "w1:pA1", "tab_id": "w1:t1", "cwd": "/repo"},
            {"pane_id": "w1:pA2", "tab_id": "w1:t1", "cwd": "/repo/sub"},
        ]
        record = []
        herdr = make_fake_herdr(record, agents, {})
        tuicr = make_fake_tuicr(record, self.SESSIONS, self.COMMENTS)
        rc = self.run_send(record, herdr, tuicr)
        self.assertEqual(rc, 1)
        self.assertEqual([c for c in record if c[0] == "paste"], [])
        notifications = [c[3] for c in record if c[:3] == ("herdr", "notification", "show")]
        self.assertTrue(
            any("w1:pA1" in n and "w1:pA2" in n for n in notifications)
        )

    def test_paste_failure_marks_nothing(self):
        # Delivery failure must leave notes unmarked so the next run retries.
        record = []
        herdr = make_fake_herdr(record, self.AGENTS, {"left": "w1:pAGENT"})
        tuicr = make_fake_tuicr(record, self.SESSIONS, self.COMMENTS)
        paste = make_fake_send_input(record, ok=False)
        rc = self.run_send(record, herdr, tuicr, paste=paste)
        self.assertEqual(rc, 1)
        self.assertEqual(hr.read_json_state("sent.json", {}), {})
        notifications = [c[3] for c in record if c[:3] == ("herdr", "notification", "show")]
        self.assertTrue(any("failed to paste" in n for n in notifications))


class SendInputTests(unittest.TestCase):
    """Wire-level herdr_send_input tests against a real Unix socket, because
    the NDJSON request shape is exactly what a fake would get wrong."""

    def serve_once(self, reply):
        """One-shot NDJSON server; returns (socket_path, received_lines)."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "herdr.sock")
        received = []
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(path)
        server.listen(1)

        def run():
            conn, _ = server.accept()
            with conn, conn.makefile("rb") as reader:
                received.append(reader.readline())
                conn.sendall(reply)
            server.close()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        return path, received

    OK_REPLY = b'{"id":"herdr-review:send-input","result":{"type":"ok"}}\n'

    def test_sends_pane_send_input_request_and_accepts_ok(self):
        path, received = self.serve_once(self.OK_REPLY)
        with mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": path}):
            self.assertTrue(hr.herdr_send_input("w1:p2", "line one\nline two"))
        request = json.loads(received[0])
        self.assertEqual(request["method"], "pane.send_input")
        # No "keys" entry: the server must not press Enter on the draft.
        self.assertEqual(
            request["params"], {"pane_id": "w1:p2", "text": "line one\nline two"}
        )

    def test_error_response_is_failure(self):
        path, _ = self.serve_once(
            b'{"id":"herdr-review:send-input","error":{"code":"pane_not_found"}}\n'
        )
        with mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": path}):
            self.assertFalse(hr.herdr_send_input("w1:p2", "text"))

    def test_empty_envelope_is_failure(self):
        # A bare {} carries no acknowledgment; treating it as success would
        # consume the notes on a misdelivery (review finding, P2).
        path, _ = self.serve_once(b"{}\n")
        with mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": path}):
            self.assertFalse(hr.herdr_send_input("w1:p2", "text"))

    def test_mismatched_response_id_is_failure(self):
        path, _ = self.serve_once(b'{"id":"other","result":{"type":"ok"}}\n')
        with mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": path}):
            self.assertFalse(hr.herdr_send_input("w1:p2", "text"))

    def test_missing_result_is_failure(self):
        path, _ = self.serve_once(b'{"id":"herdr-review:send-input"}\n')
        with mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": path}):
            self.assertFalse(hr.herdr_send_input("w1:p2", "text"))

    def test_wrong_result_type_is_failure(self):
        path, _ = self.serve_once(
            b'{"id":"herdr-review:send-input","result":{"type":"pong"}}\n'
        )
        with mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": path}):
            self.assertFalse(hr.herdr_send_input("w1:p2", "text"))

    def test_missing_socket_env_is_failure(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HERDR_SOCKET_PATH", None)
            self.assertFalse(hr.herdr_send_input("w1:p2", "text"))

    def test_dead_socket_path_is_failure(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "gone.sock")
        with mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": path}):
            self.assertFalse(hr.herdr_send_input("w1:p2", "text"))


# Keep this at the true end of file: unittest.main() exits the process, so a
# mid-file guard silently drops every test class defined after it (false
# green: direct runs executed only 5 of the suite's tests once).
if __name__ == "__main__":
    unittest.main()
