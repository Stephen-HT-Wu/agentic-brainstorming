# Stage 14 Note（stage14-signals：訊號擴充）

> 這個 stage 是 `practice/stage13-double-diamond/` 的完整副本（`cp -r`，不是
> `git mv`，兩個 stage 各自獨立存在），所以下面到「## Stage 14（stage14-signals）
> 新增：訊號擴充」這個標題之前的內容，是原封不動繼承自
> `stage13-double-diamond/note.md` 的歷史記錄——內文提到的「stage13」都是
> 指這份文件被寫下時那個階段的名字（重新命名前），保留不動是為了忠實記錄
> 當時的真實踩坑過程，不是筆誤。stage14-signals 自己這一輪新增的 5 項功能
> 設計與真實驗證，記錄在「## Stage 14（stage14-signals）新增：訊號擴充」
> 這個標題之後。

## 目標（以下沿用 stage13-double-diamond/note.md 原文，見上方說明）

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

## Stage 14（stage14-signals）新增：訊號擴充

### 目標

stage13-double-diamond 驗證完成後，使用者提出下一輪 5 項規劃（原話見
`/Users/stephen/.claude/plans/bubbly-fluttering-wombat.md`）：(1) 訪談對象
生成改成可切換「多元」vs「依題目自動生成適配」兩種視角，人數可設定；
(2) 新增虛擬問卷模組，用 LLM 模擬不同人口特徵的虛擬受訪者，量化訊號
輔助 Define 收斂；(3) 前台把 fan-out/fan-in 的平行分支視覺化呈現；
(4) stage 資料夾除了數字，加上對人類有意義的名稱；(5) 新增公司背景自動
調查模組，取代人工填寫 `company.md` 的麻煩。使用者確認的範圍決策：全部
5 項包進同一個新 stage（不拆成多個小改動）；連現有 stage13 也一併改名；
虛擬問卷的量化數據要餵給 Define 收斂決策，不是獨立不影響流程的報告。

虛擬問卷這個構想正是上一節「尚未實作、留給後續的構想」提到的、stage12
診斷多樣性問題時就出現過、使用者當時要求「先不要做，幫我思考一下」的
構想（已知方法論限制：LLM 模擬的虛擬受訪者不管抽樣幾人都來自同一個模型
的內部分布，不是真實母體的獨立抽樣，不具統計顯著性）——這次使用者主動
要求動手做，設計上必須誠實標示這個限制，不能包裝成有統計意義的真數據。

### 命名：stage13 → stage13-double-diamond，新 stage 叫 stage14-signals

`practice/stage13/` 用 `git mv` 改名成 `practice/stage13-double-diamond/`
（純資料夾改名，沒有其他 stage import 它）；`practice/stage14-signals/`
用 `cp -r` 從改名後的 stage13-double-diamond 複製（延續「每個 stage 一份
完整獨立副本，不互相 import」的既有慣例）。內部產出檔名（events.jsonl／
checkpoint db／run json／JS 常數名）從這次複製起改用 `stage14_*`／
`STAGE14_*` 新慣例；stage13-double-diamond 自己的內部檔名（`stage13_*`）
維持不變，這些是 gitignore/執行期產生的內部識別碼，改了沒有「對人類有
意義」的實質好處，只會徒增 `run_worker.py`／`build_replay.py`／
`server.py` 的改動量。

程式碼裡描述「這個檔案自己設計」的 docstring/註解（例如
`desk_research_hypothesize_jobs()` 的說明、`build_parent_graph()` 的拓樸
說明）用 sed 把自我指涉的「stage13」改成「stage14-signals」，因為這些文字
描述的是「目前這份程式碼的架構」，改名後依然成立；但 note.md 記錄的是
「stage13 這個名字底下發生過的具體事件」（跑測時間、量到的數字、修過的
bug），屬於歷史記錄，維持原文不動（見本檔案最上方的說明）。

### 1. 訪談對象生成：多元 vs 依題目自動生成，人數可設定

`desk_research_hypothesize_jobs()` 新增 `interview_mode`（`"topic_fit"`／
`"diverse"`，預設 `topic_fit`）與 `n_interviewees_per_job` 兩個從 `state`
讀取、有預設值的欄位——跟現有 `topic`/`company` 同一個機制，不是另外讀
`os.environ`，維持可測試性。`INTERVIEW_MODE_PROMPT_FRAGMENTS` 常數：
`topic_fit` 要求訪談對象背景合理對應候選 job 情境（但仍要求年齡/性別/
情境差異，不能像同一人的兩個版本）；`diverse` 刻意要求拉開差異、涵蓋
邊緣使用者/懷疑論者/非典型情境，用來測試候選 job 假設在更廣泛母體裡
站不站得住腳。下游所有 `N_INTERVIEWEES_PER_JOB` 的 slicing（包含 LLM
解析失敗時的保底分支）全部改讀 `n_interviewees` 局部變數，不再是寫死的
模組常數。設定管道（環境變數 → `main()` → `run_worker.py --interview-mode`／
`--n-interviewees` → `server.py CreateRunRequest` → 前端下拉選單＋數字
輸入框）比照現有 `BRAINSTORM_TOPIC` 的機制，一路往下接。

### 2. 虛擬問卷模組：`run_virtual_survey` 循序節點

**拓樸決策**：一開始考慮把虛擬問卷做成另一條平行分支指向
`select_job_and_define_problem`，會重現 stage12 那個「兩條長度不同的分支
指向同一節點，LangGraph 不保證等兩條都完成」的真實 bug（也會讓
`test_no_unexpected_multi_predecessor_nodes` 開始失敗）。改成拓樸上的
**循序節點**，插在 `discover_and_evaluate_jobs` 跟
`select_job_and_define_problem` 之間，整條鏈維持線性：

```
desk_research_hypothesize_jobs → discover_and_evaluate_jobs → run_virtual_survey
  → select_job_and_define_problem → assemble_persona_team → ...
```

`run_virtual_survey` 本身是模式 B（單一節點同步呼叫
`survey_panel_graph.invoke()`），內部再用模式 A `Send()` fan-out 到
`survey_one_stratum`——跟這個專案已驗證安全的兩種模式的遞迴組合完全
一致，不發明新 join。加入這個節點後立刻重跑
`test_no_unexpected_multi_predecessor_nodes`，斷言依然只有 `ask_question`
有多個前驅（見「執行順序與驗證」一節）。

**不對每一位虛擬受訪者各自 fan-out**（stage12/note.md 已經分析過這個
障礙：fan-out 到幾百個獨立 LLM 呼叫會撞 rate limit、拉長壁鐘時間）。改成
fan-out 到 8 個明確寫死的人口特徵分層（`SURVEY_STRATA`：年齡層 × 性別 ×
職業類別交叉），每個分支一次 LLM 呼叫模擬 `n_respondents`
（預設 `DEFAULT_SURVEY_RESPONDENTS_PER_STRATUM=8`，總模擬樣本數約 64）位
虛擬受訪者、只回傳彙整後的統計量（高困擾比例、平均困擾強度、至多一則
標明模擬的引述），不是逐字稿。`_aggregate_survey_results()` 依
`n_simulated` 加權平均每個候選 job 的統計量，回傳的 dict 一律內含
`SURVEY_METHOD_CAVEAT` 這段警語原文——保證不會在報告/回放/prompt 裡被
靜默丟掉。

`select_job_and_define_problem` 的 prompt 新增一段明確標籤「量化補充
（虛擬問卷，模擬訊號，非真人樣本，僅供參考）」的區塊＋警語原文，system
prompt 明確要求「質化證據永遠是主要依據」、禁止把模擬數據講成有統計
顯著性的真實調查結果。`build_final_report_markdown()`／`build_replay.py`
都新增對應的虛擬問卷呈現區塊（回放頁的六維對比表多一列）。

### 3. 前台 fan-out/fan-in 視覺化：展開/收合 mini-lane

`.topo` 容器本來就是 `flex-wrap`，子元素設 `flex-basis:100%` 就能強制
換行——不用畫新的樹狀圖，直接讓「點開一個拓樸方塊、在下面展開它底下的
平行分支」用既有 CSS 做到。把原本的 `renderLanes()` 拆成純函式
`renderLaneRows(eventIdxs)`（給一組事件索引，回傳依角色分列的 HTML），
`renderLanes()` 改成呼叫它並傳全部事件索引。新增
`CHILDREN_OF_SUPERVISOR`（從 `NODE_TO_SUPERVISOR` 反推哪些 supervisor
底下真的有 fan-out 子節點）、`expandedTopoNode` 狀態＋
`toggleTopoDetail(name)`，`renderTopo()` 在被點開的方塊後面插入一個
`.topo-detail` 區塊，內容是該 supervisor 底下子事件的 `renderLaneRows()`
結果，沿用 `.lane`/`.chip` 既有樣式。

### 4. Stage 資料夾改名

見上面「命名」一節。

### 5. 公司背景自動調查模組

設計成「設定階段的一次性動作」，不是圖節點——使用者原話「可以在設定的
時候」暗示這是開會前、無狀態、不需要 checkpoint/HITL 的單次動作。新檔案
`company_research.py`：`fetch_url_text(url)` 先試 Tavily 的 `/extract`
端點（`TAVILY_API_KEY` 存在時，跟既有 `_search_tavily()` 完全一樣的
`urllib.request` POST 手法），沒有 key 或抓失敗時退回純標準庫的
`urllib.request.urlopen()` + 一個繼承 `html.parser.HTMLParser` 的簡易
標籤剝除器（`_TextStripper`，跳過 `<script>`/`<style>` 內容）——刻意不加
`requests`/`bs4`/`trafilatura` 這類新依賴。`research_company(name, urls)`
對每個 URL 抓不到內容時退回把網域當查詢字串丟給既有的 `web_search()`；
沒有網址時直接對公司名稱做 `web_search()`；組好素材後一次 `call_llm`
呼叫，輸出 200-500 字的 `company.md` 風格描述，明確要求具體描述部門/
技術/內容素材/通路/既有業務關係，不要行銷空話（因為
`derive_company_domains()` 需要具體輸入才能衍生職能）。
`write_company_profile(slug, markdown)` 寫入 `practice/{slug}-company.md`。

`server.py` 新增同步端點 `POST /api/company-research`（不經過
`run_worker.py` 的 subprocess/checkpoint 機制，一次 10-30 秒的呼叫前端
轉圈可以接受），另外提供等效 CLI `research_company_cli.py`。前端新增
「公司研究（選用）」設定卡片：公司名稱、可重複新增/移除的 URL 輸入框、
slug 輸入框、「產生公司說明」按鈕、唯讀顯示產生的 markdown。刻意不做
「一鍵覆蓋 company.md」，使用者自己確認滿意後手動 `cp` 取代。`.gitignore`
新增 `*-company.md`（確認不會誤擋 `company.md` 或 `company.example.md`）。

**真實跑測踩到的坑**：`server.py` 原本讓公司研究的 slug 沒填時借用既有
`_slugify()`（給 `run_id` 用，只留 ASCII 字元，保底字串是 `"topic"`）。
真實瀏覽器測試用一個全中文公司名稱（沒有 ASCII 字元）時，slug 落到保底，
寫出來的檔案變成 `topic-company.md`——對公司研究這個情境完全是誤導性的
檔名（讓人以為調查失敗或抓錯資料，實際上是 `_slugify()` 這個保底字串
本來就是設計給「主題」不是「公司名稱」用的）。修法：新增
`_company_slugify()`，中文公司名稱落到保底時改用帶時間戳的
`co-<timestamp>`，不借用 `_slugify()` 的 `"topic"` 保底字串。

### 執行順序與驗證

1. 先做 stage13 改名＋複製出 stage14-signals，跑一次既有 offline test
   suite（64 個全綠）確認搬移沒有破壞任何東西。
2. 依序實作：訪談模式/人數 → 虛擬問卷（含 `run_virtual_survey` 拓樸
   插入，插入當下立刻重跑拓樸安全性測試）→ 前端視覺化 → 公司研究模組。
3. 新增/擴充 24 個 offline 測試（`InterviewModeConfigTests`、
   `FanOutSurveyStrataTests`、`SurveyOneStratumTests`（含解析失敗/超出
   範圍值的保底）、`AggregateSurveyResultsTests`（加權平均數學）、
   `RunVirtualSurveyTests`、`SelectJobAndDefineProblemTests` 擴充的量化
   區塊/警語斷言、新檔案 `test_company_research.py` 的抓取 fallback 鏈
   與寫檔測試）。連同既有測試共 88 個全綠，pyflakes 乾淨（只有繼承自
   stage13-double-diamond 的既有 `test_avatars.py:1:1 're' imported but
   unused` 警告，不是新問題）。
4. 真實 CLI/瀏覽器驗證（主題「如何提升訂閱制新聞的續訂率」，
   `n_interviewees_per_job=1`、`survey_respondents_per_stratum=2`，
   `--example-config`）：確認 `events.jsonl` 裡 `run_virtual_survey`
   剛好一筆、`survey_one_stratum` 剛好 `len(SURVEY_STRATA)=8` 筆，總模擬
   樣本數 16；人工檢查 `why_selected` 只引用質化訪談證據（「花2-3天比對
   多家新聞、詢問銀行朋友…」），完全沒有把模擬問卷數字講成統計顯著的
   真數據；最終報告的「虛擬問卷」段落正確印出警語原文；回放頁對比表
   新增的「虛擬問卷」列正確顯示總樣本數與選定 job 的模擬統計量。
5. 真實瀏覽器驗證即時面板：點開 `run_virtual_survey` 拓樸方塊，
   `.topo-detail` 正確展開成 8 個分層的 mini-lane（`flex-basis:100%`
   強制換行，沿用既有 `.lane`/`.chip` 樣式），點開其中一個分層的 chip
   能看到該分層的完整模擬統計 JSON（`used_fallback: false`，真實數字）；
   走完唯一的 HITL 暫停點（skip）、resume 正常跑到底。確認
   `build_replay.py` 沒有針對虛擬問卷的新節點多發出「檢查
   COST_SNAPSHOT_ACTIONS」警告（`survey_one_stratum` 是一個 invocation
   一個 emit，不是 mid-snapshot 模式）。這次真實跑測仍然撞到既有的
   `total_cost_usd` 經過 HITL resume 後少算的已知問題（見上一節「已知
   限制」，跨 stage9-13 都有、這次繼承到 stage14-signals，`build_replay.py`
   自己的回放器成本加總不受影響，是可信的數字）——不在這輪修，維持獨立
   後續任務的狀態。
6. 真實瀏覽器驗證公司研究卡片：用一個全中文虛構公司名稱（無真實業務、
   `https://example.com` 當佔位網址）跑一次「產生公司說明」，發現並修好
   上面記錄的 slug 保底字串誤導問題，修好後確認寫出
   `co-<timestamp>-company.md`、內容誠實標註自己是「練習範例公司…非真實
   營運實體」（因為佔位網址沒有可用內容，LLM 誠實承認素材不足，沒有
   幻想出一家假公司的具體數據）。

### 已知限制與後續

- `total_cost_usd` 經 HITL resume 少算的問題（見上一節，跨 stage9-13
  都有）繼承到 stage14-signals，這次驗證中再次確認存在，維持獨立追蹤，
  不在這輪處理。
- 虛擬問卷目前是固定 8 個人口特徵分層、每層一次 LLM 呼叫；真實跑測的
  `high_distress_pct`/`avg_intensity` 在不同分層之間確實有差異（不是每
  次都給同樣數字），但樣本數極小（測試用 `survey_respondents_per_stratum=2`，
  預設 8）時的穩定性沒有進一步量化驗證——這本來就是這個模組方法論上的
  已知限制（模擬訊號，非真實統計），設計上已經用 `SURVEY_METHOD_CAVEAT`
  強制警語隨資料傳遞，這裡誠實記錄「連這個警語標註的限制本身也還沒有
  被系統性驗證過在不同 N 下的行為」，留給有興趣深入的後續工作。

## Stage 15（stage15-market-fit）新增：打破框架重新思考——策略導向、由上而下的市場驗證

### 目標

使用者在真實會議測試完 stage14-signals 後提出一個「打破框架重新思考」的
新場景（原話）：「如果我們要做依照公司高層策略 (例如：APP 建立會員付費
訂閱加值功能機制) 出發，快速驗證實踐戰略的點子 (要做什麼樣的功能會有
市場競爭力？)，我們應該如何設計這個流程？」現有 stage9-14 全部都是
**由下而上發現問題**（desk research 假設候選 job → 訪談驗證 → 收斂成一個
問題陳述 → 才開始想解法）；這次策略方向由公司高層由上而下給定，不需要
再驗證「這個方向對不對」，真正要快速回答的是「在這個既定策略方向下，
做什麼樣的具體功能會有市場競爭力？」——這跟 Discover/Define 建立在
「問題本身需要被發現」這個前提上完全不同，需要真正重新設計拓樸，不是在
stage14-signals 上加欄位。

使用者確認的三個決策：(1) 蓋成獨立的新 stage（`stage15-market-fit`），
不是加模式開關；(2) 競品資訊用純 `web_search()` 自動找，不做使用者輸入
競品的 UI；(3) 「快速驗證市場競爭力」三個機制都要——DFV 新增「市場競爭力」
評審視角、虛擬問卷改測購買意願/差異化感受、保留簡化版概念測試訪談。

### 歷史教訓：避免功能層級重演 stage12 的多樣性坍縮

stage9-12 的 `analyze_and_scope()` 曾在任何訪談發生之前就用一次 LLM 呼叫
「發明」一個 `strategic_goal`，導致嚴重多樣性坍縮（`idea_diversity.avg_distance`
量到 0.2382/0.2537/0.2821，全部低於「同一個 idea 換句話說」的校準基準值
0.579，見 `practice/stage12/note.md`）。這次策略方向雖然是外部合法給定
（不是 LLM 自己編的），但同樣的坍縮風險會在**功能層級**重現：如果流程讓
某一步在市場驗證之前就收斂成「一個」功能提案，一樣會複製這個 bug。設計上
維持「策略方向給定，但候選功能提案仍然平行、多元，驗證完才收斂」的結構
（詳見下面「真實驗證」一節量到的實際數字）。

### 真實踩過的坑：為什麼舊版五力分析的競爭者分析都大同小異

使用者在規劃階段中途提出一個具體疑問（原話）：「我之前發現每次做五力
分析中，競爭者分析都大同小異？這是為什麼？」追查根因有兩層：(1)
`desk_research_hypothesize_jobs()` 的搜尋詞太籠統（`f"{topic} 競爭者
替代方案"`），搜到的多半是產業趨勢文章，不是具體競品頁面；(2) 輸出欄位
只要求一個 <=60 字的抽象強度描述，沒有像 `draft_one_idea()` 的 `sources`
欄位那樣要求「url 只能來自提供的搜尋素材，不能捏造」這種結構性具體性
要求——LLM 在沒有具體事實逼它寫具體內容時，自然滑向安全泛用的說法。

新的 `research_competitive_landscape()` 從一開始就用結構驗證擋掉空泛
輸出：搜尋詞改成鎖定「命名+比較」而不是「趨勢」（例如「有哪些 APP 或
服務已經在做 案例」「知名品牌 功能比較」）；輸出 schema 要求
`competitor_name`/`feature_description`/`source_url`，解析後**程式碼
檢查 `source_url` 是否真的出現在這次 `web_search()` 回傳的網址清單裡**，
不信任 LLM 的自稱宣告，沒通過驗證的條目直接丟棄，不硬湊數量；一筆有效
競品都沒有時 `used_fallback_competitive_landscape=True`，報告/回放頁
誠實標示「本次未能取得具體競品資訊，以下評分僅供參考」。

### 拓樸：Discover/Define 整個拿掉，換成 `research_competitive_landscape` + `validate_market_fit`

```
START → research_competitive_landscape → assemble_persona_team
  → [Send fan-out] draft_one_feature → ask_question ⇄ answer_question
  → validate_market_fit → pick_winner → generate_prototype → generate_evaluators → END
```

`validate_market_fit` 是模式 B（單一節點同步依序呼叫三個子圖）：先跑
`survey_panel_graph`（虛擬問卷，測購買意願/差異化感受）、再跑
`concept_test_panel_graph`（簡化版 1-2 輪固定問題訪談，跟 stage14-signals
的完整 5 輪 JTBD switch 訪談不同）、最後跑 `dfv_panel_graph`（4 個 lens，
新增的 `market_fit` lens 讀前兩步彙整結果當佐證）。三個子圖依序同步呼叫
（不是三條分支各自指到 `pick_winner`，那會複製 stage12 的 join bug）。
概念測試的訪談對象刻意設計成固定共用小組（`load_users()[:n]`），不是像
stage14-signals 那樣依候選項目動態各自生成——避免多一次 LLM 呼叫跟
「功能→訪談對象」ID 對應的複雜度，不影響拓樸設計。

### 真實驗證（CLI + 瀏覽器，主題「APP建立會員付費訂閱加值功能機制」）

用真實 `company.md`/`personas.yaml`/`users.yaml` 跑一場完整會議
（`BRAINSTORM_N_CONCEPT_TEST_INTERVIEWEES=3 BRAINSTORM_SURVEY_N=4`），
驗收全部通過。實際觀察到、值得記錄的發現：

- **競品掃描這次真實落到 fallback，但這正是安全機制設計上該有的行為**：
  `research_competitive_landscape` 真的搜到一筆具體競品頁面（Shopify 的
  Appstle Memberships），但 LLM 抽取步驟沒有把它整理成通過 `source_url`
  驗證的結構化條目，最終 `used_fallback_competitive_landscape=True`。
  報告誠實標示「本次未能取得具體競品資訊」，沒有硬湊或美化——這正是
  設計這個安全機制的目的：寧可誠實回報找不到，也不要讓下游假裝有扎實
  競品事實可以引用。
- **即使競品掃描 fallback，`market_fit` DFV 評審的批評仍然高度具體**：
  5 筆 critique 分別點名 Appstle Memberships、RevenueCat Paywalls、
  Superwall、Adapty、Patreon、Substack、Twitch/Bilibili 等真實存在的
  競品，且每筆批評內容彼此不同、緊扣各自功能的商業模式（不是換個名字
  的同一段話）——原本使用者抱怨的「競爭者分析都大同小異」問題在這個
  新節點沒有重現，即使結構化的 `competitive_landscape` 這次是空的，
  LLM 自身的知識庫仍然提供了具體、多樣的競品名稱佐證。
- **虛擬問卷/概念測試訪談的數字確實有餵進 `market_fit` 評分**：例如
  「在地達人分級訂閱制」的 critique 明確引用「模擬問卷34.6%購買意願與
  35.6%差異化感受」跟訪談逐字稿的具體反饋，不是自說自話。
- **`idea_diversity.avg_distance` 量到 0.4304**：明顯高於 stage12 歷史
  坍縮值（0.2382/0.2537/0.2821），代表策略方向給定沒有讓功能層級重演
  同樣嚴重的坍縮；但仍低於 0.579 的「同一個 idea 換句話說」校準基準，
  說明外部給定策略方向雖然避開了最嚴重的坍縮，多樣性還是有改進空間
  （5 個候選功能的 `target_segment`/`monetization_mechanism` 讀起來確實
  彼此不同，不是換句話說，但主題本身「會員訂閱加值」天然比開放式問題
  探索空間窄，這可能是分數比 stage13/14 完全開放式的候選 job 稍低的
  結構性原因，留給後續觀察）。
- **這次是 baseline 贏，而且贏得不模糊**（agent 3.67 分 vs baseline
  5.67 分，差距 -2.00）——`generate_final_verdict` 的分析誠實指出問題出在
  收斂判斷而非發散不足：`pick_winner` 選出的贏家「SEO口袋清單訂閱牆」
  格局偏窄（單點導流機制優化），baseline 的「食尚玩家嚴選會員」雖然
  搜尋引用數=0（可能編造）卻涵蓋更多加值面向，完整度反而更高。這是一次
  誠實、非人工做出來讓 agent 贏的真實結果，跟這個專案「過程比結論重要」
  的定位一致（見 [[agentic-brainstorming-stage14-virtual-survey-user-insight]]）。
- **回放頁驗證**：新的 `research_competitive_landscape`／
  `concept_test_turn`（含完整被訪談者人物設定渲染）／`validate_market_fit`
  詳情面板都正確渲染真實資料；六維對比表正確顯示新指標（競品掃描覆蓋度、
  購買意願/差異化、DFV 4 面向×5 個 idea 共 20 筆）。過程中發現並修好一個
  遺留自 stage13/14 複製的文字 bug：回放頁標題/副標題仍寫死「Double
  Diamond」「Discover→Define→Develop→Deliver」，跟 stage15-market-fit
  實際拓樸不符，已改成「策略導向市場驗證」「Research→Ideate→Validate→
  Deliver」。

### 已知限制

- **`total_cost_usd` 經 HITL resume 少算的既有問題**（跨 stage9-14 都有，
  見上面各節）這次驗證中再次確認存在——events.jsonl 加總得到 $1.0191，
  CLI 印出的 `total_cost_usd` 是 $1.0116，`build_replay.py` 的成本加總
  警告正確觸發。這是已追蹤的獨立問題，不在這輪處理。
- 競品掃描的 URL 驗證安全機制目前是「全有或全無」：LLM 抽取步驟只要
  沒把搜到的真實頁面整理成結構化條目，就整批落到 fallback，即使
  `research_items` 裡明明有可用的真實競品頁面。之後如果想更常見到真的
  有競品資料的案例，可以考慮放寬抽取 prompt 或多跑一輪重試，但這會
  增加成本跟複雜度，這輪先不處理，維持現有「寧可誠實 fallback 也不要
  硬湊」的保守設計。
