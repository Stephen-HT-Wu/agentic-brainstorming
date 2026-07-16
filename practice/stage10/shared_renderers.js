// stage10（build_replay.py 的靜態回放頁）跟 stage11（static/index.html 的
// 即時面板）共用的事件細節渲染邏輯——同一份原始碼，不是兩份手動同步的
// 複製（這正是使用者要求「整理重複用到的 code」要解決的問題）。
//
// 使用慣例：呼叫方要準備好兩個全域變數 PERSONAS／USERS（陣列，每個元素
// 至少有 id/name，USERS 元素另外可能有 age/context/pain_points/tone）：
// - stage10：build_replay.py 產生 HTML 時，這份檔案的內容被直接內嵌進
//   <script> 標籤，PERSONAS/USERS 用 const 宣告在被內嵌的內容「之前」，
//   整場會議固定不變。
// - stage11：用 <script src="/static/shared_renderers.js"> 引入，
//   PERSONAS/USERS 是 `let` 變數，會隨著使用者切換到不同 run 而更新
//   （見 index.html 的 loadRunConfig()）。
//
// 兩邊「刻意不同」的地方（不勉強塞進這份共用檔案）：
// - present/baseline 事件在缺少 e.detail 時的容錯文字——stage11 即時畫面
//   可能在完整提案資料還沒 attach 之前就先收到這個事件，stage10 靜態頁
//   一定已經有完整資料，兩邊在各自的 renderExtraGeneric 呼叫前已經處理，
//   這裡的版本兩種情況都安全。

// 真實資料踩到的坑：像 conduct_interviews／generate_prototype_and_test 這種
// 一次節點呼叫裡多次 emit_event 的節點，中間每筆事件的 cost_usd 記的是
// 「這次呼叫目前為止的累計花費」，不是這筆事件單獨的花費——直接加總全部
// 事件的 cost_usd 會嚴重高估（實測：一場會議原始加總 $0.874，但 pipeline
// 自己印出的真實總成本是 $0.668）。這幾個 action 是「過程中的快照」，真正
// 該計入總額的是同一次呼叫最後那筆「總結」事件，加總時要排除快照、只算
// 總結。跟 build_replay.py 的 Python 版 sum_display_cost()／
// COST_SNAPSHOT_ACTIONS 是同一套邏輯——stage11 即時畫面要顯示即時成本，
// 需要同一套排除規則，不是只有離線回放才用得到。
const COST_SNAPSHOT_ACTIONS = new Set(['interview_turn', 'generate_prototype', 'test_prototype']);
// exclude 預設是 stage9/10/11 那份排除清單，可選帶入覆寫——stage12 的
// generate_prototype 是單次生成、只有一筆 emit_event（不像 stage9 的
// generate_prototype_and_test 是「生成→測試→修正」三步驟共用一個
// invocation，中間那筆快照事件才需要排除），沿用同一份排除清單反而會把
// stage12 唯一一筆原型花費也錯誤地歸零；stage12 自己的排除清單見
// stage12/static/index.html 的 STAGE12_COST_SNAPSHOT_ACTIONS。
function sumDisplayCost(events, exclude = COST_SNAPSHOT_ACTIONS) {
  return events.reduce((s, e) => s + (exclude.has(e.action) ? 0 : (e.cost_usd || 0)), 0);
}

function escapeHtml(s) {
  // 不能用「d.innerText = s; return d.innerHTML」這招——實測踩到：
  // persona 的 background 是多行文字（YAML `>` 折疊出來的段落），Chromium
  // 幫 innerText 塞值時會把字串裡的換行字元轉成真的 <br> 節點，讀回
  // innerHTML 時這些 <br> 就變成輸出字串的一部分。這段文字如果被塞進
  // <textarea> 的 value——textarea 顯示的是原始文字，不會解析 HTML，於是
  // 使用者看到的是字面上的「<br>」字樣，不是換行。改成純字串取代，不碰
  // DOM，換行字元原封不動保留，對屬性值（雙引號/單引號）也安全。
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}
function esc(s) { return escapeHtml(s === undefined || s === null ? '' : String(s)); }

function ul(items) {
  if (!items || !items.length) return '';
  return '<ul class="detail-list">' + items.map(i => `<li>${esc(typeof i === 'object' ? (i.id ? i.id + '：' : '') + (i.text || JSON.stringify(i)) : i)}</li>`).join('') + '</ul>';
}
function kv(label, value) {
  if (value === undefined || value === null || value === '') return '';
  return `<div class="kv"><span class="k">${esc(label)}</span><span class="v">${esc(value)}</span></div>`;
}
function block(title, html) {
  if (!html) return '';
  return `<div class="detail-block"><div class="detail-block-title">${esc(title)}</div>${html}</div>`;
}

// 互評/評分/收斂等事件的 extra 裡到處混著英文 persona_id（例如
// target_persona_id="alex"）跟中文姓名（role="persona:陳建宏"），使用者
// 反應「名字時中時英很難認人」。統一解析成姓名——找不到就照原樣顯示
// （對缺資料/舊格式安全，也對「值本來就已經是名字」的情況安全）。
function personaName(idOrName) {
  if (!idOrName) return idOrName;
  const hit = (typeof PERSONAS !== 'undefined' ? PERSONAS : []).find(p => p.id === idOrName);
  return hit ? hit.name : idOrName;
}

function findUser(idOrName) {
  const list = typeof USERS !== 'undefined' ? USERS : [];
  return list.find(u => u.id === idOrName || u.name === idOrName);
}

function renderUserProfile(user) {
  if (!user) return '';
  let html = '';
  if (user.age !== undefined && user.age !== null) html += kv('年齡', user.age);
  html += block('情境', user.context ? `<p>${esc(user.context)}</p>` : '');
  html += block('痛點', ul(user.pain_points));
  html += kv('說話口吻', user.tone);
  return html;
}

const BMC_ORDER = ["客群","價值主張","通路","顧客關係","收益流","關鍵資源","關鍵活動","關鍵夥伴","成本結構"];
// 「收益流」「成本結構」量化後是結構化物件（narrative/monthly_estimate_twd/
// basis），不是純字串——沒有特判會被 esc() 印成 "[object Object]"。
const QUANT_BMC_KEYS = new Set(["收益流", "成本結構"]);
function renderBmc(bmc) {
  if (!bmc) return '';
  let html = '<div class="bmc-grid">';
  for (const k of BMC_ORDER) {
    const v = bmc[k];
    if (!v) continue;
    if (QUANT_BMC_KEYS.has(k) && typeof v === 'object') {
      const margin = v.monthly_estimate_twd;
      html += `<div class="bmc-cell"><div class="bmc-key">${esc(k)}</div>` +
        `<div class="bmc-val">${esc(v.narrative)}<br><strong>NT$${esc(Number(margin || 0).toLocaleString())}/月</strong>` +
        (v.basis ? `<div class="detail-note">${esc(v.basis)}</div>` : '') +
        `</div></div>`;
    } else {
      html += `<div class="bmc-cell"><div class="bmc-key">${esc(k)}</div><div class="bmc-val">${esc(v)}</div></div>`;
    }
  }
  html += '</div>';
  return html;
}

function renderSources(sources, opts) {
  if (!sources || !sources.length) return '';
  if (opts && opts.unverified) {
    // baseline 沒有真實搜尋素材可用，模型可能編造網址——不要渲染成看起來
    // 像真的可點連結（實測 example.com 這種佔位網址點開來是空的，使用者
    // 回報「baseline 的 link 是錯的」，根因是呈現方式，不是資料本身錯）。
    return '<div class="detail-note">來源可能是模型編造，非真實連結，不提供點擊：</div><ul class="detail-list">' +
      sources.map(s => `<li>${esc(s.title || s.url)}${s.how_used ? ' — ' + esc(s.how_used) : ''}</li>`).join('') + '</ul>';
  }
  return '<ul class="detail-list">' + sources.map(s =>
    `<li><a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${esc(s.title || s.url)}</a>${s.how_used ? ' — ' + esc(s.how_used) : ''}</li>`
  ).join('') + '</ul>';
}

function renderProposal(p, note, opts) {
  if (!p) return '';
  let html = '';
  if (note) html += `<div class="detail-note">${esc(note)}</div>`;
  if (p.title) html += `<div class="detail-proposal-title">${esc(p.title)}</div>`;
  if (p.summary) html += `<p>${esc(p.summary)}</p>`;
  html += block('POV', p.pov ? `<p>${esc(p.pov)}</p>` : '');
  html += block('HMW', p.hmw ? `<p>${esc(p.hmw)}</p>` : '');
  html += block('HMW 回應', p.hmw_response ? `<p>${esc(p.hmw_response)}</p>` : '');
  html += kv('自評分數', p.self_score !== undefined ? p.self_score + (p.score_reason ? '：' + p.score_reason : '') : '');
  html += block('修正說明', p.revision_note ? `<p>${esc(p.revision_note)}</p>` : '');
  html += block('Business Model Canvas', renderBmc(p.bmc));
  html += block('真實搜尋依據', renderSources(p.sources, opts));
  html += kv('引用洞見', (p.insight_refs || []).join(', '));
  html += kv('引用跨場記憶', (p.memory_refs || []).join(', '));
  return html;
}

function renderPrototype(proto) {
  if (!proto) return '';
  const lp = proto.landing_page || {};
  let html = '';
  if (lp.headline) html += `<div class="detail-proposal-title">${esc(lp.headline)}</div>`;
  if (lp.subheadline) html += `<p>${esc(lp.subheadline)}</p>`;
  html += block('概念一頁書', lp.concept_one_pager ? `<p>${esc(lp.concept_one_pager)}</p>` : '');
  html += block('功能', ul((lp.features || []).map(f => `${f.title}：${f.desc}`)));
  html += block('測試後修改內容', proto.diff_text ? `<pre>${esc(proto.diff_text)}</pre>` : '');
  if (proto.reactions && proto.reactions.length) {
    html += block('模擬用戶反應', proto.reactions.map(r => {
      const profile = renderUserProfile(findUser(r.user_id) || findUser(r.user_name));
      return `<div class="quote"><b>${esc(r.user_name)}：</b>${esc(r.reaction)}</div>` + (profile ? `<div class="detail-note">${esc(r.user_name)} 的人物設定</div>${profile}` : '');
    }).join(''));
  }
  html += kv('原型檔案', proto.html_path);
  return html;
}

// 使用者要求能追溯「看了哪些網頁才形成後面的想法」——collect/dedup 的
// extra 裡的 title/url/snippet 就是研究足跡。
function renderResearchItems(items) {
  if (!items || !items.length) return '';
  return '<ul class="detail-list">' + items.map(it =>
    `<li><a href="${esc(it.url)}" target="_blank" rel="noopener noreferrer">${esc(it.title || it.url)}</a>${it.snippet ? ' — ' + esc(it.snippet) : ''}</li>`
  ).join('') + '</ul>';
}

function renderExtraGeneric(action, extra) {
  if (!extra) {
    if (action === 'present' || action === 'baseline') return '<div class="detail-note">完整提案內容會在會議跑完後的回放頁顯示（/api/runs/&lt;id&gt;/replay）。</div>';
    return '';
  }
  switch (action) {
    case 'analyze_problem':
      // 使用者要求反過來從問題出發：全會議只發生一次，回答「為什麼問這些
      // 人、為什麼問這些問題」——五力五格＋趨勢分析＋問題陳述＋動態生成的
      // 訪談對象清單並陳，不用等到報告才看得到推導過程。
      return block('問題陳述', extra.problem_statement ? `<p>${esc(extra.problem_statement)}</p>` : '') +
        block('五力分析', ul(Object.entries(extra.five_forces || {}).map(([k, v]) => `${k}：${v}`))) +
        block('趨勢分析（科技/環境/人口結構/世代價值觀）', extra.trend_analysis ? `<p>${esc(extra.trend_analysis)}</p>` : '') +
        block('動態生成的訪談對象', (extra.interview_targets || []).map(u =>
          `<div class="quote"><b>${esc(u.name)}</b>${u.age ? '（' + esc(u.age) + ' 歲）' : ''}<br>${esc(u.context || '')}</div>`).join('')) +
        (extra.used_fallback_users ? '<div class="detail-note">分析解析失敗，已退回既有預設訪談名單。</div>' : '');
    case 'collect':
      return block('搜尋查詢', ul(extra.queries)) + kv('原始結果數', extra.n_results) + block('搜尋到的網頁（研究足跡）', renderResearchItems(extra.results));
    case 'dedup':
      return block('去重後保留的網頁（真正流進提案的研究依據）', renderResearchItems(extra.items));
    case 'recall_memory':
      return block('命中的跨場記憶', (extra.hits || []).map(h =>
        `<div class="quote"><b>[${esc(h.topic || h.collection || '')}${h.round_id ? ' · ' + h.round_id : ''}]</b>${h.distance != null ? ' (distance ' + h.distance.toFixed(3) + ')' : ''}<br>${esc(h.text)}</div>`).join(''));
    case 'design_interview_guide':
      return block('訪綱', ul(extra.questions));
    case 'interview_turn':
      return `<div class="quote"><b>問（第 ${esc(extra.round)} 輪 · 對象：${esc(extra.user_name)}）：</b>${esc(extra.question)}</div><div class="quote"><b>答：</b>${esc(extra.answer)}</div>` +
        block('被訪談者的人物設定', renderUserProfile(findUser(extra.user_id)));
    case 'extract_insights':
      return block('萃取洞見', ul(extra.insights));
    case 'write_pov_hmw':
      return block('POV', `<p>${esc(extra.pov)}</p>`) + block('HMW', `<p>${esc(extra.hmw)}</p>`);
    case 'draft_proposal':
      // 使用者要求初稿一形成就看得到完整提案跟 BMC，不用等到 present 事件
      // 或會議跑完看回放——extra.proposal 是 graph.py 端加的完整提案物件。
      return (extra.bmc_missing && extra.bmc_missing.length ? block('BMC 缺漏欄位', ul(extra.bmc_missing)) : '') +
        renderProposal(extra.proposal, '初稿');
    case 'refine':
      // diff 回答「改了什麼」，完整提案回答「現在長怎樣」——兩者並陳，
      // 不是只挑一個顯示。
      return kv('修正輪次', extra.round) + kv('embedding 位移', extra.embedding_distance) +
        kv('自評分數變化', `${extra.self_score_before} → ${extra.self_score_after}（Δ${extra.self_score_delta > 0 ? '+' : ''}${extra.self_score_delta}）`) +
        block('具體改了什麼（跟分數並陳，不是只有數字）', extra.diff_text ? `<pre>${esc(extra.diff_text)}</pre>` : '') +
        renderProposal(extra.proposal, `第 ${extra.round} 輪修正後的完整提案`);
    case 'co_create_turn':
      // 使用者要求把「各自提案、互評選 Top-K」改成「共創收斂成一個
      // 提案」——這是共創迴圈裡的一輪，跟 refine 一樣 diff／完整提案並陳，
      // 額外多一行「整合了誰的觀點」（built_on_persona_ids，已在後端驗證
      // 過是真實存在的其他成員，不是模型自己編的）。
      return kv('第幾輪', `${extra.turn}`) +
        kv('整合了', (extra.built_on_persona_ids || []).map(personaName).join('、') || '（無）') +
        kv('這輪貢獻', extra.contribution_note) +
        kv('embedding 位移', extra.embedding_distance) +
        block('這輪具體改了什麼', extra.diff_text ? `<pre>${esc(extra.diff_text)}</pre>` : '') +
        renderProposal(extra.proposal, `第 ${extra.turn} 輪編輯後的共創草稿`);
    case 'homework_done':
      // 使用者要求點 homework_done 就看得到完整洞察／提案／BMC，才問得出
      // 好問題——不用等到 present 事件或回放頁。POV/HMW 特意從 extra 頂層
      // 拿（不是 extra.proposal.pov/hmw）：提案 JSON schema 只有
      // hmw_response，沒有 pov/hmw 這兩個欄位，renderProposal() 讀
      // p.pov/p.hmw 對真實提案物件永遠是空的。
      return kv('耗時', extra.elapsed_s ? extra.elapsed_s.toFixed(1) + ' 秒' : '') +
        block('POV', extra.pov ? `<p>${esc(extra.pov)}</p>` : '') +
        block('HMW', extra.hmw ? `<p>${esc(extra.hmw)}</p>` : '') +
        block('訪談洞見', ul(extra.insights)) +
        renderProposal(extra.proposal, '做完功課後的完整提案');
    case 'facilitator_decide':
      return kv('第幾輪', extra.round) + kv('動作', extra.action) + kv('指定人選', extra.chosen_persona_name) +
        block('理由', extra.reason ? `<p>${esc(extra.reason)}</p>` : '') +
        kv('已用預算', extra.budget_used_usd != null ? '$' + extra.budget_used_usd.toFixed(4) : '');
    case 'give_feedback':
      return kv('是否回應了 HMW', extra.hmw_addressed === true ? '是' : (extra.hmw_addressed === false ? '否' : '')) +
        block('理由', extra.hmw_addressed_reason ? `<p>${esc(extra.hmw_addressed_reason)}</p>` : '') +
        block('✅ 認同', ul(extra.agreements)) +
        block('❌ 異議', ul(extra.disagreements)) +
        block('💡 洞見', ul(extra.insights));
    case 'revise_after_feedback':
      return kv('回應的評論者', (extra.addressed_reviewer_ids || []).map(personaName).join(', '));
    case 'master_critique':
      return kv('評論視角', extra.angle) + block('點評內容', extra.critique ? `<p>${esc(extra.critique)}</p>` : '') + kv('最看好', personaName(extra.top_pick_persona));
    case 'write_wisdom':
      return kv('寫入集體智慧筆數', extra.wisdom_written) + kv('寫入訪談逐字稿筆數', extra.interviews_written);
    case 'score_proposal':
      return kv('評分者', extra.rater_name) + kv('對象', personaName(extra.target_persona_id)) + kv('分數', extra.score) +
        block('理由', extra.reason ? `<p>${esc(extra.reason)}</p>` : '');
    case 'run_collective_scoring':
      return block('Top-K 入選', ul((extra.top_k_ids || []).map(personaName))) +
        block('各人平均分／標準差', ul(Object.entries(extra.aggregates || {}).map(([k, v]) => `${personaName(k)}：平均 ${v.mean}，標準差 ${v.stdev}（n=${v.n}）`)));
    case 'test_prototype':
      return block('模擬用戶反應', extra.reaction ? `<div class="quote">${esc(extra.reaction)}</div>` : '') +
        block('被訪談者的人物設定', renderUserProfile(findUser(extra.user_id)));
    case 'refine_after_test':
      return kv('embedding 位移', extra.embedding_distance);
    case 'three_lens_check':
      return block('👍 正面', ul(extra.positive)) + block('👎 負面', ul(extra.negative)) + block('💡 洞見', ul(extra.insight));
    case 'human_qa':
      return kv('提問者', extra.asked_by) + block('問題', extra.question ? `<p>${esc(extra.question)}</p>` : '') + block('回答', extra.answer ? `<p>${esc(extra.answer)}</p>` : '');
    case 'evaluate_final_outputs':
      // 使用者要求讓模擬使用者對共創方案跟 baseline 各自獨立給意見＋
      // 0-10 分——這裡是單一使用者的評分明細，並陳兩邊不用切換頁面比較。
      return `<div class="detail-block"><div class="detail-block-title">共創方案（${esc(extra.agent_score)} 分）</div><p>${esc(extra.agent_reaction)}</p></div>` +
        `<div class="detail-block"><div class="detail-block-title">Baseline（${esc(extra.baseline_score)} 分）</div><p>${esc(extra.baseline_reaction)}</p></div>`;
    case 'user_evaluation_summary':
      // 使用者要求「兩者平行呈現」——這筆事件的 extra 帶了完整的兩份
      // 提案（含 BMC），用既有 renderProposal() 並排顯示，不用另外拼湊
      // 多筆事件才看得到完整內容。
      return kv('共創方案平均分', extra.agent_avg_score) + kv('Baseline 平均分', extra.baseline_avg_score) +
        kv('差距', extra.score_delta != null ? (extra.score_delta > 0 ? '+' : '') + extra.score_delta : '') +
        block('各使用者評分明細', (extra.evaluations || []).map(e =>
          `<div class="quote"><b>${esc(e.user_name)}</b> — 共創 ${esc(e.agent_score)} 分／Baseline ${esc(e.baseline_score)} 分<br>` +
          `共創：${esc(e.agent_reaction)}<br>Baseline：${esc(e.baseline_reaction)}</div>`
        ).join('')) +
        renderProposal(extra.final_proposal, '共創最終提案') +
        renderProposal(extra.baseline_proposal, 'Baseline 提案', { unverified: true });
    case 'generate_final_verdict':
      return block('AI 對照評語（agent 流程 vs baseline）', extra.verdict ? `<p>${esc(extra.verdict)}</p>` : '');
    case 'baseline':
      // 使用者要求在即時畫面就看得到 baseline 的完整敘事＋BMC（第 13
      // 點），不用等會議跑完——stage12 的 run_baseline() 把完整
      // proposal（含 BMC）放進 extra，跟 user_evaluation_summary 裡的
      // baseline_proposal 一樣用 renderProposal() 呈現，標記 unverified
      // （baseline 沒有經過訪談洞見驗證，是直接問 LLM 的對照組）。
      return renderProposal(extra.proposal, 'Baseline（直接問 LLM，沒有經過訪談洞見驗證）', { unverified: true });
    // ---- stage12（簡化版腦力激盪）專屬事件 ----
    case 'analyze_and_scope':
      // stage12 把「問題陳述」換成「先選策略目標 + target audience，再
      // 依此動態生成訪談對象」——跟舊版 analyze_problem 的欄位形狀不同
      // （多了 strategic_goal/target_audience，沒有 problem_statement），
      // 不能沿用同一個 case。
      return block('五力分析', ul(Object.entries(extra.five_forces || {}).map(([k, v]) => `${k}：${v}`))) +
        block('趨勢分析（科技/環境/人口結構/世代價值觀）', extra.trend_analysis ? `<p>${esc(extra.trend_analysis)}</p>` : '') +
        block('策略目標', extra.strategic_goal ? `<p>${esc(extra.strategic_goal)}</p>` : '') +
        block('Target Audience', extra.target_audience ? `<p>${esc(extra.target_audience)}</p>` : '') +
        block('動態生成的訪談對象', (extra.interviewees || []).map(u =>
          `<div class="quote"><b>${esc(u.name)}</b>${u.age ? '（' + esc(u.age) + ' 歲）' : ''}<br>${esc(u.context || '')}</div>`).join('')) +
        (extra.used_fallback_interviewees ? '<div class="detail-note">分析解析失敗，已退回既有預設訪談名單。</div>' : '');
    case 'system_research':
      // 訪談逐字稿本身透過 interview_turn 事件各自顯示，這裡是訪談結束
      // 後萃取的洞見——真實跑測發現全場共用一份 BMC 會壓低點子多樣性
      // （見 stage12/note.md），改成每位 persona 在 draft_idea 時自己
      // 設計自己的 BMC，這裡不再顯示 BMC。
      return block('訪談洞見', ul((extra.insights || []).map(i => i.text)));
    case 'generate_personas':
      return block('動態生成的腦力激盪參與者', (extra.personas || []).map(p =>
        `<div class="quote"><b>${esc(p.name)}</b>（${esc(p.role || '')}）<br>${esc(p.background || '')}</div>`).join('')) +
        (extra.used_fallback_personas ? '<div class="detail-note">生成解析失敗或人數不足，已退回既有預設人設。</div>' : '');
    case 'generate_evaluators':
      return block('動態生成的最終評估者（跟訪談對象同一 target audience，但不重複個體）', (extra.evaluators || []).map(u =>
        `<div class="quote"><b>${esc(u.name)}</b>${u.age ? '（' + esc(u.age) + ' 歲）' : ''}<br>${esc(u.context || '')}</div>`).join('')) +
        (extra.used_fallback_evaluators ? '<div class="detail-note">生成解析失敗或人數不足，已退回既有預設用戶名單（已排除訪談對象）。</div>' : '');
    case 'draft_idea': {
      // 每位 persona 獨立發想一個 idea 就結束（第 8 點：不互評、不自己
      // 改），沒有 stage9 那種 refine/co_create 的多輪版本可看，這裡直接
      // 顯示完整 idea 內容。BMC 是這位 persona 自己設計的一份（不是共用
      // 範本，見 stage12/note.md 的多樣性發現），跟 idea 一起顯示。
      // stage15-market-fit 額外多 target_segment/monetization_mechanism/
      // differentiation_vs_competitors 三個欄位（沒有 insight_refs，因為
      // 沒有 Discover/Define 收斂出的訪談洞見可以引用了）——用
      // kv()/block() 對缺欄位安全的特性，同一個 case 兩邊都能正確顯示。
      const idea = extra.idea || {};
      return block(`《${idea.title || ''}》`, idea.summary ? `<p>${esc(idea.summary)}</p>` : '') +
        block('理由', idea.rationale ? `<p>${esc(idea.rationale)}</p>` : '') +
        kv('目標客群', idea.target_segment) +
        kv('貨幣化機制', idea.monetization_mechanism) +
        block('差異化說法（對比真實競品）', idea.differentiation_vs_competitors ? `<p>${esc(idea.differentiation_vs_competitors)}</p>` : '') +
        kv('引用的訪談洞見', (idea.insight_refs || []).join('、')) +
        block('引用來源', renderResearchItems(idea.sources)) +
        block('這位參與者自己設計的 Business Model Canvas', renderBmc(idea.bmc || {}));
    }
    case 'dfv_score':
      // DFV（Desirability/Feasibility/Viability）三面向評審之一——每次
      // 呼叫只評一個面向，保留原本一大段文字批評（使用者確認的 Q4）。
      return kv('面向', extra.lens_name) + kv('對象', extra.persona_name) + kv('分數（0-10）', extra.score) +
        block('批評', extra.critique ? `<p>${esc(extra.critique)}</p>` : '');
    case 'pick_winner':
      return block('各 idea 總分（三面向加總）', ul(Object.entries(extra.totals || {}).map(([id, score]) => `${personaName(id)}：${Number(score).toFixed(1)} 分`))) +
        kv('贏家', extra.winner_idea ? `${extra.winner_idea.persona_name}《${extra.winner_idea.title}》` : '') +
        kv('idea 多樣性（平均 pairwise 距離，0-1，越高代表越是真正各自獨立發散）', extra.idea_diversity && extra.idea_diversity.avg_distance != null ? extra.idea_diversity.avg_distance : '');
    case 'generate_prototype':
      // extra.prototype 是完整原型物件（含贏家 idea 自己設計的 BMC，不是
      // 共用範本），不用等回放頁——stage12 直接在即時畫面渲染。
      return renderPrototype(extra.prototype) + block('贏家自己設計的 Business Model Canvas', renderBmc((extra.prototype && extra.prototype.bmc) || {}));
    // ---- stage13（Double Diamond 重構）專屬事件 ----
    case 'desk_research_hypothesize_jobs':
      // Discover 階段起點，取代 stage12 的 analyze_and_scope——這裡只
      // 「假設」候選 job（JTBD 陳述，刻意不含解法），不像 stage12 直接
      // 選定一個 strategic_goal；每個候選 job 各自帶一組訪談對象。
      return block('五力分析', ul(Object.entries(extra.five_forces || {}).map(([k, v]) => `${k}：${v}`))) +
        block('趨勢分析（科技/環境/人口結構/世代價值觀）', extra.trend_analysis ? `<p>${esc(extra.trend_analysis)}</p>` : '') +
        block('假設的候選 job（刻意不含解法）', (extra.candidate_jobs || []).map(cj =>
          `<div class="quote"><b>[${esc(cj.id)}] ${esc(cj.job_statement)}</b><br>${esc(cj.hypothesis_rationale || '')}<br>` +
          `訪談對象：${(cj.interview_pool || []).map(u => esc(u.name)).join('、')}</div>`).join('')) +
        (extra.used_fallback_candidate_jobs ? '<div class="detail-note">分析解析失敗，已退回系統保底候選 job。</div>' : '');
    case 'research_one_candidate_job': {
      // Discover 階段：對一個候選 job 做完整 switch 訪談＋證據萃取——
      // 訪談逐字稿本身透過 interview_turn 事件各自顯示，這裡是訪談結束
      // 後的證據判斷（supported 與否＋依據＋洞見），不管這個候選 job 最
      // 後有沒有雀屏中選都看得到，方便稽核 Define 階段的決策。
      const ev = extra.evidence || {};
      return kv('候選 job', extra.candidate_job ? `[${extra.candidate_job.id}] ${extra.candidate_job.job_statement}` : '') +
        kv('supported', ev.supported) +
        block('證據依據', ev.evidence_summary ? `<p>${esc(ev.evidence_summary)}</p>` : '') +
        block('萃取洞見', ul((ev.insights || []).map(i => i.text)));
    }
    case 'select_job_and_define_problem':
      // Define 收斂：只定義問題，不定義解法——選定的 job 帶 why_selected
      // （回溯具體證據），problem_statement/hmw 是解法無關的問題陳述。
      return kv('選定候選 job', extra.selected_job_id) +
        block('選定依據（why_selected）', extra.selected_job && extra.selected_job.why_selected ? `<p>${esc(extra.selected_job.why_selected)}</p>` : '') +
        kv('Target Audience', extra.target_audience) +
        block('Problem Statement', extra.problem_statement ? `<p>${esc(extra.problem_statement)}</p>` : '') +
        block('How Might We', extra.hmw ? `<p>${esc(extra.hmw)}</p>` : '');
    case 'derive_company_domains':
      // Develop 起點的第一步：從 company.md 衍生出這家公司實際具備、
      // 彼此明顯不同的職能，不是跟公司無關的任意領域。
      return block('從公司能力衍生出的職能', ul(extra.domains)) +
        (extra.used_fallback_domains ? '<div class="detail-note">解析失敗或衍生數量不足，已退回跟公司無關的保底領域池。</div>' : '');
    case 'generate_one_persona_for_domain': {
      // Develop 起點：derive_company_domains() 衍生出的其中一個職能，
      // 各自獨立生成一位參與者，不是 LLM 一次判斷整支「互補團隊」。
      const p = extra.persona || {};
      return kv('公司衍生職能', p.domain) +
        block(`${p.name || ''}（${p.role || ''}）`, p.background ? `<p>${esc(p.background)}</p>` : '');
    }
    case 'assemble_persona_team':
      return block('扣著公司能力衍生職能組成的參與者', (extra.personas || []).map(p =>
        `<div class="quote"><b>${esc(p.name)}</b>（職能：${esc(p.domain || '')}）<br>${esc(p.background || '')}</div>`).join('')) +
        (extra.used_fallback_personas ? '<div class="detail-note">生成解析失敗或人數不足，已退回既有預設人設。</div>' : '');
    // ---- stage14-signals（訊號擴充）專屬事件 ----
    case 'survey_one_stratum': {
      // 虛擬問卷：單一人口特徵分層的模擬統計——stage14-signals 測的是
      // 候選 job 的困擾度/強度（job_stats/high_distress_pct/avg_intensity），
      // stage15-market-fit 改測候選功能的購買意願/差異化感受
      // （feature_stats/purchase_intent_pct/differentiation_pct）。同一個
      // action 名稱、不同 stage 的 extra 形狀不同，用欄位是否存在判斷
      // 要用哪一種呈現方式，不是各自寫一份重複的 case。
      const rows = extra.feature_stats || extra.job_stats || [];
      const isFeature = !!extra.feature_stats;
      return kv('人口特徵分層', extra.stratum_label) + kv('模擬樣本數', extra.n_simulated) +
        block(
          isFeature ? '各候選功能的模擬統計（虛擬問卷，非真人樣本）' : '各候選 job 的模擬統計（虛擬問卷，非真人樣本）',
          rows.map(r => isFeature
            ? `<div class="quote"><b>[${esc(r.feature_id)}] ${esc(r.feature_title || '')}</b><br>` +
              `模擬購買意願：${esc(r.purchase_intent_pct)}%　模擬差異化感受：${esc(r.differentiation_pct)}%` +
              (r.sample_quote ? `<br>模擬引述：${esc(r.sample_quote)}` : '') + `</div>`
            : `<div class="quote"><b>[${esc(r.job_id)}] ${esc(r.job_statement || '')}</b><br>` +
              `高困擾百分比：${esc(r.high_distress_pct)}%　平均強度：${esc(r.avg_intensity)}/5` +
              (r.sample_quote ? `<br>模擬引述：${esc(r.sample_quote)}` : '') + `</div>`
          ).join('')
        ) +
        (extra.used_fallback ? '<div class="detail-note">部分候選項目解析失敗，已退回中位數保底。</div>' : '');
    }
    case 'run_virtual_survey': {
      // 彙整後、真正餵給 select_job_and_define_problem 的量化訊號（僅
      // stage14-signals 使用——stage15-market-fit 的彙整結果改附在
      // validate_market_fit 事件裡，見下面的說明）：依 n_simulated 加權
      // 平均全部分層的統計量，一律內附方法論警語，不能被誤讀成有統計
      // 顯著性的真實調查結果。
      const summary = extra.survey_summary || {};
      const byJob = summary.by_job || {};
      return kv('總模擬樣本數', summary.total_simulated_n) +
        block('各候選 job 彙整後的模擬統計', Object.entries(byJob).map(([jobId, stats]) =>
          `<div class="quote"><b>[${esc(jobId)}] ${esc(stats.job_statement || '')}</b><br>` +
          `高困擾百分比：${esc(stats.high_distress_pct)}%　平均強度：${esc(stats.avg_intensity)}/5` +
          ((stats.sample_quotes || []).length ? `<br>模擬引述：${stats.sample_quotes.map(esc).join('；')}` : '') +
          `</div>`).join('')) +
        (summary.caveat ? `<div class="detail-note">⚠️ ${esc(summary.caveat)}</div>` : '');
    }
    // ---- stage15-market-fit（策略導向市場競爭力驗證）專屬事件 ----
    case 'research_competitive_landscape':
      // 取代整個 Discover/Define：真實 web_search 找競品，程式碼驗證過
      // source_url 確實出現在搜尋結果裡（不信任 LLM 自稱），見 graph.py
      // research_competitive_landscape() 的說明。
      return block('真實競品掃描', (extra.competitive_landscape || []).map(c =>
        `<div class="quote"><b>${esc(c.competitor_name)}</b>：${esc(c.feature_description)}` +
        (c.source_url ? `<br>來源：${esc(c.source_url)}` : '') + `</div>`).join('')) +
        (extra.used_fallback_competitive_landscape ? '<div class="detail-note">本次未能取得具體競品資訊，以下評分僅供參考。</div>' : '');
    case 'concept_test_turn': {
      // 簡化版概念測試訪談（1-2 輪固定問題，不是 5 輪 JTBD switch）：
      // 看第一反應＋會不會付費/切換，附加一次輕量分類（CHEAP_MODEL）
      // 判斷 would_pay，模擬訊號，不是真實統計。訪談對象是同一組固定
      // 小組，不像 stage14-signals 那樣有全域訪談對象清單可以事後查表
      // （findUser()），這裡完整內嵌在 extra.interviewee 裡，直接渲染，
      // 不查表。
      const transcript = extra.transcript || [];
      return block('被訪談者的人物設定', renderUserProfile(extra.interviewee)) +
        kv('候選功能', extra.feature_title) + kv('會不會付費/切換', extra.would_pay ? '會' : '不會') +
        block('訪談逐字稿', transcript.map(t => `<div class="quote"><b>Q：</b>${esc(t.question)}<br><b>A：</b>${esc(t.answer)}</div>`).join('')) +
        kv('反應摘要', extra.reaction_summary);
    }
    case 'validate_market_fit': {
      // 收斂前的快速市場驗證彙整：依序呼叫虛擬問卷／概念測試訪談／DFV
      // 四面向評分（見 graph.py validate_market_fit() 的說明），這裡把
      // 前兩者的彙整結果並陳——DFV 評分本身透過各自的 dfv_score 事件
      // 顯示，不重複列在這裡。
      const survey = extra.survey_summary || {};
      const conceptTest = extra.concept_test_summary || {};
      const surveyByFeature = survey.by_feature || {};
      const conceptByFeature = conceptTest.by_feature || {};
      return kv('虛擬問卷總模擬樣本數', survey.total_simulated_n) +
        block('虛擬問卷：各候選功能的購買意願/差異化感受', Object.entries(surveyByFeature).map(([fid, s]) =>
          `<div class="quote"><b>[${esc(fid)}] ${esc(s.feature_title || '')}</b><br>` +
          `模擬購買意願：${esc(s.purchase_intent_pct)}%　模擬差異化感受：${esc(s.differentiation_pct)}%` +
          ((s.sample_quotes || []).length ? `<br>模擬引述：${s.sample_quotes.map(esc).join('；')}` : '') +
          `</div>`).join('')) +
        block('概念測試訪談：各候選功能的付費/切換意願', Object.entries(conceptByFeature).map(([fid, s]) =>
          `<div class="quote"><b>[${esc(fid)}] ${esc(s.feature_title || '')}</b><br>` +
          `${esc(s.n_interviewed)} 位模擬訪談中 ${esc(s.would_pay_pct)}% 表示願意付費/切換` +
          ((s.sample_reactions || []).length ? `<br>代表反應：${s.sample_reactions.map(esc).join('；')}` : '') +
          `</div>`).join('')) +
        (survey.caveat ? `<div class="detail-note">⚠️ ${esc(survey.caveat)}</div>` : '');
    }
    default:
      return '<pre>' + escapeHtml(JSON.stringify(extra, null, 2)) + '</pre>';
  }
}

function renderDetail(e) {
  if (e.detail && e.detail.kind === 'proposal') {
    return renderProposal(e.detail.proposal, e.detail.note, e.role === 'baseline' ? { unverified: true } : undefined);
  }
  if (e.detail && e.detail.kind === 'prototype') return renderPrototype(e.detail.prototype);
  return renderExtraGeneric(e.action, e.extra);
}
