# Stage 9 Note — 三鏡檢核 + 最終報告（全員共同動作、完整會議報告）

## 目標（對照 `PLAN.md`）

- top-K 最終提案交給**全體 persona**做三鏡檢核（正面／負面／洞見）——
  這是全員共同動作，不是同儕互評那種「別人審我」
- 把整場會議的完整歷程組成一份可讀的 Markdown 最終報告

對應程式：`practice/stage9/graph.py`（stage 8 的完整獨立副本再擴充，不 import stage8）

## 架構

做功課子圖、同儕互評子圖、Facilitator、HITL、大師點評、Chroma 集體智慧、
集體評分、Prototype/Test 逐行沿用 stage8。新增的部分：

```
run_prototype_test
  → run_three_lens_check（新，three_lens_panel_graph：N×K Send fan-out，含自評）
  → END
```

`main()` 裡新增 `build_final_report_markdown`——刻意**不是**圖節點，因為報告
需要 baseline 對照資料，那一直是在 `graph.invoke()` 完成之後才在 `main()`
裡跑的（沿用既有時序，不把 baseline 硬塞進圖裡）。

## 你會學到什麼

1. **同一套 3/3/3 協議，套用在不同時機／對象上意義完全不同**：
   `fan_out_three_lens` 刻意**包含自評**（`persona.id == target_persona_id`
   的組合也在 Send 清單裡）——跟 stage4 的同儕互評（`fan_out_reviewers`
   明確排除自己）是對照組。stage4 問的是「別人怎麼看我的提案」，
   stage9 問的是「所有人（含提案者自己）怎麼看這個已經定案的候選」，
   同一個 `_pad_to_three` 保底函式可以直接重用，不用重寫結構驗證邏輯
2. **報告完整性要驗證『內容』，不是只驗證『檔案存在』**：`report_complete`
   除了檢查檔案存在與大小，還逐一確認驗收要求的具體段落標題
   （「人類提問記錄」「第一輪訪談（Empathize」「第二輪訪談（Test」
   「三鏡檢核」）真的出現在文字裡——這是從 stage8「原型可直接開啟」
   （檢查檔案真的存在＋非空）延伸出的同一種紀律，只是這次連內容結構
   都要對得上
3. **checkpoint 保護的不只是程式碼崩潰，連 process 被外部殺掉也算**：
   這次真實踩到的情境比 stage7 的「未捕捉例外崩潰」更接近現實——
   background shell process 因為 session 本身重啟而被砍掉，完全不是
   程式邏輯的問題。用同一個 `--thread` 重跑，`run_meeting()` 一樣正確
   偵測到「上次是節點執行中崩潰（`task.error=None`，因為根本沒有
   Python 例外，是 process 直接被 SIGTERM/SIGKILL）」，用
   `invoke(None, config)` 接續，完全沒有重新執行已完成的做功課階段
   跟前三輪發表——這證明 stage5/6/7/8 建立的 checkpoint 機制擋住的
   不只是「程式寫錯」，是更廣義的「任何原因造成的中途中斷」

## 踩到的坑：不是程式邏輯，是 session 本身被中斷

真實跑測進行到一半（陳建宏、周若琪已發表，林美華剛拿到人類提問並發表），
背景執行的 process 被殺掉——不是程式崩潰，是驅動這次對話的 agent
session 本身重啟，連帶砍掉了背景 shell。事後查證：
`task.error` 是 `None`（不是任何 Python 例外訊息），純粹是外部訊號
把 process 結束掉。

這正好是 stage9 note 想強調的重點：`run_meeting()` 判斷「要不要當作
真正的 interrupt() 續跑」只看 `task.interrupts` 是否為空，不管
「中斷的原因」是例外、還是外部訊號——只要不是真正呼叫過
`interrupt()`，就一律用 `invoke(None, config)` 接續。這次沒有寫任何
新代碼、也不需要新的回歸測試，直接用同一個 `--thread` 重跑就正確
接上了，完整驗證了這個防禦機制的泛用性。

**成本對比**：完整重建 `events.jsonl`（含被中斷的第一次嘗試＋成功接續
的第二次嘗試）總成本 $1.3029；但**接續那一次單獨只花了 $0.2622**——
省下的是已經完成的做功課階段（4 人 × 完整訪談/refine）跟前 3-4 輪
發表互評，如果從零開始重跑，這些早就已經花過的錢會全部重付一次。

## 怎麼跑

```bash
cd practice
.venv/bin/python -m unittest stage9/test_graph.py -v   # 9 個測試，零成本

BRAINSTORM_TOPIC="你的主題" .venv/bin/python stage9/graph.py --thread demo --script /tmp/script.json
# 中途若因任何原因中斷（程式崩潰、process 被砍、電腦重開機），
# 用同一個 --thread 重跑同一行指令即可從斷點接續，不會重複收費。
# 最終報告在 outputs/reports/{thread}-final-report.md
```

## 實際觀察（2026-07-11，真實跑測，主題「如何提升新聞短影音互動率」）

**集體評分聚合**：陳建宏（alex）以 mean=6.667 排名第一，但也是唯一跟
周若琪（joyce）一樣有真實分歧度（stdev=0.471）的兩份提案之一——有趣的
是，三位大師（技術／商業／策略）**一致選陳建宏**當首選，跟集體評分的
結果吻合，但集體評分同時誠實地留下「不是全票通過」的分歧度數字，
比大師的單一定性判斷更細緻。

**三鏡檢核**：4 人 × 3 個 top-K 提案 = 12 筆檢核，每筆都是恰好
3 正面／3 負面／3 洞見，結構不變量 100% 通過。

**最終報告**：`outputs/reports/s9round1-final-report.md`（258 行），
完整含：Facilitator 6 輪決策軌跡、真實的人類問答（林美華對「差異化」
提問的完整三點回答）、三位大師點評、集體評分表、3 個 top-K 提案各自的
POV/HMW/BMC/原型路徑/測試後修正說明/**兩輪訪談逐字稿**（Empathize 需求
探索 + Test 概念驗證反應）/三鏡檢核明細、baseline 對照。

**測試反應持續產出真實的產品轉向**（延續 stage8 觀察到的模式）：
陳建宏的提案因為模擬用戶反應「有距離感」「太複雜」，最終修正不是加
更多功能，而是**主動刪減**——「開發時間減至4-5週，刪除複雜的廣告主
eCPM試驗」，這是真實測試反饋驅動範疇收斂的具體案例。

## 驗收（對照 PLAN.md 階段 9）

- [x] 三鏡檢核格式不變量通過（12/12 筆，每筆恰好 3/3/3）
- [x] 報告完整含人類問答與兩輪訪談記錄（`report_complete` 逐段驗證通過，
      不是只檢查檔案存在；肉眼確認過 258 行報告的三個 top-K 章節都有
      完整的兩輪訪談逐字稿）
