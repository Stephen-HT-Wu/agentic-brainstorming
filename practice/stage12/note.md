# Stage 12 Note — 簡化版腦力激盪（5 分鐘 demo）

## 目標（對照使用者的 16 點需求）

1. 五力＋趨勢分析先選一個策略目標＋target audience，依此動態生成 3 位訪談
   對象，系統（不是每位 persona）做訪談＋做功課，整合成洞見＋一份共用 BMC
2. 依策略目標動態生成腦力激盪參與者（不是固定 personas.yaml）
3. 最終評分用另外 3 位同一 target audience、但不重複訪談對象的模擬評估者
4. 5 分鐘內跑完的 demo
5. 降低 API 成本但不犧牲準確度
6. 拿掉 facilitator，流程仍然順暢
7. 拿掉每位 persona 各自的 dedup/recall_memory/使用者訪談，改成系統做一次
8. 拿掉互評與自我修正
9. 三位大師的簡短點評改成 Desirability/Feasibility/Viability（DFV）三面向
   結構化評分
10. 收斂機制改成三面向分數加總，不是共創合併
11. Prototype 由另外 3 位同一 target audience 的評估者（不是訪談對象）評分
12. Baseline 用同一批評估者評分
13. 即時畫面看得到 baseline 的完整敘事＋BMC
14. 最終報告存成 `.md`，即時畫面與歷史紀錄都看得到
15. 每個拓樸方框右上角顯示這一步目前花費，用長條比例看出哪一步最貴
16. 複雜版（stage9）完全保留可切換，這是全新獨立的 stage12

## 這一步練到的新 agentic 概念

stage9 的敘事重點是「多輪迭代協作、互相修正」——要感受到差異需要看很多輪。
stage12 換一個更適合 5 分鐘 demo 的敘事：**結構化商業分析先行 → 動態組隊 →
平行發散 → 結構化評分收斂 → 誠實對照評分**。這逼出的新概念是「用結構化評分
框架（DFV）取代開放式大師點評」——同樣是評估，DFV 把「好不好」拆成三個
正交維度各自獨立評分，收斂機制從「LLM 選一個」變成「純 Python 加總，零
額外成本」，這是本輪最直接的「省成本不犧牲準確度」實作（第 5 點）。

## 架構決策

### 拿掉的機制與其精神

- 每位 persona 各自 `collect`/`dedup`/`recall_memory`/`design_interview_guide`/
  `conduct_interviews`：改成 `system_research()` 一次做完（3 位訪談對象
  `Send()` 平行受訪，訪談完一次 LLM 呼叫萃取洞見＋畫一份**全場共用**的
  BMC）。idea 本身不再帶自己的 `bmc` 欄位。
- `run_peer_review`/`give_feedback`/`revise_after_feedback`/`refine`/
  `co_create_turn`：整個刪除，每位 persona 獨立發想一個 idea 就結束，不會
  被自己或別人修改（`draft_one_idea`）。
- `facilitator_decide`：`ask_question` 改成靜態邊直接進來（不靠
  `Command.goto` 動態導向）。
- 舊的 `master_critique`/`MASTERS`：改成 `DFV_LENSES`（desirability/
  feasibility/viability 三個角度），`fan_out_dfv` 對每個 `(lens, idea)`
  組合各評一次，只評自己那個面向，保留原本的長篇文字批評風格（不只是
  數字）。
- `generate_prototype_and_test()` 的「測試再修正」迴圈：`generate_prototype()`
  只生成一次就直接送去評分，不修正。
- 沒有任何集體智慧庫寫入（`write_collective_wisdom`/Chroma 相關程式碼整個
  刪除）——demo 用的簡化版不需要跨場記憶。

### 真實踩到的坑：LangGraph 的多條靜態邊不保證等全部前驅完成才觸發

最初設計把 `system_research`／`generate_personas` 做成兩個平行的圖節點，
各自一條靜態邊指到同一個下游節點（`fan_out_ideas` 掛在那個 join 節點上）；
`run_baseline` 也設計成從 `START` 平行跑、跟主線一起在 `evaluate_with_agents`
這個節點 join。這個假設來自對 LangGraph Pregel superstep 模型的錯誤推廣：
以為「一個節點有多條靜態入邊 = 等全部前驅完成才觸發」。

第一次真實跑測（CLI，非 mock）就實測踩到：查看 `events.jsonl` 的真實
timestamp，`evaluate_with_agents`（原本設計為 `run_baseline_node` 與主線
兩條邊的 join 節點）在 `13:29:59` 就跑完了——而 `run_baseline_node` 是
`13:29:43` 完成的短分支，主線的 `draft_idea` 事件卻是 `13:31:24` 才開始，
比 `evaluate_with_agents` 執行還晚超過一分鐘。也就是說 LangGraph 一看到
**第一個**前驅（短分支）完成就觸發了下游節點，用的是還沒填值的預設 state
（`prototype`/`evaluators` 都是空字典），算出來的最終評分是可疑的雙 0
（`共創平均=0.0 baseline平均=0.0`）——這不是理論風險，是真的算出一組
沒有意義的假資料寫進了報告。

**修法**（兩種都已在真實碼裡驗證過安全）：

1. `system_research`／`generate_personas` 這兩個長度接近的分支：不再各自
   是圖節點＋靜態邊 join，改成 `research_and_team()` 一個節點內依序呼叫
   兩個純 Python 函式（犧牲一點點序列化的時間，換取正確性）。
2. `run_baseline` 跟主線這種長度懸殊的分支：整個退出圖拓樑，回到 stage9
   驗證過的模式——`main()` 裡用 `threading.Thread` 背景跑 `run_baseline()`，
   `run_meeting()` 跑完主線之後才 `.join(timeout=120)`，完全不靠 LangGraph
   的邊語意做同步。`evaluate_with_agents`/`generate_final_verdict`/
   `save_outputs`/`build_final_report_markdown` 全部改回主線跑完後在
   `main()` 裡依序直接呼叫，不是圖節點。

最終 `build_parent_graph()` 是一條純線性 DAG（`analyze_and_scope →
research_and_team → draft_one_idea → ask_question ⇄ answer_question →
dfv_scoring → pick_winner → generate_prototype → generate_evaluators →
END`），用 `g.get_graph().edges` 實測確認沒有任何多重入邊的節點。另外在
`main()` 的驗收清單裡加了一項 `scores_look_real`（`not (agent_avg_score ==
0.0 and baseline_avg_score == 0.0)`），專門攔這類「靜默算出假資料」的
回歸。

真正的 fan-out/fan-in（`interview_panel_graph`／`dfv_panel_graph`）沒有
這個問題，因為用的是完全不同、已證實安全的模式：單一節點呼叫子圖的
`.invoke()`，這是同步呼叫，保證整個子圖跑完才返回。

### HITL：唯一保留的互動點，靜態邊而非動態導向

`ask_question` 現在一次列出全部 N 個 idea，人類可以指定任何一個 idea 問
一題（`{"action": "ask", "target_idea_id": ..., "question": ...}`），
`answer_question` 依 `target_idea_id` 找到對應 idea 的 persona 回答，
純粹給 demo 現場的人問得出問題、看得到回答，不修改 idea 內容（呼應第 8
點刪掉「回饋會改提案」的精神）。可以連續問，`skip` 才真的往下走進 DFV
評分。`interrupt()`/`Command(resume=...)` 證實是完全通用的機制，不用
`facilitator` 也能正常運作，`run_meeting()` 的驅動迴圈完全不用改。

### 即時面板：`stage12/{server.py, run_worker.py, avatars.py, static/index.html}`

整份複製 stage11 再改（拓樸差異太大，不嘗試把 stage11 參數化成兩種
pipeline 共用）。`static/index.html` 的 `TOPO_NODES`/`NODE_TO_SUPERVISOR`
整個重寫成 stage12 的 9 個真實圖節點＋`evaluate_with_agents`（非圖節點，
`main()` 裡手動 `set_current_node()` 標記，比照 `baseline` 的既有做法；
`baseline` 本身刻意不給拓樑方框，跟 stage9 的既有慣例一致）。

**第 15 點（每步花費長條圖）** 的真實坑：`emit_event()` 的 `cost_usd` 是
「這次 instrument() invocation 累計到目前為止」的花費，不是這筆事件單獨
的花費——`research_and_team` 底下 `system_research()`／`generate_personas()`
共用同一個 invocation，先發的 `system_research` 事件只是過程中的快照，
真正的累計總額在後面的 `generate_personas` 事件裡。這跟 stage9/10/11
既有的 `COST_SNAPSHOT_ACTIONS` 排除清單同一套邏輯，但**不能直接沿用同一份
清單**：stage9 的清單把 `generate_prototype` 整個排除（因為 stage9 的
`generate_prototype_and_test` 是「生成→測試→修正」三步驟共用一個
invocation），但 stage12 的 `generate_prototype` 是單次生成、只有一筆
`emit_event`，沿用那份清單會把 stage12 唯一一筆原型花費也錯誤地歸零。
修法：`shared_renderers.js` 的 `sumDisplayCost(events, exclude)` 加一個
可選的排除清單參數（預設值＝原本的 `COST_SNAPSHOT_ACTIONS`，對 stage9/10/11
零行為變化），stage12 自己定義 `STAGE12_COST_SNAPSHOT_ACTIONS = new
Set(['interview_turn', 'system_research'])` 明確帶入。

**耗時統計的坑（跟第 15 點一起發現）**：`isHitlWaitGap()`（判斷「這段
是不是真人在想問題，不算進 agent 耗時」）直接複製了 stage11 的版本，判斷
邏輯是 `prev.role === 'facilitator' && prev.action === 'facilitator_decide'`
——但 stage12 完全沒有 facilitator，這個判斷式永遠是 `false`，導致每次
真人在 HITL 停頓思考的時間都被整段算進「累計耗時（不含等待人類）」。
真實瀏覽器驗證時抓到：同一場已跑完的會議，修好前這個數字顯示 441.8s，
修好後正確顯示 225.8s（差距正好是兩次人類停頓的時間）。修法：改成判斷
`prev.action === 'draft_idea' || prev.action === 'human_qa'` 且
`cur.action === 'human_qa' || cur.action === 'dfv_score'`——涵蓋 ask_question
前後所有真實會發生的事件轉換組合。

**第 14 點（最終報告）**：`stage12/server.py` 新增 `GET
/api/runs/{run_id}/report`，直接回傳報告 `.md` 的原始內容
（`PlainTextResponse`）。刻意**不做** stage10 風格的完整事件回放頁
——`build_replay.compute_comparison()` 是照 stage9 的資料形狀寫的，
stage12 的節點拓樸/state 形狀完全不同，硬套會全部對不上；「報告 .md ＋
已經串流過的即時畫面事件記錄」對這個簡化版來說就足夠，這是刻意控制實作
範圍的決定，不是遺漏。即時畫面偵測到 `status=="done"` 就直接嵌入報告
內容，歷史紀錄清單每個已完成的 run 也連到同一個端點。

`shared_renderers.js` 繼續沿用 stage10 那一份共用檔案（`renderProposal`/
`renderBmc`/`kv`/`block`/`ul`/`findUser`/`sumDisplayCost` 等底層渲染函式
對新事件一樣通用），只在 `renderExtraGeneric()` 新增 stage12 專屬的
`case`：`analyze_and_scope`/`system_research`/`generate_personas`/
`generate_evaluators`/`draft_idea`/`dfv_score`/`pick_winner`/
`generate_prototype`/`baseline`（新增，因為 stage12 的 baseline 事件現在
帶完整 `extra.proposal`，不再落入「沒有 extra 就顯示容錯文字」的舊分支，
需要一個新 case 把它渲染成完整提案＋BMC，滿足第 13 點）。`human_qa`/
`interview_turn`/`evaluate_final_outputs`/`user_evaluation_summary`/
`generate_final_verdict` 這幾個舊 case 的欄位形狀跟 stage12 完全吻合，
原封不動重用。

## 真實驗證

### CLI（`--stop-after-first-interrupt` 手動過 HITL 點）

第一次真實跑撞到上述 join-semantics bug（評分雙 0），修好拓樸後第二次
真實跑：

```
=== 驗收 ===
每位參與者都提了一個 idea（3/3）：是
DFV 評分筆數正確（9，應為 3x3）：是
Prototype 已寫出可開啟的 HTML：是
最終評估者跟訪談對象不重複（3 位）：是
最終評估者對兩個方案都留下合法評分：是
評分數字不是可疑的雙 0（agent=5.67, baseline=3.33）：是
最終報告完整：是
總耗時：159.3s（目標 <300s）
總成本 USD：0.3794
```

各節點耗時／成本明細（`print_run_summary()`）：`analyze_and_scope` 29.8s
$0.0349、`interview_one_person` ×9 30.8s $0.0199、`research_and_team` ×2
49.2s $0.0381、`draft_one_idea` ×3 32.9s $0.0475、`score_one_dimension` ×14
197.8s $0.1627（三面向 DFV 評分是目前最貴、也最花時間的環節）、
`generate_prototype` 17.3s $0.0158、`generate_evaluators` 13.1s $0.0125。

### 瀏覽器（真實會議：「如何提升健身房會員續約率」）

用真實瀏覽器（非 mock）開一場會議，逐項驗證：

- 拓樸方框：`analyze_and_scope`／`research_and_team` 幾乎同時亮起（平行
  訪談＋動態組隊），每個方框右上角即時顯示累計花費＋長條比例，
  `score_one_dimension`（DFV）花費最高（$0.1162）在長條圖上一眼可見。
- HITL：一次看到全部 3 個 idea 卡片，點選其中一個（`陳柏宇`）並提問
  「這個漏斗機制怎麼跟現有會員 CRM 系統串接？」——`answer_question` 事件
  正確記錄 `target_idea_id: "p2"`、`presenter_name: "陳柏宇"`，persona
  誠實回答「這部分我還沒有深入研究過…」而不是瞎掰，符合設計預期。可以
  連續提問（第二次停在同一個 idea 清單，`questions_asked_so_far` 正確
  累加），`skip` 後正確進入 DFV 評分。
- DFV 評分明細：點開單一 `dfv_score` 事件，正確顯示面向名稱、對象、
  0-10 分數＋一大段具體批評（不是空泛通則）。
- `baseline` 事件：改用新增的 `case 'baseline'` 後，即時畫面正確顯示完整
  敘事＋BMC（九宮格），不再是原始 JSON 傾印。
- 最終評估者（張力德/許雅婷/黃俊彥）跟訪談對象（陳映如/林承宇/王美惠）
  確認無重複個體。
- `status=="done"` 後，最終報告 `.md` 內容正確嵌入即時畫面，`GET
  /api/runs/{id}/report` 端點也能被歷史紀錄清單獨立連到。
- 修好 `isHitlWaitGap()` 後，同一場會議重新整理頁面重播事件，「累計耗時
  （不含等待人類）」從錯誤的 441.8s 修正為 225.8s。

## 已知限制（刻意不在這輪處理）

- `server.py` 的 `RUNS_DIR`（`practice/outputs/runs/`）跟 stage11 共用
  同一個實體目錄——這是從 stage11 複製下來的既有慣例（每個新 stage 都
  沿用同一份 `outputs/runs/`），真實驗證時歷史紀錄清單裡混雜了 stage11
  留下的舊會議紀錄。點開一個 stage11 的舊紀錄會因為它的 `state.json`
  沒有 `report_path` 欄位而在 stage12 的「查看報告」連結上失敗
  （`404 找不到報告檔案`）。這不是 stage12 這輪新增的問題，但值得未來
  決定是否要讓每個 stage 的即時面板有自己獨立的 `outputs/runs/<stage>/`
  子目錄。
