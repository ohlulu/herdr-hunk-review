---
summary: Task breakdown for the herdr-hunk-review MVP (machine phases T001-T017 + Human Acceptance)
read_when:
  - Executing the MVP implementation
  - Checking implementation progress or what remains
---

# Tasks: herdr-hunk-review MVP

## Phase 1: Foundation

- [x] T001 [REQ-001, REQ-006, REQ-009] Create `herdr-plugin.toml` per DEC-003: id `herdr-hunk-review`, `min_herdr_version = "0.8.0"`, one `[[panes]]` entry `picker` (placement split, `sh -c` exec via `$HERDR_PLUGIN_ROOT`), two `[[actions]]` `review` / `send-notes` running `python3 scripts/hunk_review.py open-picker|send-notes`. Verify: `herdr plugin link ~/Developer/ohlulu/herdr-hunk-review && herdr plugin list | grep -q herdr-hunk-review` → exit 0
- [x] T002 [REQ-001..008] Create `scripts/hunk_review.py` skeleton (DEC-001, DEC-002): stdlib-only, subcommand dispatch (`open-picker` / `picker` / `send-notes`), thin subprocess runners (`run_herdr` / `run_git` / `run_hunk`) kept separate from pure decision functions so tests can inject fakes, atomic JSON state read/write (temp + rename) under `HERDR_PLUGIN_STATE_DIR`. Verify: `python3 -m py_compile scripts/hunk_review.py && ! python3 scripts/hunk_review.py bogus 2>/dev/null` → exit 0
- [x] T003 [REQ-004, REQ-008] Create `tests/test_hunk_review.py` (stdlib unittest, `sys.path` shim to `scripts/`) with state IO tests: missing file → default, write-then-read roundtrip, temp+rename atomic replace. Verify: `python3 -m unittest discover -s tests` → OK

## Phase 2: Picker entry (REQ-001)

- [x] T004 [REQ-001] Implement `open-picker` action (DEC-004): pure `resolve_review_cwd(context_json, pane_list)` reading `HERDR_PLUGIN_CONTEXT_JSON.focused_pane_cwd` with `herdr pane list` focused-pane fallback; then `herdr plugin pane open --plugin herdr-hunk-review --entrypoint picker --placement split --direction right --cwd <cwd> --focus`; failures → `herdr notification show` + stderr (DEC-011). Tests cover context-present and fallback paths. Verify: `python3 -m unittest discover -s tests` → GREEN
- [x] T005 [REQ-001] Implement picker startup guard: normalize cwd via `git rev-parse --show-toplevel`; non-repo → print `not a git repository`, wait for one keypress — tty: raw single-byte read via `termios`/`tty` cbreak with restore in `finally`; non-tty: `readline()` — then exit 0 (AC-002, DEC-011). Verify: `cd /tmp && printf '\n' | python3 ~/Developer/ohlulu/herdr-hunk-review/scripts/hunk_review.py picker | grep -q 'not a git repository'` → exit 0

## Phase 3: Menu + base resolution (REQ-002, REQ-003)

- [x] T006 [REQ-003] Implement `resolve_base(git)` pure function over an injected git runner: `@{u}` skipped when it is the current branch's own remote-tracking ref, then `origin/HEAD`, then first existing of `main` / `master` / `trunk`, else None. Tests cover AC-006 (upstream-self skip) and all-fail → None. Verify: `python3 -m unittest discover -s tests` → GREEN
- [x] T007 [REQ-002] Implement `build_menu(base)` + `target_argv(target, ...)` (DEC-005): menu order merge base → uncommitted → last commit → pick commit → pick range → branch vs branch, merge-base row omitted when base is None (AC-004, AC-005); exec argv and reload args exactly per DEC-005 table (uncommitted execs `hunk diff HEAD --watch`, reload drops `--watch`). Tests assert both menu shapes and the complete argv table. Verify: `python3 -m unittest discover -s tests` → GREEN
- [x] T008 [REQ-002] Wire interactive fzf flows (DEC-006): main menu fzf (first row default); Pick commit `git log --oneline --color=always -200 | fzf --ansi`; Pick range same + `--multi 2` mapping marks to `old..new` (one mark → that commit vs worktree); Branch vs branch two-round fzf (`base>` then `compare>`) over `git branch -a --format='%(refname:short)' --sort=-committerdate` (`-a` required for remote-tracking branches; filter out the `origin/HEAD` alias row); Esc at any stage → exit 0. Verify: `python3 -m py_compile scripts/hunk_review.py` → exit 0 (interactive UX verified in Human Acceptance)

## Phase 4: Reuse + viewer launch (REQ-004, REQ-005)

- [x] T009 [REQ-004] Implement reuse path (DEC-007): on confirm, `hunk session get --repo <repo> --json` succeeds → `hunk session reload --repo <repo> -- <reload args>` → `herdr plugin pane focus` the OLD viewer pane id read from `state/panes.json` (ignore focus failure) → exit 0. This branch never writes the picker's own `$HERDR_PANE_ID` into the mapping — recording happens only in T010's exec branch, so a new picker cannot overwrite the live viewer's id. Tests with injected runners assert reload is called, focus targets the recorded old id (not the current pane), mapping is unchanged, and exec is not called (AC-007). Verify: `python3 -m unittest discover -s tests` → GREEN
- [x] T010 [REQ-005] Implement exec path: no live session → write `repo → $HERDR_PANE_ID` into `state/panes.json`, then `os.execvp("hunk", argv)` so the picker pane becomes the viewer (AC-008). Tests inject an exec recorder and assert mapping written before exec and argv for Uncommitted = `hunk diff HEAD --watch`. Verify: `python3 -m unittest discover -s tests` → GREEN

## Phase 5: Send notes (REQ-006, REQ-007, REQ-008)

- [x] T011 [REQ-007] Implement `resolve_agent(...)` pure function over injected herdr data (DEC-009): focused pane if present in `herdr agent list` → neighbors left/right/up/down via `herdr pane neighbor` → unique same-tab agent pane whose cwd resolves to the same repo root; zero or 2+ candidates → failure result carrying candidate pane ids. Tests cover AC-012 and AC-013. Verify: `python3 -m unittest discover -s tests` → GREEN
- [x] T012 [REQ-006] Implement `format_prompt(worktree, notes)` (DEC-010): fixed English template; note line = `filePath:line — body` using Hunk note fields `filePath` / `body`, line from `newRange[0]` → `oldRange[0]` → omit `:line`; multi-line bodies preserved verbatim. Tests cover all three line fallbacks. Verify: `python3 -m unittest discover -s tests` → GREEN
- [x] T013 [REQ-008] Implement sent-id store (DEC-008): `state/sent.json` keyed by hunk session id; `filter_unsent(notes, sent)` excludes recorded `noteId`s (AC-014); GC drops entries whose session id is absent from `hunk session list --json` → `.sessions[].sessionId`. Test fixtures use the full Hunk 0.18 JSON envelopes (DEC-008), not invented shapes. Verify: `python3 -m unittest discover -s tests` → GREEN
- [x] T014 [REQ-006] Implement `send-notes` orchestration against the Hunk 0.18 JSON contract (DEC-008): repo from focused pane cwd → `hunk session get --repo <repo> --json` → `.session.sessionId` (none → notification, AC-011) → `hunk session comment list --repo <repo> --type user --json` → `.comments[]` with `noteId`/`filePath`/`oldRange`/`newRange`/`body` → filter unsent (empty → `No new notes to send`, AC-010) → resolve agent (failure → notification listing candidates) → `herdr agent prompt <pane> <text>` without `--wait` → mark sent ids → per-note `hunk session comment rm --repo <repo> <noteId>` (failure → notify only, no rollback) → `Sent N note(s) to …` (AC-009). Tests assert call order prompt → mark → rm and both notification branches, with fixtures using the full envelopes. Verify: `python3 -m unittest discover -s tests` → GREEN. Depends: T011, T012, T013

## Phase 6: Keybindings + docs (REQ-009)

- [ ] T015 [REQ-009] Add kitty CSI u bridge in `~/.config/kitty/kitty.conf` TUI launchers block: `cmd+shift+h` → `\x1b[104;8u`, `cmd+shift+s` → `\x1b[115;8u`, matching existing entry style (DEC-012). Verify: `grep -c -e '104;8u' -e '115;8u' ~/.config/kitty/kitty.conf` → 2
- [ ] T016 [REQ-009] Add herdr `[[keys.command]]` entries in `~/.config/herdr/config.toml`: `["prefix+alt+h", "ctrl+alt+shift+h"]` → `plugin_action herdr-hunk-review.review`, `["prefix+alt+s", "ctrl+alt+shift+s"]` → `plugin_action herdr-hunk-review.send-notes`, comment style per existing lazygit/yazi entries, then reload. Verify: `herdr server reload-config && grep -c 'herdr-hunk-review\.' ~/.config/herdr/config.toml` → 2
- [ ] T017 [REQ-009] Create `README.md` with sections: Install (git clone + `herdr plugin link` on each machine), Keybindings (cmd+shift+h / cmd+shift+s table), Review targets (semantics per REQ-002), Sending notes (behavior + agent resolution order). Verify: `grep -q 'herdr plugin link' README.md && grep -q 'cmd+shift+h' README.md && grep -q 'cmd+shift+s' README.md && grep -qi 'review target' README.md && grep -qi 'agent resolution' README.md` → exit 0

## Human Acceptance

- [ ] H001 [AC-001] In a git-repo pane, trigger review → new right pane with fzf main menu, cursor on first row
- [ ] H002 [AC-002] In a non-git pane, trigger review → `not a git repository`, any key closes the pane
- [ ] H003 [AC-003] Esc in main menu → pane closes with no residue
- [ ] H004 [AC-004, AC-005] Repo with resolvable base shows `Merge base (origin/main...HEAD)` first; repo without base starts at `Uncommitted`
- [ ] H005 [AC-007] With a live session, confirm any target from a new picker → existing viewer reloads in place and takes focus, picker pane gone
- [ ] H006 [AC-008] Without a session, pick `Uncommitted` → same pane becomes `hunk diff HEAD --watch`; a staged-only change is visible (DEC-005 verification note)
- [ ] H007 [REQ-002] Sub-pickers work: Pick commit, Pick range (two marks → `old..new`; one mark → commit vs worktree), Branch vs branch two rounds
- [ ] H008 [AC-009, AC-010] Two user notes → cmd+shift+s → agent pane receives one prompt with both `file:line — body` lines, notes removed from hunk, `Sent 2 note(s) to …`; second press → `No new notes to send`
- [ ] H009 [AC-011] No live session → send-notes → notification only, agent not prompted
- [ ] H010 [AC-012, AC-013] Left-neighbor agent receives notes; two same-repo agents with no agent neighbor → no send, notification lists candidate panes
- [ ] H011 [AC-015] kitty cmd+shift+h full chain opens the picker
- [ ] H012 [AC-016] cmd+shift+s inside the hunk pane runs send-notes; hunk TUI does not receive the key
