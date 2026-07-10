# Stage 1 Note — 單一 persona 做功課子圖 + baseline

## 目標（對照 `PLAN.md`）

- 一個 persona 完整跑：`collect → dedup → 彙整 → 提案(BMC) → 自我修正×3`
- 量測每輪 **delta**（embedding 距離 + 自評分）
- 同場加跑一次「直接問 LLM」**baseline** 存檔備比
- 節點開始寫 `outputs/events.jsonl`

對應程式：`practice/stage1/graph.py`

## 你會學到什麼

1. **巢狀子圖**：`homework_graph` 是獨立編譯的做功課子圖；父圖 `meeting_graph`
   用一個 `homework` 節點 `invoke` 它——邊界清楚，之後 stage 2 才能對 N 個
   persona `Send` 同一份子圖
2. **Agent tool use**：`collect` 真的打 web search（DuckDuckGo；有
   `TAVILY_API_KEY` 則改走 Tavily），提案必須引用回傳的 URL
3. **embedding 去重**：搜尋結果用中文 2/3-gram feature-hashing cosine 去重
   （不花 token；避免把整段連續中文誤當成單一 token）；
   之後 stage 7 再升級成 Chroma
4. **BMC 結構不變量**：九格缺一不可，程式用 `assert_bmc_complete` 驗證
5. **自我修正有數字**：每輪記 `embedding_distance` 與 `self_score_delta`，
   用 `judge_third_round()` 回答「第三輪值不值得」
6. **Baseline 對照**：同一主題一次 LLM 呼叫，用同一套指標（真實引用數、
   BMC 完整度、成本）並排——這是 demo「跟直接問 ChatGPT 差在哪」的資料來源
7. **事件流**：每個 node invocation 有獨立 context，事件只記該次 invocation
   的 token／成本；巢狀子圖返回後會恢復父節點 context。每個動作 append 一筆到
   `outputs/events.jsonl`（之後 stage 10 回放器會讀它）

## 設定檔雙軌

| 真實（gitignore） | 公開示範（commit） |
|---|---|
| `personas.yaml` | `personas.example.yaml` |
| `company.md` | `company.example.md` |
| `.env` | `.env.example` |

程式：真實檔存在就用真實檔，否則 fallback 到 example。

## 怎麼跑

```bash
cd practice
cp .env.example .env   # 填入 ANTHROPIC_API_KEY
.venv/bin/pip install -r requirements.txt
.venv/bin/python stage1/graph.py
```

可選環境變數：`BRAINSTORM_TOPIC=你的主題`

## 產出（皆在 gitignore 的 `outputs/`）

- `events.jsonl` — 本場事件流
- `stage1-run-*.json` / `stage1-latest.json` — agent 提案、versions、deltas、baseline、對照指標

## 實際觀察（2026-07-10）

主題「如何提升新聞短影音互動率」、persona 林美華：

| 指標 | Agent（做功課子圖） | Baseline（直接問） |
|---|---|---|
| 真實搜尋引用 | 3 | 0 |
| BMC | 9/9 | 9/9 |
| 成本 | ~$0.07 | ~$0.005 |

自我修正 delta：

- round 1：embed_dist=0.50，score 7→8（+1.0）
- round 2：embed_dist=0.55，score 8→8.5（+0.5）
- round 3：embed_dist=0.39，score 8.5→9（+0.5）→ **仍有可見改善，值得保留**

本質差異已用數字呈現：agent 有真實 URL 依據；baseline 九格也能編出來，但 `real_citations=0`。

## 驗收（對照 PLAN.md 階段 1）

- [x] 提案引用真實搜尋結果（`real_citations >= 1`）
- [x] BMC 九格齊全
- [x] 三輪 delta 印出，並有第三輪判斷句
- [x] baseline 與指標已寫入 `outputs/stage1-*.json`
- [x] `outputs/events.jsonl` 有節點事件
