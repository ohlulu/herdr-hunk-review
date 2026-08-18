# herdr-hunk-review

A [herdr](https://herdr.dev) plugin that turns code review into a
two-key loop: `cmd+shift+h` opens a picker pane that becomes a
[hunk](https://hunk.dev/) diff viewer for the target you choose;
`cmd+shift+s` pastes every inline note you wrote in hunk into the neighboring
agent pane's input as one editable draft — you press Enter to send it — and
clears them from the viewer.

Single stdlib-only Python script, no build step.

## Install

Requires herdr ≥ 0.8, hunk ≥ 0.18, fzf, git, and python3 ≥ 3.9 on `PATH`.

```sh
herdr plugin install ohlulu/herdr-hunk-review
```

herdr fetches the repo, pins the commit, and keeps its own copy; re-run the
command to upgrade. To stay on a released version instead, pin its tag —
`herdr plugin list` then shows the version rather than a commit:

```sh
herdr plugin install ohlulu/herdr-hunk-review --ref v0.3.0
```

For development, link a checkout instead — nothing is copied, edits are live,
and `git pull` is the whole upgrade story:

```sh
git clone https://github.com/ohlulu/herdr-hunk-review
herdr plugin link ./herdr-hunk-review
```

Either way `herdr plugin list` should now show `herdr-hunk-review`.

## Keybindings

The plugin ships two actions and no default keys. Bind them in
`~/.config/herdr/config.toml`:

```toml
[[keys.command]]
key = ["prefix+alt+h", "ctrl+alt+shift+h"]
type = "plugin_action"
command = "herdr-hunk-review.review"

[[keys.command]]
key = ["prefix+alt+s", "ctrl+alt+shift+s"]
type = "plugin_action"
command = "herdr-hunk-review.send-notes"
```

Then run `herdr server reload-config`.

On macOS kitty, bridge the `cmd` chords onto those bindings with CSI u
sequences in `~/.config/kitty/kitty.conf`:

```
map cmd+shift+h         send_text all \x1b[104;8u
map cmd+shift+s         send_text all \x1b[115;8u
```

The bridge lets herdr intercept the chord even when the focused TUI (hunk
included) never sees the key. In other terminals use the `prefix+alt+h/s` or
`ctrl+alt+shift+h/s` bindings directly.

| Key | Action |
|-----|--------|
| `cmd+shift+h` (kitty) · `prefix+alt+h` · `ctrl+alt+shift+h` | Open the review target picker |
| `cmd+shift+s` (kitty) · `prefix+alt+s` · `ctrl+alt+shift+s` | Draft hunk notes into the agent pane |

## Review targets

The picker opens as a focused split on the right, rooted at the focused pane's
repository. Picking a target either reloads the repo's live hunk session in
place (and focuses it), or — when there is none — the picker pane itself
`exec`s into the hunk viewer.

| Target | Shows |
|--------|-------|
| Merge base | `<base>...HEAD` — your branch since it diverged from the base; base resolves via a non-own-tracking `@{u}`, then fork-parent detection (the nearest branch this one was forked from, so stacked branches review against their parent instead of the repo default branch; branches containing `HEAD` — stack children — and the branch's own remote copies are never the base), then `origin/HEAD`, then `main` / `master` / `trunk`; row hidden when nothing resolves |
| Uncommitted | Staged + unstaged changes vs `HEAD`, live (`--watch`) |
| Last commit | The `HEAD` commit |
| Pick commit | fzf over `git log`, view one commit |
| Pick range | Two fzf rounds: `old>` picks the range base, `new>` picks the newer end among commits above it — or the leading `(worktree)` row (default) for that commit vs the worktree → `old..new` |
| Branch vs branch | Two fzf rounds (`base>`, then `compare>`) → `base...compare` |

Esc anywhere closes the picker with no side effects.

## Drafting notes

`cmd+shift+s` collects the repository's unsent user-authored hunk notes and
pastes them into one agent pane's input as a single prompt draft
(`file:line — body` per note) without submitting it — review, edit, and press
Enter yourself. The notes are then removed from hunk and a notification
reports `Pasted N note(s) into … — press Enter to send`. Nothing to send →
`No new notes to send`; no live hunk session → a notification says so and the
agent pane is untouched.

Pasted counts as delivered: discarding the draft in the composer does not put
the notes back — they are already recorded in `sent.json` and cleared from
hunk.

Agent resolution order:

1. The focused pane itself, when it hosts an agent.
2. The first neighboring agent pane, probing left, right, up, down.
3. The only agent pane in the current tab whose cwd is inside the same
   repository — with zero or several candidates the draft aborts and a
   notification lists the candidate pane ids; move next to the target agent
   and retry.

Delivered note ids are recorded per hunk session (`sent.json` in the plugin
state dir), so a note that failed to clear from hunk is still never delivered
twice. Records for dead sessions are garbage-collected on the next send.
