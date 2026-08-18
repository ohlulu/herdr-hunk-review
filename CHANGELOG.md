# Changelog

## [0.3.0] - 2026-08-18

- send-notes now pastes the notes into the agent pane's input as an editable
  draft instead of submitting them immediately — review, edit, and press
  Enter yourself. Delivery goes through the herdr daemon's `pane.send_input`
  API (bracketed paste, no Enter); the CLI's `agent prompt` always submits
  and `pane send-text` writes raw bytes that a chat TUI may treat as
  submissions on each newline. Discarding the draft does not restore the
  notes: pasted counts as delivered
- Notification wording follows: `Pasted N note(s) into … — press Enter to
  send`

## [0.2.0] - 2026-08-18

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
