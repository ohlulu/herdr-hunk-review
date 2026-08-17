# Changelog

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
