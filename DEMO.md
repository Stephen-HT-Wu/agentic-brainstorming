# DEMO — 15 分鐘導覽腳本

給不熟悉 agentic AI 的人看，目標是讓他們看完能自己說出「這跟直接問
ChatGPT/Claude 有什麼本質差異」的至少 3 個具體理由，不是只有「感覺比較厲害」。

不需要 API key、不需要架伺服器——`demo/sample-run/` 底下的檔案都是
用公開範例設定（`personas.example.yaml`／`users.example.yaml`／
`company.example.md`，全部虛構）真實跑出來的，直接雙擊開啟即可。

## 開場（1 分鐘）——先講規則，不要先講技術

一句話講完整個練習專案在做什麼：

> 「輸入一個題目，四個不同背景的 AI persona（產品/技術/UX/業務）先各自
> 做功課、訪談模擬用戶、提案，然後互相發表、批判、找大師點評、投票收斂、
> 做出可以點開的原型、再拿去給模擬用戶測試一次——最後產出一份完整報告。
> 這整套流程叫 agentic AI，跟『直接問 ChatGPT 給我一個點子』是完全不同
> 量級的東西，等一下用真的數字給你看差在哪。」

不要在開場解釋 LangGraph／Send fan-out／checkpointer 這些技術細節，
那是給懂技術的人事後問才展開的。

## 第一步：看對比表（3 分鐘）

打開 `demo/sample-run/replay.html`，畫面最上方就是對比表，逐行念過去：

| 維度 | 這張表在說什麼 |
|---|---|
| 真實搜尋依據 | Baseline 可能編造來源、agent 每份提案平均引用近 3 筆真實搜尋結果 |
| BMC 完整度 | 兩邊都填滿，但 agent 是被程式強制驗證過的，不是模型自稱填滿 |
| 點子多樣性 | Baseline 只有一個答案，連「多樣性」這個概念都不存在；agent 有 4 個真的不同的觀點，可以量測 |
| 被批判／測試後改良次數 | Baseline 從沒被挑戰過；agent 的提案被同儕異議、被模擬用戶當面吐槽之後**真的改了**，改了幾次都有數字 |
| 跨場記憶引用 | Baseline 每次都是失憶的新對話；這場會議真的引用了**前一場會議**留下的集體智慧，不是同一次對話裡的東西 |
| 成本 | 誠實揭露：agent 流程貴很多（$0.5-0.7 vs 不到一分錢），這是用結構換來的，不是免費的午餐 |

**關鍵時刻**：跨場記憶那一行——點開報告裡提到的具體提案，可以看到
`memory_refs` 真的指向前一場會議的某個大師點評或某位用戶的訪談洞見，
不是空口說「參考過去經驗」。

## 第二步：播放會議回放（8 分鐘）

往下滑到「會議實況回放」，按播放（可以調到「快速」）。挑幾個時間點暫停講解：

1. **開頭的 `collect`／`recall_memory`**：每個 persona 平行做功課，
   `recall_memory` 那幾筆事件顯示這場會議一開始就在查詢過去的集體智慧
2. **`facilitator_decide` 事件**：點開來看 `extra.reason`——主持人為什麼
   選這個人發言、為什麼有人被「加輪」，這是即時 LLM 判斷，不是寫死的順序
3. **`give_feedback`／`revise_after_feedback` 成對事件**：同儕互評
   （3 認同/3 異議/3 洞見）之後提案真的被改了，`embed_dist` 是這次修改
   幅度的量化證據
4. **`master_critique`**：三位大師（技術/商業/策略）各自的角度點評，
   點出彼此意見一致或分歧的地方
5. **`generate_prototype`／`test_prototype`**：這是整場最有戲劇性的部分——
   模擬用戶看到原型後的真實反應（常常是「聽不懂」「太複雜」），逼提案
   者放棄原本的核心機制、重新設計

## 第三步：點開一個原型（2 分鐘）

`demo/sample-run/prototype-*.html` 三個檔案，挑一個雙擊打開——這是
AI agent 自己生成文案、程式碼渲染成的真實 landing page，不是文字描述。

## 第四步：翻報告（1 分鐘）

`demo/sample-run/final-report.md` 是整場會議的完整記錄：Facilitator
決策軌跡、人類問答、兩輪訪談逐字稿（做功課階段的需求訪談 + 原型測試
階段的概念驗證訪談）、三鏡檢核。這份報告本身就是「這不是黑盒子」的證據
——每一個結論都能回溯到具體的對話或數據。

## 收尾：三個本質差異，逼觀眾自己講一次

問觀眾：「你覺得跟直接問 ChatGPT 差在哪？」引導出（不用照抄，但方向是）：

1. **有真實依據，不是憑空生成**——搜尋結果、訪談逐字稿、跨場記憶都是
   真的資料，可以逐筆追溯，不是模型腦補
2. **會被挑戰、會真的改變**——同儕互評、模擬用戶測試都讓提案「真的變了」
   （有 embedding 距離跟文字 diff 為證），不是一次生成定案
3. **有記憶、會累積**——今天的會議結論會變成明天會議可以檢索的智慧，
   不是每次都從零開始的新對話
4.（進階）**全程可觀測**——每一步的時間、token、成本都被記錄，可以
   算出「這個決定花了多少錢」，不是一個黑盒子

## 給懂技術的人：怎麼重新產生這份樣本

```bash
cd practice/stage10
../.venv/bin/python run_sample_meeting.py          # 第一輪，Chroma 從空的開始
../.venv/bin/python run_sample_meeting_round2.py   # 第二輪，相關主題，展示真實 cross-round recall
../.venv/bin/python build_replay.py demo_workspace/outputs/events.jsonl \
    demo_workspace/outputs/stage9-run-<第二輪的 timestamp>.json \
    demo_workspace/outputs/reports/replay.html
```

整個 demo 樣本用**隔離的工作區**（`practice/stage10/demo_workspace/`）
跑出來，不會碰到你自己在 `personas.yaml`／`users.yaml`／`company.md`
裡的真實設定，也不會寫進你真實在用的 `practice/chroma_db/`。
