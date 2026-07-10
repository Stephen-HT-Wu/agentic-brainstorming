# Stage 3 Note — 需求探索訪談 + Define（agent 對 agent 角色扮演）

## 目標（對照 `PLAN.md`）

- 每個 persona 做完功課後、**在提出任何點子之前**，獨立設計自己的需求訪綱
- 訪談 `users.yaml` 定義的模擬使用者（也是 agent 扮演，2-3 位，各 3 輪對話）
- 從逐字稿萃取洞見 → 寫 POV 陳述 + HMW 問句 → 提案必須回應自己的 HMW

對應程式：`practice/stage3/graph.py`（stage 2 的完整獨立副本再擴充，不 import stage2）

## 跟 stage 2 的差異

只在做功課子圖裡插入四個新節點（`synthesize` 之後、`draft_proposal` 之前）：

```
collect → dedup → synthesize
  → design_interview_guide → conduct_interviews → extract_insights → write_pov_hmw
  → draft_proposal → refine ×3
```

父圖的 `Send` fan-out（stage 2 的核心）完全不變，只是 payload 多帶一個
`users` 欄位。平行度沒有降低——訪談是每位 persona 子圖內部的循序步驟，
但 4 位 persona 之間仍然平行跑。

## 你會學到什麼

1. **Agent 對 agent 角色扮演對話**：`simulate_user_answer`（模擬使用者）與
   `generate_followup_question`（persona 追問）是兩個獨立的角色扮演 LLM
   呼叫，各自只看得到自己該看的資訊——模擬使用者的 system prompt 完全不
   提「這是產品研究」，只給人設/情境/痛點，刻意製造資訊不對稱
2. **Agent 自己決定問什麼**：訪綱只給第一輪的開場問題；第 2、3 輪的追問
   由 persona 依對方剛剛的回答**動態生成**，不是照本宣科念完一份固定清單
3. **可驗證的引用鏈**：`insight_refs` 必須是 `extract_insights` 真的萃取出來
   的洞見 id，`count_real_insight_refs` 驗證這件事——跟 stage1 的
   `count_real_citations`（URL 必須來自真實搜尋結果）是同一種「可驗證引用」
   設計手法，只是這次驗證的是訪談洞見而非搜尋來源
4. **POV/HMW 不靠模型覆誦**：`_ensure_hmw_fields` 把 `state["pov"]`/`state["hmw"]`
   直接覆寫進提案（不是要求模型在提案裡重寫一次），避免模型抄錯字或漂移
5. **雙重防禦貫穿四個新節點**：訪綱、洞見、POV/HMW 三個地方都有「模型輸出
   異常就用機械式後備值頂著」的保底邏輯，理由跟 stage1 的 BMC 保底一致——
   結構不變量不能因為單次 LLM 輸出不穩定就整場失敗

## 踩到的坑：`extract_json` 回傳非 dict 導致整個 persona 崩潰

第一次真實跑測，3 位 persona 都順利跑完到 `refine`，但 `陳建宏` 在
`write_pov_hmw` 直接炸掉：

```
AttributeError: 'list' object has no attribute 'get'
```

根因：`extract_json(text)` 底層就是 `json.loads`，**合法 JSON 不保證是
object**——模型那次吐出的東西被 fence 抓出來後解析成一個 list（不是
`{"pov":..., "hmw":...}`）。`design_interview_guide`／`extract_insights`／
`write_pov_hmw` 三處都只 `except (json.JSONDecodeError, ValueError)`
沒檢查型別，直接 `.get()` 就出事。而且因為當時**沒有 checkpointer**
（那是 stage 5 的東西），LangGraph 同步跑的另外 3 位 persona 已經花的
真錢全部隨著整個 process crash 一起浪費掉，不能斷點續跑。

修正：新增 `extract_json_object()`，把「非 dict 也當解析失敗」統一處理，
三處呼叫點都改用它。補一則回歸測試重現這個確切輸入
（`extract_json_object("[1, 2, 3]") == {}`）。修正後重新完整跑測，
4 位 persona 全數成功、無崩潰。

**教訓**：往後任何預期拿到 dict 的 JSON 解析，一律要檢查
`isinstance(data, dict)`，不能只捕 exception——合法 JSON 的型別範圍比
「exception 或 dict」大得多。這比「加 checkpointer 才能斷點續跑」更根本，
即使有 checkpointer，這種 bug 一樣會讓那個節點反覆失敗到 retry 上限。

## 怎麼跑

```bash
cd practice
.venv/bin/python stage3/graph.py            # 真實跑一次（約 $0.35，4 persona × 2 用戶訪談）
.venv/bin/python -m unittest stage3/test_graph.py -v   # 純邏輯測試，零成本
```

可選環境變數：`BRAINSTORM_TOPIC=你的主題`；改人數／人設編輯
`personas.yaml`；改模擬使用者編輯 `users.yaml`（不存在則 fallback 到
`*.example.*`）。

## 實際觀察（2026-07-11，修正 bug 後的乾淨跑測）

主題「如何提升新聞短影音互動率」，4 位 persona × 2 位模擬使用者（陳小姐/王先生）：

| Persona | BMC | POV/HMW | insight_refs 真實引用 | 逐字稿 | 洞見 | self_score | 耗時 | 成本 |
|---|---|---|---|---|---|---|---|---|
| 林美華 | 9/9 | OK | 3 | 6 筆 | 5 則 | 8.2 | 128.0s | $0.0844 |
| 陳建宏 | 9/9 | OK | 3 | 6 筆 | 5 則 | 9.0 | 131.3s | $0.0849 |
| 周若琪 | 9/9 | OK | 3 | 6 筆 | 5 則 | 8.8 | 131.8s | $0.0855 |
| 王承翰 | 9/9 | OK | 3 | 6 筆 | 5 則 | 9.0 | 139.9s | $0.0886 |

四人的 HMW 明顯朝不同方向（陳建宏關注標題誠實度、王承翰關注可信度信號
與商業掛鉤、周若琪關注推薦系統設計、林美華關注資訊蒐集效率），不是同一
份訪談洞見換句話說：

**訪綱彼此不同**：兩兩平均 embedding 距離 **0.5614**（範圍 0.50-0.61），
比同一批人最終提案的兩兩距離（0.3596）還高——訪談角度比最終提案更早分岔，
符合直覺（提案會被 BMC 協議、公司定位等共同約束往中間拉一點）。

**平行比循序省時**：平行 wall-clock 139.86s，循序估計 530.94s，**加速 3.8x**
（維持跟 stage2 相近的倍率，訪談步驟沒有拖慢並行效率）。

**提案彼此明顯不同**：兩兩平均距離 0.3596，去重後 **4/4 全部獨立**。

**總成本**：$0.3482（4 persona + baseline；比 stage2 多花約 $0.10，全部
是新增的訪談 4 節點：`design_interview_guide` $0.0064 +
`conduct_interviews` $0.0425 + `extract_insights` $0.0122 +
`write_pov_hmw` $0.0074 ≈ $0.0685/組 × 4 ≈ $0.27... 實際上跟 `draft_proposal`/
`refine` 因為多了 POV/HMW/insight_refs 欄位、prompt 變長，也各自漲了一點）。

## 驗收（對照 PLAN.md 階段 3）

- [x] 訪綱彼此不同（兩兩平均距離 0.5614，遠高於「非零」的弱門檻）
- [x] 每個提案都有明確對應的 POV/HMW（4/4，`pov`/`hmw` 由程式從 state 覆寫，保證非空）
- [x] 提案有可歸因到訪談洞見的內容（4/4 都有 ≥1 個真實 `insight_refs`，實測皆為 3）
- [x] 逐字稿完整記錄（每位 persona 6 筆，等於 2 用戶 × 3 輪，`outputs/stage3-*.json` 全存）
