# Stage 9 Note — 三鏡檢核 + 最終報告（全員共同動作、完整會議報告）

## 目標（對照 `PLAN.md`）

- top-K 最終提案交給**全體 persona**做三鏡檢核（正面／負面／洞見）——
  這是全員共同動作，不是同儕互評那種「別人審我」
- 把整場會議的完整歷程組成一份可讀的 Markdown 最終報告

對應程式：`practice/stage9/graph.py`（stage 8 的完整獨立副本再擴充，不 import stage8）

## 架構

做功課子圖、同儕互評子圖、Facilitator、HITL、大師點評、Chroma 集體智慧、
集體評分、Prototype/Test 逐行沿用 stage8。新增的部分：

```
run_prototype_test
  → run_three_lens_check（新，three_lens_panel_graph：N×K Send fan-out，含自評）
  → END
```

`main()` 裡新增 `build_final_report_markdown`——刻意**不是**圖節點，因為報告
需要 baseline 對照資料，那一直是在 `graph.invoke()` 完成之後才在 `main()`
裡跑的（沿用既有時序，不把 baseline 硬塞進圖裡）。

## 你會學到什麼

1. **同一套 3/3/3 協議，套用在不同時機／對象上意義完全不同**：
   `fan_out_three_lens` 刻意**包含自評**（`persona.id == target_persona_id`
   的組合也在 Send 清單裡）——跟 stage4 的同儕互評（`fan_out_reviewers`
   明確排除自己）是對照組。stage4 問的是「別人怎麼看我的提案」，
   stage9 問的是「所有人（含提案者自己）怎麼看這個已經定案的候選」，
   同一個 `_pad_to_three` 保底函式可以直接重用，不用重寫結構驗證邏輯
2. **報告完整性要驗證『內容』，不是只驗證『檔案存在』**：`report_complete`
   除了檢查檔案存在與大小，還逐一確認驗收要求的具體段落標題
   （「人類提問記錄」「第一輪訪談（Empathize」「第二輪訪談（Test」
   「三鏡檢核」）真的出現在文字裡——這是從 stage8「原型可直接開啟」
   （檢查檔案真的存在＋非空）延伸出的同一種紀律，只是這次連內容結構
   都要對得上
3. **checkpoint 保護的不只是程式碼崩潰，連 process 被外部殺掉也算**：
   這次真實踩到的情境比 stage7 的「未捕捉例外崩潰」更接近現實——
   background shell process 因為 session 本身重啟而被砍掉，完全不是
   程式邏輯的問題。用同一個 `--thread` 重跑，`run_meeting()` 一樣正確
   偵測到「上次是節點執行中崩潰（`task.error=None`，因為根本沒有
   Python 例外，是 process 直接被 SIGTERM/SIGKILL）」，用
   `invoke(None, config)` 接續，完全沒有重新執行已完成的做功課階段
   跟前三輪發表——這證明 stage5/6/7/8 建立的 checkpoint 機制擋住的
   不只是「程式寫錯」，是更廣義的「任何原因造成的中途中斷」

## 踩到的坑：不是程式邏輯，是 session 本身被中斷

真實跑測進行到一半（陳建宏、周若琪已發表，林美華剛拿到人類提問並發表），
背景執行的 process 被殺掉——不是程式崩潰，是驅動這次對話的 agent
session 本身重啟，連帶砍掉了背景 shell。事後查證：
`task.error` 是 `None`（不是任何 Python 例外訊息），純粹是外部訊號
把 process 結束掉。

這正好是 stage9 note 想強調的重點：`run_meeting()` 判斷「要不要當作
真正的 interrupt() 續跑」只看 `task.interrupts` 是否為空，不管
「中斷的原因」是例外、還是外部訊號——只要不是真正呼叫過
`interrupt()`，就一律用 `invoke(None, config)` 接續。這次沒有寫任何
新代碼、也不需要新的回歸測試，直接用同一個 `--thread` 重跑就正確
接上了，完整驗證了這個防禦機制的泛用性。

**成本對比**：完整重建 `events.jsonl`（含被中斷的第一次嘗試＋成功接續
的第二次嘗試）總成本 $1.3029；但**接續那一次單獨只花了 $0.2622**——
省下的是已經完成的做功課階段（4 人 × 完整訪談/refine）跟前 3-4 輪
發表互評，如果從零開始重跑，這些早就已經花過的錢會全部重付一次。

## 怎麼跑

```bash
cd practice
.venv/bin/python -m unittest stage9/test_graph.py -v   # 9 個測試，零成本

BRAINSTORM_TOPIC="你的主題" .venv/bin/python stage9/graph.py --thread demo --script /tmp/script.json
# 中途若因任何原因中斷（程式崩潰、process 被砍、電腦重開機），
# 用同一個 --thread 重跑同一行指令即可從斷點接續，不會重複收費。
# 最終報告在 outputs/reports/{thread}-final-report.md
```

## 實際觀察（2026-07-11，真實跑測，主題「如何提升新聞短影音互動率」）

**集體評分聚合**：陳建宏（alex）以 mean=6.667 排名第一，但也是唯一跟
周若琪（joyce）一樣有真實分歧度（stdev=0.471）的兩份提案之一——有趣的
是，三位大師（技術／商業／策略）**一致選陳建宏**當首選，跟集體評分的
結果吻合，但集體評分同時誠實地留下「不是全票通過」的分歧度數字，
比大師的單一定性判斷更細緻。

**三鏡檢核**：4 人 × 3 個 top-K 提案 = 12 筆檢核，每筆都是恰好
3 正面／3 負面／3 洞見，結構不變量 100% 通過。

**最終報告**：`outputs/reports/s9round1-final-report.md`（258 行），
完整含：Facilitator 6 輪決策軌跡、真實的人類問答（林美華對「差異化」
提問的完整三點回答）、三位大師點評、集體評分表、3 個 top-K 提案各自的
POV/HMW/BMC/原型路徑/測試後修正說明/**兩輪訪談逐字稿**（Empathize 需求
探索 + Test 概念驗證反應）/三鏡檢核明細、baseline 對照。

**測試反應持續產出真實的產品轉向**（延續 stage8 觀察到的模式）：
陳建宏的提案因為模擬用戶反應「有距離感」「太複雜」，最終修正不是加
更多功能，而是**主動刪減**——「開發時間減至4-5週，刪除複雜的廣告主
eCPM試驗」，這是真實測試反饋驅動範疇收斂的具體案例。

## 驗收（對照 PLAN.md 階段 9）

- [x] 三鏡檢核格式不變量通過（12/12 筆，每筆恰好 3/3/3）
- [x] 報告完整含人類問答與兩輪訪談記錄（`report_complete` 逐段驗證通過，
      不是只檢查檔案存在；肉眼確認過 258 行報告的三個 top-K 章節都有
      完整的兩輪訪談逐字稿）

## 後續重構（2026-07-12）：從「競爭選拔」改成「共創收斂」

使用者用過即時面板後提出根本性的流程修改：「與會者是各個領域的人才，
能提供不同的角度。我們不是在比賽，而是要他們在聽取彼此意見後，共創出
一個好的產品的 prototype 然後給使用者檢驗。」原本「全員互評打分→
選 Top-K→各自做原型」的競爭選拔框架整個換掉：

- **移除**：`run_collective_scoring`／`score_proposal`／
  `compute_score_aggregates()`／`select_top_k()`／`TOP_K`——整個
  「交叉評分選贏家」機制刪除，不留死碼
- **新增 `co_create_turn` 自我迴圈節點**（父圖層級的條件邊迴圈，這個
  codebase 首個單節點自環，真實跑測驗證逐輪 checkpoint／emit_event
  正常）：種子草稿取自 facilitator_log 最後一筆 present（討論實際
  收斂到的地方，不是固定 personas[0] 的設定檔順序偏見），其餘 3 位
  依序在同一份共享草稿上各自編輯一輪，每輪都把大師點評餵進 context
  （不然 run_masters 的輸出沒人讀）
- **`built_on_persona_ids` 經過驗證，不是距離代理**：每輪編輯聲稱
  整合了誰的觀點，比對真實 persona id 集合過濾幻覺——刻意不用
  embedding_distance 門檻判斷「有沒有整合」（距離大只代表改很多，
  最後一輪整份覆寫成自己的版本反而距離最大、卻是整合失敗）
- **insight_refs 命名空間問題**：每人的洞見 id 都從 "i1" 編號，多人
  輪流編輯同一份草稿會撞名——這輪編輯者只能引用自己真實存在的 id，
  驗證後加 persona id 前綴（"mei:i1"）存進共創草稿
- **下游子圖零修改**：`prototype_test_graph`／`three_lens_panel_graph`
  對「幾個提案」沒有結構性假設（fan-out 都是普通 comprehension），
  只改上層節點餵給它們單一共創提案＋合成的「共創提案小組」persona
- **對比表指標更換**：「點子多樣性」（多提案彼此差異，選拔框架下才有
  意義）→「整合的跨領域觀點數」（種子 persona + built_on 聯集，量
  「有多少不同人真的被整合」）；收斂前的 `diversity_before/after`
  維持不變——仍是「起點真的夠不同」的合理佐證

**真實跑測驗證**（2026-07-12，--example-config，總成本 $0.02 等級）：
共創迴圈 3/3 輪逐輪落地成獨立事件，`built_on_persona_ids` 全部非空且
指向真實成員（第1輪陳阿力整合 joyce+victor、第2輪周若琪整合 victor、
第3輪王承翰整合 joyce）；三輪的貢獻說明都能對應到大師點評的具體批評
（例如「回應商業大師對缺乏付費誘因批評」），證明大師意見真的被讀進
編輯 context；單一原型（共創提案小組）＋4×1 三鏡檢核＋write_wisdom
34 筆（含新 doc_type：co_created_proposal ×1、co_creation_turn ×3）
全部正確；main() 新驗收區塊全數通過。

## 第三次迭代（2026-07-13）：取消輪數上限／修正對比表沒更新的 bug／模擬使用者對照評分

使用者一次提了三件事：

1. **取消 `facilitator_decide` 的六輪硬上限**——只留預算上限
   （`MAX_BUDGET_USD`）當唯一的強制收斂條件，讓討論能自然跑到
   facilitator 自己判斷「已經充分」為止，不會湊巧到第 6 輪就被砍掉。
   `MAX_ROUNDS` 常數跟所有引用處（facilitator prompt、驗收區塊的
   `within_round_cap`）整個移除，不留死碼。真實跑測驗證：round 6 的
   「end」決策理由是純粹的品質判斷（「四人皆已發言…邊際效益低，應
   收斂結束討論」），不再是舊的「超過硬性上限（已發表 6/6 輪…）」
   強制文字。

2. **回放頁「六個可量化的差異」對比表沒更新的根因**：`compute_comparison()`
   從「共創收斂」重構完之後**忘了跟著改**，`real_sources`／
   `bmc_completeness` 還在用舊算法——取 `idea_pool_versions`（4 位
   persona 收斂前個別提案）算平均/範圍，沒有反映真正的最終產出（單一
   共創提案）。改成優先讀 `prototypes[0]['after']`（原型測試後的真正
   定案），沒有就退回 `co_created_proposal`，兩者都沒有才是共創重構前
   的舊 run，退回舊算法保持相容（`test_real_sources_falls_back_to_legacy_average_when_no_final_proposal`
   鎖住這個相容路徑）。真實資料驗證：`agent_total` 從舊算法的
   `1.5`（4 份平均）變成新算法的實際單一提案來源數 `3`，`bmc_completeness`
   從 range（`9/9 ~ 9/9`）變成單一數字（`9/9`）。

3. **模擬使用者對照評分**（回答使用者的核心問題：「編排 agents 是否
   真的比直接問一次 LLM 更有性價比？」）——新函式
   `evaluate_final_outputs_with_users()`：讓做功課階段訪談過的模擬
   使用者（不是真人）分別對共創最終提案跟 baseline 各自獨立打
   0-10 分＋意見。**刻意用「方案 A／方案 B」盲測命名**，不讓模擬
   使用者知道哪個花了更多功夫做出來——這不是裝飾性的嚴謹，是真的
   會影響結果：如果 prompt 洩漏身份，很難排除「這個比較用心」的
   月暈效應污染評分。函式本身不是圖節點（跟 `run_baseline()` 一樣，
   在 `main()` 裡直接呼叫、手動 `set_current_node()` 讓事件照樣能進
   `events.jsonl`）。

   拿到的真實分數會回頭餵進 `generate_final_verdict()` 的 prompt——
   這是這次最重要的性質改變：**AI 的最終評語從「自己讀結構性數字寫
   感想」變成「引用真實第三方評分」**，不再是自問自答。真實跑測的
   評語第一句就是「分數是硬證據：模擬使用者給共創方案 5.5 分、
   baseline 3.0 分，差距達 +2.5，且評分者事前不知道哪邊花了更多
   力氣」——AI 真的把這組數字當成論證的起點，不是裝飾。

   踩到一個真實 bug：第一版忘了在 `evaluate_final_outputs_with_users()`
   裡呼叫 `set_current_node()`，導致事件的 `node` 欄位沿用
   `run_baseline()` 留下的 `"baseline"`，即時畫面的拓樸圖新節點永遠
   不會亮（`renderExtraGeneric()` 用 `action` 分派、不受影響，只有
   拓樸圖的 `supervisorNodeFor(node)` 對應會壞掉）——加回
   `set_current_node("evaluate_final_outputs")` 修正，這也是為什麼
   `run_baseline()` 這類「不經過圖節點的函式要手動管理節點狀態」的
   細節值得寫下來：呼叫序列裡上一個設的節點名稱會一直沿用，不會自動
   歸零。

   即時畫面/回放器：`user:{name}` 是新的角色前綴（模擬使用者評分，
   不是提案的 persona），`roleDisplayName()`/`roleColor()`/`laneOrder()`
   都要比照 `master:`/`baseline` 加專屬處理，不然泳道會顯示成
   「user:陳小姐」這種不乾淨的字串。`user_evaluation_summary` 事件的
   `extra` 刻意把完整的兩份提案（含 BMC）都放進去，讓使用者要的
   「兩者平行呈現」不用點開多筆事件拼湊。

真實跑測（2026-07-13，`--example-config`，總成本 $0.036）：兩位模擬
使用者（陳小姐／王先生）都對兩個方案留下合法評分，`user_evaluation`
正確存進 run JSON 與最終報告的新區塊；回放頁對比表六列數字全部反映
新架構（不再是重構前忘了改的舊算法）；`generate_final_verdict()` 的
輸出真的引用了 5.5/3.0 這組真實分數；`main()` 新增的
`user_evaluation_ok` 驗收檢查通過。61 個離線測試（stage9/10/11）全數
通過。

## 第四次迭代（2026-07-13）：BMC 量化＋從問題定義反推訪談對象

使用者一次提了兩件事：

1. **BMC 要量化才有用**——`成本結構`／`收益流` 原本只是一句話文字
   （例如「訂閱制，中等成本」），沒有任何數字，代表沒有機制能判斷
   「這個商業模式划不划算」，agent 永遠不會因為數字不合理而換想法。
2. **訪談對象帶偏了問題方向**——`users.yaml`（陳小姐/王先生/小宇）是
   固定寫死、跟主題無關的訪談對象，每次會議不管主題是什麼都問同一批
   人。使用者要求反過來：先用五力分析＋趨勢分析（科技/環境/人口結構/
   世代價值觀變化）針對公司定義出真正該解決的問題，再依這個問題動態
   產生該訪談誰。

使用者確認的三個關鍵設計：數字不划算時**軟性引導**（把損益回饋進既有
`refine()`/`revise_after_feedback()`/`co_create_turn()` 修正迴圈的
prompt，讓 LLM 自己判斷要不要換方向，不新增獨立的可行性關卡節點）；
訪談對象**完全動態生成**（不再讀 `users.yaml`，改成 LLM 依五力+趨勢
分析現場生成）；五力+趨勢分析**全會議只做一次**（parent graph 新增
共用節點，在所有 persona 開始做功課之前執行一次）。

1. **`收益流`／`成本結構` 從字串改成結構化物件**：新增
   `QUANTIFIED_BMC_KEYS = ["收益流", "成本結構"]`，這兩格現在是
   `{"narrative": "...", "monthly_estimate_twd": 數字, "basis": "..."}`，
   其餘七格維持一句話文字——沒有理由要求「客群」「通路」這些格子也
   塞數字。`assert_bmc_complete()` 對量化格改成檢查
   `_bmc_quant_cell_valid()`（narrative 非空字串 + monthly_estimate_twd
   是數字），其餘格維持原本的字串檢查。新增共用合併函式 `_merge_bmc()`
   取代 `draft_proposal`/`refine`/`revise_after_feedback`/
   `co_create_turn`/`generate_prototype_and_test` 五處原本各自重複的
   per-key dict comprehension——既然這次要動全部五個呼叫點，順手去
   重複，不是額外機會性重構。
2. **新函式 `compute_unit_economics(bmc)`**：純函式、零額外 LLM
   成本，從量化後的收益流/成本結構算出月淨利跟是否可行，掛在每個
   產生/合併完 BMC 的地方（`proposal["unit_economics"]`），下游（報告/
   回放/RAG）都能直接讀。
3. **軟性引導**：新函式 `_viability_nudge(prev)`，在
   `refine()`/`revise_after_feedback()`/`co_create_turn()`（這三個是
   「基於上一版修正」的節點，`draft_proposal` 是第一版沒有上一版可
   比較，不用加）的 system prompt 裡附上上一版的損益數字，不划算時
   明確提示「這一輪不要只是微調用詞，請認真考慮換一個核心價值主張或
   商業模式方向」。真實跑測**直接觀察到這個機制生效**：共創編輯第
   2 輪，陳亞力克斯把原本虧損的方案「改核心商業模式為內部驗證後封閉
   授權，扭轉虧損為正淨利」——不是換句話說，是真的換了商業模式。
4. **`biz_master` 終於拿到數字**：`run_masters()` 組
   `idea_pool_summary` 時，對每個提案附加估算月收入/月成本/淨利——
   `biz_master` 的 angle 本來就寫著「單位經濟」，但過去 `master_critique()`
   只收得到標題+摘要，完全沒有數字可評論。真實跑測的商業大師點評第一
   句就是「林美方案單位經濟最健全：小樣本驗證成本可控且淨利+17000…
   周依絲最弱：月虧40000且將核心效益列為內部節省而非營收」——批評
   內容真的在引用具體數字，不是泛泛而談。
5. **新節點 `analyze_problem`**：插在 `START` 跟 `fan_out_personas`
   之間，全會議只執行一次。用既有的 `web_search()` 原語做趨勢/五力
   相關搜尋，單一次 LLM 呼叫（省成本，不拆兩次）輸出五力五格＋趨勢
   分析＋問題陳述＋3-4 位動態生成的訪談對象（跟 `users.yaml` 同一組
   欄位形狀：`id/name/age/context/pain_points/tone`，下游
   `conduct_interviews()`／`_user_system_prompt()` 完全不用改）。解析
   失敗或訪談對象是空清單時退回既有 `load_users()` 當保底。回傳的
   `problem_brief` 文字灌注進 `synthesize()`／`design_interview_guide()`
   的 prompt，訪綱設計現在會被跟公司/主題綁定的具體問題定義帶著走，
   不再只是由固定訪談對象的既有痛點決定要問什麼。`draft_proposal`/
   `refine` 不用額外加這段——POV/HMW 已經是從訪談洞見萃取出來的，
   問題定義的影響透過訪談間接傳遞到位。`company`/baseline 刻意不吃
   `problem_brief`：baseline 代表「不做額外研究、直接問一次 LLM」，
   如果連 baseline 都拿到 agent 流程才做的問題定義分析，「編排
   agents 到底有沒有比較划算」這個對比就會失真。
6. **報告／RAG**：最終報告新增「## 問題定義（五力＋趨勢分析）」區塊，
   在「## 共創最終提案」之前，列出五力五格、趨勢分析、問題陳述、動態
   訪談對象——延續「使用者在 orchestrate agents 的過程中能學到什麼」
   的精神，讓報告讀者看得到「為什麼問這些人」的推導過程。
   `write_collective_wisdom()` 新增 `doc_type: "problem_analysis"` RAG
   寫入，未來會議的 `recall_memory()` 有機會撈到「同一家公司之前是
   怎麼定義問題的」。回放頁對比表新增「單位經濟」列，直接回答「這個
   商業模式到底划不划算」。

   踩到兩個真實 bug：
   - **`call_llm()` 對 extended thinking 耗盡 token 預算沒有重試**：
     真實跑測時 `give_feedback()` 崩潰，`response.content` 只有
     `ThinkingBlock` 沒有 `TextBlock`——`stop_reason` 不是
     `"max_tokens"`（原本重試邏輯只看這個），導致整場會議直接炸掉。
     這跟本次改動沒有直接關係，是既有 `call_llm()` 的潛在缺陷，真實
     跑測撞到才發現。修法：`text_parts` 是空的也視同截斷，一併觸發
     加大 token 重試，不用只看 `stop_reason`。
   - **`build_replay.py` 的 `_unit_economics()` 對舊格式資料沒有防呆**：
     BMC 量化前的舊 run 資料、或 baseline 這種沒有結構驗證的來源，
     「收益流」可能還是純字串——直接 `.get()` 會 `AttributeError`。
     離線測試裡用 baseline fixture（故意只填 5 格、其中收益流是純
     字串）就抓到了這個問題，修法是先確認真的是合法量化物件再讀值，
     不是的話當作 0。

真實跑測驗證（2026-07-13，`bmc-quant-verify-*`，因撞到上述 `call_llm()`
bug 分兩段跑完，合計總成本約 $4.2）：`analyze_problem` 針對「如何提升
新聞短影音互動率」產生的問題陳述是「新聞短影音多為單則式資訊摘要，
缺乏可延續討論的觀點鉤子、系列化角色與世代分眾設計」，五力分析五格
皆有具體內容（例如「供應商議價力：高度依賴YouTube/IG/TikTok演算法
分發規則與抽成，媒體幾無議價空間」），動態生成 4 位訪談對象（陳威廷
腳本編導／林淑芬核心用戶／莊子安Z世代潛在用戶／王啟明品牌廣告主
窗口）——涵蓋跟五力分析呼應的不同利害關係人角度，完全不同於
`users.yaml` 的陳小姐/王先生/小宇。量化後的 BMC 真的有數字，例如
「收益流：同盟台封閉授權費＋聯播分潤＋廣告主報表加值費，估算
NT$74,000/月」附完整依據說明。`main()` 新增的 `problem_analysis_ok`
驗收檢查、其餘既有驗收檢查全數通過；68 個離線測試（stage9 37 個 +
stage10 31 個）全數通過。瀏覽器直接檢查回放頁：`analyze_problem`
事件的細節面板正確渲染五力/趨勢/問題陳述/訪談對象，`draft_proposal`
事件的 BMC 九宮格裡量化格顯示「敘述 + NT$金額 + 依據」、無
`[object Object]` 洩漏，對比表新增的「單位經濟」列正確顯示雙方淨利與
可行性；stage11 即時面板拓樸圖的 `analyze_problem` 節點正確顯示在
最前面。
