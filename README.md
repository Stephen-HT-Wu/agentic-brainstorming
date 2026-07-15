# Agentic Brainstorming

一個練習專案：讓多個 AI persona 扮演一場腦力激盪會議裡的不同角色——
產品、技術、UX、業務背景各自不同的與會者，各自做功課、訪談模擬用戶、
提案、被結構化評分——看 LangGraph 能把「多個 agent 互相對話、互相扮演」
這件事做到什麼程度。

## 這個專案在練什麼，不在證明什麼

這不是一個號稱「AI 腦力激盪比人類/比直接問 ChatGPT 更厲害」的專案。

真的做過真實對照測試後（見 [`practice/stage12/note.md`](practice/stage12/note.md)
的「誠實的自我檢討」一節），誠實的結論是：一個開好的 Google Sheet 表格
直接叫 Gemini/Claude 生成點子、評分，多樣性跟這整套多 agent pipeline
量出來的結果沒有本質差異。腦力激盪本身是一個沒有「可驗證 ground truth」
的任務——沒有測試會不會過、沒有正確答案能比對——這正是 agentic AI 的
結構化優勢（平行探索、互相檢查、迭代收斂）不容易展現出價值的場景，跟
寫程式或 RAG 查文件那種「可以自動驗證對不對」的任務完全不同。

這個專案真正在練的是：**多 agent 互相扮演角色的機制本身**——persona
怎麼訪談模擬用戶（雙方都是 agent，各自帶著人設跟資訊不對稱）、
Send() 平行 fan-out 怎麼設計才不會踩到 LangGraph 的 join 陷阱、
human-in-the-loop 怎麼跟 checkpointer 搭配續跑、結構化評分怎麼避免
「大家都給差不多分數」。每一個 stage 練一個新概念，`PLAN.md` 是路線圖，
每個 `stageN/note.md` 記錄那一輪真實跑測踩到的坑、學到的東西、有時候
是推翻前一輪設計的真實回饋。

## 值得看的地方：過程本身，不是結論

如果只看最終報告的那個點子好不好，大概率會覺得普普通通——這是誠實的
自我評估後的預期，不是意外。真正有意思的地方是打開即時面板或事後回放頁
看整個過程怎麼發生：

- 一位模擬「健身房會員」被系統研究員追問「你上次真的下定決心處理這件事
  是什麼時候」「那時候考慮過什麼替代方案又為什麼放棄」，一路問到卡關的
  瞬間跟現在滿不滿意——這是 JTBD 的 switch 訪談技巧，訪談對象不知道
  背後是在幫產品做研究
- 三位評審各自只從「顧客需求性」「技術可行性」「商業存續性」單一角度
  評分同一個提案，給出可能互相矛盾的分數，不是一個 agent 打一個綜合分數
- 五位背景完全不同的參與者（各自對應公司實際具備的某種能力，不是隨便
  兩個職稱）各自獨立想出一個提案，彼此從沒看過對方的想法
- 一場會議先假設 3 個候選需求方向、分別訪談驗證，最後只挑一個站得住腳
  的往下走——另外兩個沒被選的方向，訪談證據仍然留在報告裡可以稽核

## 現況：stage13 是目前最新的版本

`practice/stageN/` 每個資料夾都是一份完整獨立副本（不互相 import），
記錄架構演進的軌跡；`stage13` 是目前功能最完整、驗證過的版本，用
Double Diamond（Discover→Define→Develop→Deliver）重新設計了發想流程：
先發散假設多個候選需求方向、訪談驗證後只收斂留一個，再從那一個需求
重新發散出多個提案、結構化評分收斂成一個最終方案。詳細設計與每一輪
真實踩坑記錄見 [`practice/stage13/note.md`](practice/stage13/note.md)。

## 怎麼看

不需要 API key：

```
open demo/sample-run/replay.html
```

`demo/sample-run/` 是用公開的虛構範例設定（`*.example.*`，全部假資料）
真實跑出來的一場會議，回放頁可以逐步播放整個過程、看每一步的完整內容。

需要 API key 才能自己跑一場（`practice/.env.example` 複製成 `.env` 填入
`ANTHROPIC_API_KEY`；`pip install -r practice/requirements.txt`）：

```
cd practice/stage13
python3 -m uvicorn server:app --port 8913
```

打開瀏覽器輸入主題、按「開始腦力激盪」，或用 `python3 graph.py` 走 CLI。

## 隱私

`personas.yaml`／`users.yaml`／`company.md`／`.env`／`outputs/` 全部
gitignore——程式載入時真實檔存在就用真實檔，否則 fallback 到 committed
的 `*.example.*` 虛構版本。這個 repo 是公開的，任何人 clone 下來都能
直接用範例設定跑，不會看到任何人的真實公司/會議資料。
