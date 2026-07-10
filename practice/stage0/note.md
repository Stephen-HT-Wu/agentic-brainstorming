# Stage 0 Note — 骨架 + checkpointer 基礎

## 目標（對照 `PLAN.md`）

- **不呼叫 LLM**，確認 LangGraph 能跑（langgraph 0.6.11，Python 3.9）
- **看懂 checkpointer 的 thread / checkpoint 概念**——這是之後 stage 5 HITL
  （interrupt/resume）與跨 session 續跑的地基

對應程式：`practice/stage0/graph.py`

跟 agentic-articles 的 stage0 刻意不同：那邊看的是「state 怎麼在節點間傳遞」，
這邊直接從「state 怎麼被**存下來、跨 invoke 延續**」開始，因為本專案的核心
（HITL、跨 session 會議）都建立在 checkpointer 上。

## 你會學到什麼

1. **checkpointer = 自動存檔**：`compile(checkpointer=MemorySaver())` 之後，
   圖每執行一個 super-step 就把當下完整 state 存成一個 checkpoint
2. **thread_id = 哪一場會議**：呼叫時 `config={"configurable": {"thread_id": "..."}}`
   決定讀寫哪份記錄。同 thread 連續 invoke，新輸入會跟 checkpoint 裡的舊 state 合併；
   不同 thread 完全隔離
3. **reducer 決定合併方式**：`ideas: Annotated[List[str], operator.add]` 讓節點的
   回傳值「累加」而非「覆蓋」——同一機制之後也負責 stage 2 平行 fan-out 的結果合併
4. **`get_state()` / `get_state_history()`**：不重跑就能讀目前 state、回看每一步的
   歷史 checkpoint；`snapshot.next` 為空 tuple 代表圖跑到 END（之後 interrupt 停在
   半路時，`next` 會指著還沒跑的節點——stage 5 就靠這個判斷「會議停在誰發言後」）

## 怎麼跑

在 `practice/` 目錄：

```bash
.venv/bin/python stage0/graph.py
```

## 實際觀察到的行為（2026-07-10）

- meeting-A 連續 invoke 三次，點子編號**接續**：#1 → #1,#2 → #1,#2,#3
  （節點是用 `len(state["ideas"])+1` 算編號的，編號會接續＝舊 state 真的被讀回來）
- meeting-B 另開 thread，從 #1 重新開始，跟 meeting-A 互不干擾
- `get_state_history(meeting_a)` 共 9 個 checkpoint：3 次 invoke × 每次約 3 個
  （輸入合併一步 + 節點執行一步…），連 `step=-1`（最初的空輸入）都留著——
  **每一步都可回溯**，這就是之後「從任意斷點 resume」的原理
- `MemorySaver` 存在記憶體，process 結束就消失；stage 5 會換成 `SqliteSaver`
  才能做到「隔天重開 process 繼續同一場會議」

## 驗收（對照 PLAN.md 階段 0）

- [x] 跑通：`graph.invoke()` 正常執行，無錯誤
- [x] 同 thread 連續 invoke，state 延續（編號 #1→#2→#3 接續）
- [x] 看懂 thread（會議場次）/ checkpoint（每一步的存檔）兩個概念
