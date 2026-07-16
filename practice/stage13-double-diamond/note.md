# Stage 13 Note — Double Diamond 重構（Discover→Define→Develop→Deliver）

## 目標

stage12 這一輪的真實驗證（見 `practice/stage12/note.md`）一路挖出同一個根因：
`analyze_and_scope()` 在任何訪談發生之前，就已經把問題收斂成一個具體、常常
連產品命名都定了的 `strategic_goal`——不管輸入的 `topic` 多開放，這一步都會
收斂成一個方向，後面訪談做得多深、BMC 各自設計得多獨立，所有 persona 都
只能在同一個已經選定的框架裡精修。真實跑測量到的 `idea_diversity.avg_distance`
（0.2382、0.2537、0.2821）全部低於「同一個 idea 換句話說」的校準基準值
（0.579）。另外拿一份真實的內部腦力激盪表格對照，發現「開放式問題框架＋
刻意跨領域的角色設定」比「要不要接訪談」更能解釋真實的多樣性落差。

使用者要求「突破之前的框架」：完全脫離 stage12「5 分鐘 demo、盡量省成本」
的限制，用 Double Diamond（Design Council 的經典框架：Discover 發散→Define
收斂→Develop 發散→Deliver 收斂）重新設計 agent 拓樸，把「發現問題」跟
「發想解法」兩個菱形真正分開——這是 stage12/note.md 記錄的 JTBD 優先構想
的具體實作。使用者確認的範圍：(1) 不受時間/成本預算限制，設計正確優先；
(2) 第一個菱形收斂成 1 個機會/job，不做多候選策略 fan-out；(3) Discover
階段先假設 2-4 個候選 job，每個各自訪談少量人，用 JTBD 的 switch 訪談技巧
（不是驗證某個預設方向）。

## 整體流程對照：兩個菱形分別在哪裡

```
                              1 個開放主題
                                   │
                                   ▼
                ┌───────────── Discover（發散）──────────────┐
                │ desk_research_hypothesize_jobs              │
                │   → 讀 company.md 做五力/趨勢分析，          │
                │     假設 2-4 個候選 job（刻意不含解法）       │
                │ discover_and_evaluate_jobs                  │
                │   → 每個候選 job 各自訪談驗證                │
                │     （JTBD switch 技巧，不是 5-Whys）        │
                └─────────────────────────────────────────────┘
                                   │  N 個候選 job，各自帶著
                                   │  supported/evidence/insights
                                   ▼
                ┌───────────── Define（收斂）───────────────┐
                │ select_job_and_define_problem               │
                │   → 讀全部候選 job 的訪談證據，選定 1 個，   │
                │     定義 target_audience/problem_statement/  │
                │     hmw——只定義問題，不准講解法              │
                └─────────────────────────────────────────────┘
                                   │  1 個解法無關的問題陳述
                                   │  ← 這就是修掉 stage12「太早
                                   │    收斂到單一 strategic_goal」
                                   │    的關鍵收斂點
                                   ▼
                ┌───────────── Develop（發散）───────────────┐
                │ assemble_persona_team                       │
                │   （內部 derive_company_domains 先從        │
                │    company.md 衍生職能，再各自獨立生成       │
                │    一位參與者）                              │
                │ draft_one_idea                              │
                │   → 每人獨立發想 1 個 idea + 自己的 BMC      │
                └─────────────────────────────────────────────┘
                                   │  N 個獨立發想的 idea
                                   ▼
                ┌───────────── Deliver（收斂）───────────────┐
                │ dfv_scoring → pick_winner →                 │
                │ generate_prototype → generate_evaluators    │
                │   → DFV 三面向結構化評分、選總分最高、       │
                │     做原型、找不重複的評估者對照 baseline    │
                └─────────────────────────────────────────────┘
                                   │
                                   ▼
                          1 個最終方案（原型 + 對照評分）
```

第一個菱形（Discover→Define）處理「這個問題到底是什麼」——先發散假設
多個候選 job，訪談驗證後只收斂留下 1 個；第二個菱形（Develop→Deliver）
處理「這個問題怎麼解」——從那 1 個問題出發重新發散出多個方案，DFV 評分
收斂成 1 個最終方案。兩個菱形中間的頸部（`select_job_and_define_problem`
選定 1 個 job）就是整個重構的核心：stage12 是在任何訪談發生「之前」就把
問題收斂掉，stage13 把這個收斂點往後推到訪談證據都到齊「之後」才發生。

## 架構決策

### 新的 `MeetingState`：拿掉 `strategic_goal`，換成 Discover/Define 的完整軌跡

state 不再有 `strategic_goal` 這個一次到位的解法欄位，改成：
`candidate_jobs`（2-4 個 JTBD 陳述，刻意不含解法/產品命名，各自帶一組小規模
`interview_pool`）→ `job_evidence`（每個候選 job 各自的訪談證據：
supported/evidence_summary/insights）→ `selected_job`（含 `why_selected`，
回溯具體證據）→ `problem_statement`/`hmw`（解法無關的問題陳述，銜接
Develop）。落選的候選 job 證據不會被丟棄，全部留在 `job_evidence` 裡，
最終報告會把它們跟贏家並列，讓 Define 階段的決策可以被稽核。

### 節點拓樸：遞迴組合兩種已驗證安全的 fan-in 模式，不發明新 join

stage12/note.md 記錄的真實教訓：LangGraph 對「兩條長度不同的分支都指到
同一個節點」不保證等全部前驅完成才觸發。stage13 的拓樸只用兩種模式的
遞迴組合：(A) `Send()` fan-out 到同一個節點名稱；(B) 單一節點內同步呼叫
`xxx_graph.invoke()`。四層巢狀（`discover_and_evaluate_jobs` 呼叫
`candidate_job_panel_graph.invoke()` → 對每個候選 job `Send()` 到
`research_one_candidate_job` → 該節點同步呼叫既有的 `interview_panel_graph`
→ 對每個訪談對象 `Send()` 到 `interview_one_person`），每一層都是模式 A/B
的直接重複。`build_parent_graph()` 有一個自動化測試（`test_graph.py` 的
`GraphTopologySafetyTests`）斷言除了 `ask_question`（HITL 迴圈的
either/or 入口，不是需要等待全部前驅的 fan-in）以外，沒有任何節點有兩個
以上不同的靜態前驅——把 join-safety 從 docstring 宣稱變成回歸測試。

JTBD 重排序帶來一個沒預期到的額外好處：`select_job_and_define_problem`
（Define 收斂）→ `assemble_persona_team`（Develop 起點）是嚴格單一前驅鏈，
不像 stage12 `system_research`／`generate_personas` 是兩個互不相依卻要
同時完成的平行分支——這個接縫本身就不是 join，不需要「折成一個節點依序
呼叫」的技巧，直接消除了一整類 join 風險。

### Discover 訪談改用 JTBD 的「switch」技巧，不是 5-Whys

5-Whys 是往下挖一個已知症狀的根因，前提是已經知道要往哪裡挖；switch 訪談
是重建一次真實「轉換／採取行動」決策的時間軸，用來檢驗一個候選 job 假設
是不是真的——這是不同形狀的問題，不是深度差異。五輪固定意圖：回溯契機→
被動觀望→考慮過的替代方案→卡關瞬間→現況滿意度（`SWITCH_INTERVIEW_ROUND_INTENTS`）。

### Develop 起點：程式碼強制跨領域抽樣，不靠 LLM 自己判斷「互補團隊」

真實跑測發現 LLM 自己判斷的互補團隊常常還是同一個大領域裡的不同分工
（例如都偏行銷/內容），跨領域的距離天生比刻意跨領域小很多。
`assemble_persona_team()` 改用 `random.sample(DOMAIN_ARCHETYPE_POOL, k=N)`
（純 Python，零 LLM 判斷）從跟腦力激盪主題無關的領域池（建築、餐飲內場、
製造業產線、高教行政、保險理賠、農業供應鏈、職業運動訓練、法律實務、
社工、遊戲設計、零售門市、硬體工程）強制抽樣，每位參與者各自獨立一次
LLM 呼叫生成（不是一次呼叫生成全部 N 位，避免角色彼此的措辭/選擇互相
關聯）。

**這個設計後來被使用者的真實回饋推翻，見下面「第二輪修正」——跨領域
確實拉開了多樣性，但那些領域跟公司無關，方案很可能公司根本做不出來。**

## 真實驗證

### CLI（`--script skip.json`，單一連續 process，兩次真實 API 跑測）

用某公司資料（`company.md`）跑了兩次：

| | 跑測一 | 跑測二 |
|---|---|---|
| 候選 job 數 | 3 | 3 |
| supported 數 | 3/3 | 2/3 |
| 參與者 domain | 5 個不重複 | 5 個不重複 |
| **idea 多樣性 avg_distance** | **0.3222** | **0.2295** |
| 總耗時 | 305.7s | 227.7s |
| 總成本 | $1.1441 | $0.8393 |
| 驗收 | 通過 | 通過 |

兩次都通過全部驗收項（候選 job 數在 2-4 之間、每個候選 job 都完成訪談
證據、選定 job 有具體 `why_selected`、參與者跨領域不重複、DFV/prototype/
evaluators 齊全、報告完整）。

### 瀏覽器（真實會議：「如何提升健身房會員續約率」，經由即時控制面板）

用真實瀏覽器開一場會議，走完整條流程：設定主題→即時畫面看拓樸方框依序
亮起（`desk_research_hypothesize_jobs`→`discover_and_evaluate_jobs`→
`select_job_and_define_problem`→`assemble_persona_team`→`draft_one_idea`→
…）→ 3 個候選 job 平行訪談（6 位訪談對象，各 5 輪 switch 訪談）→ Define
收斂選出 job1→5 位跨領域參與者→5 個 idea→HITL 暫停（5 個 idea 卡片正確
顯示）→ skip 進 DFV 評分→收斂→prototype→評估者對照（agent 7.33 分 vs
baseline 1.67 分，差距 +5.66）→ 完整報告與回放頁。idea 多樣性 0.2791。

三次真實跑測的多樣性數字：0.3222、0.2295、0.2791——都比 stage12 的三筆
歷史值（0.2382、0.2537、0.2821）中的兩筆高，但也有一次落在同樣的區間，
且全部仍低於「同一主題下真的不同 idea」的校準基準值 0.749（更接近甚至
低於「同一個 idea 換句話說」的 0.579）。**誠實的結論**：Double Diamond
重排確實把根因（premature convergence 到單一 `strategic_goal`）消除了，
方向是對的，但光靠拿掉這個根因，多樣性並沒有穩定、大幅超過 stage12——
變異度本身就很大（0.23-0.32 之間跳動），意味著還有其他因素（例如訪談
對象的措辭風格、DFV 評分本身的收斂壓力）在影響最終量到的距離。這比
「宣稱問題解決了」更值得記錄下來。

## 第二輪修正：使用者看過真實逐字稿後提出的兩個回饋

### 1. 被訪談者的回答太常說「土法煉鋼」

使用者看過真實訪談逐字稿後發現「土法煉鋼」這個成語重複出現在多位不同
受訪者、不同場次的回答裡。追查發現不是 LLM 自己收斂到同一個說法，而是
`_user_system_prompt()`（訪談對象的模擬 persona 系統提示詞，這段從
stage3 起就原封不動複製到 stage12，stage13 一開始也沒動它）字面上寫著
「如果被問到解法，就誠實說你目前的土法煉鋼做法或『我沒想過』」——是
prompt 明文指示每一位受訪者都用這四個字概括自己的應付方式，不是模型的
巧合。修法：改成要求受訪者用自己的話具體描述現況（可能克難、不方便，
或根本沒認真想過），並明講不要用「土法煉鋼」這種現成成語去概括。用真實
API 直接對三位不同情境的受訪者測試，修正後三人給出完全不同、各自具體
的應付方式（拖著不理教練／一個一個傳訊息問／手機開好幾個備忘錄各記
一類），成語重複的狀況消失。這個坑其實跨 stage3 到 stage12 都存在，
這次只在 stage13 修，其餘 stage 尚未處理。

### 2. 跨領域抽樣的人物與公司無關，方案很可能公司做不出來

`DOMAIN_ARCHETYPE_POOL`（建築、農業供應鏈、職業運動訓練…）確實比 LLM
自己判斷的「互補團隊」拉開了 idea 多樣性，但使用者指出：這些領域跟
公司本身完全無關，找一位農業供應鏈專家幫 APP 提案，想出來的
東西很可能公司根本沒能力做出來——多樣性的代價是失去可行性。

修法：新增 `derive_company_domains(company, problem_statement, hmw)`，
讀 `company.md` 衍生出這家公司實際具備、彼此明顯不同的職能（不是跟
公司無關的任意領域），一樣要求不能全部落在同一個大領域的不同分工
（避免又退回「都是行銷/內容」的同質化問題）。`assemble_persona_team()`
改呼叫這個函式取代 `random.sample(DOMAIN_ARCHETYPE_POOL, ...)`，舊的
領域池只在解析失敗/衍生數量不足時當保底。用某公司的
company.md 測試，衍生出五個彼此明顯不同、但都是這家公司真實具備的能力
（例如既有內容資產庫、App 端的導購/交易機制、既有異業合作關係、內容
產製技術、跨世代受眾資料等，實際字眼依測試當下的公司描述而定）——用
其中一個生成的 persona 提案時，背景與 focus 明確扣著其中一項實際資產，
不是憑空想像的外部視角。

兩個修正都新增/更新了對應的 offline 測試（`test_graph.py` 的
`DeriveCompanyDomainsTests`、重寫的 `AssemblePersonaTeamTests`），並
用真實 API（不是 mock）驗證過修正後的行為，64 個 offline 測試全綠。

## 真實跑測發現並修好的坑（stage13 特有，跟拓樸/join 無關）

### 候選 job 的訪談對象 id 跨 job 撞號

`desk_research_hypothesize_jobs()` 一開始讓 LLM 每個候選 job 各自從
`u1`/`u2` 起算 id（fallback 分支也可能因為 `load_users()` 樣本不夠而讓
不同 job 分到同一批 id）——三個候選 job 就有三組 `u1`，即時畫面／回放頁
的 `findUser(id)` 用 `.find()` 找第一個符合的，撞號時只會顯示錯的那一位
受訪者的人物設定。修法：全部候選 job 底定後做一次全域重新編號
（`u1`..`uN`），保證整場會議的訪談對象 id 不重複。用真實跑測資料驗證過
（重跑一次，確認 job1/job2/job3 的 id 分別是 u1-u2/u3-u4/u5-u6，不重複）。

### `build_replay.py` 的成本加總對不上真實總成本

`interview_turn` 沿用既有的整批排除慣例（同一位受訪者 5 輪訪談共用一個
invocation，每輪事件的 cost_usd 是累計數，全部加總會重複計入），但整批
排除會把訪談成本整個從加總消失，而不是排除法通常假設的「只排除重複計入
的部分」——真實跑測用 `abs(display_total - true_total) > 0.001` 的檢查
抓到 $0.10 的落差。修法：不是整批排除，而是每位受訪者只留「round 最大」
的那一則（累計數字就是這次訪談的真實總額），改用 `user_name`（不是
`user_id`，會撞號）分組。另外 `evaluate_final_outputs_with_users()` 對
每位評估者發的事件也是同一種 mid-snapshot 模式（真正的累計總額在後面的
`user_evaluation_summary` 事件裡），比照 stage12 `system_research`／
`generate_personas` 的排除法整批排除。兩處都修好後，兩次真實跑測的
回放器成本加總都跟真實總成本完全一致（$1.1441、$0.8393，零落差）。

## 已知限制（真實驗證中發現、這一輪刻意不處理）

### `total_cost_usd` 在經過 HITL resume 之後會少算（跨 stage 的既有問題）

用真實瀏覽器走一次「開始會議→HITL 暫停→resume」的完整流程後，比對
`build_replay.py` 算出的回放器成本（$0.9462）跟 `state.json`／CLI 印出的
`total_cost_usd`（$0.0590）——落差非常大。追查發現：`run_worker.py` 的
`cmd_resume()` 先直接呼叫 `graph.invoke(Command(resume=...), config)`
把剩下的圖（`dfv_scoring`起）跑完，這時候的成本會累積進
`graph.py` 模組層級的 `usage_log`；但緊接著呼叫的
`_run_main_and_capture()` 會再呼叫一次 `sg.main()`，而 `main()` 開頭就
`reset_metrics()`＋`usage_log.clear()`——把剛剛那次直接 invoke 累積的
成本整個清空，只留下第二次 `sg.main()` 呼叫自己（重新起一個 baseline
背景執行緒＋`evaluate_final_outputs_with_users`＋`generate_final_verdict`）
的成本。這個 `cmd_resume()`/`cmd_start()`/`_run_main_and_capture()` 的
寫法是從 stage12（乃至更早的 stage）原封不動複製過來的，不是 stage13
這次重構引入的新問題——換句話說，**任何一個 stage（9-13）只要走過真實
瀏覽器的 HITL 暫停/resume 流程，存下來的 `total_cost_usd` 大概率都是
錯的**，只有像這次驗證用的 CLI 單一連續 process（`--script skip.json`，
沒有 subprocess 重啟）才會算對。這個問題影響範圍跨好幾個 stage、需要
仔細設計修法（例如比照 `is_fresh_thread` 的邏輯只在真正全新 thread 才
`reset_metrics()`），這一輪先誠實記錄下來，交給獨立的後續任務處理，不在
這次重構裡動手改。

### 跨 stage 共用同一份 `outputs/runs` 歷史紀錄，但回放格式各自不同

`server.py` 的 `RUNS_DIR = PRACTICE_DIR / "outputs" / "runs"` 是所有
stage 共用的同一個資料夾，「歷史紀錄」頁面因此會列出 stage11/12/13（甚至
更早）全部 run——但每個 stage 的 `/replay` 路由都無條件呼叫「自己那份」
`build_replay.compute_comparison()`，形狀對不上時不會報錯，只會靜默生出
誤導性的數字（真實測試：用 stage13 的伺服器開一個 stage12 run 的回放，
「動態組隊」顯示「0 個不重複 domain」、「候選 job 覆蓋率」顯示「假設 0
個候選 job...選定 None」——因為 stage12 的 run JSON 根本沒有 `domain`／
`candidate_jobs` 這些欄位）。這是跨 stage 共用歷史紀錄、但個別 stage
維護獨立資料形狀這個既有設計本身的缺口，不是 stage13 這次新增的問題，
這一輪一樣先記錄、不處理。

## 尚未實作、留給後續的構想

`practice/PLAN.md` 跟先前 session 的記憶檔案裡記錄的策略目標 fan-out
（多個候選 job 各自跑一條完整 develop/deliver 分支再跨分支選贏家）、
策略回饋迴路、虛擬問卷量化補充等構想，這次都刻意沒有整合進 stage13——
使用者明確要求這一輪先把 JTBD 單一收斂的版本做完整、做對，多候選分支
是另一個獨立、會讓架構更複雜的決定，留給使用者自己決定要不要接下去做。
