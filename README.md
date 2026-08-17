# herdr-hunk-review

A [herdr](https://github.com/herdr-dev/herdr) plugin that turns code review into a
two-key loop: `cmd+shift+h` opens a picker pane that becomes a
[hunk](https://github.com/jesseduffield/hunk) diff viewer for the target you choose;
`cmd+shift+s` sends every inline note you wrote in hunk to the agent pane sitting
next to it, as one prompt, and clears them from the viewer.

Single stdlib-only Python script, no build step.

## Install

Requires herdr ≥ 0.8, hunk ≥ 0.18, fzf, git, and python3 ≥ 3.9 on `PATH`.
Repeat on each machine (no auto-deploy):

```sh
git clone https://github.com/ohlulu/herdr-hunk-review ~/Developer/ohlulu/herdr-hunk-review
herdr plugin link ~/Developer/ohlulu/herdr-hunk-review
```

`herdr plugin list` should now show `herdr-hunk-review`.

The keybindings are hand-written into the tracked configs (see the entries added
to `~/.config/kitty/kitty.conf` under *TUI launchers* and the two
`[[keys.command]]` entries in `~/.config/herdr/config.toml`); after editing run
`herdr server reload-config`.

## Keybindings

| Key | Where | Action |
|-----|-------|--------|
| `cmd+shift+h` | kitty (bridged to `ctrl+alt+shift+h`) | Open the review target picker |
| `prefix+alt+h` | any terminal attached to herdr | Same as above |
| `cmd+shift+s` | kitty (bridged to `ctrl+alt+shift+s`) | Send hunk notes to the agent pane |
| `prefix+alt+s` | any terminal attached to herdr | Same as above |

The kitty rows are CSI u bridges (`\x1b[104;8u`, `\x1b[115;8u`) so herdr
intercepts the chord even when the focused TUI (hunk included) never sees it.

## Review targets

The picker opens as a focused split on the right, rooted at the focused pane's
repository. Picking a target either reloads the repo's live hunk session in
place (and focuses it), or — when there is none — the picker pane itself
`exec`s into the hunk viewer.

| Target | Shows |
|--------|-------|
| Merge base | `<base>...HEAD` — your branch since it diverged from the base; base resolves via `@{u}` (skipped when it is this branch's own remote-tracking ref), then `origin/HEAD`, then `main` / `master` / `trunk`; row hidden when nothing resolves |
| Uncommitted | Staged + unstaged changes vs `HEAD`, live (`--watch`) |
| Last commit | The `HEAD` commit |
| Pick commit | fzf over `git log`, view one commit |
| Pick range | fzf with two marks → `old..new`; one mark → that commit vs the worktree |
| Branch vs branch | Two fzf rounds (`base>`, then `compare>`) → `base...compare` |

Esc anywhere closes the picker with no side effects.

## Sending notes

`cmd+shift+s` collects the repository's unsent user-authored hunk notes and
delivers them to one agent pane as a single prompt
(`file:line — body` per note), then removes them from hunk and reports
`Sent N note(s) to …`. Nothing to send → `No new notes to send`; no live hunk
session → a notification says so and no agent is prompted.

Agent resolution order:

1. The focused pane itself, when it hosts an agent.
2. The first neighboring agent pane, probing left, right, up, down.
3. The only agent pane in the current tab whose cwd is inside the same
   repository — with zero or several candidates the send aborts and a
   notification lists the candidate pane ids; move next to the target agent
   and retry.

Delivered note ids are recorded per hunk session (`sent.json` in the plugin
state dir), so a note that failed to clear from hunk is still never delivered
twice. Records for dead sessions are garbage-collected on the next send.
