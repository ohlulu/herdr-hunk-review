---
summary: Technical plan for the herdr-hunk-review MVP (single Python script, manifest shape, state layout, verification commands)
read_when:
  - Implementing the MVP
  - Verifying implementation against decisions
---

# herdr-hunk-review — Plan

## Technical approach

- 單一 Python 3 stdlib script `scripts/hunk_review.py`，以 subcommand 分工：`open-picker`（action，開 pane）、`picker`（pane 內互動）、`send-notes`（action）。
- picker pane 由 `herdr plugin pane open` 開出（placement split、direction right、focus），pane 內 fzf 選完 target 後 `os.execvp` 成 hunk，pane 即 viewer。
- 已有 hunk session 時走 `hunk session reload`，picker pane 功成身退（exit 0 自關）。
- send-notes 全走查證過的 pull 介面：`hunk session get / comment list --type user / comment rm` + `herdr pane neighbor / agent list / agent prompt`。
- 狀態檔兩個（sent ids、repo→pane 映射）放 `HERDR_PLUGIN_STATE_DIR`，JSON、原子寫入（temp + rename）。

## Decisions

### DEC-001: Python 3, stdlib only

- Choice: `#!/usr/bin/env python3`，只用 `json / subprocess / os / sys / pathlib / tempfile`。
- Alternatives: bash + jq（字串處理易碎）、TypeScript（要 npm ci + build，兩台 node 版本不一）。
- Rationale: 使用者選擇；JSON 邏輯多，Python 最穩且零 build。macOS 系統 python3 ≥ 3.9 即可跑。
- Satisfies: 全部 REQ。

### DEC-002: Single script with subcommands

- Choice: 一個 `scripts/hunk_review.py`（估 <500 行），不拆模組。
- Alternatives: 三個 script + common module（import 路徑要顧）；jhochenbaum 式多檔 + build。
- Rationale: 三個進入點共用 herdr/git/hunk helper，單檔讓 manifest 與安裝最簡單。超過 ~600 行再拆。
- Satisfies: REQ-001..008。

### DEC-003: Manifest shape

- Choice: 1 個 pane entrypoint + 2 個 action：

```toml
id = "herdr-hunk-review"          # 無點號，keybinding command = herdr-hunk-review.<action>
min_herdr_version = "0.8.0"

[[panes]]
id = "picker"
placement = "split"
command = ["sh", "-c", "exec python3 \"$HERDR_PLUGIN_ROOT/scripts/hunk_review.py\" picker"]

[[actions]]
id = "review"       # open-picker
[[actions]]
id = "send-notes"
# action command = ["python3", "scripts/hunk_review.py", ...]（herdr 0.8 action cwd = plugin root）
```

- Rationale: pane command 的 cwd 是 open 時的 `--cwd`（repo），必須經 `$HERDR_PLUGIN_ROOT` 定位 script（JacquesvanWyk 用 env 傳路徑、jhochenbaum 用 `$HERDR_PLUGIN_ROOT`，取後者）。action command 相對 plugin root 直接可跑。
- Satisfies: REQ-001、REQ-006、REQ-009。

### DEC-004: Picker pane opening

- Choice: `open-picker` 從 `HERDR_PLUGIN_CONTEXT_JSON.focused_pane_cwd`（fallback：`herdr pane list` 的 focused pane）取 cwd，`herdr plugin pane open --plugin herdr-hunk-review --entrypoint picker --placement split --direction right --cwd <cwd> --focus`。
- Rationale: picker 需要鍵盤，必須 `--focus`；direction right 與現有 nvim pane 慣例一致，hunk 需要寬度。repo root 正規化留給 pane script（`git rev-parse --show-toplevel`），action 保持最薄。
- Satisfies: REQ-001。

### DEC-005: Target → hunk argv map

- Choice:

| Target | 無 session（exec） | 有 session（reload `--`） |
|--------|--------------------|---------------------------|
| Merge base | `hunk diff <base>...HEAD` | `diff <base>...HEAD` |
| Uncommitted | `hunk diff HEAD --watch` | `diff HEAD` |
| Last commit | `hunk show` | `show` |
| Pick commit | `hunk show <sha>` | `show <sha>` |
| Pick range | `hunk diff <old>..<new>` | `diff <old>..<new>` |
| Branch vs branch | `hunk diff <base>...<compare>` | `diff <base>...<compare>` |

- Rationale: `hunk diff HEAD` = staged+unstaged 相對 HEAD（同 `git diff HEAD`），即使用者要的 uncommitted；`--watch` 只對 uncommitted 有意義（其他 target 內容不隨工作樹變動）。reload 語法查證過支援 `-- diff [ref]` 與 `-- show [ref]`。
- Verification note: `hunk diff HEAD` 的 staged 涵蓋行為在 T-verify 實測。
- Satisfies: REQ-002、REQ-004、REQ-005。

### DEC-006: Sub-pickers

- Choice: Pick commit / Pick range 用 `git log --oneline --color=always -200 | fzf --ansi`（range 用 `--multi 2`，git log 新在上，diff `老..新`）；Branch vs branch 用兩輪 fzf（prompt `base>` 再 `compare>`，來源 `git branch -a --format='%(refname:short)' --sort=-committerdate`，`-a` 才會列 remote-tracking branches，濾掉 `origin/HEAD` alias 列），避免猜 `--multi 2` 的輸出順序。子選單按 Esc 直接關 pane（重按 cmd+shift+h 成本低）。
- Rationale: 沿用 JacquesvanWyk 驗證過的 log picker；branch 比較的方向語義（誰是 base）用兩輪 prompt 消除歧義。
- Satisfies: REQ-002。

### DEC-007: Reuse + focus-back

- Choice: 確認 target 後先判 session：`hunk session get --repo <repo> --json` 成功 → `hunk session reload` → `herdr plugin pane focus` panes.json 記錄的舊 viewer pane（失敗靜默）→ exit 0，不碰 mapping；無 session → 把 `repo → 自身 HERDR_PANE_ID` 寫進 `state/panes.json` 再 exec hunk。
- Alternatives: jhochenbaum 的 reuse 在 action 層判斷（需要 index-store + entrypoint 對照）；掃 pane title 找 review pane（脆）；啟動時即寫 mapping（新 picker 會覆寫舊 viewer 的 id，reuse 時 focus 到即將退出的自己）。
- Rationale: pane script 是唯一同時知道 repo 與自身 pane id 的位置；只在 exec 分支寫入，mapping 永遠指向真正的 viewer。殘留記錄無害（focus 失敗即忽略）。
- Satisfies: REQ-004。

### DEC-008: Sent-id state per hunk session

- Choice: `state/sent.json`，shape `{ "<hunk-session-id>": ["<noteId>", ...] }`。send 流程走 Hunk 0.18 JSON contract：`hunk session get --repo <repo> --json` → `.session.sessionId` → `hunk session comment list --repo <repo> --type user --json` → `.comments[]`（欄位 `noteId` / `filePath` / `oldRange` / `newRange` / `body`）→ 過澾已送 → prompt 成功先 markSent → 逐筆 `hunk session comment rm --repo <repo> <noteId>`（失敗僅 notify，不回滾）→ 用 `hunk session list --json` 的 `.sessions[].sessionId` GC 死 entry。
- Alternatives: 以 worktree 為 key（jhochenbaum）— 跨 session 撞 note id 的風險由「假設 id 全域唯一」扛。
- Rationale: note 是 session-persistent，以 session id 為 key 讓 state 生命週期與 note 一致，GC 直接可判定。
- Satisfies: REQ-008。

### DEC-009: Agent resolution implementation

- Choice: 依 REQ-007 順序：(1) `herdr agent list` 內含 focused pane id → 送它；(2) `herdr pane neighbor --direction left|right|up|down --pane <focused>` 依序，pane id 在 agent list 內即中；(3) `herdr pane list` 過濾同 tab 且 agent 非 null 且 `git -C cwd rev-parse --show-toplevel` == repo，恰一個即中；否則 `herdr notification show` 列候選。送出用 `herdr agent prompt <pane_id> <text>`（不 `--wait`，fire-and-forget）。
- Rationale: 空間關係優先（使用者語義「隔壁」），repo 匹配兜底；多候選寧可失敗要求使用者站位，不猜。
- Satisfies: REQ-006、REQ-007。

### DEC-010: Prompt format

- Choice: 硬編碼英文 template：

```
Human inline review comments on your changes in {worktree}:

- {file}:{line} — {body}
...

Address each comment and verify the result. If a comment is unclear, ask a focused question before proceeding.
```

- Rationale: jhochenbaum template 的精簡版；line 取 `newRange[0]`，fallback `oldRange[0]`，再 fallback 省略 `:line`。多行 body 原样保留（`herdr agent prompt` 走 bracketed paste）。
- Satisfies: REQ-006。

### DEC-011: Error reporting

- Choice: action 層錯誤 → `herdr notification show` + stderr（herdr plugin log 可查）；picker pane 內錯誤 → print + 等按鍵再退出（讓人讀得到）。
- Satisfies: REQ-001 AC-002、REQ-006 AC-011。

### DEC-012: Keybinding wiring（hand-written）

- Choice: kitty `TUI launchers` 區塊加兩行（`\x1b[104;8u`、`\x1b[115;8u`）；herdr config 加兩條 `[[keys.command]]`（`["prefix+alt+h","ctrl+alt+shift+h"]` → `plugin_action herdr-hunk-review.review`；s 同理 → `send-notes`），註解風格照現有 lazygit/yazi 條目。不做 setup-keys managed block。
- Satisfies: REQ-009。

## Change Map

| File | Action | REQ |
|------|--------|-----|
| `~/Developer/ohlulu/herdr-hunk-review/herdr-plugin.toml` | create | REQ-001, 006 |
| `~/Developer/ohlulu/herdr-hunk-review/scripts/hunk_review.py` | create | REQ-001..008 |
| `~/Developer/ohlulu/herdr-hunk-review/tests/test_hunk_review.py` | create（stdlib unittest，injected-runner 測純邏輯） | REQ-002..008 |
| `~/Developer/ohlulu/herdr-hunk-review/README.md` | create（安裝、keybinding、行為說明） | REQ-009, Out-of-scope 部署 |
| `~/.config/kitty/kitty.conf` | edit（TUI launchers 兩行） | REQ-009 |
| `~/.config/herdr/config.toml` | edit（兩條 keys.command） | REQ-009 |

## Verification

- `python3 -m unittest discover -s tests` → 純邏輯（base resolution、menu/argv map、agent resolution、prompt format、sent-id filter）全綠。
- `herdr plugin link ~/Developer/ohlulu/herdr-hunk-review` + `herdr plugin list` → plugin 可見、無 manifest 錯誤。
- `herdr plugin action invoke review --plugin herdr-hunk-review` → picker pane 開啟（AC-001）；非 git cwd pane 觸發 → AC-002。
- `hunk diff HEAD` 語義實測：staged-only 變動要出現（DEC-005 note）。
- 選單六項逐一確認 argv / reload（AC-004..008；用測試 repo 造 upstream-self、無 base 等情境驗 AC-005、AC-006）。
- send 全鏈路：hunk TUI 手動加 note → cmd+shift+s → agent pane 收到 prompt、note 消失、再按一次得 no-new-notes（AC-009、010、014）。
- `herdr server reload-config` 後 kitty 全鏈路（AC-015、016）。

## Review Dispositions

Critic round 1（2026-08-17，tasks phase review）— 全數 accept，皆為 correction：

| Finding | Disposition | 處置 |
|---------|-------------|------|
| [P1] Reuse mapping 覆寫舊 viewer pane id | accept | DEC-007 改為只在 exec 分支寫入 mapping；T009/T010 同步，測試斷言 focus 舊 id |
| [P1] Hunk JSON contract / `comment rm` argv 未釘定 | accept | DEC-008 釘定 `.session.sessionId` / `.comments[]` 欄位 / `.sessions[].sessionId` / `comment rm --repo <repo> <noteId>`；T012/T013/T014 同步，fixture 用完整 envelope |
| [P2] TTY 「任意鍵」無可執行定義 | accept | T005 指定 termios/tty cbreak + finally 還原；non-tty 走 readline |
| [P2] README Verify 過弱 | accept | T017 Verify 改為五段 grep 驗段落存在 |
| [P3] tasks.md 缺 frontmatter | accept | 已補 summary + read_when |
| [P3] `git branch` 缺 `-a`（locked note） | accept | DEC-006/T008 改 `git branch -a`，濾 `origin/HEAD` alias；修正符合原意圖（含 remotes） |

Code review round 2（2026-08-17，post-build diff review）：

| Finding | Disposition | 處置 |
|---------|-------------|------|
| [P1] Reused Uncommitted session 遺失 `--watch` | accept — DEC-005 reload 欄修正 | hunk 0.18.1 的 watch 狀態取自當前 input（App.tsx `watchEnabled = input.options.watch && …`），reload 整組替換 input，`--watch` 為全域旗標可進 reload args。Uncommitted reload args 改 `diff HEAD --watch`（其餘 target 不變）；T007 任務文字中「reload drops --watch」被此條目取代 |
| [P1] send-notes read→prompt→mark 非 atomic，concurrent invocation 重複 prompt | accept | `state/send.lock` + `fcntl.flock`（LOCK_EX\|LOCK_NB）包住整段流程；持鎖失敗 → notification『send already in progress』+ exit 1；flock 隨 process 結束自動釋放，crash 不残留 |
| [P2] fzf 執行錯誤被視為 Esc | accept | run_fzf 只把 rc 1/130 當取消；OSError / 其餘 rc 招 RuntimeError，cmd_picker 接住後 print + 等按鍵 + exit 1（DEC-011） |
| [P2] 直接執行 test file 只跑 5 個 tests | accept | `__main__` guard 搬至檔尾（先前 append 測試類別落在 guard 之後，direct run 提早 sys.exit） |
| [P3] README hunk link 失效 | accept | herdr/hunk 兩個連結皆為臆造，改 brew 登記的官方首頁 herdr.dev / hunk.dev |
