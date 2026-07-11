# Stage 10 Note — Demo 層（回放器 + 對比表 + DEMO.md）

## 目標（對照 `PLAN.md`）

- 單檔 HTML 回放器：讀 `events.jsonl`，零依賴、雙擊即開，播放會議實況
- Baseline vs 完整會議流程的 side-by-side 對比報告
- `DEMO.md` 導覽腳本：15 分鐘 demo 流程

對應驗收：「不懂技術的人看 15 分鐘 demo 能說出 agent 與『直接問
ChatGPT』的至少 3 個本質差異；回放器零依賴、雙擊即開」

## 這不是新的 agentic 概念，是消費 stage9 產出的展示層

跟 stage1-9 不同，stage10 沒有新的 LangGraph 機制要學——它是建立在
stage9 完整管線之上的工具層，所以：

- **沒有 `stage10/graph.py`**：`run_sample_meeting.py` 直接 `import`
  stage9 的 `graph` 模組（`sys.path.insert` 指到 `../stage9`），monkeypatch
  掉 `load_personas`/`load_users`/`load_company`（強制只讀
  `*.example.*`，不動使用者真實設定檔）跟六個路徑常數（`OUTPUT_DIR`／
  `CHROMA_DIR` 等，全部指向獨立的 `demo_workspace/`，不會污染使用者
  真實在用的 `practice/outputs/`／`practice/chroma_db/`）
- **`build_replay.py`** 是純資料轉換工具：吃 `events.jsonl` + stage9 的
  run JSON，吐出一個自足的 `.html`

## 架構決策

1. **零依賴＝資料內嵌，不用 `fetch()`**：瀏覽器在 `file://` 協定下大多
   會擋掉本地檔案的 fetch，所以事件資料直接序列化進 `<script>` 標籤裡
   的 JS 變數——這是「雙擊即開」能成立的關鍵，不是隨便選的實作方式
2. **對比表在回放器最上面，不是另外的檔案**：PLAN.md 說「報告」跟
   「回放 HTML」是兩個東西，但實際做出來發現把六個對比維度直接放進
   同一個 HTML 檔案最上方，體驗更好——不熟悉技術的人打開就先看到
   結論，想深入細節的人才往下滑看事件回放
3. **兩輪demo樣本**：只跑一輪的話「跨場記憶引用」這個維度會是死氣沉沉
   的 0/0（Chroma 剛啟用，理所當然沒東西可查）——這在 demo 情境下很
   致命，六個對比維度裡有一個看起來「這功能沒用」。所以真的花錢跑了
   第二輪（換一個相關主題），讓 demo 樣本能展示真實非零的跨場記憶引用
   （recall 命中 12 次、8 筆真的被寫進提案的 `memory_refs`），這個決定
   直接對應 stage7/9 已經驗證過的「第一輪必然是 0、第二輪才有得查」
   模式，不是新發現，是把已知的敘事套進 demo 素材裡

## 踩到的坑：`events.jsonl` 的 cost_usd 在某些節點裡是累計值，不是單筆

做 `build_replay.py` 的成本進度條時，先把 `events.jsonl` 全部事件的
`cost_usd` 直接加總，結果跟 pipeline 自己印出的「總成本 USD」對不上
——原始加總 **$0.8741**，但 pipeline 印出的真實總成本是 **$0.6680**，
差了快 31%。

查證：`emit_event`（`_common.py`／stage1-9 一路沿用的實作）算 `cost_usd`
的方式是「這次節點呼叫（invocation）目前為止的累計花費」——對『一次
呼叫只 emit 一次事件』的節點完全正確，但 `conduct_interviews`（每輪
訪談都 emit 一次 `interview_turn`）跟 `generate_prototype_and_test`
（依序 emit `generate_prototype`／`test_prototype`×N／
`refine_after_test`）這種**一次呼叫裡多次 emit** 的節點，中間每筆事件
記的其實是累計值，不是那筆事件單獨的花費——實測 `interview_turn` 的
`cost_usd` 序列是 `[0.00093, 0.002693, 0.004962, ...]`，單調遞增，
直接加總當然爆量。

這個 bug 從 stage3 引入 `conduct_interviews` 的逐輪 emit 開始就存在，
一路沿用到 stage9，只是沒人真的把整份 `events.jsonl` 拿去加總過
——stage10 是第一個真的這樣做的地方，所以是這裡才踩到。

**修正範圍的決定**：沒有回頭改 stage3-9 的 `emit_event`（那些已經
tag 過的階段，且 pipeline 自己的 `total_cost_usd`——真正用來做預算
控管、寫進「驗收」欄位的數字——從頭到尾都是對的，用的是
`total_cost()` 直接加總 `usage_log`，不是靠事件記錄反推）。問題只在
「事後用 `events.jsonl` 反推總成本」這個用法上，而這個用法是 stage10
才第一次真的需要，所以修正放在 `build_replay.py`：`sum_display_cost()`
明確排除 `interview_turn`／`generate_prototype`／`test_prototype`
這三種『過程快照』action，只計入同一次呼叫最後的『總結』事件
（`conduct_interviews`／`refine_after_test`）。用真實資料驗證過：
排除後的加總 `$0.6680` 跟 pipeline 自己印出的數字**完全吻合**。
`main()` 也加了一個防呆：如果以後又冒出新的多次 emit 節點，加總會跟
run JSON 的 `total_cost_usd` 對不上，會印警告提醒該更新排除清單。

## 怎麼跑

```bash
cd practice/stage10
../.venv/bin/python -m unittest test_build_replay.py -v   # 13 個測試，零成本

../.venv/bin/python run_sample_meeting.py          # 第一輪（Chroma 從空的開始）
../.venv/bin/python run_sample_meeting_round2.py   # 第二輪（相關主題，展示真實 recall）
../.venv/bin/python build_replay.py demo_workspace/outputs/events.jsonl \
    demo_workspace/outputs/stage9-run-<第二輪 timestamp>.json \
    demo_workspace/outputs/reports/replay.html
```

## 實際觀察（2026-07-11，真實跑測，虛構範例設定）

**Round 1**「如何提升新聞短影音互動率」：$0.6680，recall 命中 0（預期，
Chroma 剛啟用）。

**Round 2**「公司要不要導入 AI 自動生成新聞內容」（同一個隔離 Chroma，
接續 round1 留下的集體智慧）：$0.6910，**recall 命中 12 次、8 筆真的
寫進提案的 `memory_refs`**——這輪被選為 `demo/sample-run/` 的主要展示
素材，因為它同時展示了完整流程又有真實非零的跨場記憶。

**最終對比表六個維度**（round2 資料，`demo/sample-run/run-data.json`）：

| 維度 | Baseline | Agent |
|---|---|---|
| 真實搜尋依據 | 1 筆（無法驗證是否真實） | 平均 2.75 筆／份，來自真實搜尋 |
| BMC 完整度 | 9/9 | 9/9（結構驗證強制） |
| 點子多樣性 | N/A（單一答案） | 兩兩平均距離 0.2649 |
| 被批判/測試後改良次數 | 0 | 7 次 |
| 跨場記憶引用 | 0 | 8 筆真實引用（命中 12 次） |
| 成本 | $0.0045 | $0.6910 |

## 驗收（對照 PLAN.md 階段 10）

- [x] 回放器零依賴、雙擊即開（事件資料內嵌，不用 fetch()；用 Node.js
      對嵌入的 JS 做過語法/執行驗證，因為瀏覽器預覽工具當時不穩定）
- [x] Baseline vs 會議流程對比報告（六個維度都是真實資料算出來的，含
      修正過的成本加總）
- [x] `DEMO.md` 15 分鐘導覽腳本（含開場一句話定調、四個步驟、收尾
      引導觀眾自己講出三個本質差異）
- [x] `demo/sample-run/` 只含虛構範例設定跑出的真實資料，外人不需要
      API key 就能看回放（`.gitignore` 已排除 `chroma_db/`／`outputs/`／
      `practice/stage10/demo_workspace/`，只放行這個目錄）
