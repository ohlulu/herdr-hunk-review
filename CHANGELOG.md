# Changelog

## [Unreleased]

- Merge base now detects the branch the current branch was forked from
  (first-parent fork point + containing refs), so stacked branches review
  against their parent branch instead of `origin/HEAD` / `main`; trunk
  branches keep the old fallback chain. Branches containing `HEAD` (stack
  children, same-tip twins) are excluded from detection, and remote-copy
  exclusion parses remote prefixes against the configured remote names, so
  slash-named remotes (`team/origin`) cannot leak the branch's own copy
  into the base
- Pick range is now two fzf rounds (`old>`, then `new>` over newer commits
  plus a `(worktree)` default row) instead of one `--multi 2` round — Tab
  marking was undiscoverable, and Enter with one mark launched the
  single-commit fallback early, reading as "cannot select the second
  commit"

## [0.1.0] - 2026-08-17

Initial release.

- fzf review-target picker pane: merge base, uncommitted, last commit,
  pick commit, pick range, branch vs branch
- hunk viewer launch: reuses the repo's live session (reload + focus-back)
  or execs the picker pane into a new viewer
- send-notes: delivers unsent user-authored hunk notes to the neighboring
  agent pane as one prompt, with per-session duplicate suppression and a
  cross-process send lock
- keybinding wiring documented in README: herdr `[[keys.command]]` entries
  plus kitty CSI u bridge for `cmd+shift+h` / `cmd+shift+s`
