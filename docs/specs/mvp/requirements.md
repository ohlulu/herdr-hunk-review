---
summary: Requirements for the herdr-hunk-review plugin MVP (picker-driven hunk review pane + note roundtrip to agent)
read_when:
  - Implementing or verifying the MVP plugin behavior
  - Deciding whether a behavior change is in scope
---

# herdr-hunk-review — Requirements

## Summary

herdr plugin：`cmd+shift+h` 在當前 tab 開一個 picker pane 選 review target 後就地變成 hunk viewer；`cmd+shift+s` 把 hunk 裡人寫的 inline note 一次送回隔壁 agent pane 並清除。

## Requirements

### REQ-001: Review picker entry

WHEN the user invokes the `review` action (cmd+shift+h via kitty, ctrl+alt+shift+h / prefix+alt+h in herdr), the plugin SHALL open a new focused split pane in the current tab, rooted at the focused pane's cwd, running an interactive fzf target picker.

| AC | Given | When | Then |
|----|-------|------|------|
| AC-001 | focused pane 的 cwd 在 git repo 內 | 觸發 review action | 當前 tab 右側出現新 pane，顯示 fzf 主選單，游標停在第一項 |
| AC-002 | focused pane 的 cwd 不在 git repo | 觸發 review action | pane 顯示 `not a git repository`，按任意鍵後 pane 關閉 |
| AC-003 | 主選單開啟 | 按 Esc | pane 關閉，無殘留 |

### REQ-002: Target menu

The picker SHALL list review targets in this order — merge base, uncommitted, last commit, pick commit, pick range, branch vs branch — and the first row SHALL be the default selection.

| AC | Given | When | Then |
|----|-------|------|------|
| AC-004 | base 解析成功（例 `origin/main`） | 選單顯示 | 第一項顯示 `Merge base (origin/main...HEAD)`，為預設選項 |
| AC-005 | base 解析失敗（無 upstream、fork-parent 偵測無果、無 origin/HEAD / main / master / trunk） | 選單顯示 | Merge base 項不出現，`Uncommitted` 成為第一項與預設 |

各 target 的語義：

| 選項 | 內容 |
|------|------|
| Merge base | `<base>...HEAD`（merge-base diff，不含未 commit 變動） |
| Uncommitted | staged + unstaged 相對 HEAD 的全部變動，live（`--watch`） |
| Last commit | HEAD 這個 commit |
| Pick commit | fzf 瀏覽 `git log` 選一個 commit |
| Pick range | 兩輪 fzf：`old>` 選範圍舊端，`new>` 只列比 old 新的 commits 加首列 `(worktree)`（預設，選它則 diff old 至工作樹），diff `old..new` |
| Branch vs branch | 兩輪 fzf：先選 base branch，再選 compare branch，diff `base...compare` |

### REQ-003: Base resolution

The plugin SHALL resolve the merge-base ref in this order: `@{u}` (skipped when it is the current branch's own remote-tracking ref), fork-parent detection (skipped on `main` / `master` / `trunk` and on the `origin/HEAD` target branch), `origin/HEAD`, then the first existing of `main` / `master` / `trunk`.

Fork-parent detection SHALL find the nearest branch the current branch was forked from: walk the first-parent chain to the first commit contained in any other branch (the fork point), then label it with the best containing ref — an unmoved parent (tip == fork point) first, then local over remote, then conventional names. The current branch, every remote's copy of it (remote prefixes parsed against the configured remote names, which may themselves contain slashes), remote `HEAD` aliases, and every ref that contains `HEAD` (stack children and same-tip twins, which can never be the fork parent) SHALL be excluded as candidates — from both the fork-point race and the labeling. When the `HEAD`-containment probe fails, detection SHALL fail closed into the fallback chain.

Known limitation: a child forked from a mid-chain commit after this branch advanced is indistinguishable from a parent by reachability alone; the menu row shows whichever ref won, and setting an explicit upstream (`git branch --set-upstream-to=<parent>`) overrides detection entirely.

| AC | Given | When | Then |
|----|-------|------|------|
| AC-006 | 當前 branch `feature` 的 upstream 是 `origin/feature` | 解析 base | 跳過 `@{u}`（避免空 diff），進入 fork-parent 偵測（排除 `origin/feature` 本尊 copy），無果才落到 `origin/HEAD` 或 conventional branch |
| AC-017 | `feature-b` 從 `feature-a` 開出（stacked branch），兩者皆有後續 commit | 解析 base | base 為 `feature-a`（非 `main`），diff 只含 `feature-b` 自身的 commits |
| AC-018 | parent branch 本地已刪、只剩 `origin/feature-a` | 解析 base | base 為 `origin/feature-a` |
| AC-019 | stack `main ← feature-a ← feature-b ← feature-c`，站在 `feature-b`（child `feature-c` 包含 HEAD） | 解析 base | base 為 `feature-a`；child 不參與 fork-point 計算與標籤 |

### REQ-004: Session reuse

IF a live hunk session already exists for the repository, WHEN a picker target is confirmed, the plugin SHALL reload that session in place, focus its pane, and close the picker pane instead of opening a second viewer.

| AC | Given | When | Then |
|----|-------|------|------|
| AC-007 | repo 已有開著的 hunk review pane | picker 確認任一 target | 原 review pane 內容更新為新 target 並取得 focus，picker pane 消失 |

### REQ-005: Viewer launch

WHEN a target is confirmed and no live session exists, the picker SHALL `exec` hunk with that target's arguments, so the picker pane itself becomes the review pane.

| AC | Given | When | Then |
|----|-------|------|------|
| AC-008 | 無既有 session | 選 `Uncommitted` | 同一個 pane 變成 `hunk diff HEAD --watch` 的 viewer |

### REQ-006: Send notes

WHEN the user invokes the `send-notes` action (cmd+shift+s), the plugin SHALL deliver all unsent user-authored hunk notes of the focused pane's repository to the resolved agent pane as one prompt, then remove the delivered notes from hunk.

| AC | Given | When | Then |
|----|-------|------|------|
| AC-009 | hunk session 有 2 條未送 user note，隔壁 pane 是 agent | 觸發 send-notes | agent 收到一則含兩條 `file:line — body` 的 prompt；兩條 note 從 hunk 消失；notification 顯示 `Sent 2 note(s) to …` |
| AC-010 | 無未送 note | 觸發 send-notes | notification `No new notes to send`，不 prompt agent |
| AC-011 | 該 repo 無 live hunk session | 觸發 send-notes | notification 說明無 session，不 prompt agent |

### REQ-007: Agent target resolution

The plugin SHALL resolve the receiving agent in this order: (1) the focused pane itself when it hosts an agent, (2) the first neighbor pane (left, right, up, down) hosting an agent, (3) the only agent pane in the current tab whose cwd is inside the same repository. Otherwise it SHALL report failure without sending.

| AC | Given | When | Then |
|----|-------|------|------|
| AC-012 | focused 是 hunk pane，agent pane 在左鄰 | 觸發 send-notes | note 送到左鄰 agent |
| AC-013 | 四鄰皆非 agent，同 tab 有兩個同 repo 的 agent pane | 觸發 send-notes | 不送；notification 列出候選 pane id 要求使用者站到目標旁再送 |

### REQ-008: Duplicate suppression

The plugin SHALL record delivered note ids per hunk session id, SHALL exclude recorded ids from later sends, and SHALL drop records whose session no longer exists.

| AC | Given | When | Then |
|----|-------|------|------|
| AC-014 | note A 已送出但事後的 `comment rm` 失敗，A 仍在 hunk 裡 | 再次觸發 send-notes | A 不重送（notification 為 no new notes 或僅含其他新 note） |

### REQ-009: Keybinding wiring

The keybindings SHALL be hand-written into the tracked configs following the existing style: kitty CSI u bridge for cmd+shift+h / cmd+shift+s, herdr `[[keys.command]]` entries on `ctrl+alt+shift+h` / `ctrl+alt+shift+s` with `prefix+alt+h` / `prefix+alt+s` variants.

| AC | Given | When | Then |
|----|-------|------|------|
| AC-015 | 兩份 config 改完並 `herdr server reload-config` | 在 kitty 按 cmd+shift+h | picker pane 開啟（全鏈路） |
| AC-016 | 同上 | 在 hunk pane 按 cmd+shift+s | send-notes 動作執行（herdr 攔截，hunk TUI 不收到該鍵） |

## Out of Scope

- Agent idle 時自動開 diff（JacquesvanWyk autodiff）
- `Staged`、`Stash` 選單項（可日後加一行）
- git pager 整合、GitHub link handler、managed keybinding block（jhochenbaum 那三套）
- mbp 端自動部署 — README 記手動安裝步驟，兩台各裝一次
