# Stage 5 Note — HITL 提問（`interrupt()` + `SqliteSaver` 跨 process 續跑）

## 目標（對照 `PLAN.md`）

- 每位 persona 發表後暫停整個 graph：人類可**連續提問**（agent 回答）或**跳過**
- 提問後 agent 的回答要進入共享狀態，**被後續同儕互評引用**
- 支援**中斷後重開 process**（不是重開函式呼叫，是真的整個 Python process
  結束再重啟）從斷點續跑，而且不能重新花錢跑已經做完的步驟

對應程式：`practice/stage5/graph.py`（stage 4 的完整獨立副本再擴充，不 import stage4）

## 架構關鍵：發表迴圈從『節點內 Python for 迴圈』改成『圖層級迴圈』

stage4 的 `run_presentation_rounds` 是一個節點，內部用 Python `for` 迴圈
跑過每一位發表者。這個模式在 stage5 行不通——`interrupt()` 的語意是
「恢復時，卡住的那個節點函式會整個重跑一次」（先前呼叫過的 `interrupt()`
會立刻回傳快取值，但呼叫之間的一般程式碼，包括真正的 LLM 呼叫，會被
重新執行、重新花錢）。如果把 `interrupt()` 塞進 stage4 那種大迴圈節點，
每次恢復都會把前面所有發表者的整輪同儕互評重新呼叫一次 LLM。

正確做法：把發表者迴圈搬到**圖層級**（`route_presenter` 用
`add_conditional_edges` 決定要不要回到 `ask_question`，跟 stage1 `refine`
迴圈同一種模式），並且讓 `ask_question` 這個節點**只做一件事**——呼叫
`interrupt()`。真正花錢的 `answer_question`（persona 回答）拆成獨立的
下一個節點，不跟 `interrupt()` 共用節點函式，所以恢復時只有輕量的
`ask_question` 會重跑，已完成的節點（不管本輪還前面幾輪）都不會重新
執行——這是本階段跟 stage2-4 最大的架構差異。

```
[Send fan-out ×N persona] homework_worker
  → route_presenter（圖層級迴圈起點，同 stage1 refine 的模式）
      有下一位 → ask_question（唯一含 interrupt() 的節點）
          有問題 → answer_question（真的花錢，不含 interrupt）→ 回 ask_question（可連問）
          跳過   → run_peer_review（同 stage4 的巢狀 Send 子圖，多帶 qa_context）
              → route_presenter（下一位或結束）
      沒有下一位 → END
```

## 你會學到什麼

1. **`interrupt()` 只能是它所在節點的全部內容**：這是本階段最重要的
   教訓，來自對 LangGraph 恢復語意的推導，不是踩坑才學到——先用零成本
   的 toy graph（`_common.py` 風格的最小骨架）驗證了這個假設完全正確
   （見下方「先驗證再花錢」）
2. **`Command(resume=value)` 是恢復的唯一入口**：呼叫時用同一個
   `config`（同一個 `thread_id`），`interrupt(payload)` 的回傳值就是
   這次傳進去的 `value`
3. **`get_state(config).tasks[0].interrupts[0].value`**：不重新呼叫
   `invoke()` 就能讀出「圖現在卡在哪裡、payload 是什麼」——CLI 驅動
   迴圈完全靠這個 API 判斷要不要印訊息給人類、要不要繼續等輸入
4. **`SqliteSaver` 讓 checkpoint 真的落地成檔案**：跟 stage0 的
   `MemorySaver`（process 結束就消失）不同，這裡指向
   `outputs/stage5_checkpoints.sqlite`——全新 Python process、全新
   `sqlite3.connect()`、全新 `SqliteSaver` 物件，只要 `thread_id`
   相同，`get_state()` 就能讀到正確的斷點
5. **多輪提問 = 圖層級的小迴圈**：`ask_question ⇄ answer_question`
   跟外層「發表者迴圈」是同一種 `route_*` conditional-edge 模式，只是
   巢狀了一層——LangGraph 沒有替「連續提問」這種需求準備特殊 API，
   用一般的迴圈原語就能組出來

## 先驗證再花錢：用零成本 toy graph 鎖定 API 用法

寫真正的 pipeline 之前，先用一個三行邏輯的最小 `StateGraph`（`idx`
從 0 數到 3、每輪呼叫一次 `interrupt()`、`SqliteSaver` 落地存 `/tmp`）
驗證了三件事：

1. `snapshot.tasks[0].interrupts[0].value` 真的能拿到 `interrupt()` 傳入的 payload
2. 完全獨立的第二個 Python process（新的 `sqlite3.connect()`、新的
   `SqliteSaver`、新的 `graph = build(...)`）指向同一個 db 檔案 + `thread_id`，
   `get_state()` 正確顯示 `snapshot.next=('step',)`
3. 用 `Command(resume=...)` 連續恢復三次，`log` 累積正確（`idx=0` 只出現一次，
   沒有因為後續恢復被重複記錄）——證實只有卡住的節點會重跑，不是整張圖

這三個結論確認後才動手改真正的 pipeline，避免拿真錢去除錯 LangGraph API 的細節。

## 怎麼跑

```bash
cd practice
.venv/bin/python -m unittest stage5/test_graph.py -v   # 純邏輯測試，零成本
                                                          # （含 mock 過 LLM 呼叫的完整
                                                          #   interrupt/resume/跨 process 測試）

# 真實跑測：兩個完全獨立的 process，示範中斷後續跑
.venv/bin/python stage5/graph.py --thread demo1 --stop-after-first-interrupt
# ↑ 花真錢跑完做功課階段，印出第一個人類介入點的 payload 後主動結束 process

.venv/bin/python stage5/graph.py --thread demo1 --script hitl_script.example.json
# ↑ 全新 process，從 sqlite 斷點接續，用腳本化回答跑完剩下的流程
# 不給 --script 則走互動式 stdin（自己在終端機輸入問題或按 Enter 跳過）
```

`hitl_script.example.json` 是跟 `personas.example.yaml` 順序對齊的腳本：
第 1 位（林美華）問 1 題、第 2 位（陳建宏）跳過、第 3 位（周若琪）問
2 題、第 4 位（王承翰）跳過——刻意涵蓋「連續提問」「跳過」兩條路徑。

## 實際觀察（2026-07-11，真實兩個 process 的完整跑測）

**Process 1**（`--stop-after-first-interrupt`）：真的花錢跑完 4 位
persona 的做功課＋訪談＋提案（跟 stage3/4 一樣的流程），134.1 秒後
命中第一個人類介入點（林美華發表後），印出 payload，**process 主動結束**。

**Process 2**（全新 process，`--script hitl_script.example.json`）：

- 立刻偵測到 `thread hitl-demo-1` 有未完成的會議，直接進入續跑迴圈——
  **完全沒有重新呼叫 `collect`/`synthesize`/`conduct_interviews`/`refine`
  等任何做功課階段的節點**（process 2 自己的 `print_run_summary()`
  只列出 `ask_question`/`answer_question`/`give_feedback`/
  `revise_after_feedback`/`run_peer_review`/`baseline` 六行，完全沒有
  做功課階段的節點——這是「中斷後不用重花錢」這個驗收標準最直接的證據）
- 依腳本問了 3 個問題（林美華 1 題、周若琪 2 題），跳過陳建宏、王承翰——
  兩條路徑都真的跑到
- 170.2 秒後完整跑完，驗收通過

**橫跨兩個 process 重建的真實總成本**（`events.jsonl` 是同一份檔案，
兩個 process 都往裡面 append，用它加總才是這場會議的真實總花費——
process 2 自己印出的 `$0.1368` 只是它自己那一半，不是整場的數字）：

| 節點 | 成本 |
|---|---|
| conduct_interviews | $0.1691 |
| refine | $0.1285 |
| synthesize | $0.0935 |
| revise_after_feedback | $0.0770 |
| give_feedback | $0.0494 |
| draft_proposal | $0.0481 |
| write_pov_hmw | $0.0180 |
| extract_insights | $0.0116 |
| design_interview_guide | $0.0060 |
| answer_question | $0.0055 |
| baseline | $0.0049 |
| **合計** | **$0.6115** |

跟 stage4 的 $0.53（沒有 HITL）相比，新增的 `answer_question` 只花了
$0.0055（3 次回答）——HITL 本身幾乎不增加成本，真正的成本都在做功課／
互評階段，而這些階段**完全沒有因為中斷續跑而重複收費**。

**一個真實的意外收穫**：問周若琪「模擬使用者真的這樣說過嗎？可以引用
一下嗎？」，她誠實回答「這是我基於用戶研究洞察合成的代表性角色，而
不是直接引用某位真實使用者的原話⋯這是我的不足之處」——這正好驗證了
PLAN.md 裡「模擬訪談的定位要誠實：agent 扮演的用戶不是真實用戶」這條
設計判斷，persona 在被問到時會誠實承認資料的性質，不是被追問就硬拗。

**提案多樣性**：互評前 0.3639 → 互評後 0.3105（跟 stage4 同樣的收斂
模式：同儕互評磨掉明顯弱點，但 BMC 核心定位仍各自不同）。

## 驗收（對照 PLAN.md 階段 5）

- [x] 每人發表後暫停，人類可連續提問或跳過（`ask_question ⇄ answer_question`
      迴圈；本次跑測周若琪連問 2 題、陳建宏/王承翰各跳過 1 次）
- [x] 提問後 agent 的回答進入共享狀態、被後續互評引用（`qa_context` 傳進
      `give_feedback`，reviewer 的 prompt 明確包含人類問答段落）
- [x] 支援中斷後重開 process 續跑（兩個完全獨立的 Python process、獨立
      `sqlite3.connect()`，真實驗證；做功課階段的 12 個節點在 process 2
      完全沒有被重新執行/計費）
- [x] 跳過不影響流程（陳建宏、王承翰皆被跳過，流程正常推進到下一位）
