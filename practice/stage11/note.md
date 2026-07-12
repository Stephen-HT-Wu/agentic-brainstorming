# Stage 11 Note — 即時控制面板（Live Control Panel）

## 目標（對照使用者需求）

1. 打主題、調整 persona，按下開始後即時看到會議進行，撞到 human-in-the-loop
   提問點真的暫停，人類回答/跳過後繼續
2. 多位 persona 平行做功課時，能看到「每條線平行跑」的樣子（graph 的
   node/edge 視覺化）
3. Persona 顯示角色身份，不只名字
4. 每次開新會議不覆蓋舊紀錄
5. 每個 agent 配一張頭像

## 這一步練到的新 agentic 概念

跟 stage0-10 一路建立的 CLI pipeline 不同，stage11 要解決的是「用無狀態的
HTTP request/response 跨越多次呼叫去驅動一個有 checkpointer 的 LangGraph」
——不是 CLI 那種一個 process 從頭跑到尾的 while-loop，而是每次 HTTP 請求
可能是完全獨立的 subprocess，中間可能間隔數分鐘（等人類回答問題）。這正好
逼出 LangGraph checkpointer 設計上「本來就該支援跨 process 續跑」這個特性
的真正威力——stage7/stage9 只驗證過「process 被砍掉、用同一個 thread 重開」
這種被動情境，stage11 是第一次主動把「開新 subprocess 驅動同一個 thread」
當成核心架構，而不是意外/災難恢復。

## 架構決策

### 為什麼每個「跑一段」的動作都是獨立 subprocess，不是常駐 process 裡的 thread

`practice/stage9/graph.py` 的 `OUTPUT_DIR`／`EVENTS_PATH`／
`CHECKPOINT_DB_PATH`／`CHROMA_DIR` 等路徑是模組層級變數。如果在同一個
`server.py` process 裡直接 import `graph` 模組、用多條 thread 各自
monkeypatch 這些路徑來跑不同的會議，thread 之間會互相覆蓋對方的路徑設定，
事件會寫到錯的檔案——這是真的會發生的 race，不是理論風險。

改成 `practice/stage11/run_worker.py` 一支 CLI，每次「開始一場會議」或
「回答/跳過一次人類提問」都是 `server.py` 用 `subprocess.Popen` 開一個全新
的 Python process 呼叫它。每個 subprocess 各自 import 自己的 `graph.py`
副本、monkeypatch 只影響那個 subprocess 自己的記憶體——不同會議之間天然
沒有共用可變模組狀態的問題，也順便讓 SQLite checkpoint db 不同會議各自
一個檔案，不會有多個 writer 搶同一個檔案鎖的疑慮。

能這樣設計，是因為 stage9 的 checkpointer 本來就是設計成可以跨 process
續跑的（`run_worker.py` 開頭有詳細說明）。

### 每次會議獨立目錄，不覆蓋舊紀錄

`server.py` 的 `POST /api/runs` 幫每場會議算一個獨立的 `run_id`（時間戳 +
主題 slug + 隨機後綴），對應到 `practice/outputs/runs/<run_id>/` 底下自己的
`events.jsonl`／`checkpoints.sqlite`／`prototypes/`／`reports/`——沿用
stage10 `run_sample_meeting.py` monkeypatch 路徑常數的手法，只是從「一個
固定的 demo_workspace」改成「每場會議一個獨立目錄」。`CHROMA_DIR` 刻意
**不**跟著隔離：真實會議的集體智慧本來就該跟使用者真實的
`practice/chroma_db` 共用、持續累積，這是 stage7 起的設計本意，只有
`--example-config`（範例設定試跑模式）才會連 Chroma 也隔離。

### 供 persona 平行泳道視覺化：`events.jsonl` 是唯一夠細的資料源

探索時發現：LangGraph 自己的 task/stream API 只看得到 persona 這一層的
平行度（`Send()` 分派），因為每個 persona 的整個「做功課」子圖是在一個
`homework_worker` 節點函式內用 `.invoke()` 呼叫完成的，不是 LangGraph
checkpoint 得到的獨立節點——子步驟（collect/dedup/recall_memory/…）只存在
於 `events.jsonl`。所以即時泳道完全靠 SSE tail `events.jsonl`，不靠
LangGraph 的執行狀態 API。

## 踩到的真實坑（依發現順序）

### 1. 中文 run_id 讓 HTTP request line 直接壞掉

第一次拿中文主題產生的 `run_id`（例如
`20260711-225538-stage11-backend-驗證-訂閱制內容`）直接放進 URL path，
`curl -N http://localhost:8899/api/runs/<run_id>/events` 回
`Invalid HTTP request received.`——h11/uvicorn 對 request line 裡未
percent-encode 的多位元組 UTF-8 直接判為不合法請求。percent-encode 過的
同一個 URL 測試沒問題，證實問題出在「run_id 本身含非 ASCII 字元」，不是
單純沒 encode。修法：`_slugify()` 只保留 ASCII 字元，主題全文仍完整存在
`meta.json`，同時額外加 6 碼隨機 hex 尾巴避免同一秒兩個全中文主題的 run_id
撞在一起變成同一個 `"topic"` slug。

### 2. `--example-config` 沒有隔離 Chroma，測試資料真的混進使用者的真實集體智慧庫

第一版 `_patch_paths()` 只隔離了 `OUTPUT_DIR`／`EVENTS_PATH`／
`CHECKPOINT_DB_PATH`——`CHROMA_DIR` 沒動。拿一個純粹要驗證 SSE 有沒有即時
串流的測試會議（`--example-config`）跑起來，`recall_memory` 事件真的讀到
`s7round1`／`s9round1` 這些**真實**開發階段留下的集體智慧！當場中止該次
測試 process（還沒跑到 `write_wisdom`，只是 read，沒有寫入污染），確認
`--example-config` 也要連 `CHROMA_DIR` 一起隔離到該次 run 自己的目錄——
跟 stage10 demo 用假資料就要整個 workspace 隔離是同一個原則。

### 3. 最嚴重的一次：resume 之後重跑整場會議，Chroma DuplicateIDError 崩潰

`cmd_resume()` 先手動呼叫一次 `graph.invoke(Command(resume=...), config)`
（一個 raw invoke，語意上會一路跑到下一個 `interrupt()` 或整場結束才
回傳），完事後為了重用 `sg.main()` 收尾的 baseline 對照／寫最終報告／
`save_outputs` 邏輯，又呼叫一次 `sg.main()`（會進去呼叫 stage9 的
`run_meeting()`）。真實跑測時，某次 resume 剛好是整場會議的最後一個
interrupt（之後不會再暫停了），我的手動 invoke 已經讓會議直接跑到底、
`write_wisdom` 也真的成功寫入了——但緊接著 `sg.main()` 內部的
`run_meeting()` 開頭判斷「這個 thread 有沒有未完成工作」只看
`snapshot.next` 是不是空的：**「從沒開始過」跟「剛剛才跑完」兩種情況
`snapshot.next` 都是空 tuple**，`run_meeting()` 分不出來，於是把整場會議
用 `initial_input` 從頭重跑一次。事件流裡 `homework_start` 從預期的 4 筆
變成 8 筆，第二次 `write_wisdom` 寫入時撞到跟第一次一樣的
deterministic id（`<round_id>-master-*`），直接
`chromadb.errors.DuplicateIDError` 崩潰——燒了一整場的真實 API 成本才抓到
（第一次全新完整跑一遍 + 重跑到一半崩潰，粗估花費比正常一場貴了將近一倍）。

**修法**：`run_worker.py` 用 `_safe_run_meeting` monkeypatch 掉
`sg.run_meeting`：多檢查一個條件——光看 `snapshot.next` 是空的還不夠，
一個「從沒開始過」的全新 thread_id 一樣會回報 `snapshot.next == ()`
（連 checkpoint 都不存在），這裡兩者的區別要看 `snapshot.values` 是不是
空字典（全新 thread 是 `{}`，已完成的 thread 是最後一次 checkpoint 的完整
state）。已完成就直接回傳既有 state，不會再去呼叫真正會重跑的
`initial_input` 分支——這樣 `sg.main()` 收尾的邏輯還是會正常跑，只是不會
再把整張圖重跑一次。用 `test_run_worker.py` 的玩具 `StateGraph`（零成本，
不打真實 API）鎖住這個行為，包含證明「風險本身是真的存在」的對照測試。

修完之後重新驗證整條「開會 → 即時看 events → 撞到 interrupt → 回答/跳過 →
continue → 跑完」的迴圈，`homework_start` 穩定維持 4 筆、`write_wisdom`
穩定維持 1 筆，`stage9-run-*.json`／最終報告都正確產生。

### 4. `/replay` 的路徑組合差一層目錄

`_inspect_final_state()` 存 `report_path`／`run_json_path` 時是相對
`sg.OUTPUT_DIR.parent` 算出來的（`sg.OUTPUT_DIR` 被 monkeypatch 成
`run_dir` 本身，不是 `run_dir/outputs`，所以那個 parent 正是
`RUNS_DIR`）。`server.py` 的 `get_replay()` 一開始寫成
`run_dir.parent.parent / state["run_json_path"]`——多繞了一層，指到
`practice/outputs/` 而不是 `practice/outputs/runs/`，回放頁直接
`FileNotFoundError` 500。修成 `run_dir.parent / ...` 後才對。

### 5. 前端：多行文字經過 `escapeHtml()` 之後憑空冒出字面上的 `<br>`

點頭像旁邊的 persona 編輯表單，`background`（YAML `>` 折疊出來的多行段落）
在 `<textarea>` 裡顯示成一行文字結尾多了個「<br>」——但直接查
`personas.yaml` 原始檔完全沒有這個字串，DOM 裡讀到的 `.value` 也是乾淨的
`\n`，證明不是資料問題。根因：`escapeHtml()` 沿用了 stage10
`build_replay.py` 那招「`d.innerText = s; return d.innerHTML`」——這招對
單行摘要文字沒事，但 Chromium 幫 `innerText` 塞值時，字串裡的換行字元會被
轉成真的 `<br>` 節點，讀回 `innerHTML` 時這些 `<br>` 就變成輸出字串的一
部分；而 `<textarea>` 顯示的是原始文字、不會解析 HTML，於是使用者看到的是
字面上的「<br>」，不是換行。改成不碰 DOM 的純字串取代（手動跳脫
`& < > " '`），多行文字才正確保留純換行。

同一次順手發現並修掉一個相關但獨立的問題：`esc()` 原本沒跳脫引號
（`"`／`'`），但整個檔案到處把它的結果塞進雙引號屬性值（`input value="..."`、
`href="..."`、`data-topic="..."`）——使用者打的主題或人設文字只要剛好含
一個雙引號就會把屬性截斷。連帶把歷史紀錄列表的
`onclick="openHistoryRun('...')"` 這種「HTML 屬性裡包一段 JS 字串常值」的
寫法整個換掉，改用 `data-*` 屬性 + `addEventListener`——這種巢狀跳脫（HTML
屬性 + JS 字串）疊在一起很容易漏掉某個組合，不如從根本避開。

### 6. 拓樸圖一直不會亮

平行泳道能正確即時長出節點，但上方的 supervisor 拓樸圖在整個「做功課」
階段完全不會亮——因為 `events.jsonl` 的 `node` 欄位是 `instrument()` 包住
的「實際跑的那個函式」名字，對巢狀子圖（`homework_worker`／
`run_peer_review`／`run_masters`／`run_collective_scoring`／
`run_prototype_test`／`run_three_lens_check` 底下各自 compile 的子圖）而言
是子步驟自己的名字（`collect`／`dedup`／`give_feedback`／
`master_critique`…），不是最外層 10 個 supervisor 節點的名字。補上一張
`NODE_TO_SUPERVISOR` 對照表（依 `practice/stage9/graph.py` 各
`build_*_subgraph()` 的 `add_node()` 清單手動整理），子步驟事件先映射到
所屬的 supervisor 節點再更新「最近活躍」時間戳，拓樸圖才會正確跟著亮。

## 怎麼跑

```bash
cd practice/stage11
../.venv/bin/python -m unittest test_avatars.py test_run_worker.py -v   # 13 個測試，零成本

../.venv/bin/uvicorn server:app --port 8899
# 開瀏覽器到 http://localhost:8899/
```

設定畫面可以打主題、編輯/新增/刪除 persona（存到真實的
`practice/personas.yaml`，`.example.` 檔不會被動到）；勾選「使用範例
設定」可以用 `personas.example.yaml` 試跑，不動用真實人設也不寫進真實
集體智慧庫，適合先驗證流程。

## 實際觀察（2026-07-12，真實跑測）

用 `--example-config` 完整跑過三次（含一次browser 真實點擊觸發、真的撞到
`facilitator_decide` 暫停、真的按跳過按鈕繼續）：事件流即時串到瀏覽器、
泳道正確依 persona 平行長出節點、supervisor 拓樸圖正確跟著亮、HITL
面板正確自動彈出並顯示發表者頭像/角色/提案摘要、跳過後正確繼續跑到
`write_wisdom`／`run_collective_scoring`／`run_prototype_test`／
`run_three_lens_check`／完成，`stage9-run-*.json` 與最終報告正確產生，
歷史紀錄列表正確顯示 `done` 並連到重用 `build_replay.py` 產生的完整
回放頁（BMC/來源/POV-HMW 等細節點得開）。

其中一次真實跑測的總成本只有 $0.0053（遠低於 stage10 note.md 記錄的
$0.6680）——不是 bug，是因為測試主題（"stage11 前端瀏覽器驗證測試" 這類
無實質內容的測試字串）讓每個 persona 的搜尋/訪談/提案內容都極度精簡，
token 用量自然大幅下降；事件數（147 筆）跟完整流程結構完全一致，證實
不是流程被截斷，只是內容量小。

## 驗收

- [x] 打主題、調整 persona（新增/刪除/編輯，含角色/背景/關注面向/發言
      風格），按下開始後即時看到會議進行；撞到 HITL 提問點真的暫停，
      人類回答/跳過後真的繼續（真實 UI 點擊驗證過，不只 curl）
- [x] 多位 persona 平行做功課時能看到「每條線平行跑」的樣子：supervisor
      拓樸圖 + persona 平行泳道，兩者都會依真實事件即時更新
- [x] Persona 顯示角色身份（泳道抬頭、HITL 面板都秀出「角色／專業」，
      不是只有姓名）
- [x] 每次開新會議不覆蓋舊紀錄（獨立 `run_id` 目錄，歷史紀錄列表可以
      回顧全部跑過的會議）
- [x] 每個 agent 配一張頭像（本地決定性產生，零成本，`avatars.py` +
      `test_avatars.py` 8 個測試鎖住 deterministic 這個不變量）

## 第二階段：可觀測性／UX／RAG 擴充 12 項（2026-07-12）

使用者用過即時面板後提出 12 項後續要求（研究足跡追蹤、回放成本顯示、
detail 面板不用滑頁面、baseline 連結修正、refine 具體改了什麼、模擬用戶
編輯器、即時成本/耗時追蹤、程式碼共用抽離、HITL 顯示名稱、RAG 寫入範圍
擴大、AI 對照評語、降本增效框架），完整規劃見
`/Users/stephen/.claude/plans/agentic-ai-hazy-hummingbird.md`。程式碼變更
本身（`stage9/graph.py`／`stage10/build_replay.py`／
`stage10/shared_renderers.js`／`stage11/server.py`／
`stage11/run_worker.py`／`stage11/static/index.html`）不重複記錄在這裡；
這節只記真實跑測抓到的坑。

### 7. 即時畫面剛開始一律顯示「系統」，看不到真正角色

`loadRunConfig()` 在 `POST /api/runs` 拿到 `run_id` 後立刻 fetch
`/api/runs/{id}/config`，但 worker subprocess 剛 spawn 時（python 啟動 +
`import graph.py` 有 `chromadb` 等重量級套件）`config_snapshot.json` 還沒
寫出來——`/config` 這時回傳的是後端預設的 `{"personas": [], "users": []}`，
前端只 fetch 一次、沒有重試，導致 `PERSONAS`/`USERS` 整場會議都是空陣列，
泳道角色標籤、受訪者人物設定全部退回「系統」這種通用 fallback，使用者
完全看不出是誰在發言。真實計時：這次跑測 `meta.json` 與
`config_snapshot.json` 只差 2 秒，但沒有重試機制的話一次沒接住就是一整場
都是空的。**修法**：`loadRunConfig()` 改成最多重試 8 次、間隔 400ms，直到
拿到非空資料才寫入全域變數。修完後用同一個 run_id 重新載入即時畫面驗證，
角色標籤正確顯示「業務開發」「用戶研究」等，不再是「系統」。

### 8. 瀏覽器分頁快取舊版 `index.html`，改完前端程式碼卻沒生效

修完坑 7 之後直接用 `navigate` 重新整理頁面驗證，結果讀出來的
`loadRunConfig.toString()` 還是修前的舊版函式內容——瀏覽器把 `/`
這條路由的回應快取住了，一般的整頁導覽沒有強制略過快取。加
`?_cb=1` 這種 cache-busting query string 才真正拿到新版程式碼。這不是
程式邏輯的 bug，但提醒了一件事：往後每次改完 `static/index.html` 要在
瀏覽器裡驗證，都要用 cache-busting 網址或強制重新整理，不能只靠一般
`navigate`。

### 9. 最嚴重的一次：write_wisdom 的新 RAG 寫入邏輯撞 DuplicateIDError，且 resume 沒有把錯誤回報給前端

真實跑測到第 6 輪（facilitator 硬上限）結束討論、進入
`run_peer_review`／`run_masters`／`write_wisdom` 時整場崩潰：

```
chromadb.errors.DuplicateIDError: Expected IDs to be unique, found
duplicates of: ...-review-alex-mei, ...-review-joyce-mei, ...-review-victor-mei
```

根因：這次為了項目 10（擴大 RAG 寫入範圍）新寫的
`write_collective_wisdom()` 迴圈假設「每個 (reviewer, presenter) 配對只會
出現一次」，用 `f"{round_id}-review-{reviewer_id}-{presenter_pid}"` 當
deterministic id。但 facilitator 真的會讓同一個人「加輪」回應累積的異議
（這次真實跑測林美華被排了兩輪：第 1 輪首次發表、第 5 輪回應異議）——
每次加輪都會重新跑一次 `run_peer_review`，同樣 3 位審閱者會再審一次她的
（可能已修正的）提案，產生內容不同、但 (reviewer, presenter) 這組 key
相同的第二筆 review，直接撞 id。**修法**：id 加上 `idea_pool_versions`
的順位索引（`-v{version_idx}`），讓每一輪的獨立內容都保留下來，不是
覆蓋掉後面幾輪的回饋——這些「同一個人被重複審閱、內容真的不同」的資料
本身是有價值的，不該被 id 衝突擋掉。

**這次崩潰還暴露了第二個、更嚴重的獨立 bug**：`cmd_resume()`
一開始那次手動 `graph.invoke(Command(resume=user_input), config)`
沒有包在 try/except 裡——它不是只「跑到下一個 interrupt」這麼單純，
可能一路跑穿 peer review／masters／write_wisdom 一路到底。這次跑測，
這個 invoke 撞到上面的 DuplicateIDError 直接讓 subprocess 帶著**完全沒
更新的 `state.json`** 崩潰退出：前端 `/status` 一直讀到的還是上一次
pause 的舊 payload（林美華、reason 一字不改），UI 看起來像「還在等
facilitator 決策」，完全沒有任何錯誤提示。使用者這時如果照常點「跳過」
或「提問」，只會默默開一個新 subprocess、再撞一次同一個 bug、白燒一次
真實 API 成本——這次就是這樣連續踩了兩次才發現不對勁，靠直接讀
`worker.log` 才抓到 traceback（API 回應本身完全看不出任何異常）。
**修法**：把這次 invoke 包進 try/except，跟 `_run_main_and_capture()`
用同一招接住例外、寫入 `state.json` 的 `status: error`，前端至少能看到
真正的錯誤狀態，不會誤以為還在正常等待。

兩個修法都用 `python3 -m py_compile` 過語法檢查，並直接用
`run_worker.py resume`（帶 `--example-config`）在 CLI 重跑同一個卡住的
`run_id`，確認整場會議真的能跑完：`draft_proposal`→`refine`→
prototype test→三鏡檢核→最終報告→`save_outputs` 全部正確產生，
`write_collective_wisdom()` 這次成功寫入（`wisdom` collection 38 筆，
含新增的 `pov_hmw`／`peer_review`（15 筆，含加輪的重複審閱）／
`three_lens_check`；`interviews` collection 44 筆，含新增的
`interview_transcript`），沒有再撞 id 衝突。

### 其餘項目的真實驗證重點

- **項目 1（研究足跡）**：即時畫面點開 `collect` 事件，正確顯示搜尋
  查詢與可點擊的真實網頁連結（title/url/snippet）。
- **項目 5（refine 具體改了什麼）**：點開 `refine` 事件，自評分數
  變化（`7 → 7.5`）與 `_diff_proposals()` 產生的 unified diff 文字並陳
  顯示，不再只有數字。
- **項目 9（HITL 顯示名稱）**：真人在即時畫面填「Stephen」發問，
  `events.jsonl` 的 `human_qa` 事件正確記下 `"asked_by": "Stephen"`。
- **項目 11（AI 對照評語）**：`generate_final_verdict()` 真實產生一段
  針對這場資料的具體評語（不是空泛套話），回放頁對比表下方正確顯示。
- **項目 2/7（即時成本/耗時）**：即時畫面的成本卡片從 `$0.0000` 隨事件
  即時累加到跟 `save_outputs` 印出的總成本一致；回放器的成本進度條
  `goTo()` 每次點擊/播放都正確更新（查證後確認這條路徑原本就沒有 bug，
  真正缺的是即時畫面完全沒有成本顯示，不是回放器邏輯錯）。
- **項目 4（baseline 連結）**：這次真實跑測 baseline 的 `sources`
  欄位剛好是空陣列（LLM 這次沒有編造任何來源），沒能在真實資料上示範
  「不可點的假連結＋提示文字」這個畫面，但程式碼路徑
  （`shared_renderers.js` 的 `renderSources(..., {unverified: true})`）
  在較早的離線測試資料上已驗證過會正確跳過 `<a href>`。
- **項目 6（模擬用戶編輯器）**：即時畫面設定頁正確讀出真實
  `users.yaml` 的三位模擬用戶（陳小姐／王先生／小宇）完整欄位。
