# Stage 6 Note — Facilitator 動態路由（supervisor + `Command(goto=...)` + 頻寬預算）

## 目標（對照 `PLAN.md`）

- 主持人（Facilitator）動態決定發言順序、是否讓某人「加輪」、何時收斂
- 有 token／回合預算上限，不完全依賴 LLM 自律
- 兩個不同主題跑出不同的路由軌跡（決策 log 可比對）
- 超預算強制收斂

對應程式：`practice/stage6/graph.py`（stage 5 的完整獨立副本再擴充，不 import stage5）

## 架構關鍵：路由決策本身就是一次 LLM 判斷，不是純函式

stage1-5 的路由（`route_after_refine`／`route_presenter`／`route_after_question`）
都是純 Python 函式：讀 state 裡的計數器，回傳一個固定字串。stage6 的
`facilitator_decide` 不同——它**先呼叫一次 LLM** 判斷「接下來該找誰、
還是該結束」，再把這個判斷轉成路由決定。LangGraph 對應的原語是
`Command(goto=...)`：節點函式直接回傳 `Command(goto="ask_question", update={...})`
或 `Command(goto=END, update={...})`，同時完成「更新 state」跟「決定下一步」
兩件事，不需要（也不能同時）再用 `add_conditional_edges` 幫它配路由函式。

```
[Send fan-out ×N persona] homework_worker
  → facilitator_decide（新，唯一用 Command(goto=...) 的節點）
      硬上限觸發 → END（不問 LLM，直接短路）
      LLM 判斷「present」→ ask_question（HITL，沿用 stage5）
          → run_peer_review（沿用 stage5）→ 回 facilitator_decide
      LLM 判斷「end」→ END
```

`ask_question`/`answer_question`/`run_peer_review` 三個節點逐行沿用
stage5，只是改讀 Facilitator 動態指定的 `next_presenter_id`，不是固定的
`presenter_index`——這讓「加輪」變得可能：同一個人可以在會議中被
`facilitator_decide` 選中不只一次。

## 你會學到什麼

1. **`Command(goto=...)` 不需要額外的邊宣告**：先用一個三行邏輯的 toy
   `StateGraph` 驗證過，`facilitator_decide` 節點完全沒有 `add_edge`／
   `add_conditional_edges` 幫它接下一步，`compile()` 照樣成功，執行時
   路由完全照 `Command` 指定走
2. **硬上限要在呼叫 LLM 之前擋掉，不是之後**：`facilitator_decide` 一開頭
   就檢查 `presented_so_far >= MAX_ROUNDS or budget_used > MAX_BUDGET_USD`，
   超過直接回傳 `Command(goto=END)`，**連 LLM 都不呼叫**——省下最後一次
   不必要的花費，也保證這個安全閥不會因為 LLM 輸出異常而失效
3. **「加輪」靠讀取歷史記錄實現，不是額外的狀態機**：`_proposal_for_persona`
   優先找 `idea_pool_versions` 裡這個人最新的版本，找不到才退回做功課
   階段的原始草稿——同一個人被 Facilitator 選中兩次，第二次看到的是
   自己第一次修正後的版本，討論才能真的往前推進，不是原地打轉

## 踩到的坑（兩個，都是真實跑測抓到的）

### 坑一：Facilitator 判斷對了，但被自己的保底邏輯誤判成「id 無效」

第一次真實跑測（主題 B），第 5 輪 Facilitator 的 `reason` 寫得清清楚楚：
「林美華提案有多項未解異議尚待其回應，應加輪釐清後再收斂」——判斷完全
正確，**但最終 `action` 卻是 `end`，`forced: true`**。查證後端事件：
模型回傳的 `persona_id` 不是 `"mei"`，而是寫成了姓名或其他不符合 id
格式的字串，`chosen_id not in valid_ids` 判斷為真，程式的保底邏輯直接
把整個「加輪」意圖丟掉、強制結束會議——這跟 stage4 踩過的
`addressed_reviewer_ids` 姓名/id 混填是同一種坑，只是這次後果更嚴重：
不只是報表數字不準，是**會議真的少開了一輪它該開的討論**。

修正：跟 stage4 同一招，建 `name_to_id` 對照表，`persona_id` 不管是
id 還是姓名都能正確解析。

### 坑二：`.get(...).strip()` 假設值一定是字串，模型偶爾吐出 list

修正坑一後重新真實跑測，換一個地方崩潰：`write_pov_hmw` 對
`data.get("pov")` 呼叫 `.strip()` 時噴 `AttributeError: 'list' object
has no attribute 'strip'`——模型這次把 `pov` 欄位吐成了一個 list，不是
預期的字串。`extract_json_object` 只保證頂層是 dict，不保證每個值的
型別；`(x.get(...) or "").strip()` 這個全專案常見的寫法對非字串值一律
會炸。統一加了 `_safe_str()` 這個小工具函式（非字串一律當空字串，交給
既有的『結構性保底』邏輯補上預設值），套用到 `pov`/`hmw`/`hmw_response`/
`revision_note`/`hmw_addressed_reason`/`reason`/`persona_id` 等七處。

**這次崩潰也連帶暴露一個值得記錄的事實**：即使 stage6 有真的接上
`SqliteSaver`，這次崩潰仍然讓另外 3 位已經跑完整輪 `refine` 的 persona
的工作全部作廢——因為 `homework_worker` 是 `Send` fan-out 出去的平行
任務，LangGraph 的 checkpoint 只在**整個 super-step 的所有平行任務都
成功**之後才前進；有 checkpointer 不代表「部分成功」也會被存下來，
它保護的是「已經完整跑完的 super-step」，不是「跑到一半的平行任務」。
這跟 stage5 展示的『中斷後不用重花錢』並不衝突（那邊是刻意設計
`ask_question` 單獨成一個輕量節點來規避這個限制），但提醒了『有
checkpointer』跟『任何時候崩潰都不會浪費錢』是兩件不同的事。

兩個坑都補了對應的回歸測試（不需要真的花錢就能重現），修正後才重新
花真錢完整跑一次主題 B。

## 怎麼跑

```bash
cd practice
.venv/bin/python -m unittest stage6/test_graph.py -v   # 15 個純邏輯測試，零成本
                                                          # （含『病態 Facilitator 永遠選同一人』
                                                          #   的壓力測試，證明硬上限真的擋得住）

BRAINSTORM_TOPIC="主題A" .venv/bin/python stage6/graph.py --thread topicA --script /tmp/skip_all.json
BRAINSTORM_TOPIC="主題B" .venv/bin/python stage6/graph.py --thread topicB --script /tmp/skip_all.json
# /tmp/skip_all.json 內容是 {}（HITL 提問機制沿用 stage5 已驗證過，
# 本階段驗證重點在 Facilitator，用全跳過腳本降低變因跟成本）
```

## 實際觀察（2026-07-11，兩個真實主題的完整跑測）

**主題 A**「如何提升新聞短影音互動率」：

```
第1輪：present 林美華（強制）— 尚無人發言，依序讓林美華先發表
第2輪：present 陳建宏（強制）— 尚有三人未發言，優先讓未發表者輪流
第3輪：present 周若琪（強制）— 尚有兩人未發言，優先讓未發表者輪流
第4輪：present 王承翰（強制）— 王承翰尚未發言，需優先讓其發表
第5輪：end            — 四人皆已發表且異議充分表達，無人明顯需加輪，可收斂
```

4 次發表、無加輪、成本 $0.5156。

**主題 B**「公司要不要導入 AI 自動生成新聞內容」：

```
第1輪：present 林美華 — 第1輪，尚無人發言，依序讓林美華先發表
第2輪：present 陳建宏 — 尚有三人未發言，依序讓陳建宏發表
第3輪：present 周若琪 — 尚有兩人未發言，優先讓周若琪發表
第4輪：present 王承翰 — 王承翰尚未發言，須確保每人至少發表一次
第5輪：present 陳建宏 — 陳建宏提案異議最多且核心（成本、演算法風險未解），需加輪釐清
第6輪：end            — 四人皆已發言且陳建宏已加輪回應，異議已充分討論，預算有限應收斂
```

5 次發表（陳建宏加輪一次，**真的、非強制的判斷**——`forced: false`）、
成本 $0.4924。

**兩條軌跡確實不同**：前 4 輪都是「均衡發言」的硬性規則在主導（兩個主題
一樣，因為這條規則不看內容），差異出現在第 5 輪之後——主題 A 的四份
提案異議都被充分回應，Facilitator 直接收斂；主題 B（一個更容易引發
路線分歧的主題：AI 自動生成內容牽涉信任/成本/風險，四個角色天然容易
對立）第 5 輪判斷陳建宏的技術可行性異議還沒解決，主動加開一輪。這正是
「Facilitator 動態路由」要展示的東西：路由軌跡不是寫死的，是根據討論
內容真的長出不同形狀。

**硬上限**：兩場都沒真的觸發 `MAX_ROUNDS=6`／`MAX_BUDGET_USD=1.2`
（最高只到 5-6 輪、花費 $0.49-0.52），但單元測試裡的「病態 Facilitator」
壓力測試（一個永遠只選同一人的假 LLM）證實硬上限真的能在恰好
`MAX_ROUNDS` 次發表後強制收斂，不會因為 LLM 不配合而失控。

## 驗收（對照 PLAN.md 階段 6）

- [x] 主持人決定發言順序／是否加輪／何時收斂（主題 B 第 5 輪是真實、
      非強制的加輪決策；兩場的第 6+ 步都由 LLM 自行判斷收斂，不是撞到
      硬上限）
- [x] token 與回合預算上限（`MAX_ROUNDS`／`MAX_BUDGET_USD`，在呼叫 LLM
      前短路檢查；單元測試證明對病態輸入依然生效）
- [x] 兩個不同主題跑出不同路由軌跡（主題 A 4 輪收斂／主題 B 5 輪含一次
      加輪，決策 log 具體記錄差異與理由）
- [x] 超預算強制收斂（單元測試覆蓋；真實跑測預算充足未觸發，行為由
      壓力測試驗證）
