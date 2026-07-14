# Stage 12 Note — 簡化版腦力激盪（5 分鐘 demo）

## 目標（對照使用者的 16 點需求）

1. 五力＋趨勢分析先選一個策略目標＋target audience，依此動態生成 3 位訪談
   對象，系統（不是每位 persona）做訪談＋做功課，整合成洞見（BMC 原本設計
   成系統產生一份共用範本，後來因為點子多樣性偏低而改回每位 persona 自己
   設計自己的 BMC，見下方「第二輪修正」）
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
  `Send()` 平行受訪，訪談完一次 LLM 呼叫萃取洞見）。BMC 原本設計成這裡
  順便畫一份全場共用的範本，後來因為點子多樣性偏低而改回每位 persona
  自己設計自己的 BMC（見「第二輪修正」），`system_research()` 現在只做
  訪談＋萃取洞見這一件事。
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
（`PlainTextResponse`）。即時畫面偵測到 `status=="done"` 就直接嵌入報告
內容，歷史紀錄清單每個已完成的 run 也連到同一個端點。

第一輪刻意沒做 stage10 風格的完整事件回放頁（理由：`compute_comparison()`
是照 stage9 的資料形狀寫的，硬套會全部對不上），後來使用者要求補回來，
見下方「第二輪修正」——`stage12/build_replay.py` 是全新寫的一份，不是
硬套 stage10 那份。

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

## 第二輪修正：點子多樣性太低

真實驗證跑完後，使用者盯著報告裡的 `idea 多樣性（發想階段彼此的兩兩平均
距離）：0.4263` 這個數字問「這樣算高還是低」——校準後發現這個數字其實
**比「同一個 idea 換句話說」的基準值（0.579）還低**（詳見下方「已知限制」
的校準結果），這才是真正戳破問題的起點：三個 idea 長得太像了。使用者
接著提出三個具體修正：

### 1. 拿掉全場共用 BMC，改回每位 persona 自己設計

**根因假設**：全場共用一份 BMC 等於先把商業模式框死了，三位 persona
只能在同一個框架裡想變化（同樣的客群、同樣的收費模式），差異自然只能
發生在「怎麼包裝」而不是「怎麼做生意」，這正是點子多樣性偏低的結構性
原因之一。

**修法**：`system_research()` 現在只做訪談＋萃取洞見，不再產生任何
`bmc`——`MeetingState`/`IdeaTask` 都拿掉 `shared_bmc` 欄位。BMC 改成
`draft_one_idea()` 跟 idea 本身**同一次 LLM 呼叫**一起生成（`_IDEA_SCHEMA_HINT`
多了 `bmc` 欄位的規格），不多花一次呼叫成本；用既有的 `_merge_bmc()`
補上結構合法的預設值，任何一位 persona 輸出格式不完整都不會讓整個
fan-out 崩潰（這是平行呼叫场景下刻意選擇的容錯策略，不是漏了驗證——
`assert_bmc_complete()` 存在但這裡故意不對它的結果 `raise`）。`pick_winner`/
`generate_prototype` 都改讀 `winner_idea.get('bmc')`，不再有任何地方讀
`state['shared_bmc']`。

**真實驗證結果（誠實記錄，不是預期中的簡單勝利）**：兩場真實會議
（CLI「如何提升線上課程完課率」、瀏覽器「如何提升線上讀書會出席率」）
跑完後，`avg_distance` 分別是 **0.2537** 跟 **0.2382**——**比修正前的
0.4263/0.563 更低**，也比「單純換句話說」的基準值 0.579 低更多。人工
檢查三份 idea 的 BMC 內容，確認每位 persona 真的設計出結構不同的商業
模式（不同的收益流估算、不同的顧客關係機制、不同的通路組合，例如某位
persona 加了「IG 限動打卡分享」通路，另一位完全沒有）——**BMC 本身的
多樣性確實達成了**。但 `avg_distance` 量的是 title+summary+bmc 全部
文字的表面 n-gram 相似度，兩場真實會議裡三位 persona 的 idea **標題／
摘要用詞高度收斂**（例如「8分鐘定長單元」「里程碑」「通勤」這幾個詞在
三個 idea 標題裡重複出現）——這很可能是因為三位 persona 讀的是同一批
訪談洞見與同一個策略目標措辭，即使各自獨立發想，也容易收斂到同樣的
表層框架。**結論：這輪修正確實讓 BMC 層級的多樣性變好，但沒有讓（也
不能簡單假設會讓）表層文字相似度指標變好——兩者是不同的多樣性維度，
不能只看一個數字判斷這個修正有沒有效**。如果之後還要繼續處理表層用詞
收斂的問題，下一個該檢視的地方是 `generate_personas()`（人設是否給了
足夠不同的關注面向）或 `draft_one_idea()` 的 prompt（是否鼓勵更激進的
措辭差異化），但這是使用者尚未要求的下一步，這裡先如實記錄現象。

### 2. 訪談改用 5-Whys，問滿 5 輪（原本 2 輪）

**修法**：`INTERVIEW_ROUNDS` 從 2 改成 5；`generate_followup_question()`
的 prompt 改成明確要求「用 5-Whys 根本原因分析技巧，一層一層往下挖」，
並把目前是第幾層（`round_i`/`INTERVIEW_ROUNDS`）餵給 LLM，避免每輪都在
同一層換句話問。

**真實驗證**：兩場真實訪談的追問內容確實一路變深，不是在原地打轉，
例如其中一場的其中一位受訪者：

```
第1輪：關於「XX」，能不能先聊聊你平常的情境跟遇到的困擾？
第2輪：為什麼忘記劇情內容會讓你選擇放棄而不是回頭查看？
第3輪：為什麼重新進入代入感對你這麼耗費心力？
第4輪：為什麼在有限通勤時間裡，情緒醞釀對你特別重要？
第5輪：為什麼通勤時間對你來說必須是「享受」而非資訊吸收？
```

第 4-5 輪已經挖到接近價值觀/情緒需求層級的根本原因，不是重複問「還有
什麼困擾」這種表層問題——這是 5-Whys 技巧確實生效的證據。**代價誠實
記錄**：訪談從 2 輪（每人 ~3 次 LLM 呼叫）變成 5 輪（每人 ~9 次呼叫），
訪談環節的呼叫量接近 3 倍，這是使用者明確要求、用成本/時間換取洞見深度
的取捨，不是意外的性能回歸。

### 3. 補回完整事件回放頁

`stage12/build_replay.py` 是全新寫的一份（不 import stage10 那份，維持
「每個 stage 一份完整獨立副本」的既有慣例），比 stage10 那份簡單很多：

- **不需要 `_attach_details()`**：stage9 的舊事件只有摘要片段，回放時要
  另外從 run JSON「補接」完整資料回事件上；stage12 的事件（`draft_idea`/
  `dfv_score`/`baseline`…）在這一輪一開始做 `renderExtraGeneric()` 新
  case 時就已經把完整資料放進 `extra` 了，回放頁直接原樣渲染即可。
- **六個可量化差異全部換過**：真實搜尋依據、BMC 完整度（改讀贏家 idea
  自己設計的那一份，不是共用範本）、單位經濟、動態組隊涵蓋角度數、idea
  多樣性、DFV 結構化評分覆蓋，外加一列「最終評估者誠實對照評分」的
  stat card。
- `server.py` 新增 `GET /api/runs/{run_id}/replay`，`index.html` 的
  歷史紀錄清單／即時畫面報告卡都加了「完整回放 ↗」連結（跟「報告 ↗」
  並列，兩者互補不是二選一）。

**真實驗證**：瀏覽器開一場真實會議，跑完後開啟 `/replay`，確認：比較表
六個維度數字正確（`idea 多樣性 0.2382`、`agent 5.33 分 vs baseline 2.67
分`）、事件時間軸可以用 `goTo()` 跳到任一事件並在旁邊看到完整細節（含
`draft_idea` 事件裡該 persona 自己設計的 BMC 九宮格）、AI 對照評語正確
顯示在比較表下方。

## 已知限制（刻意不在這輪處理）

- **`idea_diversity.avg_distance`（收斂結果的「多樣性」數字）不是可靠的
  絕對指標，只能相對比較**：這個數字是 `pairwise_text_diversity()` 算出來
  三個 idea 兩兩之間 `1 - cosine 相似度` 的平均，底層 `embedding_distance()`
  用的不是真正的語意 embedding（沒有呼叫 LLM），而是手刻的字元 n-gram
  （bigram/trigram）雜湊做 bag-of-words cosine——對「表面詞彙重疊度」極
  敏感，不等於「策略上到底差多少」。真實跑測校準過（拿同一個
  `embedding_distance()` 函式測不同對照組）：同一段文字 vs 自己 ≈ 0.00、
  **同一個 idea 只是換句話說重寫一次 ≈ 0.579**、同主題但真的不同 idea
  ≈ 0.749、完全不相干的主題 ≈ 0.770。也就是說，真實一場會議量到的
  `avg_distance`（例如 0.4263、0.563）常常**比「單純換句話說」的基準值還
  低**——因為三個 idea 討論的是同一個主題（同一個 target audience、同一份
  共用 BMC、同一批訪談洞見），本來就會共用大量詞彙（健身、會員、續約、
  短影音…），這個方法量出來的數字會被詞彙重疊壓得偏低，不代表三位
  persona 真的沒有各自獨立想出不同角度——真正要判斷「是不是真的各自發散」
  還是要直接看三個 idea 的標題/摘要/理由本身，這個數字目前只適合拿來做
  「同一個 pipeline 跑不同場次時，這次比上次多樣還是少樣」的相對比較，
  不該當成絕對好壞的門檻。要修正的話需要換一個真正的語意 embedding
  方法（例如呼叫一次 embedding API），但這會增加成本跟延遲，跟第 5 點
  「降低成本」的目標有取捨，這輪先如實記錄這個限制，不動手改。
  （後續：這個發現直接促成了「第二輪修正」的三個改動——拿掉共用 BMC、
  訪談改 5-Whys——但兩場真實驗證顯示 `avg_distance` 改完後是 0.2537／
  0.2382，比這裡記錄的 0.4263/0.563 更低，見上方「第二輪修正」第 1 節
  的完整分析：BMC 層級的多樣性確實變好了，但這個表層文字指標量的是
  不同的東西，沒有跟著變好，兩者不能混為一談。）

- `server.py` 的 `RUNS_DIR`（`practice/outputs/runs/`）跟 stage11 共用
  同一個實體目錄——這是從 stage11 複製下來的既有慣例（每個新 stage 都
  沿用同一份 `outputs/runs/`），真實驗證時歷史紀錄清單裡混雜了 stage11
  留下的舊會議紀錄。點開一個 stage11 的舊紀錄會因為它的 `state.json`
  沒有 `report_path` 欄位而在 stage12 的「查看報告」連結上失敗
  （`404 找不到報告檔案`）。這不是 stage12 這輪新增的問題，但值得未來
  決定是否要讓每個 stage 的即時面板有自己獨立的 `outputs/runs/<stage>/`
  子目錄。

## 未來方向：更多場真實驗證＋業界標準流程對照（都尚未實作）

另外兩場真實會議（「食尚玩家 APP 訂閱功能」題目）測出 `avg_distance`
分別是 0.2821／再更早一場 0.2537、0.2382——三筆修正後的數據呈現緩慢
上升的趨勢，但都還在低於「單純換句話說」基準值（0.579）的範圍。深入
追查其中一場的三個 idea 內容後，定位出**比 BMC 共用更上游的收斂點**：
`analyze_and_scope()` 的 prompt 被設計成「選定一個現階段**最可行**的
策略目標」，不管輸入的 `topic` 多開放，這一步都會收斂成一個具體策略
目標＋一個 target audience，而且往往已經半成品化到連產品命名都定了
（例如某場所有 persona 都收斂到同一個命名「食尚PLUS」、同一個 LBS
地理圍欄機制）。後面訪談做得多深、BMC 各自設計得多獨立，所有 persona
都是在同一個已經選定的框架裡精修，多樣性的天花板在這一步就被封住了。

這連帶引出幾個**尚未實作**的架構構想（都只記錄分析，使用者明確要求
先不動手）：

1. **策略目標 fan-out**：`analyze_and_scope` 改成產出 N 個候選策略
   目標（各自帶自己的 target audience），對每個候選各自 fan-out 出
   一整條獨立的研究/團隊/評分分支，最後跨分支選總贏家。
2. **策略回饋迴路**：訪談完之後加一個有界限（只允許一次，不迭代）的
   修正檢查點，讓策略目標可以被真實訪談內容回頭修正，而不是訪談洞見
   只能單向流向下游。
3. **JTBD（Jobs-to-be-Done）優先**：比第 2 點更根本的重排序——把
   `analyze_and_scope` 拆成「只假設候選 job，不假設是誰、不假設解法」
   跟「訪談發現真正的 job」兩步，target audience 跟策略方向要等訪談
   驗證完某個候選 job 是真實的之後才定案，不是一開始就定案。跟第 2 點
   是同一個問題的不同解法（可能更簡潔），跟第 1 點互補（訪談挖出的
   多個候選 job 可以餵給 fan-out）。
4. **虛擬問卷／量化研究補充**：現在的訪談只有 3 人，質化樣本太窄；
   討論過用 LLM 模擬大量（幾百到上千）虛擬受訪者填問卷的可行性——
   純 API 成本其實不高（用 Haiku 估算，1,000 份約 $1.7、5,000 份約
   $8.5），真正的障礙是（a）幾千次平行呼叫會撞到速率限制，真跑起來要
   好幾分鐘到十幾分鐘，跟 5 分鐘 demo 衝突；（b）更根本的方法論問題：
   幾千個「虛擬受訪者」全部來自同一個 LLM 的內部分布，不是真實人類
   母體的獨立樣本，用這個包裝成「73% 受訪者說 X」這種量化數字，看起來
   比 n=3 質化訪談更有說服力，但其實更容易誤導人。
5. **baseline-after-strategy**：（見「架構決策」一節前面已提過的
   ablation 對照組構想）如果之後真的做了第 1 點的 fan-out，這個構想
   的「全場只有一個共用策略目標」假設會過時，需要重新設計成「對照
   哪一個策略分支」。

**業界標準流程對照**（用來確認這些構想不是憑空發明，而是業界已有名字
的標準做法）：

| 業界框架 | 核心主張 | 對應構想 |
|---|---|---|
| **Double Diamond**（Design Council） | Discover→Define（收斂：定義問題）→Develop→Deliver（收斂：解法定案），兩個菱形分開跑，問題發現階段完全不談解法 | 驗證構想 3（JTBD 優先）：現在把兩個菱形壓成一步，業界標準是分開 |
| **JTBD + ODI**（Outcome-Driven Innovation，Tony Ulwick） | 質化訪談萃取「期望結果」→**真人**大樣本量化問卷評重要性/滿意度→算 Opportunity Score 排序 | 對應構想 3＋4：量化驗證步驟依賴真人樣本才成立，這是構想 4（虛擬問卷）方法論上最大的隱憂 |
| **Lean Startup**（Eric Ries） | 先找風險最高的假設（leap-of-faith assumption），針對它設計最小可行測試，用真實行為數據取代內部辯論 | 目前 pipeline 是無差別訪談 3 人，沒有先辨識「最不確定的假設是什麼」這一步 |
| **Continuous Discovery Habits**（Teresa Torres） | 每週持續少量訪談＋維護「機會解方樹」（Opportunity Solution Tree）：一個目標分支出多個機會（job），每個機會分支出多個候選解法 | 這棵樹同時對應構想 3（機會＝job）跟構想 1（一個機會下多個候選解法分支），業界傾向兩者合併著做，不是分開實作 |

以上全部都還沒實作，只是在使用者要求下記錄設計思路與業界對照，供
之後決定優先順序時參考。
