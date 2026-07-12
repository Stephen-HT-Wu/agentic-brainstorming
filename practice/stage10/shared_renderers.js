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
function sumDisplayCost(events) {
  return events.reduce((s, e) => s + (COST_SNAPSHOT_ACTIONS.has(e.action) ? 0 : (e.cost_usd || 0)), 0);
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
function renderBmc(bmc) {
  if (!bmc) return '';
  let html = '<div class="bmc-grid">';
  for (const k of BMC_ORDER) { if (bmc[k]) html += `<div class="bmc-cell"><div class="bmc-key">${esc(k)}</div><div class="bmc-val">${esc(bmc[k])}</div></div>`; }
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
