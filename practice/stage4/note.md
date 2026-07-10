# Stage 4 Note — 發表與互評（多輪共享狀態、結構化互評）

## 目標（對照 `PLAN.md`）

- homework 階段（stage1-3）結束後，N 位 persona **輪流發表**（固定順序，
  不是動態路由——那是 stage 6 的事）
- 其他人各給恰好 **3 個認同／3 個異議／3 個洞見**，並檢查提案有沒有
  回應自己的 HMW
- 發表者針對『異議』做實質修正，形成**可歸因**（回應了誰）的版本歷史

對應程式：`practice/stage4/graph.py`（stage 3 的完整獨立副本再擴充，不 import stage3）

## 跟 stage 3 的差異

做功課子圖（collect→…→refine×3）逐行相同。新的東西全部在**父圖**：
homework 階段的 Send fan-out 結束後，多接一個 `run_presentation_rounds`
節點，固定順序逐一邀請每位 persona 發表：

```
[Send fan-out ×N persona] homework_worker
  → run_presentation_rounds（新，父圖層級，固定順序逐一進行）
      對每位發表者：
        review_round_graph.invoke(...)
          → [Send fan-out ×(N-1) reviewer] give_feedback
          → revise_after_feedback
  → END
```

這是本專案第一次出現「多個 agent 真的看得到彼此的產出並互相回應」，
跟 stage2/3 的『平行但互不知情』fan-out 是質的不同——give_feedback 的
Send payload 直接帶著發表者的真實提案內容，回傳的意見會匯聚回同一位
發表者的修正輸入。

## 你會學到什麼

1. **Send 用於『同一個目標的多重審閱』**：stage2/3 的 Send 是「N 個獨立
   worker 各自處理不同輸入」；這裡是「N-1 個 reviewer 平行審閱同一份
   提案」——同一個 fan-out 原語，換一種資料流向就長出新的協作模式
2. **巢狀子圖的第三層**：`meeting_graph`（父）→ `review_round_graph`
   （子，每位發表者一次）→ 內部再 Send 到 `give_feedback`（孫，每位
   審閱者一次）。子圖從一層變兩層，但呼叫方式（`invoke()` 包一層節點
   函式）完全沿用 stage1 就建立的模式
3. **可驗證的歸因鏈延伸到「誰的意見被回應」**：`addressed_reviewer_ids`
   必須是真的參與這輪審閱的 reviewer——跟 stage1 的搜尋引用、stage3 的
   訪談洞見引用是同一種「不能空口白話」設計，這次驗證的對象換成同儕意見
4. **結構化互評的雙重防禦**：`_pad_to_three` 保證 3/3/3 永遠成立（多了
   截斷、少了補系統保底句），不管模型這次心情好不好給幾條

## 踩到的坑：`addressed_reviewer_ids` 有時填姓名不填 id

第一次真實跑測，4 位 persona 全數通過硬驗收，但細看逐筆資料：3 位
`回應 3 位審閱者的異議`，只有周若琪顯示 `回應 1 位審閱者的異議`——
雖然她的 `revision_note` 明明寫著「回應林美華、陳建宏、王承翰異議」
（三個人都提到了）。

查證：她收到的三筆審閱是 `reviewer_id=mei/林美華`、`reviewer_id=alex/陳建宏`、
`reviewer_id=victor/王承翰`，但模型輸出的 `addressed_reviewer_ids`
顯然混了中文姓名進去（不是全部乖乖填 `mei`/`alex`/`victor`），嚴格
比對 id 集合的 `_ensure_revision_fields` 就把姓名全部濾掉，只留下
剛好格式正確的 `alex` 一個。

這不是硬驗收會抓到的問題（`>=1 個有效 id` 這個結構不變量仍然成立），
是「敘事上明明回應了三人，結構化欄位卻只算到一人」的精度問題——跟
stage2 的 BMC 鍵名污染、stage3 的 `extract_json` 非 dict 是同一類
「真實跑測才會冒出來的資料品質坑」。

修正：`_ensure_revision_fields` 改吃完整的 `reviews` 清單（不再只吃
`valid_reviewer_ids` 這個 id 集合），額外建一份 `reviewer_name → reviewer_id`
對照表，`addressed_reviewer_ids` 裡的項目不管是 id 還是姓名都能正確
解析回 id（且去重）。用四個單元測試鎖住這個修正（含「id 和姓名同時出現
指向同一人要去重」的邊界案例），因為只是把一個**已經通過硬驗收**的
軟性精度指標修得更準，沒有材料值得為此重跑一次要價的完整流程
——跟 stage2 用已存檔資料零成本驗證 embedding 修正是同一種判斷。

## 怎麼跑

```bash
cd practice
.venv/bin/python stage4/graph.py            # 真實跑一次（約 $0.5，4 persona 做功課+訪談+提案+互評）
.venv/bin/python -m unittest stage4/test_graph.py -v   # 純邏輯測試，零成本
```

## 實際觀察（2026-07-11，修正前的真實跑測；修正本身只用單元測試驗證，見上）

主題「如何提升新聞短影音互動率」，4 位 persona：

**互評結構**：互評總數 12（= 4×3，每人被另外 3 人審閱），每則恰好 3/3/3——通過。

**版本歷史（發表→互評→修正）**：

| Persona | 回應審閱者數 | embed_dist（修正前後） |
|---|---|---|
| 林美華 | 3 | 0.1574 |
| 陳建宏 | 3 | 0.1190 |
| 周若琪 | 1（修正前的 bug，見上；修正後應為 3） | 0.1386 |
| 王承翰 | 3 | 0.1589 |

四人的修正內容都具體可讀（例：林美華下調訂閱目標 5%、新增 A/B 測試驗證
留存；陳建宏補充 API SLA 與衝突決策規則；王承翰改為 3 個月小流量 A/B
測試取代直接宣稱 ROI），不是「已依審閱意見微調」這種空話保底——保底
句在這次真實跑測完全沒被觸發。

**提案多樣性：互評前 vs 互評後**：

- 互評前（stage3 產出的版本）：兩兩平均距離 0.3661
- 互評後（回應異議修正後）：兩兩平均距離 0.2917

多樣性下降，符合直覺：同儕互評會把明顯的弱點/風險磨掉，四份提案在
「可執行性」上被拉向彼此更接近的水準——但根據上面的修正內容，這是
「品質收斂」而非「內容趨同」，BMC 的核心定位仍保持各自差異。

**時間與成本**：

- 做功課階段：平行 145.53s vs 循序估計 537.02s（加速 **3.69x**，
  跟 stage2/3 一致，訪談＋互評新節點沒有拖慢這段的並行效率）
- 發表與互評階段：225.41s（刻意固定順序、不平行——4 位發表者依序進行，
  但每位發表者的 N-1 位審閱者仍平行進行）
- 總成本：**$0.5315**（新增的 `give_feedback`+`revise_after_feedback`
  共 $0.179，佔總成本約 34%）

## 驗收（對照 PLAN.md 階段 4）

- [x] 每則互評恰好 3/3/3（12 則全數通過結構檢查）
- [x] 點子池有版本歷史，且可驗證「被誰的異議改良」（`addressed_reviewer_ids`
      經過真實審閱者 id 集合驗證，發現並修正了姓名/id 混填的精度問題）
