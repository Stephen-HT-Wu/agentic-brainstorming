# Stage 8 Note — 收斂 + Prototype + Test（多 agent 判斷聚合、原型生成、概念驗證訪談）

## 目標（對照 `PLAN.md`）

- 全體 persona 交叉評分彼此的最終提案（多 agent 判斷聚合），選出 top-K
- 每個 top 點子產出一頁概念書 + 可直接開啟的 mock landing page HTML（Prototype）
- 對模擬使用者做第二輪概念驗證訪談，依真實反應做最終修正（Test），修正前後可 diff

對應程式：`practice/stage8/graph.py`（stage 7 的完整獨立副本再擴充，不 import stage7）

## 架構

做功課子圖、同儕互評子圖、Facilitator、HITL、大師點評、Chroma 集體智慧
逐行沿用 stage7。新增的部分全部接在 `write_wisdom` 之後：

```
write_wisdom
  → run_collective_scoring（新，scoring_panel_graph：N×(N-1) 交叉評分 Send fan-out）
      算出每份提案的 mean/stdev，選 top-K
  → run_prototype_test（新，prototype_test_graph：K 個 Send fan-out）
      每個 top-K 點子：生成 landing page 文案 → 程式碼渲染成 .html
      → 對模擬使用者做概念驗證訪談 → 依反應做最終 refine（可 diff）
  → END
```

## 你會學到什麼

1. **多 agent 判斷聚合要看分歧度，不是只看平均分**：`compute_score_aggregates`
   算的不只是 mean，還有 stdev——這才是「這是全體共識還是有人力排眾議」
   的量化依據。真實跑測就看到有意思的對比：三份提案的評分者剛好都給
   一致分數（stdev=0.0），只有一份（王承翰）評分者之間真的有分歧
   （stdev=0.471）
2. **Guardrail 放程式碼，創意內容交給模型**：`render_landing_page_html`
   是固定的 Python 模板，模型只負責填 JSON 欄位（headline/features/
   concept），版面、CSS、`html.escape()` 全部是程式碼決定——這保證輸出
   永遠是合法、可直接開啟的靜態 HTML，不管模型這次心情好不好
3. **Test 階段的模擬使用者要夠『笨拙』才有意義**：訪談 prompt 沒有特別
   要求使用者裝懂，他們對「eCPM 溢價」「SLA」這種產品/商業術語的
   真實反應是「聽不懂」——這正是把原型丟給真實用戶會發生的事，也是
   這個測試步驟存在的理由：逼 persona 用使用者聽得懂的語言重新框架
   點子，不是自己關起門來想

## 怎麼跑

```bash
cd practice
.venv/bin/python -m unittest stage8/test_graph.py -v   # 13 個測試，零成本
                                                          # （含 XSS 逃逸檢查、
                                                          #   完整 mock 流程測試）

BRAINSTORM_TOPIC="你的主題" .venv/bin/python stage8/graph.py --thread demo --script /tmp/skip_all.json
# 原型輸出在 outputs/prototypes/{round_id}-{persona_id}.html，可直接雙擊開啟
```

## 這次沒有踩坑（值得記錄一下為什麼）

跟 stage7 那趟三個真實 bug 的旅程不同，stage8 第一次真實跑測就完整
通過驗收，沒有崩潰、沒有卡死。回頭看差異：

1. **`anthropic.Anthropic(timeout=90.0)` 從一開始就寫進去**——不是像
   stage7 那樣先踩到 3 小時網路卡死才加上去，這次是把 stage7 學到的
   教訓直接沿用到新檔案的第一版
2. **`FACILITATOR_MODEL` 的呼叫（`facilitator_decide`／`master_critique`）
   `max_tokens` 從一開始就開到 2000**——同樣是把 stage7 的 extended
   thinking 坑直接繞過，不是事後補
3. **13 個單元測試涵蓋了所有新邏輯（含一次 XSS 逃逸檢查）才動真錢**——
   尤其是 `generate_prototype_and_test` 那個完整流程的 mock 測試，先
   在零成本環境把「JSON 解析→HTML 渲染→測試訪談→最終 refine→diff」
   整條路徑跑過一次，抓到的唯一問題是我自己測試 fixture 給的分數沒有
   變異度（不是程式邏輯的 bug）

這是這幾個階段下來一個具體的正向回饋：把前幾階段真實踩過的坑往前搬到
「新檔案的第一版就寫對」，加上更完整的單元測試覆蓋，確實讓這次真實
花錢的跑測一次就過。

## 實際觀察（2026-07-11，真實跑測，主題「如何提升新聞短影音互動率」）

**集體評分聚合**（4 位 persona 交叉評分，各收到 3 筆評分）：

| Persona | mean | stdev（分歧度） | Top-K |
|---|---|---|---|
| 陳建宏 | 7.0 | 0.0 | ★ |
| 林美華 | 6.0 | 0.0 | ★ |
| 周若琪 | 6.0 | 0.0 | ★ |
| 王承翰 | 4.333 | **0.471** | |

王承翰是唯一評分者意見不一致的提案（真實分歧度數字），也是唯一沒進
top-3 的——多 agent 判斷聚合在這裡展示的正是「排名不是靠感覺，是有
可以拿出來看的數字」。

**Prototype + Test（top-3）**，每個都真的寫出可雙擊開啟的 HTML：

- `outputs/prototypes/s8round1-alex.html`（陳建宏）
- `outputs/prototypes/s8round1-mei.html`（林美華）
- `outputs/prototypes/s8round1-joyce.html`（周若琪）

**一個具體的「測試反應真的改變版本」案例**（林美華，embed_dist=0.2933，
三者中最大）：原始提案的核心機制是「漸進式驗證徽章（交易驗證→朋友圈
信號→編輯背書）」，模擬使用者陳小姐的第一反應：

> 「說實在話，我根本不會點那個驗證徽章啦，就是多一個動作而已。」

林美華的最終修正**整個放棄了驗證徽章這個核心機制**，改成「編輯直接
背書於短影音內（語音/圖卡）」——用 `_diff_proposals` 產出的 unified
diff 可以看到 title、summary、BMC 九格全部因應這個轉向而改寫，不是
換個說法而已，是真的改變了產品方向。三份 top-K 提案的 `embedding_distance`
分別是 0.1613／0.2933／0.1704，全部 > 0，且都有非空的文字 diff。

**總成本**：$0.7463（延續 stage7 的做功課＋互評＋大師＋Chroma 部分約
$0.5-0.6，新增的評分聚合 `score_proposal`(16 次) + 原型生成/測試/refine
`generate_prototype_and_test`(top-3 × ~6 次) 合計約 $0.11）。

## 驗收（對照 PLAN.md 階段 8）

- [x] 評分聚合有分歧度數字（stdev 逐筆列出，王承翰 0.471 vs 其他人 0.0
      的真實對比）
- [x] 原型可直接開啟（3 個 `.html` 檔案真的寫到磁碟，`Path.exists()`
      與檔案大小 > 0 都驗證過）
- [x] 測試訪談的反應真的改變了最終版本（3/3 top-K 的 `embedding_distance
      > 0` 且 `diff_text` 非空；林美華案例更是整個核心機制被推翻重寫）
