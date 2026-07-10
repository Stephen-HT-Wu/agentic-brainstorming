# Stage 2 Note — 平行多 persona（Send fan-out）

## 目標（對照 `PLAN.md`）

- `personas.yaml` 定義 3-4 人，**全部平行**做功課（stage 1 的做功課子圖原封不動重用）
- 量化三件事：改設定檔就能換人設、平行比循序省時、提案彼此明顯不同

對應程式：`practice/stage2/graph.py`（stage 1 的完整獨立副本再擴充，不 import stage1）

## 跟 stage 1 的差異

只有「父圖」不同：stage 1 是一個 `homework` 節點 `invoke` 一次子圖；
這裡改成一個 routing 函式 `fan_out_personas` 依 `state["personas"]`
（長度＝設定檔人數，**不寫死**）產生一份 `Send` 清單，同一個 super-step
內平行排程到 `homework_worker` 節點。做功課子圖本身（collect→dedup→
synthesize→draft→refine×3）逐行相同。

## 你會學到什麼

1. **`Send` fan-out**：routing 函式回傳 `[Send(node, payload), ...]`，
   LangGraph 對同一個 super-step 裡的多個 `Send` 目標平行排程；I/O-bound
   的 `web_search`/`call_llm` 會釋放 GIL，**同步 `invoke()` 也真的並發**
   （不需要 async/await）——這是跟 agentic-articles 的平行化階段用
   `asyncio.gather` 不同的路線，LangGraph 原生支援
2. **reducer 在 map-reduce 的用法**：`persona_results: Annotated[List[dict], operator.add]`
   讓 N 個 worker 各自的回傳值自動累加合併，不用手動處理鎖
3. **平行執行下的可觀測性要重新設計**：`_common.py` 的 `ContextVar` 化
   （上一輪的 hardening commit）加上 stage2 新增的 `role` 標籤，是這裡
   能正確拆分「每個 persona 各花多少成本」的關鍵——多個執行緒交錯寫入
   同一個 `usage_log`，只能靠 context 傳遞的身份標籤事後歸屬，不能靠
   「哪個時間點呼叫」判斷
4. **多樣性量測的坑**：`proposal_text_for_embed` 原本把整包 `json.dumps(bmc)`
   （含九個逐字相同的鍵名）餵進去，稀釋了真正的內容差異訊號，見下方
   「踩到的坑」

## 踩到的坑：BMC 鍵名污染相似度

第一次真實跑測後，4 份提案的兩兩距離全部擠在 0.25-0.29 之間，且用
`IDEA_DEDUP_THRESHOLD=0.75` 一量竟然有 2 份被判定成「重複」——但 4 個
標題（情感驅動+廣告主績效掛鉤 / 垂直頻道×完播激發 / 數據對比視覺 /
搜尋關鍵字化+標準化）明顯是不同方向。

根因：`proposal_text_for_embed` 把 `json.dumps(bmc, sort_keys=True)`
整包塞進嵌入文字，而 BMC 的九個鍵名（客群、價值主張…）在**每一份**
提案裡都逐字相同——這些重複鍵名佔掉不少 token，把任何兩份提案的
cosine 相似度往上拉。修正：只取 `bmc.values()`，不含鍵名。

修正前後用**同一批已存檔的真實提案**（零額外 API 成本）重算比對：

| | 平均兩兩距離 | 去重後獨特提案數 |
|---|---|---|
| 修正前（含鍵名） | 0.2685 | 2 / 4 |
| 修正後（只取值） | 0.2885 | **4 / 4** |

確認是真的 bug 不是雜訊後，才花真錢重新完整跑一次（見下方數字），
不是為了湊出「4 個都不同」而調閾值——閾值 `0.75` 全程沒動。

## 怎麼跑

```bash
cd practice
.venv/bin/python stage2/graph.py          # 真實跑一次（約 $0.25，4 persona + baseline）
.venv/bin/python -m unittest stage2/test_graph.py -v   # 純邏輯測試，零成本
```

可選環境變數：`BRAINSTORM_TOPIC=你的主題`；改人數／人設直接編輯
`personas.yaml`（不存在就 fallback 到 `personas.example.yaml`）。

## 實際觀察（2026-07-11，修正 embedding bug 後的乾淨跑測）

主題「如何提升新聞短影音互動率」，4 位 persona（林美華/陳建宏/周若琪/王承翰）：

| Persona | BMC | self_score | 耗時 | 成本 |
|---|---|---|---|---|
| 林美華 | 9/9 | 9.0 | 88.0s | $0.0611 |
| 陳建宏 | 9/9 | 9.0 | 94.2s | $0.0623 |
| 周若琪 | 9/9 | 9.0 | 95.2s | $0.0643 |
| 王承翰 | 9/9 | 9.0 | 80.4s | $0.0555 |

**平行比循序省時**：

- 平行 wall-clock：95.23s
- 循序估計（4 人耗時加總）：357.83s
- **加速倍率：3.76x**（4 人平行只比單人稍慢一點，接近理想的「近乎同時完成」）

**提案彼此明顯不同**：

- 兩兩平均 embedding 距離：0.3418（範圍 0.296-0.383，比修正前的 0.25-0.29 明顯打開）
- 去重後獨特提案數：**4 / 4**（沒有任何一對被判定重複）
- USD / 獨特提案：$0.0608

**改設定檔就能換人設**：`personas.yaml` 目前 4 人來自 `personas.example.yaml`；
程式碼裡沒有任何寫死人數或人名的地方，`load_personas()` 回傳整份清單、
`fan_out_personas` 依清單長度動態 `Send`。

## 驗收（對照 PLAN.md 階段 2）

- [x] 改設定檔就能換人設（`len(personas.yaml)` 動態決定，程式無寫死）
- [x] 平行比循序省時（3.76x，硬驗收要求 `speedup_x > 1.0`）
- [x] 提案彼此明顯不同（4/4 去重後仍獨立，兩兩距離 0.30-0.38）
- [x] `outputs/stage2-*.json`、`outputs/events.jsonl` 皆已存檔
