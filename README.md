# herdr-review

A [herdr](https://herdr.dev) plugin that turns code review into a
two-key loop: `cmd+shift+h` opens a picker pane that becomes a
[tuicr](https://tuicr.dev/) diff viewer for the target you choose;
`cmd+shift+s` pastes every inline comment you wrote in tuicr into the
neighboring agent pane's input as one editable draft — you press Enter to send
it.

Single stdlib-only Python script, no build step.

## Install

Requires herdr ≥ 0.8, tuicr ≥ 0.22, fzf, git, and python3 ≥ 3.9 on `PATH`.

```sh
herdr plugin install ohlulu/herdr-review
```

herdr fetches the repo, pins the commit, and keeps its own copy; re-run the
command to upgrade. To stay on a released version instead, pin its tag —
`herdr plugin list` then shows the version rather than a commit:

```sh
herdr plugin install ohlulu/herdr-review --ref v0.4.0
```

For development, link a checkout instead — nothing is copied, edits are live,
and `git pull` is the whole upgrade story:

```sh
git clone https://github.com/ohlulu/herdr-review
herdr plugin link ./herdr-review
```

Either way `herdr plugin list` should now show `herdr-review`.

## Keybindings

The plugin ships two actions and no default keys. Bind them in
`~/.config/herdr/config.toml`:

```toml
[[keys.command]]
key = ["prefix+alt+h", "ctrl+alt+shift+h"]
type = "plugin_action"
command = "herdr-review.review"

[[keys.command]]
key = ["prefix+alt+s", "ctrl+alt+shift+s"]
type = "plugin_action"
command = "herdr-review.send-notes"
```

Then run `herdr server reload-config`.

On macOS kitty, bridge the `cmd` chords onto those bindings with CSI u
sequences in `~/.config/kitty/kitty.conf`:

```
map cmd+shift+h         send_text all \x1b[104;8u
map cmd+shift+s         send_text all \x1b[115;8u
```

The bridge lets herdr intercept the chord even when the focused TUI (tuicr
included) never sees the key. In other terminals use the `prefix+alt+h/s` or
`ctrl+alt+shift+h/s` bindings directly.

| Key | Action |
|-----|--------|
| `cmd+shift+h` (kitty) · `prefix+alt+h` · `ctrl+alt+shift+h` | Open the review target picker |
| `cmd+shift+s` (kitty) · `prefix+alt+s` · `ctrl+alt+shift+s` | Draft review comments into the agent pane |

## Review targets

The picker opens as a focused split on the right, rooted at the focused pane's
repository. Picking a target closes the repo's previous viewer pane, if any,
and `exec`s the picker pane itself into the tuicr viewer — one viewer per
repository.

| Target | Shows |
|--------|-------|
| Merge base | `<base>...HEAD` — your branch since it diverged from the base; base resolves via a non-own-tracking `@{u}`, then fork-parent detection (the nearest branch this one was forked from, so stacked branches review against their parent instead of the repo default branch; branches containing `HEAD` — stack children — and the branch's own remote copies are never the base), then `origin/HEAD`, then `main` / `master` / `trunk`; row hidden when nothing resolves |
| Uncommitted | Staged + unstaged changes vs `HEAD` (a snapshot — tuicr has no watch mode; re-open to refresh) |
| Last commit | The `HEAD` commit |
| Pick commit | fzf over `git log`, view one commit |
| Pick range | Two fzf rounds: `old>` picks the range base, `new>` picks the newer end among commits above it — or the leading `(worktree)` row (default) for that commit through the worktree |
| Branch vs branch | Two fzf rounds (`base>`, then `compare>`) → `base...compare` |

Esc anywhere closes the picker with no side effects.

tuicr persists a review session per target, so leaving a target and coming back
restores the comments and per-file reviewed state you had there.

## Drafting notes

Write comments in tuicr with `c` (line), `C` (file), `;c` (review-level), or
select a range with `v` / `V` first. `cmd+shift+s` then collects the
repository's unsent local-draft comments and pastes them into one agent pane's
input as a single prompt draft without submitting it — review, edit, and press
Enter yourself. A notification reports
`Pasted N note(s) into … — press Enter to send`. Nothing to send →
`No new notes to send`; no tuicr session for the repository → a notification
says so and the agent pane is untouched.

Each row is `path:lines — [type] content`: a range comment keeps both bounds
(`src/main.rs:10-14`), a deleted-line comment keeps tuicr's `[old]` marker
(`src/main.rs:12 [old]` — the numbers are pre-change coordinates), a file
comment has no line suffix, a review-level comment shows as `(review)`, and a
classification (`nit`, `issue`, …) is tagged inline.

Pasted counts as delivered: discarding the draft in the composer does not put
the notes back — they are already recorded in `sent.json`.

Known limitation: tuicr writes a new comment to its session file immediately,
but a comment deleted with `dd` only reaches `tuicr review comments` when the
viewer exits. A comment you deleted can therefore still be sent while the
viewer is open — close it first, or edit the comment instead of deleting it.

Agent resolution order:

1. The focused pane itself, when it hosts an agent.
2. The first neighboring agent pane, probing left, right, up, down.
3. The only agent pane in the current tab whose cwd is inside the same
   repository — with zero or several candidates the draft aborts and a
   notification lists the candidate pane ids; move next to the target agent
   and retry.

Delivered comment ids are recorded per tuicr session slug (`sent.json` in the
plugin state dir). tuicr has no CLI delete, so comments stay in the viewer as
your review record and this log is what prevents a second delivery. Records for
sessions that no longer exist are garbage-collected on the next send. If the
log exists but cannot be parsed, send-notes aborts with a notification instead
of re-delivering history — fix or remove the file, then retry.

The plugin picks the running viewer's session; with none running it falls back
to the most recently updated session for the repository, so comments written
before you closed a viewer are still recoverable. That fallback names its
source in the notification (`Pasted N note(s) from closed session … `) so it
can never happen silently.
