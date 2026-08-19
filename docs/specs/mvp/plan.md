---
summary: Technical plan for the herdr-review MVP (single Python script, manifest shape, state layout, verification commands)
read_when:
  - Implementing the MVP
  - Verifying implementation against decisions
---

# herdr-review — Plan

## Technical approach

- 單一 Python 3 stdlib script `scripts/review.py`，以 subcommand 分工：`open-picker`（action，開 pane）、`picker`（pane 內互動）、`send-notes`（action）。
- picker pane 由 `herdr plugin pane open` 開出（placement split、direction right、focus），pane 內 fzf 選完 target 後 `os.execvp` 成 tuicr，pane 即 viewer。
- 已有 viewer pane 時先關舊 pane（tuicr 無 reload），新 picker pane 接手成為該 repo 唯一 viewer。
- send-notes 走 tuicr 官方 agent 介面（`docs/REVIEW_CLI.md`）：`tuicr review list` → `tuicr review comments` + `herdr pane neighbor / agent list`；投遞走 daemon API `pane.send_input`（DEC-014，draft 貼入不送出）。
- 狀態檔兩個（sent ids、repo→pane 映射）放 `HERDR_PLUGIN_STATE_DIR`，JSON、原子寫入（temp + rename）。

## Decisions

### DEC-001: Python 3, stdlib only

- Choice: `#!/usr/bin/env python3`，只用 `json / subprocess / os / sys / pathlib / tempfile`。
- Alternatives: bash + jq（字串處理易碎）、TypeScript（要 npm ci + build，兩台 node 版本不一）。
- Rationale: 使用者選擇；JSON 邏輯多，Python 最穩且零 build。macOS 系統 python3 ≥ 3.9 即可跑。
- Satisfies: 全部 REQ。

### DEC-002: Single script with subcommands

- Choice: 一個 `scripts/review.py`（估 <500 行），不拆模組。
- Alternatives: 三個 script + common module（import 路徑要顧）；jhochenbaum 式多檔 + build。
- Rationale: 三個進入點共用 herdr/git/tuicr helper，單檔讓 manifest 與安裝最簡單。超過 ~600 行再拆。
- Satisfies: REQ-001..008。

### DEC-003: Manifest shape

- Choice: 1 個 pane entrypoint + 2 個 action：

```toml
id = "herdr-review"               # 無點號，keybinding command = herdr-review.<action>
min_herdr_version = "0.8.0"

[[panes]]
id = "picker"
placement = "split"
command = ["sh", "-c", "exec python3 \"$HERDR_PLUGIN_ROOT/scripts/review.py\" picker"]

[[actions]]
id = "review"       # open-picker
[[actions]]
id = "send-notes"
# action command = ["python3", "scripts/review.py", ...]（herdr 0.8 action cwd = plugin root）
```

- Rationale: pane command 的 cwd 是 open 時的 `--cwd`（repo），必須經 `$HERDR_PLUGIN_ROOT` 定位 script（JacquesvanWyk 用 env 傳路徑、jhochenbaum 用 `$HERDR_PLUGIN_ROOT`，取後者）。action command 相對 plugin root 直接可跑。
- Satisfies: REQ-001、REQ-006、REQ-009。

### DEC-004: Picker pane opening

- Choice: `open-picker` 從 `HERDR_PLUGIN_CONTEXT_JSON.focused_pane_cwd`（fallback：`herdr pane list` 的 focused pane）取 cwd，`herdr plugin pane open --plugin herdr-review --entrypoint picker --placement split --direction right --cwd <cwd> --focus`。
- Rationale: picker 需要鍵盤，必須 `--focus`；direction right 與現有 nvim pane 慣例一致，tuicr 需要寬度。repo root 正規化留給 pane script（`git rev-parse --show-toplevel`），action 保持最薄。
- Satisfies: REQ-001。

### DEC-005: Target → tuicr argv map（2026-08-18 改寫）

- Choice:

| Target | exec argv |
|--------|-----------|
| Merge base | `tuicr -r <base>...HEAD` |
| Uncommitted | `tuicr -w` |
| Last commit | `tuicr -r HEAD` |
| Pick commit | `tuicr -r <sha>` |
| Pick range | `tuicr -r <old>..<new>` |
| Pick range（單標記，至工作樹） | `tuicr -r <old>..HEAD -w` |
| Pick range（單標記=tip，至工作樹） | `tuicr -w`（塌回 Uncommitted） |
| Branch vs branch | `tuicr -r <base>...<compare>` |

- Rationale: tuicr 用單一 `-r <revset>` 取代 hunk 的 diff/show 動詞，且 revset 直接吃 git 的三點/兩點語法，映射是一對一的。`-w` 單獨用時是 uncommitted，搭 `-r` 時是「把範圍延伸到工作樹」—— 對應 hunk 時代 `hunk diff <old>`（單邊界 = 比到工作樹）的語意。例外（2026-08-19）：old 即為 tip 時 `<old>..HEAD` 是空 commit range，tuicr 帶著 `-w` 也照樣拒絕（`No changes to review`），picker 將此選擇塌回 Uncommitted 的 argv。
- Satisfies: REQ-002、REQ-004、REQ-005。

### DEC-016: Uncommitted 失去 watch（2026-08-18）

- Choice: Uncommitted target 為 snapshot，不再 live 更新；要刷新就重按 cmd+shift+h。
- Rationale: tuicr 沒有 watch 模式，而這正是換掉 hunk 的主因 —— 實測 hunk watch 每次檔案變更要燒 ~112ms CPU，agent 寫檔 burst 時直接卡死主執行緒（詳見下方效能數據）。掉的是一個實際有害的功能；tuicr 啟動快（單一 binary），重開成本低。
- Satisfies: REQ-002。

### DEC-006: Sub-pickers

- Choice: Pick commit / Pick range 用 `git log --oneline --color=always -200 | fzf --ansi`；Pick range 走兩輪（prompt `old>` 選舊端，`new>` 只列比 old 新的 commits 加首列 `(worktree)` 預設項，構造上保證 `old..new` 順序）；Branch vs branch 用兩輪 fzf（prompt `base>` 再 `compare>`，來源 `git branch -a --format='%(refname:short)' --sort=-committerdate`，`-a` 才會列 remote-tracking branches，濾掉 `origin/HEAD` alias 列）。子選單按 Esc 直接關 pane（重按 cmd+shift+h 成本低）。
- Rationale: 沿用 JacquesvanWyk 驗證過的 log picker；方向語義（誰是舊端 / base）一律用兩輪 prompt 消除歧義。Pick range 原為單輪 `--multi 2`：Tab 標記不可發現，且游標行按 Enter 只回傳已標記項，單標記 fallback 會提前開 viewer，體感為「選不到第二個 commit」——2026-08-18 改為兩輪。
- Satisfies: REQ-002。

### DEC-007: Viewer reuse — 關舊 pane 而非 reload（2026-08-18 改寫）

- Choice: 確認 target 後，若 `state/panes.json` 有該 repo 的 pane 且不是自己，先 `herdr plugin pane close <old>`（失敗靜默），再把 `repo → 自身 HERDR_PANE_ID` 寫回 mapping 並 exec tuicr。
- Alternatives: 先送 `q` 結束舊 pane 的 tuicr 再 `herdr pane run` 塔新指令（靠猬 TUI 退出時機，脆）；focus 舊 pane 忽略新 target（使用者選了卻沒反應）。
- Rationale: tuicr 沒有 reload 動詞，無法就地換 target。關舊開新保住了 AC-007 真正在乎的不變式「一個 repo 一個 viewer」；且 tuicr 按 target slug 持久化 comment，回頭選同一 target 會還原筆記，hunk 時代關掉就沒了。
- Satisfies: REQ-004。

### DEC-008: Sent-id state per tuicr session slug（2026-08-18 改寫）

- Choice: `state/sent.json`，shape `{ "<tuicr-session-slug>": ["<comment-id>", ...] }`。send 流程：`tuicr review list --repo <repo>` → 挑 session（DEC-018）→ `tuicr review comments --session <slug> --repo <repo>` → 過濾 `lifecycle_state == "local_draft"`（DEC-019）→ 過濾已送 id → paste 成功後 markSent → 用 `tuicr review list --all` 的 slug 集合 GC 死 entry。
- Rationale: comment 隨 session 檔持久化，以 slug 為 key 讓 state 生命週期與 comment 一致。GC 必須用 `--all`：`--repo` 只列單一 checkout，拿它做 GC 會誤刪其他 repo 的已送記錄（hunk 的 `session list` 是全域的，語意不同）。讀取 fail-closed（2026-08-19）：sent.json 不存在 → 空 state；存在但損壞/不可讀 → 中止 send 並通知（當空 state 用等於失去唯一防線，會重送全部歷史）。panes.json 維持 lenient：best-effort cache，損壞下次寫入自癒。
- Satisfies: REQ-008。

### DEC-009: Agent resolution implementation

- Choice: 依 REQ-007 順序：(1) `herdr agent list` 內含 focused pane id → 選它；(2) `herdr pane neighbor --direction left|right|up|down --pane <focused>` 依序，pane id 在 agent list 內即中；(3) `herdr pane list` 過濾同 tab 且 agent 非 null 且 `git -C cwd rev-parse --show-toplevel` == repo，恰一個即中；否則 `herdr notification show` 列候選。投遞走 DEC-014 的 draft 貼入，非 `herdr agent prompt`。
- Rationale: 空間關係優先（使用者語義「隔壁」），repo 匹配兜底；多候選寧可失敗要求使用者站位，不猜。
- Satisfies: REQ-006、REQ-007。

### DEC-010: Prompt format

- Choice: 硬編碼英文 template：

```
Human inline review comments on your changes in {worktree}:

- {path}:{lines} — [{type}] {content}
...

Address each comment and verify the result. If a comment is unclear, ask a focused question before proceeding.
```

- Rationale: jhochenbaum template 的精簡版。多行 body 原樣保留（`pane.send_input` 走 bracketed paste，DEC-014）。行號規則見 DEC-020。
- Satisfies: REQ-006。

### DEC-011: Error reporting

- Choice: action 層錯誤 → `herdr notification show` + stderr（herdr plugin log 可查）；picker pane 內錯誤 → print + 等按鍵再退出（讓人讀得到）。
- Satisfies: REQ-001 AC-002、REQ-006 AC-011。

### DEC-012: Keybinding wiring（hand-written）

- Choice: kitty `TUI launchers` 區塊加兩行（`\x1b[104;8u`、`\x1b[115;8u`）；herdr config 加兩條 `[[keys.command]]`（`["prefix+alt+h","ctrl+alt+shift+h"]` → `plugin_action herdr-review.review`；s 同理 → `send-notes`），註解風格照現有 lazygit/yazi 條目。不做 setup-keys managed block。
- Satisfies: REQ-009。

### DEC-013: Fork-parent base detection（2026-08-18）

- Choice: REQ-003 的 base resolution 在非-own-tracking `@{u}` 之後、`origin/HEAD` 之前插入 fork-parent 偵測，固定 6 次 git 呼叫：(1) `git remote` 取 remote 名單（remote 名可含 `/`，如 `team/origin`，branch 部分以最長 remote prefix 匹配切出，未知 layout 退回第一段切割）；(2) `for-each-ref` 列候選 refs（排除當前 branch、各 remote 對它的 copy、remote `HEAD` alias；用 full refname 解析，巢狀 local 名稱如 `feature/x` 不會誤比）；(3) `for-each-ref --contains HEAD` 找出包含 HEAD 的 refs（stack 的 child、同 tip 雙胞胎），從候選與標籤兩階段都排除 —— 它們不可能是 parent，留著會把 fork point 壓成 HEAD 讓偵測直接收場；探針失敗（當前 branch 必含 HEAD，空答案即失敗）則 fail closed 走 fallback；(4) `rev-list --count --first-parent HEAD --not <candidates>` 得專屬 commit 數 k；(5) `HEAD~k` = fork point；(6) `for-each-ref --contains <fork-point>` 找含有它的 refs，優先序：tip == fork point（未移動的 parent）> local > conventional（main/master/trunk）> 名稱字序。k == 0（候選集與 ref 更新競賽）或 `HEAD~k` 不存在（全史專屬）→ 回傳 None 走舊 fallback。在 main/master/trunk 或 `origin/HEAD` 目標 branch 上直接跳過偵測（trunk 沒有 parent）。
- Alternatives: 逐 branch `merge-base` + distance（N 次呼叫，大 repo 慢）；reflog `Created from`（只記 `HEAD`，不可靠；且 clone 來的 branch 的 reflog 起點是 clone 當下，非真正創建點）；`merge-base --fork-point`（需先知道 candidate 且依賴 reflog）。
- Rationale: stacked branch（feature-b 從 feature-a 開出）先前落到 `origin/HEAD`/`main`，diff 把 parent 的 commits 一併捲進來；git 沒有 parent-branch metadata，first-parent 鏈上第一個被其他 branch 包含的 commit 就是 fork point，含有它的 ref 即 parent 候選；排除自身 remote copy 是 AC-006 的廣義化（否則 pushed branch 永遠 diff 到只剩 unpushed commits）；排除含 HEAD 的 refs 是同一邏輯對 child 方向的對稱式（code review 指出的 P1：活的 stack 中 child 常駐 tip，不排除則原 bug 復發）。已知限制：child 從我方鏈中途 commit 開出、我方其後續有新 commit 時，單靠 reachability 無法與 parent 區分（方向資訊只存在於未被記錄的創建事件）；選單標籤會顯示勝出的 ref，使用者可用 branch-vs-branch 或 `--set-upstream-to=<parent>`（REQ-003 第一優先）明確指定。
- Satisfies: REQ-003 AC-006、AC-017–AC-019。

### DEC-014: Draft 投遞——daemon API `pane.send_input`（2026-08-18）

- Choice: send-notes 投遞從 `herdr agent prompt`（bracketed paste + Enter，直接送出）改為直接對 herdr daemon 發 NDJSON request：`{"method": "pane.send_input", "params": {"pane_id", "text"}}`（無 `keys`），Unix socket 路徑取 `HERDR_SOCKET_PATH`（server 注入 plugin action 環境，runtime.rs）。server 端經 `encode_api_text` 依 pane app 的 bracketed-paste 狀態包 `\x1b[200~…\x1b[201~`，多行文字落地等同人手貼上，不送 Enter —— 使用者在 composer 微調後自行送出。Python 側新增 stdlib `socket`（DEC-001 精神不變）。
- Alternatives: `herdr pane send-text`（raw bytes 直寫 PTY，無 bracketed paste，多行 `\n` 可能被 chat TUI 當成送出，v0.8.0 原碼 handle_pane_send_text 實證）；`herdr pane run` / `agent prompt`（都追加 Enter）；等 herdr CLI 新增 paste-without-enter 動詞（master 尚無）。
- Rationale: 使用者要求送出前可微調；`pane.send_input` 是 `agent prompt` 同一條編碼路徑減去 Enter，行為保證相同。已知取捨：(1) pasted = delivered，人在 composer 丟棄 draft 則這批 note 不可重送（sent.json 已記錄）；(2) 繞過 CLI 的 protocol guard，未來 herdr 換 wire 形狀時以 error response / 連線失敗顯現，notification 可見。
- Satisfies: REQ-006 AC-009。

### DEC-015: 從 hunk 換到 tuicr（2026-08-18）

- Choice: viewer 後端全面換成 tuicr（Rust），plugin 更名 `herdr-hunk-review` → `herdr-review`，script 更名 `hunk_review.py` → `review.py`。
- Rationale: 實測數據（同一台 M 系機器、同一份 20 檔 / 2.7k 行變更的 diff）：

| 指標 | hunk 0.18.1 | tuicr 0.22.0 |
|------|-------------|--------------|
| RSS | 368MB（+85MB session 子程序） | 57MB |
| `]` 跳 hunk | ≈28ms CPU/次 | ≈3.7ms CPU/次 |
| watch 每次檔案變更 | ≈112ms CPU | 無 watch 模式（DEC-016） |

  hunk 的卡頓是架構天花板而非實作瑕疵：React 19 + OpenTUI + TextMate highlight 全擠在單條 JS 執行緒，上游自己也在往 SolidJS 遷（modem-dev/hunk#443）；0.19.0 與實驗性 `--fast` 實測對這個 workload 皆無改善。換完另得到 hunk 沒有的 range comment（visual mode `v/V`）、comment 分型、stepwise gap 展開（`Enter` 20 行）、session 跨重啟持久化。
- Alternatives: 留在 hunk 等上游修（時程不明）；自寫一個 Rust viewer（tuicr 已經是那個輪子，成本差一個數量級）；difit 等本地 web UI（要離開終篯）。
- Satisfies: 全文。

### DEC-017: 整合面——tuicr review CLI 而非 session JSON（2026-08-18）

- Choice: 所有讀取走官方 `tuicr review list` / `tuicr review comments`，不直讀 `~/Library/Application Support/tuicr/reviews/sessions/*.json`。
- Rationale: tuicr `docs/REVIEW_CLI.md` 明文寫著這組指令「is intended for scripts and coding agents」，是受支援的整合面；輸出預設即 JSON、timestamp 為 RFC3339。session JSON 是內部 schema，只有它才有 `author` 欄位（見 DEC-019 限制），但不值得為此點換成逆向工程。
- Satisfies: REQ-006、REQ-008。

### DEC-018: Session 選擇——先 active，再最新（2026-08-18）

- Choice: `tuicr review list --repo <repo>` 中挑 `active: true` 者；皆非則取 `updated_at` 最新。
- Rationale: tuicr 在 TUI 執行中即時落盤並標記 `active`（實測：TUI 內存檔後外部進程立刻讀得到），所以不必像 hunk 那樣依賴 daemon 存活。回退到最新 session 是刻意的：tuicr 關閉後保留 session 檔，舊流程裡「關掉 viewer 就拿不回 note」的損失沒必要延續。
- Satisfies: REQ-006 AC-011、AC-022。

### DEC-019: 只送 local draft comment（2026-08-18）

- Choice: 過濾 `lifecycle_state == "local_draft"`。
- Rationale: 對應 hunk 時代的 `--type user`，排掉已 publish 到 forge 的遠端 comment（PR session 會包含它們）。已知限制：官方 `review comments` 輸出**不含** `author`（只有內部 session JSON 有），所以若日後加入 agent 回寫（`tuicr review add --username`），必須先向上游要 `author` 欄位或改讀 session JSON，否則 agent 會把自己的回覆再送給自己。本輪 plugin 不寫入 comment，故不受影響。
- Satisfies: REQ-006 AC-021。

### DEC-020: Prompt 行號與分型（2026-08-18）

- Choice: `start_line == end_line` → `path:12`；不同 → `path:10-14`；無 `start_line`（file 層級）→ 只有 path；無 path（review 層級）→ `(review)`。`comment_type` 非 `none` 時前置 `[type] `。side 為 old（刪除行，行號屬變更前檔案）→ 尾綴 ` [old]`，與 tuicr 自身的 location 渲染一致（2026-08-19）。
- Rationale: tuicr 帶真正的 range（hunk 沒有，舊實作只能取 `newRange[0]` 塌成單行），不把它傳給 agent 等於丟掉新能力；nit / issue / question 的分型直接影響 agent 該花多少力氣，值得一併帶上。
- Satisfies: REQ-006 AC-020。

### DEC-021: 送出後不刪 comment（2026-08-18）

- Choice: 送出只寫 sent.json，comment 留在 tuicr 裡。
- Alternatives: 直接改寫 session JSON 刪除（碰內部 schema，且與執行中的 TUI 竞寫）。
- Rationale: tuicr 沒有 CLI 刪除動詞（只有 TUI 內 `dd`）。但這反而比 hunk 好：comment 成為留存的 review 記錄，人可以回頭看自己提過什麼；重複防護完全落在 sent.json（本來就是主防線，hunk 時代的 rm 只是額外保險）。
- Satisfies: REQ-008 AC-014。

### DEC-022: 已知限制——TUI 內刪除延遲落盤（2026-08-18，實測）

- Observed: tuicr 的新增 comment **即時**寫入 session 檔（TUI 內存檔後外部進程立刻讀得到），但 `dd` 刪除**不會**立刻反映到 `tuicr review comments`：實測刪除後 35 秒 CLI 仍讀到舊值，TUI 畫面已無該 comment；直到 TUI 退出才落盤（該 session 隨即因為空而被 tuicr 自行移除）。
- Consequence: 人在 viewer 裡刪掉的 comment，在 viewer 關閉前仍會被 `cmd+shift+s` 送出。
- Disposition: 接受並記錄。不在 plugin 側繞過（直讀/改寫 session JSON 會與執行中的 TUI 竞寫，風險高於收益）。規避方式：要撤回就先關 viewer，或編輯 comment 內容而非刪除。值得向上游回報 —— `docs/REVIEW_CLI.md` 承諾 agent 可以即時解析已宣告的 session，刪除不同步與該承諾不一致。

## Change Map

| File | Action | REQ |
|------|--------|-----|
| `~/Developer/ohlulu/herdr-review/herdr-plugin.toml` | create | REQ-001, 006 |
| `~/Developer/ohlulu/herdr-review/scripts/review.py` | create | REQ-001..008 |
| `~/Developer/ohlulu/herdr-review/tests/test_review.py` | create（stdlib unittest，injected-runner 測純邏輯） | REQ-002..008 |
| `~/Developer/ohlulu/herdr-review/README.md` | create（安裝、keybinding、行為說明） | REQ-009, Out-of-scope 部署 |
| `~/.config/kitty/kitty.conf` | edit（TUI launchers 兩行） | REQ-009 |
| `~/.config/herdr/config.toml` | edit（兩條 keys.command） | REQ-009 |

## Verification

- `python3 -m unittest discover -s tests` → 純邏輯（base resolution、menu/argv map、agent resolution、prompt format、sent-id filter）含真實 git 暫存 repo 的 fork-parent integration tests（AC-017–019、slash remote、trunk fallback）全綠。
- `herdr plugin link ~/Developer/ohlulu/herdr-review` + `herdr plugin list` → plugin 可見、無 manifest 錯誤。
- `herdr plugin action invoke review --plugin herdr-review` → picker pane 開啟（AC-001）；非 git cwd pane 觸發 → AC-002。
- `tuicr -w` 語義實測：staged-only 變動要出現（DEC-005）。
- 選單六項逐一確認 argv（AC-004..008；用測試 repo 造 upstream-self、無 base、stacked + child、remote-only parent 等情境驗 AC-005、AC-006、AC-017–019）。
- send 全鏈路：tuicr TUI 手動 `c` 加 comment（含 `v` range）→ cmd+shift+s → agent pane 輸入框出現 draft（未送出，含 `path:10-14` 列）、comment 留在 tuicr、再按一次得 no-new-notes（AC-009、010、014、020）。
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
