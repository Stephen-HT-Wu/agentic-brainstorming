# Agentic Brainstorming — 多 agent 腦力激盪練習計劃

## 目標

用 agentic AI 模擬一場「有準備、有結構、有集體記憶」的正式腦力激盪會議：
輸入一個主題，多個 AI persona 先各自做功課（真實情報搜集）、訪談模擬用戶、
提出含商業模式思考的提案，再互相發表、批判、整合，最終收斂出經過測試的點子與原型。

主要目的是**理解 agentic AI 的可能性**（多 agent 對話、動態路由、human-in-the-loop、
真實 RAG 記憶、agent 自主工具使用），並且**容易 demo 給其他人**看懂 agent 的運作
方法與價值——特別是「與直接問 ChatGPT/Claude 的本質差異」。
不追求一開始就做到完美或商用等級。

（前一個練習專案 `agentic-articles` 已涵蓋：固定 pipeline、條件邊退回重做、
embedding 去重、平行化+壓縮、模型分級、可觀測性。本專案刻意練**沒碰過的**概念。）

## 這個專案練到的新 agentic 概念

- 巢狀子圖（persona 做功課子圖）+ 子圖平行 fan-out（`Send`）
- Agent 對 agent 的角色扮演多輪對話（persona 訪談模擬使用者，雙方都是 agent、各有人設與資訊不對稱）
- Agent 互相回應的多輪共享狀態（發表/聽取/整合，O(N²) 受控）
- Supervisor 動態路由與發言頻寬管理（Facilitator，`Command(goto)`）
- `interrupt()` + checkpointer 的 human-in-the-loop（發表後人類提問迴圈）
- 多 agent 判斷聚合（集體評分、分歧度分析）
- 跨輪/跨 session 的組織級 RAG 記憶（集體智慧庫，真實向量資料庫）
- Agent 自主 tool use（情報搜集用 web search）

## 角色

1. **Persona 與會者 ×3-5（設定檔可改）** — `personas.yaml`：姓名、背景、關注面向、發言風格；另有 `company.md` 描述自家定位供做功課引用
2. **模擬使用者 ×2-3（設定檔可改）** — `users.yaml`：目標用戶輪廓（年齡、情境、痛點、口吻），受訪時只依人設回應、不知道提案背後的商業考量（資訊不對稱）
3. **Facilitator 主持人** — supervisor，動態決定發言順序/加輪/收斂時機，鼓勵均衡發言，在有限頻寬（token 預算）內讓每人充分表達
4. **三名大師** — 技術/商業/策略，對整合後的點子給高階意見
5. **人類與會者（你）** — 每位 agent 發表後可連續提問或跳過
6. **（共同動作，非獨立 agent）** 三鏡檢核（正面/負面/洞見）、集體評分收斂——每個 persona 各自檢視、各自評分再聚合，不是單一 agent 說了算

## Design Thinking 五步驟映射

整個流程對齊 DT 循環（Empathize → Define → Ideate → Prototype → Test）：

| DT 步驟 | 對應流程 | 設計重點 |
|---|---|---|
| **Empathize** | 做功課（市場/技術/競品/自家定位）+ 第一輪需求探索訪談 | 訪談拆兩輪：第一輪不帶點子、只探索痛點/情境/現有解法 |
| **Define** | 每個 persona 寫 POV 陳述（「[用戶] 需要 [需求]，因為 [洞見]」）+ HMW 問句 | 提案必須回應自己的 HMW；互評多一個檢核維度「點子有沒有回答它的 HMW」 |
| **Ideate** | 提案（含 BMC）→ 發表 → 3認同/3異議/3洞見 → Facilitator → 大師 | 腦力激盪核心 |
| **Prototype** | 收斂出 top-K 後，每個點子產出輕量原型：一頁概念書 + mock landing page HTML | 只做 top-K 控成本；demo 亮點——會議開完直接有可點開的原型 |
| **Test** | 第二輪概念驗證訪談：模擬用戶對**原型**給反應 → 最終 refine | 對具體原型的反應比對抽象點子具體得多 |

## 全流程（end state）

```
輸入主題
→ [Empathize|平行 ×N persona] 做功課子圖:
    collect(web search)→ dedup(embedding)→ 彙整 → 寫入個人記憶
→ [Empathize|平行 ×N persona] 第一輪需求探索訪談子圖:
    獨立設計需求訪綱(不帶點子)→ 訪談 2-3 個模擬用戶(各 3-5 輪對話)
    → 萃取需求洞見 → 逐字稿 + 洞見寫入集體智慧庫
→ [Define|每 persona] 寫 POV 陳述 + HMW 問句
→ [Ideate|每 persona] 撰寫提案(回應自己的 HMW,含精簡 BMC 九宮格)
    → 自我修正 ×3(量測每輪 delta)
→ [Ideate] 逐人發表(Facilitator 排序/控頻寬)
    → 每人發表後 interrupt():人類提問 ×多次(agent 回答)或跳過
    → 其他人聽取/記憶 → 給 3認同/3異議/3洞見(含「是否回答了 HMW」)→ 整合
→ [Ideate] 三大師點評
→ 集體智慧寫入向量庫(跨輪可檢索;第二輪起會先 recall)
→ 集體評分收斂 → top-K
→ [Prototype] top-K 各生成輕量原型:一頁概念書 + mock landing page HTML
→ [Test] 第二輪概念驗證訪談:模擬用戶對原型給反應 → 最終 refine
    → 逐字稿 + 洞見寫入集體智慧庫
→ 三鏡檢核(正面/負面/洞見,全員共同動作)
→ 最終報告(markdown:POV/HMW、點子、BMC、依據、辯論摘要、人類問答、
              大師意見、原型連結、測試反應、三鏡檢核、得分)
```

### 關鍵協議設計

- **提案含 BMC 九宮格**（客群、價值主張、通路、顧客關係、收益流、關鍵資源、關鍵活動、關鍵夥伴、成本結構），精簡形式，九格齊全是結構不變量
- **3認同/3異議/3洞見**：互評的結構化格式，每項恰好 3 個（可程式驗證的不變量）
- **三鏡檢核**（正面/負面/洞見）：與互評同一套三分法協議，格式一致、驗證程式可共用；只施加在 top-K（K=3-5）
- **三輪自我修正**：保留但**量測每輪改善幅度**（版本間 embedding 距離 + 自評分數），用數字回答「第三輪值不值得」
- **模擬訪談的定位要誠實**：agent 扮演的用戶不是真實用戶，產出是「假設」不是「驗證」；練習價值在 agent 對 agent 的角色扮演對話模式與強制換位思考

## Demo 與價值展示（一等公民，不是事後補）

回答「這跟直接問 ChatGPT/Claude 有什麼本質差異」，三個機制：

1. **Baseline 對照組**：同一主題用一次單純 LLM 呼叫產生點子（即「直接問 Claude」），與 agent 會議產出用**同一套指標**並排比較——真實依據引用數、BMC 完整度、點子多樣性、被批判後改良次數、跨場記憶引用、成本。本質差異用數字與內容呈現：有據 vs 憑記憶、多視角迭代 vs 一次取樣、可介入 vs 被動、有累積記憶 vs 無狀態、全程可觀測 vs 黑盒
2. **會議事件流 + HTML 回放器**：從 stage 1 起每個節點輸出結構化事件到 `outputs/events.jsonl`（時間、角色、動作、內容摘要、token/成本）；最終做一個零依賴單檔 HTML 回放器，像會議實況般逐步播放：誰在做功課（搜了什麼）、誰發表、3/3/3 互評的對應關係、Facilitator 決策理由、人類提問、成本累計條
3. **`DEMO.md` 導覽腳本**：15 分鐘 demo 流程（先看 baseline → 播放會議回放 → 看對比表），標注關鍵時刻：某點子被異議後真的改良、人類提問改變走向、第二輪引用前場集體智慧。demo 敘事＝「agents 跑完一整個 Design Thinking 循環」

## 可觀測性（沿用 agentic-articles 的 instrument/token/cost 模式，新增）

- **事件流 `outputs/events.jsonl`（stage 1 起所有節點必寫）**：時間、角色、動作、內容摘要、token/成本——同時是 demo 回放器與 baseline 對比的資料來源
- 每輪自我修正的改善 delta（embedding 距離 + 自評分）
- 訪談記錄：每場輪數、萃取洞見數、refine 前後提案 delta、日後被 recall 的次數
- 點子多樣性（平均兩兩 embedding 距離）、獨特點子數、USD/獨特點子
- Facilitator 決策 log（誰發言、為何、頻寬用量）
- 評分分歧度（persona 間標準差）
- 第二輪 brainstorming 的集體智慧 recall 命中數
- 人類問答記錄（問了誰、幾題、答覆是否被後續互評引用）

## 技術選型

- **LangGraph**：StateGraph、子圖、`Send`（fan-out）、`Command(goto)`、`interrupt()`、MemorySaver/SqliteSaver
- **Claude API 模型分級**：haiku（persona 做功課/訪談/互評/評分）、sonnet（Facilitator/大師/最終報告）
- **Web search**：Tavily（免費額度）或 DuckDuckGo，無爬蟲、無 ToS 疑慮
- **真正的 RAG 堆疊**：向量庫用 **Chroma**（本地持久化、免伺服器、支援 metadata 過濾）
  - collections：`wisdom`（整場結論/大師意見/三鏡檢核）、`interviews`（訪談逐字稿+萃取洞見）、`research`（做功課的彙整素材）
  - 每筆帶 metadata（round_id、topic、persona、date、doc_type），recall 時可用過濾器（例：只查訪談洞見）
  - 檢索流程是完整 RAG：embedding 寫入 → 相似度檢索 + metadata 過濾 → 取回原文塞進 prompt 並要求引用來源
  - 聚類/去重也在 Chroma 上做（查詢相鄰向量），不是記憶體內兩兩比對
- `_common.py` 模式（instrument/PRICING/cost_of）從 agentic-articles 重新抄寫，不跨 repo import
- 每 stage 結束打一個 git tag（`stage-1-homework` 等）

## 隱私與公開 repo 策略（本 repo 會公開在 GitHub）

- **設定檔雙軌制**：committed 的只有 `*.example.yaml` / `company.example.md`（虛構示範公司與人設）；真實設定（`personas.yaml`、`users.yaml`、`company.md`）一律 gitignore，程式載入時「真實檔存在就用真實檔，否則 fallback 到 example」——repo 對外人 clone 下來就能跑，又不洩漏真實公司資訊
- **記憶與產出一律不進 git**：`chroma_db/`（向量庫）、`*.sqlite`（checkpointer）、`outputs/`（逐字稿、events.jsonl、報告）全部 gitignore——這些都含會議內容
- **Demo 用的公開樣本**：用 example 虛構設定跑一場完整會議，把該場的 `events.jsonl` + 回放 HTML + 報告放進 `demo/sample-run/`（唯一允許 commit 的產出，內容全虛構），外人不需要 API key 就能看回放
- API key 全部走 `.env`（gitignore）；commit 前用 `git status` 檢查沒有真實設定/產出混入

## 已知風險（練習階段皆低）

- **公開 repo 的隱私洩漏是本專案最主要風險**：真實公司定位、人設、會議內容、向量庫都可能含敏感資訊 → 雙軌設定 + 全產出 gitignore 從第一個 commit 就生效，不是事後補救
- **成本**：end state 完整跑一輪估 80-150 次 LLM 呼叫、約 $1-2（多為 haiku）→ 模型分級 + 回合/token 預算上限，且分 stage 逐步搭建，不一次寫完整條
- **動態路由的成本較難預估**（Facilitator 可自由加輪）→ 一開始就內建預算上限，超過強制收斂
- 無爬蟲、無自動發布；search 走官方 API

## 分階段練習路線圖

| 階段 | 練的新概念 | 內容 | 驗收標準 |
|---|---|---|---|
| 0. 骨架 | checkpointer 基礎 | 最小 StateGraph + MemorySaver，同 thread 連續 invoke，state 延續 | 跑通，看懂 thread/checkpoint |
| 1. 單一 persona 做功課子圖 + baseline | 子圖、tool use（web search）、自我修正迴圈 | 一個 persona 完整跑「collect→dedup→彙整→撰寫提案（含 BMC）→自我修正×3」，量測每輪 delta；**同場加跑一次「直接問 LLM」baseline** 存檔備比；節點開始寫 `events.jsonl` | 提案引用真實搜尋結果、BMC 九格齊全（結構不變量）；三輪 delta 數字能回答「第三輪值不值得」；baseline 產出與指標已存檔 |
| 2. 平行多 persona | 子圖 fan-out（`Send`）、persona 設定檔 | `personas.yaml` 定義 3-4 人，平行做功課；多樣性指標 | 改設定檔就能換人設；平行比循序省時；提案彼此明顯不同 |
| 3. 需求探索訪談 + Define | agent 對 agent 角色扮演多輪對話 | `users.yaml` 定義 2-3 個模擬用戶；每個 persona 獨立設計需求訪綱（不帶點子）→訪談（各 3-5 輪）→萃取需求洞見→寫 POV + HMW→據此修正提案；逐字稿+洞見存檔 | 訪綱彼此不同；每個提案都有明確對應的 POV/HMW；提案有可歸因到訪談洞見的內容；逐字稿完整記錄 |
| 4. 發表與互評 | 多輪共享狀態、結構化互評 | 輪流發表，每人對他人給 3認同/3異議/3洞見（含「是否回答 HMW」檢核），整合成點子池 | 每則互評恰好 3/3/3；點子池有版本歷史（被誰的異議改良） |
| 5. HITL 提問 | `interrupt()` + SqliteSaver 續跑 | 每人發表後暫停：人類可連續提問（agent 回答）或跳過；支援中斷後重開 process 續跑 | 提問後 agent 的回答進入共享狀態、被後續互評引用；跳過不影響流程；跨 session 續跑成功 |
| 6. Facilitator 動態路由 | supervisor + `Command(goto)` + 頻寬預算 | 主持人決定發言順序/是否加輪/何時收斂；token 與回合預算上限 | 兩個不同主題跑出不同路由軌跡（決策 log）；超預算強制收斂 |
| 7. 大師點評 + 集體智慧庫 | 跨輪 RAG 記憶（真實向量庫） | 三大師點評；整場結論**與 stage3 的訪談逐字稿/洞見**一起嵌入 **Chroma**（含 metadata）；第二輪先 recall 前輪智慧（相似度 + metadata 過濾，回答須引用來源） | 跑兩輪不同主題，第二輪能引用第一輪的相關結論**與用戶訪談反應**（recall 命中數 > 0），且引用可追溯到向量庫的原始文件 |
| 8. 收斂 + Prototype + Test | 多 agent 判斷聚合、原型生成、概念驗證訪談 | 集體評分→top-K→每個 top 點子生成一頁概念書 + mock landing page HTML→第二輪概念驗證訪談（模擬用戶對原型反應）→最終 refine | 評分聚合有分歧度數字；原型可直接開啟；測試訪談的反應真的改變了最終版本（可 diff） |
| 9. 三鏡檢核 + 最終報告 | 全員共同檢核 | 三鏡檢核（正/負/洞見，全員）→最終報告（含 POV/HMW、原型、測試反應、完整歷程） | 三鏡檢核格式不變量通過；報告完整含人類問答與兩輪訪談記錄 |
| 10. Demo 層 | 價值展示與對照實驗 | 單檔 HTML 回放器（讀 `events.jsonl` 播放會議實況）+ baseline vs 會議的 side-by-side 對比報告 + `DEMO.md` 導覽腳本 | 不懂技術的人看 15 分鐘 demo 能說出 agent 與「直接問 ChatGPT」的至少 3 個本質差異；回放器零依賴、雙擊即開 |

**建議**：每個階段結束開一個 git commit/tag（例如 `stage-1-homework`），方便回頭比較架構演進的差異。
