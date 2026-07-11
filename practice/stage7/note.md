# Stage 7 Note — 大師點評 + 集體智慧庫（真實向量庫 Chroma，跨輪 RAG 記憶）

## 目標（對照 `PLAN.md`）

- Facilitator 收斂會議後，三位大師（技術／商業／策略）對整場最終提案組合給高階點評
- 點評、最終提案摘要、訪談洞見寫入 **Chroma**（真實語意 embedding，不是
  stage1-6 用的 feature-hashing）
- 跑兩輪不同主題，第二輪能引用第一輪的相關結論與訪談反應
  （recall 命中數 > 0），引用可追溯到向量庫原始文件

對應程式：`practice/stage7/graph.py`（stage 6 的完整獨立副本再擴充，不 import stage6）

## 架構

做功課子圖、同儕互評子圖、Facilitator、HITL 逐行沿用 stage6。新增：

```
[Send fan-out ×N persona] homework_worker
  子圖內新增：collect→dedup→recall_memory（新）→synthesize→…→refine×3
  draft_proposal 新增 memory_refs 欄位（跟 insight_refs 同一種可驗證引用）
→ facilitator_decide（沿用 stage6，goto 目標從 END 改成 "run_masters"）
→ run_masters（新，Send fan-out 到 3 位大師，看最終提案組合）
→ write_wisdom（新，把大師點評＋最終提案摘要＋訪談洞見寫進 Chroma）
→ END
```

`recall_memory` 查 Chroma 兩個 collection（`wisdom`／`interviews`），排除
自己這一輪（用 `round_id` 過濾）寫入的資料，只留距離 ≤`RECALL_MAX_DISTANCE`
的結果。`synthesize` 的研究彙整跟 `draft_proposal` 的 `memory_refs` 都能
引用查到的記憶——後者是結構化欄位，跟 `insight_refs`／`addressed_reviewer_ids`
同一種「不能空口說引用」設計：`count_real_memory_refs` 驗證 id 真的存在
於這次 recall 到的結果裡。

## 你會學到什麼

1. **真實語意 embedding vs feature-hashing 的差距**：`get_chroma_collection`
   用 Chroma 內建的本地 embedding（all-MiniLM-L6-v2，透過 onnxruntime 跑，
   零 API 成本）。單元測試就看得出差距：查詢「用戶留存」相關的內容，
   即使沒有字面重疊的詞彙，也能正確排出相關性順序——這是 stage1-6 的
   hash-based `embed_text` 做不到的（那個只認字面 n-gram 重疊）
2. **collection 設計要考慮『別引用自己』**：`round_id` metadata 讓
   `recall_memory` 能排除本輪剛寫的資料，避免「自己講的話繞一圈回來
   變成跨輪智慧」這種假引用
3. **`Command(goto=...)` 可以動態指向不只一個候選節點名**：stage6 的
   `facilitator_decide` 只在 `ask_question`／`END` 之間選；stage7 把
   `END` 換成 `"run_masters"`，同一個節點函式的路由目標可以隨著整條
   管線增長而改變，不用重寫路由邏輯本身

## 踩到的坑（三個，全部是真實跑測抓到的，一個比一個更值得記錄）

### 坑一：Anthropic API 呼叫真的卡了 3 小時以上，完全沒有錯誤訊息

Round 2 第一次真實跑測，`give_feedback` 呼叫卡住不動——`ps` 顯示 CPU
時間幾乎沒有增加、process 停在那裡動也不動，直到手動 kill 之前已經跑了
**3 小時 19 分**。`lsof` 看到的 TCP 連線是 ESTABLISHED／CLOSE_WAIT，沒有
任何逾時或例外。Anthropic SDK 文件說預設 timeout 是 10 分鐘，理論上該
擋住，但這次沒生效（疑似 sandbox 網路環境的邊界情況，不確定是不是
SDK bug）。因為卡住的當下已經跑完前面所有做功課＋好幾輪同儕互評，
process 被強制 kill 之後**這些工作全部無法回收**（main() 只在
`run_meeting()` 正常回傳後才存檔），真金白銀浪費了約 **$0.74**。

修正：`anthropic.Anthropic()` 改成 `anthropic.Anthropic(timeout=90.0)`，
明確設定比 SDK 預設更短的逐次呼叫上限，讓卡住的呼叫最多 90 秒就丟例外，
交給 SDK 內建的 `max_retries=2` 重試，兩次都失敗才真的往上拋（走既有的
「崩潰」處理路徑，是已知行為，不是無限期掛著）。

### 坑二：`FACILITATOR_MODEL` 的 extended thinking 把整個 token 預算吃光

修正坑一後重新真實跑測，換一個地方崩潰：`facilitator_decide` 呼叫
`FACILITATOR_MODEL`（sonnet）時，回應**只有一個 `ThinkingBlock`，完全
沒有 `TextBlock`**——`max_tokens=300` 全部被模型的內部思考過程吃掉，
`call_llm` 內建的『截斷重試一次、tokens 翻倍』機制（300→600）也不夠，
兩次嘗試後依然沒有半個字的實際輸出，程式判定「沒有 text block」直接
拋例外。這次崩潰發生在**第 5 輪**（已經跑完 4 輪發表 + 1 輪加輪的所有
互評），前面的花費理論上不該浪費——見坑三。

修正：`facilitator_decide` 與 `master_critique`（同一個模型，同樣風險）
的 `max_tokens` 直接開大到 2000，不依賴 retry-doubling 機制兜底
——跟 agentic-articles 那邊 `chief_editor`/`authority` 節點踩過同一種
坑、用同一種解法（直接開大，不靠重試機制救援）。

### 坑三：崩潰後的續跑邏輯誤判成「在等人類輸入」

坑二修正後想直接續跑（同一個 `--thread`），卻踩到**第三個**真實 bug：
`run_meeting()` 原本邏輯是「只要 `snapshot.next` 非空就當作在等
`interrupt()`，去讀 `task.interrupts[0].value` 當 payload」——但
`facilitator_decide` 是**未捕捉例外崩潰**，不是呼叫 `interrupt()` 暫停，
`task.interrupts` 是空 tuple，程式把空 dict 硬塞進
`get_human_input(payload, script)`，直接 `KeyError: 'presenter_id'`。

用一個獨立的 toy graph 驗證正確行為（見下方「先驗證再花錢」）：確認
`task.interrupts` 是否為空正是分辨「崩潰待重跑」vs「真的在等人類輸入」
的正確依據，崩潰的情況要用 `graph.invoke(None, config)` 讓 LangGraph
自己接續，不能誤走 `Command(resume=...)` 那條路。修正後真的續跑一次：
**只重跑了 `facilitator_decide`（第 6 輪）**，前面做完的整個做功課階段
與 5 輪同儕互評完全沒有被重新執行或重新計費——這輪續跑只花了
**$0.0390**（`facilitator_decide` + 3 位大師 + baseline），證實
stage5/6 建立的 checkpoint 機制在這裡確實省下了原本可能損失的一大筆錢。

## 先驗證再花錢：兩個 toy graph 分別驗證兩個機制

跟前幾個階段一樣的紀律：真實 API 呼叫前先用零成本的最小 `StateGraph`
把不確定的機制釘死：

1. **Chroma 基本 add/query**：驗證真實語意 embedding 的排序行為正確
   （見上方「你會學到什麼」第 1 點），也順便發現本機第一次使用需要下載
   ~79MB 的 onnx 模型（下載完會快取，之後都是本地運算、零 API 成本）
2. **崩潰 vs interrupt() 的續跑分辨**：一個節點第一次呼叫必崩潰、第二次
   成功的 toy graph，驗證 `snapshot.tasks[0].interrupts` 為空 tuple、
   `task.error` 有值，且 `graph.invoke(None, config)` 只重跑崩潰的節點、
   不會重跑前面已完成的節點——這個結論直接對應坑三的修正

## 怎麼跑

```bash
cd practice
.venv/bin/python -m unittest stage7/test_graph.py -v   # 12 個測試，零成本
                                                          # （含真實本地 Chroma 語意檢索、
                                                          #   崩潰續跑 vs interrupt 續跑）

BRAINSTORM_TOPIC="主題A" .venv/bin/python stage7/graph.py --thread round1 --script /tmp/skip_all.json
BRAINSTORM_TOPIC="主題B（跟主題A相關）" .venv/bin/python stage7/graph.py --thread round2 --script /tmp/skip_all.json
# 兩輪共用同一個 practice/chroma_db/，第二輪才有東西可以 recall
```

## 實際觀察（2026-07-11，兩輪真實跑測）

**Round 1**「如何提升新聞短影音互動率」：4/4 發表、1 次加輪（陳建宏）、
三大師意見分歧（技術/策略大師都選陳建宏，商業大師點名王承翰單位經濟
最清楚）、寫入 Chroma `wisdom +7`／`interviews +20`。Recall 命中數 0
（Chroma 本來就是空的，符合預期）。成本 $0.5908。

**Round 2**「公司要不要導入 AI 自動生成新聞內容」（歷經坑一/二/三，
最終續跑成功）：4/4 發表、1 次加輪（陳建宏）。**Recall 命中總數 12**，
且每位 persona 的初稿都真的在 `memory_refs` 引用了 round 1 的具體文件
id，例如：

- 周若琪《編輯查核優先：AI 輔助選題，不自動生成新聞》引用
  `s7round1-master-strategy_master`、`s7round1-insight-mei-i3`
- 陳建宏、王承翰、林美華的初稿也都各自引用了 round1 的洞見或大師意見

三大師點評出現真實分歧：技術／商業大師都選陳建宏（架構扎實、單位經濟
清楚），**策略大師明確選了周若琪**，理由是「不導入 AI 生成、專攻查核
SOP 與信譽標籤最契合媒體長期信任資產」——這正好呼應 round1 策略大師
對「查核公信力定位」的點評，可以合理推測 round2 的周若琪透過 recall
真的讀到了這個角度並據此定調自己的提案方向（不自動生成新聞），是一個
「跨輪記憶真的影響了這一輪思考方向」的具體案例，不只是引用 id 而已。

真實總成本：round1 $0.5908；round2（含中間兩次真實失敗但已修正的嘗試，
用 `events.jsonl` 完整加總）$0.8897，其中最終續跑成功的部分只花了
$0.0390——大部分成本是坑一/坑二暴露前，已經跑完的做功課＋4輪互評；
另外坑一那次網路卡死的嘗試單獨浪費了約 $0.74（無法回收，events.jsonl
在下一次嘗試時被清空重新開始，這筆數字沒有算進上面的 round2 總額）。

## 驗收（對照 PLAN.md 階段 7）

- [x] 三大師點評整場最終提案（兩輪都是 3/3 大師給出具體、彼此不同的點評，
      不是空泛通則）
- [x] 點評／訪談洞見寫入 Chroma，帶 metadata（round_id/topic/doc_type，
      round1 寫入 `wisdom +7`／`interviews +20`）
- [x] 跑兩輪不同主題，第二輪能引用第一輪的相關結論與訪談反應
      （recall 命中數 12 > 0，且 `memory_refs` 逐筆對照真實存在於 Chroma
      的文件 id，`memory_refs 皆可追溯到真實 recall 結果` 驗收通過）
