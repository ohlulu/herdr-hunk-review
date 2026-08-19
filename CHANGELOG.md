# Changelog

## [0.4.0] - 2026-08-18

Breaking: the plugin id changed from `herdr-hunk-review` to `herdr-review`.
Update the `command` in your `~/.config/herdr/config.toml` keybindings to
`herdr-review.review` / `herdr-review.send-notes`, re-link or re-install the
plugin, then `herdr server reload-config`.

- The viewer is now [tuicr](https://tuicr.dev/) instead of hunk. On the same
  2.7k-line diff, memory drops from 368MB to 57MB and hunk-to-hunk navigation
  from ~28ms to ~3.7ms of CPU per jump; hunk's watch mode cost ~112ms of CPU
  per file change, which made the viewer stutter while an agent was writing
  files
- Review comments can span a line range. `v` / `V` in tuicr selects the range
  and the draft keeps both bounds (`src/main.rs:10-14`); hunk collapsed every
  note to a single line
- Comment classifications (`nit`, `issue`, …) are tagged inline in the draft,
  and file-level and review-level comments are included
- Delivered comments are no longer deleted from the viewer. tuicr has no CLI
  delete, and keeping them makes the viewer a durable record of what you
  raised; `sent.json` remains the duplicate guard
- `Uncommitted` is now a snapshot instead of a live view — re-open to refresh.
  tuicr has no watch mode, and watch was the main source of hunk's stutter
- Picking a target closes the repo's previous viewer pane and takes over,
  since tuicr has no in-place reload. Sessions persist per target, so
  returning to a target restores its comments and reviewed state
- Notes written before a viewer was closed are still sendable: the plugin
  prefers the running session and falls back to the most recent one
- Pick range with the newest commit as the old end and `(worktree)` as the
  new end now opens the worktree diff instead of failing — `<tip>..HEAD` is
  an empty commit range tuicr rejects with `No changes to review`
- Deleted-line comments keep tuicr's `[old]` marker in the draft
  (`path:12 [old]`), so pre-change line numbers are not read as current-file
  lines
- A corrupt or unreadable `sent.json` now aborts send-notes with a
  notification instead of silently re-delivering every historical comment

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
