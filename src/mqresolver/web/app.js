import { GraphView } from './graph.js';

// ---------------------------------------------------------------------------------------------
// helpers
const $ = (s, root = document) => root.querySelector(s);
function h(tag, props = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'text') el.textContent = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
const fmt = (n) => (typeof n === 'number' ? n.toLocaleString('en-US') : n ?? '');
const label = (a) => String(a || '').replace(/^app_/, '').replace(/_/g, ' ');
const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);
const plural = (n, one, many = one + 's') => `${fmt(n)} ${n === 1 ? one : many}`;
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const REQUIRED = ['queue_name', 'queue_type', 'queue_manager', 'app_id'];
const CAT = ['#4e79a7', '#f28e2b', '#59a14f', '#e15759', '#76b7b2', '#edc948', '#b07aa1', '#ff9da7', '#9c755f'];
const PROFILES = [
  ['balanced', 'Balanced', 'Weigh simplicity, resilience and your rules evenly.'],
  ['consolidate', 'Consolidate', 'Fewest queue managers and channels.'],
  ['resilient', 'Resilient', 'Spread load so one outage touches fewer flows.'],
  ['compliance', 'Compliance', 'Your soft rules weigh most.'],
];

// ---------------------------------------------------------------------------------------------
// state
const S = {
  source: null, info: null, mapping: null, profile: 'balanced', rules: [],
  options: { eliminate_aliases: true, reuse_qm_names: true, gateway_qms: '', seed: 42 },
  result: null, ran: null, running: false, dirty: false,
  view: 'qm', colour: 'change', showEdges: true, minWeight: 1, link: false, sel: null,
  apps: { q: '', show: 'all', attr: '', value: '', page: 0, sort: 'app', dir: 1 },
};
const X = {};   // indices built from the result
let views = null;
let ruleKey = 0;

// ---------------------------------------------------------------------------------------------
// server
async function post(path, fields) {
  const fd = new FormData();
  for (const [k, v] of Object.entries(fields)) if (v !== null && v !== undefined) fd.append(k, v);
  let r;
  try { r = await fetch(path, { method: 'POST', body: fd }); }
  catch { throw new Error('Could not reach the server. Check that it is running and try again.'); }
  let data;
  try { data = await r.json(); }
  catch { throw new Error(`The server answered ${r.status} without a readable message.`); }
  if (!r.ok) throw new Error(data.error || `The request failed (${r.status}).`);
  return data;
}
const sourceFields = () => (S.source.kind === 'sample' ? { sample: '1' } : { file: S.source.file });
const mappingField = () => (S.mapping ? JSON.stringify(S.mapping) : null);
const setStatus = (t) => { $('#status').textContent = t; };

// ---------------------------------------------------------------------------------------------
// inventory
async function choose(source) {
  S.source = source; S.info = null; S.mapping = null; S.rules = [];
  $('#source-info').replaceChildren(h('p', { class: 'hint' }, 'Reading the inventory…'));
  await inspect();
}

async function inspect(keepRules = false) {
  setStatus(`Reading ${S.source.name}…`);
  try {
    S.info = await post('/api/inspect', { ...sourceFields(), mapping: mappingField() });
  } catch (e) {
    S.info = null;
    $('#source-info').replaceChildren(h('p', { class: 'error' }, e.message));
    setStatus('');
    renderRail();
    return;
  }
  if (S.info.ready && !keepRules) {
    S.rules = (S.info.suggested_policies || []).map((p) => ({ ...p, _key: ++ruleKey }));
  }
  S.dirty = true;
  renderRail();
  setStatus(S.info.ready ? `${S.source.name}: ready to simplify` : `${S.source.name}: columns need attention`);
}

function renderRail() {
  const info = S.info;
  const ready = !!(info && info.ready);
  $('#sec-mapping').hidden = !info;
  for (const id of ['#sec-profile', '#sec-rules', '#sec-options', '#run-box']) $(id).hidden = !ready;
  if (info) { renderSourceInfo(); renderMapping(); }
  if (ready) { renderProfile(); renderRules(); renderOptions(); renderRunBox(); }
}

function renderSourceInfo() {
  const i = S.info, q = i.quality || {};
  const box = $('#source-info');
  const kids = [h('p', { class: 'issue' }, h('strong', {}, S.source.name), ` ${fmt(i.rows)} rows, ${i.columns.length} columns`)];
  if (!i.ready) {
    kids.push(h('p', { class: 'error' }, i.problem || 'The file cannot be used yet.'));
    box.replaceChildren(...kids);
    return;
  }
  kids.push(h('dl', { class: 'facts' },
    h('dt', {}, 'Applications'), h('dd', {}, fmt(q.apps)),
    h('dt', {}, 'Queue managers'), h('dd', {}, fmt(q.queue_managers)),
    h('dt', {}, 'Queue definitions'), h('dd', {}, fmt(q.queue_definitions)),
    h('dt', {}, 'Apps on several queue managers'), h('dd', {}, fmt(q.apps_on_multiple_qms))));
  const issues = [];
  if (q.duplicate_rows_removed) issues.push(`${plural(q.duplicate_rows_removed, 'duplicate row')} removed`);
  if (q.unknown_role_rows) issues.push(`${plural(q.unknown_role_rows, 'row')} with no producer or consumer role`);
  if (q.external_partners) issues.push(`${plural(q.external_partners, 'external partner')} not in the inventory`);
  if (q.dangling_remote_queues?.length) issues.push(`${plural(q.dangling_remote_queues.length, 'remote queue')} pointing nowhere`);
  if (q.type_collisions?.length) issues.push(`${plural(q.type_collisions.length, 'queue name')} defined with two types`);
  if (q.attribute_conflicts?.length) issues.push(`${plural(q.attribute_conflicts.length, 'app attribute')} with conflicting values`);
  if (q.transmission_queues_required) issues.push(`${fmt(q.transmission_queues_inventoried)} of ${fmt(q.transmission_queues_required)} transmission queues are listed`);
  if (issues.length) kids.push(h('p', { class: 'issue' }, 'Data notes: ' + issues.join('; ') + '.'));
  box.replaceChildren(...kids);
}

function renderMapping() {
  const i = S.info;
  const box = $('#mapping');
  const current = { ...(i.proposed_mapping || {}), ...((S.mapping && S.mapping.columns) || {}) };
  const matched = (i.canonical_columns || []).filter((c) => current[c]).length;
  const selects = {};
  const grid = h('div', { class: 'mapping' });
  for (const c of i.canonical_columns || []) {
    const sel = h('select', { 'aria-label': `Column for ${label(c)}` },
      h('option', { value: '' }, 'Not in the file'),
      ...i.columns.map((col) => h('option', { value: col, selected: current[c] === col }, col)));
    selects[c] = sel;
    grid.append(h('span', { class: REQUIRED.includes(c) ? 'req' : '' }, label(c)), sel);
  }
  const apply = h('button', {
    class: 'btn small', type: 'button', onclick: () => {
      const cols = {};
      for (const [c, sel] of Object.entries(selects)) if (sel.value) cols[c] = sel.value;
      S.mapping = { columns: cols };
      inspect();
    },
  }, 'Use these columns');
  const det = h('details', { open: !i.ready },
    h('summary', { class: 'hint' }, `${matched} of ${(i.canonical_columns || []).length} topology columns matched. ${i.ready ? 'Review' : 'Fix the starred ones'}`),
    h('div', { style: 'margin-top:10px;display:grid;gap:10px' }, grid, apply));
  box.replaceChildren(det);
}

function renderProfile() {
  $('#profile').replaceChildren(h('div', { class: 'choices', role: 'radiogroup', 'aria-label': 'Priority' },
    ...PROFILES.map(([v, name, desc]) => h('label', { class: 'choice' },
      h('input', { type: 'radio', name: 'profile', value: v, checked: S.profile === v, onchange: () => { S.profile = v; markDirty(); } }),
      h('span', {}, name), h('small', {}, desc)))));
}

// ---------------------------------------------------------------------------------------------
// rules
function describe(p) {
  const sel = (w) => Object.entries(w || {}).map(([k, v]) => {
    if (k === 'app_id') return `apps ${[].concat(v).join(', ')}`;
    if (v && typeof v === 'object' && !Array.isArray(v)) return `${label(k)} not ${[].concat(v.not).join(' or ')}`;
    return `${label(k)} ${[].concat(v).join(' or ')}`;
  }).join(' and ');
  switch (p.type) {
    case 'capacity': {
      const parts = [];
      if (p.max_apps_per_qm) parts.push(plural(p.max_apps_per_qm, 'app'));
      if (p.max_queues_per_qm) parts.push(plural(p.max_queues_per_qm, 'queue'));
      return `At most ${parts.join(' and ')} per queue manager`;
    }
    case 'homogeneous':
      return p.max_distinct > 1
        ? `At most ${p.max_distinct} different ${label(p.attribute)} values on one queue manager`
        : `Apps on one queue manager share the same ${label(p.attribute)}`;
    case 'separate':
      return p.attribute ? `Never mix different ${label(p.attribute)} values on one queue manager`
        : `Keep ${p.groups.map(sel).join(' apart from ')}`;
    case 'isolate': return `Give every app with ${sel(p.when)} its own queue manager`;
    case 'colocate': return `Keep ${sel(p.when)} together on one queue manager`;
    case 'pin': return `Keep ${sel(p.when)} on ${p.qm}`;
    case 'qm_count':
      if (p.min && p.max) return `Use between ${p.min} and ${p.max} queue managers`;
      return p.max ? `Use at most ${plural(p.max, 'queue manager')}` : `Use at least ${plural(p.min, 'queue manager')}`;
    default: return p.id || p.type;
  }
}

function numberInput(rule, key, title, min = 1) {
  return h('label', {}, title + ' ', h('input', {
    type: 'number', min, value: rule[key] ?? '', 'aria-label': title,
    onchange: (e) => { const v = parseInt(e.target.value, 10); rule[key] = Number.isFinite(v) && v >= min ? v : null; renderRules(); markDirty(); },
  }));
}

function ruleRow(rule) {
  const seg = h('div', { class: 'seg', role: 'group', 'aria-label': `Mode for: ${describe(rule)}` },
    ...['hard', 'soft', 'off'].map((m) => h('button', {
      type: 'button', 'aria-pressed': String(rule.mode === m),
      onclick: () => { rule.mode = m; renderRules(); markDirty(); },
    }, cap(m))));
  const params = h('div', { class: 'params' }, seg);
  if (rule.type === 'capacity') params.append(numberInput(rule, 'max_apps_per_qm', 'Apps'), numberInput(rule, 'max_queues_per_qm', 'Queues'));
  if (rule.type === 'homogeneous') params.append(numberInput(rule, 'max_distinct', 'Values'));
  if (rule.type === 'qm_count') params.append(numberInput(rule, 'min', 'Min'), numberInput(rule, 'max', 'Max'));
  if (rule.mode === 'soft') params.append(numberInput(rule, 'weight', 'Weight'));
  if (rule._custom) params.append(h('button', { class: 'link', type: 'button', onclick: () => { S.rules = S.rules.filter((r) => r !== rule); renderRules(); markDirty(); } }, 'Remove'));
  return h('div', { class: `rule ${rule.mode}` }, h('p', {}, describe(rule)), params);
}

function renderRules() {
  const on = S.rules.filter((r) => r.mode !== 'off');
  const off = S.rules.filter((r) => r.mode === 'off');
  const box = $('#rules');
  const kids = [on.length ? h('div', {}, ...on.map(ruleRow)) : h('p', { class: 'hint' }, 'No rules are on. Only the structural MQ rules apply.')];
  if (off.length) {
    kids.push(h('details', { class: 'more', open: S._moreOpen || null, ontoggle: (e) => { S._moreOpen = e.target.open; } },
      h('summary', {}, `Suggested from your data (${off.length})`), h('div', {}, ...off.map(ruleRow))));
  }
  kids.push(addRuleForm());
  box.replaceChildren(...kids);
}

function addRuleForm() {
  const attrs = Object.keys(S.info.attributes || {});
  const wrap = h('div');
  if (!S._adding) {
    wrap.append(h('button', { class: 'btn small ghost', type: 'button', style: 'margin-top:10px', onclick: () => { S._adding = true; renderRules(); } }, 'Add a rule'));
    return wrap;
  }
  const type = h('select', {},
    ...[['isolate', 'Own queue manager for some apps'], ['homogeneous', 'One value per queue manager'],
      ['separate', 'Never mix values'], ['colocate', 'Keep apps together'], ['pin', 'Keep apps on a queue manager'],
      ['qm_count', 'Queue manager budget']]
      .filter(([v]) => attrs.length || !['isolate', 'homogeneous', 'separate'].includes(v))
      .map(([v, t]) => h('option', { value: v }, t)));
  const fields = h('div', { style: 'display:grid;gap:8px' });
  const attr = h('select', {}, ...attrs.map((a) => h('option', { value: a }, label(a))));
  const value = h('select');
  const fillValues = () => value.replaceChildren(...(S.info.attributes[attr.value] || []).map(([v, n]) => h('option', { value: v }, `${v} (${n})`)));
  attr.addEventListener('change', fillValues);
  const apps = h('input', { type: 'text', placeholder: 'App ids, comma separated' });
  const qm = h('input', { type: 'text', placeholder: 'Queue manager name' });
  const min = h('input', { type: 'number', min: 1, placeholder: 'Min' });
  const max = h('input', { type: 'number', min: 1, placeholder: 'Max' });
  const err = h('p', { class: 'error' });
  const L = (t, el) => h('label', {}, t, el);
  const show = () => {
    const t = type.value;
    fields.replaceChildren(...{
      isolate: [L('Attribute', attr), L('Value', value)],
      homogeneous: [L('Attribute', attr)], separate: [L('Attribute', attr)],
      colocate: [L('Apps', apps)], pin: [L('Apps', apps), L('Queue manager', qm)],
      qm_count: [h('div', { class: 'row' }, min, max)],
    }[t]);
  };
  type.addEventListener('change', show);
  fillValues(); show();
  const add = () => {
    const t = type.value;
    const ids = apps.value.split(',').map((s) => s.trim()).filter(Boolean);
    const r = { type: t, mode: 'hard', _custom: true, _key: ++ruleKey };
    if (t === 'isolate') r.when = { [attr.value]: value.value };
    if (t === 'homogeneous' || t === 'separate') r.attribute = attr.value;
    if (t === 'colocate' || t === 'pin') { if (!ids.length) { err.textContent = 'List at least one app id.'; return; } r.when = { app_id: ids }; }
    if (t === 'pin') { if (!qm.value.trim()) { err.textContent = 'Name the queue manager.'; return; } r.qm = qm.value.trim(); }
    if (t === 'qm_count') {
      const a = parseInt(min.value, 10), b = parseInt(max.value, 10);
      if (!a && !b) { err.textContent = 'Set a minimum, a maximum or both.'; return; }
      if (a) r.min = a; if (b) r.max = b;
    }
    S.rules.push(r); S._adding = false; renderRules(); markDirty();
  };
  wrap.append(h('div', { class: 'add-rule' }, L('Rule', type), fields, err,
    h('div', { class: 'row' }, h('button', { class: 'btn small', type: 'button', onclick: add }, 'Add rule'),
      h('button', { class: 'link', type: 'button', onclick: () => { S._adding = false; renderRules(); } }, 'Cancel'))));
  return wrap;
}

function renderOptions() {
  const o = S.options;
  const chk = (key, text) => h('label', { class: 'check' }, h('input', { type: 'checkbox', checked: o[key], onchange: (e) => { o[key] = e.target.checked; markDirty(); } }), text);
  $('#options').replaceChildren(h('div', { class: 'options' },
    chk('eliminate_aliases', 'Remove alias queues'),
    chk('reuse_qm_names', 'Reuse existing queue manager names'),
    h('label', {}, 'Gateway queue managers for external partners',
      h('input', { type: 'text', value: o.gateway_qms, placeholder: 'Leave empty to keep the current ones', onchange: (e) => { o.gateway_qms = e.target.value; markDirty(); } })),
    h('label', {}, 'Random seed', h('input', { type: 'number', value: o.seed, onchange: (e) => { o.seed = parseInt(e.target.value, 10) || 42; markDirty(); } }))));
}

function markDirty() { S.dirty = true; renderRunBox(); }

function renderRunBox() {
  const btn = $('#run');
  btn.disabled = S.running || !(S.info && S.info.ready);
  btn.textContent = S.running ? 'Simplifying…' : S.result ? 'Simplify again' : 'Simplify topology';
  const note = $('#run-note');
  if (S.running) note.replaceChildren(h('div', { class: 'progress' }), h('span', { id: 'elapsed' }, 'Placing applications…'));
  else if (S.result && S.dirty) note.textContent = 'Settings changed since the last run.';
  else if (!note.classList.contains('error')) note.textContent = '';
}

function buildConfig() {
  const clean = (r) => {
    const out = {};
    for (const [k, v] of Object.entries(r)) if (!k.startsWith('_') && v !== null && v !== '' && v !== undefined) out[k] = v;
    return out;
  };
  const gw = S.options.gateway_qms.split(',').map((s) => s.trim()).filter(Boolean);
  return {
    profile: S.profile,
    policies: S.rules.map(clean),
    target: { eliminate_aliases: S.options.eliminate_aliases, reuse_qm_names: S.options.reuse_qm_names, gateway_qms: gw },
    advanced: { seed: S.options.seed },
  };
}

async function run() {
  if (!S.info || !S.info.ready || S.running) return;
  const cfg = buildConfig();
  S.running = true;
  $('#run-note').classList.remove('error');
  renderRunBox();
  const t0 = performance.now();
  const tick = setInterval(() => { const el = $('#elapsed'); if (el) el.textContent = `Placing applications… ${Math.round((performance.now() - t0) / 1000)} s`; }, 500);
  setStatus(`Simplifying ${S.source.name}…`);
  try {
    const res = await post('/api/run', { ...sourceFields(), mapping: mappingField(), config: JSON.stringify(cfg) });
    S.result = res; S.ran = cfg; S.dirty = false; S.sel = null;
    showResults();
    setStatus(`${S.source.name}: simplified in ${res.seconds} s`);
  } catch (e) {
    const note = $('#run-note');
    note.classList.add('error');
    note.textContent = e.message;
    setStatus(`${S.source.name}: the run did not finish`);
  } finally {
    clearInterval(tick);
    S.running = false;
    renderRunBox();
  }
}

// ---------------------------------------------------------------------------------------------
// results
function showResults() {
  const R = S.result;
  X.dec = new Map(R.decisions.map((d) => [d.app, d]));
  X.appsOn = { as_is: new Map(), target: new Map() };
  for (const d of R.decisions) {
    for (const q of d.from) (X.appsOn.as_is.get(q) || X.appsOn.as_is.set(q, []).get(q)).push(d.app);
    (X.appsOn.target.get(d.to) || X.appsOn.target.set(d.to, []).get(d.to)).push(d.app);
  }
  X.node = {}; X.nbr = {};
  for (const side of ['as_is', 'target']) {
    const g = R.graph[side];
    X.node[side] = new Map(g.nodes.map((n) => [n.id, n]));
    const nb = new Map(g.nodes.map((n) => [n.id, new Set()]));
    for (const e of g.edges) { nb.get(e.s).add(e.t); nb.get(e.t).add(e.s); }
    X.nbr[side] = nb;
  }
  X.values = {};
  for (const a of R.graph.attributes) {
    const cnt = new Map();
    for (const d of R.decisions) { const v = d.attrs[a] || '(blank)'; cnt.set(v, (cnt.get(v) || 0) + 1); }
    X.values[a] = [...cnt.entries()].sort((x, y) => y[1] - x[1] || String(x[0]).localeCompare(String(y[0])));
  }
  X.policyById = new Map((R.config.policies || []).map((p) => [p.id, p]));

  $('#empty').hidden = true;
  $('#results').hidden = false;
  const colour = $('#colour');
  colour.replaceChildren(h('option', { value: 'change' }, 'Change'), h('option', { value: 'size' }, 'Size'),
    ...R.graph.attributes.map((a) => h('option', { value: a }, label(a))));
  if (S.colour !== 'change' && S.colour !== 'size' && !R.graph.attributes.includes(S.colour)) S.colour = 'change';
  colour.value = S.colour;
  const maxW = Math.max(1, ...R.graph.as_is.edges.map((e) => e.w));
  const mw = $('#min-weight');
  mw.max = String(clamp(maxW, 2, 40));
  if (S.minWeight > Number(mw.max)) S.minWeight = 1;
  mw.value = String(S.minWeight); $('#min-weight-out').textContent = String(S.minWeight);

  renderHeadline();
  renderDownloads();
  for (const t of ['score', 'rules', 'apps', 'checks']) renderTab(t);
  selectTab('topology');
  requestAnimationFrame(() => buildViews(false));
}

function renderHeadline() {
  const R = S.result, a = R.summary.as_is, t = R.summary.target, sc = R.score;
  const n = (v) => h('span', { class: 'num' }, fmt(v));
  const pairsVerb = t.channel_pairs <= a.channel_pairs ? 'fall' : 'rise';
  const ok = R.validation.passed;
  $('#headline').replaceChildren(
    n(a.queue_managers), ' queue managers become ', n(t.queue_managers), '. Channel pairs ', pairsVerb, ' from ', n(a.channel_pairs),
    ' to ', n(t.channel_pairs), ', and ', n(t.same_qm_flow_pct), '% of message flow stays on one queue manager, up from ',
    n(a.same_qm_flow_pct), '%. Complexity goes from ', n(sc.as_is.tci), ' to ', n(sc.target.tci), ` (${sc.change_pct}%). `,
    h('span', { class: ok ? 'ok' : 'fail' }, ok ? 'All checks passed.' : 'Some checks failed: see Checks.'));
  const m = R.migration;
  $('#cap-as-is').replaceChildren(h('strong', {}, 'As-is'), `: ${plural(a.queue_managers, 'queue manager')}, ${plural(a.channel_pairs, 'channel pair')}, ` +
    `${a.apps_on_multiple_qms} of ${fmt(R.summary.apps)} apps on several queue managers`);
  $('#cap-target').replaceChildren(h('strong', {}, 'Target'), `: ${plural(t.queue_managers, 'queue manager')} (${m.qms_reused} reused, ${m.qms_created} new), ` +
    `${plural(t.channel_pairs, 'channel pair')}, at most ${t.max_apps_per_qm} apps on one`);
}

// ---------------------------------------------------------------------------------------------
// topology views
function palette() {
  return {
    bg: css('--canvas'), ink: css('--ink'), edge: css('--muted'), spoke: css('--retired'), accent: css('--accent'),
    halo: css('--canvas'), dim: css('--line'), kept: css('--kept'), new: css('--new'), retired: css('--retired'), gateway: css('--gateway'),
  };
}

function valueColour(attr, v) {
  const vals = X.values[attr] || [];
  const i = vals.findIndex(([x]) => x === v);
  return i >= 0 && i < CAT.length && v !== '(blank)' ? CAT[i] : '#a3abb3';
}

function mix(c1, c2, t) {
  const p = (c) => [1, 3, 5].map((i) => parseInt(c.slice(i, i + 2), 16));
  const [a, b] = [p(c1), p(c2)];
  return '#' + a.map((x, i) => Math.round(x + (b[i] - x) * t).toString(16).padStart(2, '0')).join('');
}

function qmFill(side, n, P, maxApps) {
  if (n.kind === 'gateway') return P.gateway;
  if (S.colour === 'change') return side === 'as_is' ? (n.status === 'retired' ? P.retired : P.kept) : (n.status === 'new' ? P.new : P.kept);
  if (S.colour === 'size') return mix(P.retired.length === 7 ? P.retired : '#b7c0c9', P.new.length === 7 ? P.new : '#0e7c72', Math.sqrt(n.apps / maxApps));
  const top = (n.attrs[S.colour] || [])[0];
  return top ? valueColour(S.colour, top[0]) : P.retired;
}

function appFill(d, P) {
  if (S.colour === 'change') return d.stays ? P.kept : P.new;
  if (S.colour === 'size') return P.kept;
  return valueColour(S.colour, d.attrs[S.colour] || '(blank)');
}

// Push apart circles that overlap. radii are in world units; a uniform grid keeps it near linear.
function relax(nodes, radii, gap, rounds = 50) {
  const n = nodes.length;
  if (n < 2) return;
  const cell = 2 * Math.max(...radii) + gap;
  for (let it = 0; it < rounds; it++) {
    const grid = new Map();
    for (let i = 0; i < n; i++) {
      const k = Math.floor(nodes[i].x / cell) + ',' + Math.floor(nodes[i].y / cell);
      (grid.get(k) || grid.set(k, []).get(k)).push(i);
    }
    let moved = 0;
    for (let i = 0; i < n; i++) {
      const a = nodes[i], cx = Math.floor(a.x / cell), cy = Math.floor(a.y / cell);
      for (let gx = cx - 1; gx <= cx + 1; gx++) for (let gy = cy - 1; gy <= cy + 1; gy++) {
        for (const j of grid.get(gx + ',' + gy) || []) {
          if (j <= i) continue;
          const b = nodes[j];
          let dx = b.x - a.x, dy = b.y - a.y;
          let d = Math.hypot(dx, dy);
          const want = radii[i] + radii[j] + gap;
          if (d >= want) continue;
          if (d < 1e-6) { dx = Math.cos(i + j); dy = Math.sin(i + j); d = 1; }
          const push = (want - d) / 2;
          a.x -= (dx / d) * push; a.y -= (dy / d) * push;
          b.x += (dx / d) * push; b.y += (dy / d) * push;
          moved++;
        }
      }
    }
    if (!moved) break;
  }
}

function fitScale(view, nodes) {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const d of nodes) { x0 = Math.min(x0, d.x); x1 = Math.max(x1, d.x); y0 = Math.min(y0, d.y); y1 = Math.max(y1, d.y); }
  // same padding rule as GraphView._pads, so the estimate matches the real fit
  const r = nodes.reduce((m, d) => Math.max(m, d.r), 0);
  const pad = Math.min(16 + r * 1.3, Math.min(view.w, view.h) / 4), right = pad + Math.min(58, view.w / 8);
  return Math.max(0.05, Math.min((view.w - pad - right) / Math.max(x1 - x0, 1), (view.h - 2 * pad) / Math.max(y1 - y0, 1)));
}

function buildSide(side, P, view) {
  const R = S.result, g = R.graph[side];
  const maxApps = Math.max(1, ...g.nodes.map((n) => n.apps));
  const N = Math.max(1, g.nodes.length);
  const maxR = clamp(170 / Math.sqrt(N), 7, 36), minR = Math.max(2.2, maxR * 0.16);
  const appMode = S.view === 'app';
  const nodes = g.nodes.map((n) => ({
    id: n.id, kind: 'qm', side, x: n.x * 1000, y: n.y * 1000, label: n.id,
    r: n.kind === 'gateway' ? minR * 1.3 : (appMode ? Math.max(minR, maxR * 0.45) : minR + (maxR - minR) * Math.sqrt(n.apps / maxApps)),
    shape: n.kind === 'gateway' ? 'diamond' : 'circle',
    hollow: appMode && n.kind !== 'gateway',
    fill: qmFill(side, n, P, maxApps),
    ring: !appMode && !['change', 'size'].includes(S.colour) && (n.attrs[S.colour] || []).filter(([v]) => v !== '(blank)').length > 1,
  }));
  const index = new Map(nodes.map((d, i) => [d.id, i]));
  const spacing = Math.max(2.5, 260 / Math.sqrt(Math.max(1, R.decisions.length)));
  // keep circles (or, with applications shown, each queue manager's cloud of apps) from overlapping
  const count = new Map();
  if (appMode) {
    for (const d of R.decisions) {
      const qs = side === 'as_is' ? d.from : [d.to];
      if (qs.length === 1) count.set(qs[0], (count.get(qs[0]) || 0) + 1);
    }
  }
  for (let round = 0; round < 2; round++) {
    const s = fitScale(view, nodes);
    const radii = nodes.map((d) => Math.max(d.r * 1.3 / s, appMode ? spacing * (1.6 + Math.sqrt((count.get(d.id) || 0) + 0.5)) : 0));
    relax(nodes, radii, 4 / s);
  }
  const edges = S.showEdges ? g.edges.filter((e) => e.w >= S.minWeight).map((e) => ({ s: index.get(e.s), t: index.get(e.t), w: e.w })) : [];
  const spokes = [];
  if (appMode) {
    const groups = new Map();
    for (const d of R.decisions) {
      const qs = side === 'as_is' ? d.from.filter((q) => index.has(q)) : (index.has(d.to) ? [d.to] : []);
      if (!qs.length) continue;
      const key = qs.join('|');
      (groups.get(key) || groups.set(key, []).get(key)).push([d, qs]);
    }
    for (const [, members] of groups) {
      const qs = members[0][1];
      let cx = 0, cy = 0;
      for (const q of qs) { cx += nodes[index.get(q)].x; cy += nodes[index.get(q)].y; }
      cx /= qs.length; cy /= qs.length;
      const start = qs.length === 1 ? spacing * 1.6 : 0;
      members.forEach(([d], k) => {
        const ang = k * 2.39996, rad = start + spacing * Math.sqrt(k + 0.5);
        const i = nodes.length;
        nodes.push({ id: 'app:' + d.app, kind: 'app', side, app: d.app, x: cx + rad * Math.cos(ang), y: cy + rad * Math.sin(ang), r: 2.3, fill: appFill(d, P), label: d.app });
        if (qs.length > 1) for (const q of qs) spokes.push([i, index.get(q)]);
      });
    }
  }
  return { nodes, edges, spokes };
}

function buildViews(keepCamera = true) {
  if (!S.result) return;
  if (!views) {
    const mk = (side) => new GraphView($(side === 'as_is' ? '#cv-as-is' : '#cv-target'), {
      onSelect: (d) => {
        if (!d) S.sel = null;
        else if (d.kind === 'app') S.sel = { kind: 'app', id: d.app };
        else S.sel = { kind: 'qm', side, id: d.id };
        applyHighlight();
      },
      onHover: (d, x, y) => tooltip(d, x, y),
      onCamera: (v) => {
        if (!S.link || S._syncing) return;
        S._syncing = true;
        (v === views.as_is ? views.target : views.as_is).followCamera(v);
        S._syncing = false;
      },
    });
    views = { as_is: mk('as_is'), target: mk('target') };
    window.__mqr = { views, perf: () => ({ as_is: { ...views.as_is.perf }, target: { ...views.target.perf } }) };
  }
  const P = palette();
  for (const side of ['as_is', 'target']) {
    const { nodes, edges, spokes } = buildSide(side, P, views[side]);
    views[side].setPalette(P);
    views[side].setData(nodes, edges, spokes, { keepCamera });
  }
  applyHighlight();
  renderLegend(P);
}

function renderLegend(P) {
  const sw = (colour, text, cls = '') => h('span', {}, h('i', { class: cls, style: cls === 'ring' ? '' : `background:${colour}` }), text);
  const items = [];
  const appMode = S.view === 'app';
  if (S.colour === 'change') {
    if (appMode) items.push(sw(P.kept, 'App keeps one of its queue managers'), sw(P.new, 'App moves to another queue manager'));
    else items.push(sw(P.kept, 'Queue manager kept or reused'), sw(P.retired, 'Retired'), sw(P.new, 'New'));
  } else if (S.colour === 'size') {
    items.push(sw(P.retired, 'Fewer apps'), sw(P.new, 'More apps'));
  } else {
    const vals = (X.values[S.colour] || []).filter(([v]) => v !== '(blank)');
    vals.slice(0, CAT.length).forEach(([v, n], i) => items.push(sw(CAT[i], `${v} (${n})`)));
    if (vals.length > CAT.length) items.push(sw('#a3abb3', 'Other values'));
    if (!appMode) items.push(sw('', `Mixes several ${label(S.colour)} values`, 'ring'));
  }
  if (S.result.graph.as_is.nodes.some((n) => n.kind === 'gateway')) items.push(sw(P.gateway, 'External partner gateway', 'diamond'));
  if (appMode) items.push(h('span', {}, 'Rings are queue managers; lines join an app to every queue manager it uses'));
  $('#legend').replaceChildren(...items);
}

function tooltip(d, x, y) {
  const tip = $('#tooltip');
  if (!d) { tip.hidden = true; return; }
  let lines;
  if (d.kind === 'app') {
    const a = X.dec.get(d.app);
    lines = [h('div', { class: 'id' }, a.app), h('div', {}, a.name),
      h('div', {}, a.from.length > 1 ? `On ${a.from.length} queue managers, moves to ${a.to}` : (a.stays ? `Stays on ${a.to}` : `${a.from[0] || 'Nowhere'} to ${a.to}`))];
  } else {
    const n = X.node[d.side].get(d.id);
    const status = n.kind === 'gateway' ? 'External partner gateway' : ({ kept: 'Kept', retired: 'Retired', reused: 'Reused', new: 'New' }[n.status] || '');
    lines = [h('div', { class: 'id' }, n.id), h('div', {}, `${plural(n.apps, 'app')}, ${plural(n.queues, 'queue')}, ${plural(X.nbr[d.side].get(n.id).size, 'channel partner')}`), status ? h('div', {}, status) : null];
  }
  tip.replaceChildren(...lines);
  tip.hidden = false;
  const w = tip.offsetWidth, hgt = tip.offsetHeight;
  tip.style.left = Math.min(window.innerWidth - w - 8, x + 14) + 'px';
  tip.style.top = Math.min(window.innerHeight - hgt - 8, y + 14) + 'px';
}

function applyHighlight() {
  if (!views) return;
  const sel = S.sel;
  const V = views;
  const idx = (side, id) => V[side].index.get(id);
  const add = (set, side, id) => { const i = idx(side, id); if (i !== undefined) set.add(i); };
  if (!sel) {
    for (const s of ['as_is', 'target']) { V[s].setHighlight(null); V[s].select(-1); }
  } else if (sel.kind === 'qm') {
    const other = sel.side === 'as_is' ? 'target' : 'as_is';
    const same = new Set(), far = new Set();
    add(same, sel.side, sel.id);
    for (const q of X.nbr[sel.side].get(sel.id) || []) add(same, sel.side, q);
    const apps = X.appsOn[sel.side].get(sel.id) || [];
    for (const a of apps) {
      const d = X.dec.get(a);
      for (const q of sel.side === 'as_is' ? [d.to] : d.from) add(far, other, q);
      if (S.view === 'app') { add(same, sel.side, 'app:' + a); add(far, other, 'app:' + a); }
    }
    const focus = new Set([idx(sel.side, sel.id)]);
    V[sel.side].setHighlight(same, focus); V[sel.side].select(idx(sel.side, sel.id) ?? -1);
    V[other].setHighlight(far, new Set()); V[other].select(-1);
  } else {
    const d = X.dec.get(sel.id);
    const a = new Set(), t = new Set();
    for (const q of d.from) add(a, 'as_is', q);
    add(t, 'target', d.to);
    add(a, 'as_is', 'app:' + d.app); add(t, 'target', 'app:' + d.app);
    V.as_is.setHighlight(a, new Set()); V.target.setHighlight(t, new Set());
    V.as_is.select(idx('as_is', 'app:' + d.app) ?? -1);
    V.target.select(idx('target', 'app:' + d.app) ?? idx('target', d.to) ?? -1);
  }
  renderDetail();
}

function select(sel, focus = false) {
  S.sel = sel;
  applyHighlight();
  if (!focus || !views) return;
  if (sel.kind === 'qm') views[sel.side].focusNode(views[sel.side].index.get(sel.id) ?? -1, 2.5);
  else {
    const d = X.dec.get(sel.id);
    const ti = views.target.index.get('app:' + d.app) ?? views.target.index.get(d.to);
    views.target.focusNode(ti ?? -1, 2.5);
  }
}

const qmLink = (side, id) => h('button', { class: 'link id', type: 'button', onclick: () => select({ kind: 'qm', side, id }, true) }, id);
const appLink = (id) => h('button', { class: 'link id', type: 'button', onclick: () => select({ kind: 'app', id }, true) }, id);

function countList(items, side, max = 60) {
  const cnt = new Map();
  for (const q of items) cnt.set(q, (cnt.get(q) || 0) + 1);
  const rows = [...cnt.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  return h('ul', {}, ...rows.slice(0, max).map(([q, n]) => h('li', {}, qmLink(side, q), h('span', { class: 'muted' }, plural(n, 'app')))),
    rows.length > max ? h('li', { class: 'muted' }, `and ${rows.length - max} more`) : null);
}

function appList(apps, max = 120) {
  const sorted = [...apps].sort();
  return h('ul', {}, ...sorted.slice(0, max).map((a) => h('li', {}, appLink(a), h('span', { class: 'muted' }, X.dec.get(a).name))),
    sorted.length > max ? h('li', { class: 'muted' }, `and ${sorted.length - max} more`) : null);
}

function attrSummary(n) {
  const rows = Object.entries(n.attrs || {}).slice(0, 10).map(([k, vals]) => {
    const total = vals.reduce((s, [, c]) => s + c, 0);
    const text = vals.length === 1 && total === n.apps ? `all ${vals[0][0]}` : vals.map(([v, c]) => `${v} ${c}`).join(', ');
    return h('li', {}, h('span', {}, label(k)), h('span', { class: 'muted' }, text));
  });
  return h('ul', {}, ...rows);
}

function renderDetail() {
  const box = $('#detail');
  const sel = S.sel;
  if (!sel) {
    box.replaceChildren(h('p', { class: 'hint' }, 'Click a queue manager or application to see where its applications go. Scroll to zoom, drag to pan, press 0 to fit.'));
    return;
  }
  const clear = h('button', { class: 'link', type: 'button', onclick: () => { S.sel = null; applyHighlight(); } }, 'Clear selection');
  if (sel.kind === 'qm') {
    const n = X.node[sel.side].get(sel.id);
    const apps = X.appsOn[sel.side].get(sel.id) || [];
    const status = n.kind === 'gateway' ? ['External partner gateway', ''] :
      ({ kept: ['Kept in the target', ''], retired: ['Retired', 'retired'], reused: ['Reused name', ''], new: ['New queue manager', 'new'] }[n.status] || ['', '']);
    const go = sel.side === 'as_is'
      ? [h('h3', {}, 'Its applications go to'), apps.length ? countList(apps.map((a) => X.dec.get(a).to), 'target') : h('p', { class: 'muted' }, 'No applications.')]
      : [h('h3', {}, 'Its applications come from'), countList(apps.flatMap((a) => X.dec.get(a).from), 'as_is')];
    box.replaceChildren(
      h('div', { class: 'head-row' }, h('h3', { class: 'id', style: 'margin:0' }, n.id), status[0] ? h('span', { class: `tag ${status[1]}` }, status[0]) : null,
        h('span', { class: 'muted' }, `${sel.side === 'as_is' ? 'As-is' : 'Target'}: ${plural(n.apps, 'app')}, ${plural(n.queues, 'queue')}, ${plural(X.nbr[sel.side].get(n.id).size, 'channel partner')}`), clear),
      h('div', { class: 'cols' }, h('div', {}, ...go), h('div', {}, h('h3', {}, 'Applications here'), appList(apps)),
        Object.keys(n.attrs || {}).length ? h('div', {}, h('h3', {}, 'Application attributes'), attrSummary(n)) : null));
    return;
  }
  const d = X.dec.get(sel.id);
  const moveText = d.from.length === 0 ? `Placed on ${d.to}` : d.stays && d.from.length === 1 ? `Stays on ${d.to}` :
    `Uses ${plural(d.from.length, 'queue manager')} today, one in the target: ${d.to}`;
  box.replaceChildren(
    h('div', { class: 'head-row' }, h('h3', { class: 'id', style: 'margin:0' }, d.app), h('span', {}, d.name), h('span', { class: `tag ${d.stays ? '' : 'new'}` }, d.stays ? 'Keeps a current queue manager' : 'Moves'), clear),
    h('p', { style: 'margin-top:6px' }, moveText, '. ', d.reason.charAt(0).toUpperCase() + d.reason.slice(1), '.'),
    h('div', { class: 'cols' },
      h('div', {}, h('h3', {}, 'As-is queue managers'), h('ul', {}, ...d.from.map((q) => h('li', {}, qmLink('as_is', q))))),
      h('div', {}, h('h3', {}, `Partners (${d.partners_on_same_qm} of ${d.partners} on ${d.to})`),
        h('ul', {}, ...d.top_partners.map((p) => h('li', {}, appLink(p), h('span', { class: 'muted' }, X.dec.get(p)?.to || ''))))),
      Object.keys(d.attrs).length ? h('div', {}, h('h3', {}, 'Attributes'), h('ul', {}, ...Object.entries(d.attrs).map(([k, v]) => h('li', {}, h('span', {}, label(k)), h('span', { class: 'muted' }, v || '(blank)'))))) : null));
}

function find(q) {
  q = q.trim();
  if (!q || !S.result) return;
  const lower = q.toLowerCase();
  const exactApp = S.result.decisions.find((d) => d.app.toLowerCase() === lower);
  if (exactApp) return select({ kind: 'app', id: exactApp.app }, true);
  for (const side of ['target', 'as_is']) {
    const n = S.result.graph[side].nodes.find((x) => x.id.toLowerCase() === lower);
    if (n) return select({ kind: 'qm', side, id: n.id }, true);
  }
  const part = S.result.decisions.find((d) => d.app.toLowerCase().startsWith(lower) || d.name.toLowerCase().includes(lower));
  if (part) return select({ kind: 'app', id: part.app }, true);
  setStatus(`No application or queue manager matches "${q}"`);
}

// ---------------------------------------------------------------------------------------------
// other tabs
const DIMS = {
  routing_depth: ['Routing depth', 'MQ objects a message passes through, on average', (r) => `${r.mean_objects_per_message_path} objects per message`],
  app_sprawl: ['Application sprawl', 'Share of app connections beyond one queue manager per app', (r) => `${r.qms_per_app} queue manager${r.qms_per_app === 1 ? '' : 's'} per app`],
  channel_load: ['Channel load', 'Channel pairs needed per pair of apps that talk', (r) => `${fmt(r.qm_pairs_with_channels)} pairs for ${fmt(r.communicating_app_pairs)} app pairs`],
  object_overhead: ['Plumbing objects', 'Share of definitions that exist only to route messages', (r) => `${fmt(r.managed_objects)} objects for ${fmt(r.logical_queues)} queues`],
  qm_footprint: ['Queue manager footprint', 'Queue managers per application', (r) => plural(r.queue_managers, 'queue manager')],
  blast_radius: ['Blast radius', 'Chance two message endpoints share a failing queue manager', (r) => `like ${r.effective_qms} equally loaded queue managers`],
  policy_violations: ['Rule violations', 'Weighted share of apps or queue managers breaking your rules', (r) => `${(r.policy_violation * 100).toFixed(1)}%`],
};

function barRow(name, value, max, cls, raw, valueText) {
  return h('div', { class: 'bar-row' }, h('span', {}, name),
    h('div', { class: 'track' }, h('div', { class: `fill ${cls}`, style: `width:${clamp((100 * value) / (max || 1), 0, 100)}%` })),
    h('span', { class: 'val' }, valueText ?? value.toFixed(2)), raw ? h('span', { class: 'raw' }, raw) : null);
}

function renderTab(name) {
  const R = S.result;
  const box = $(`#tab-${name}`);
  if (name === 'score') {
    const sc = R.score;
    const prof = sc.profile === 'custom' ? 'your custom weights' : `the ${sc.profile} profile`;
    const w = sc.weights;
    const dims = Object.entries(DIMS).map(([k, [title, desc, raw]]) => h('div', { class: 'dim' },
      h('div', {}, h('strong', {}, title), h('small', {}, desc)),
      h('div', { class: 'bars' }, barRow('As-is', sc.as_is.dimensions[k], 1, 'as-is', raw(sc.as_is.raw)), barRow('Target', sc.target.dimensions[k], 1, 'target', raw(sc.target.raw)))));
    const all = [['As-is estate', sc.as_is.tci, 'as-is'], ['Proposed target', sc.target.tci, 'target'], ...sc.anchors.map((x) => [x.name, x.tci, 'anchor'])];
    const top = Math.max(...all.map((x) => x[1]));
    const m = R.migration;
    box.replaceChildren(
      h('div', { class: 'section' }, h('h3', {}, `Complexity goes from ${sc.as_is.tci} to ${sc.target.tci} (${sc.change_pct}%) under ${prof}`),
        h('p', {}, `The Topology Complexity Index runs from 0 (simplest) to 100. It blends simplicity (${Math.round(w.simplicity * 100)}%), ` +
          `resilience (${Math.round(w.resilience * 100)}%) and your soft rules (${Math.round(w.policy * 100)}%). The optimiser minimises this same number.`),
        h('div', { class: 'dims' }, ...dims)),
      h('div', { class: 'section' }, h('h3', {}, 'Compared with the extremes'),
        h('p', {}, sc.anchors_ok ? 'The target scores lower than the as-is estate, a single shared queue manager, and one queue manager per app.'
          : 'The target does not beat every extreme on this weighting. Review the rules and priority.'),
        h('div', { class: 'dims wide-bars' }, ...all.map(([n, v, cls]) => barRow(n, v, top, cls, null, v.toFixed(1))))),
      h('div', { class: 'section' }, h('h3', {}, 'Under other priorities'),
        h('p', {}, 'The same before and after, scored with each profile, so the improvement does not depend on one weighting.'),
        h('table', {}, h('thead', {}, h('tr', {}, h('th', {}, 'Profile'), h('th', { class: 'num' }, 'As-is'), h('th', { class: 'num' }, 'Target'), h('th', { class: 'num' }, 'Change'))),
          h('tbody', {}, ...sc.sensitivity.map((r) => h('tr', {}, h('td', {}, cap(r.profile)), h('td', { class: 'num' }, r.as_is), h('td', { class: 'num' }, r.target), h('td', { class: 'num' }, `${r.change_pct}%`)))))),
      h('div', { class: 'section' }, h('h3', {}, 'Migration effort'),
        h('p', {}, 'What it takes to get there. This is not part of the score.'),
        h('dl', { class: 'facts', style: 'max-width:460px' },
          ...[['Apps keeping a current queue manager', m.apps_keeping_a_current_qm], ['Apps moving', m.apps_moving_to_another_qm],
            ['App connections removed', m.app_connections_removed], ['Queue managers reused', m.qms_reused], ['Queue managers created', m.qms_created],
            ['Queue managers retired', m.qms_retired], ['Queue definitions before', m.queue_definitions_before], ['Queue definitions after', m.queue_definitions_after],
            ['Channel definitions after', m.channel_definitions_after], ['Transmission queues after', m.transmission_queues_after]]
            .flatMap(([k, v]) => [h('dt', {}, k), h('dd', {}, fmt(v))]))));
  }
  if (name === 'rules') {
    const asIs = new Map(R.policies.as_is.map((p) => [p.id, p]));
    const rows = R.policies.target.map((p) => {
      const a = asIs.get(p.id) || {};
      const cfg = X.policyById.get(p.id) || p;
      const off = (r) => (r.offenders || []).length ? h('details', {}, h('summary', {}, plural(r.violating, 'offender')), h('p', { class: 'id muted' }, r.offenders.join(', '))) : null;
      return h('tr', {}, h('td', {}, describe(cfg)), h('td', {}, h('span', { class: 'tag' }, cap(p.mode))),
        h('td', {}, `${fmt(a.violating ?? 0)} of ${fmt(a.scope ?? 0)}`, off(a)),
        h('td', {}, h('span', { class: `tag ${p.violating ? 'bad' : 'good'}` }, p.violating ? `${fmt(p.violating)} of ${fmt(p.scope)}` : 'Holds'), off(p),
          p.exceptions?.length ? h('p', { class: 'muted' }, `Unavoidable: ${p.exceptions.join(', ')} (one app alone is over the queue limit)`) : null),
        h('td', { class: 'muted' }, p.detail));
    });
    box.replaceChildren(h('div', { class: 'section' }, h('h3', {}, 'Your rules, before and after'),
      h('p', {}, 'Each rule is measured on the as-is estate and on the target. Hard rules must hold in the target; soft rules are traded against simplicity.'),
      rows.length ? h('table', {}, h('thead', {}, h('tr', {}, h('th', {}, 'Rule'), h('th', {}, 'Mode'), h('th', {}, 'As-is breaks'), h('th', {}, 'Target'), h('th', {}, 'Detail'))), h('tbody', {}, ...rows))
        : h('p', { class: 'hint' }, 'No rules were on for this run.')));
  }
  if (name === 'apps') renderApps();
  if (name === 'checks') {
    const v = R.validation, q = R.quality;
    const checks = v.checks.map((c) => h('li', {}, h('span', { class: `tag ${c.passed ? 'good' : 'bad'}` }, c.passed ? 'Passed' : `Failed (${c.count})`), ' ', c.rule,
      c.examples.length ? h('ul', {}, ...c.examples.map((e) => h('li', {}, e))) : null));
    const facts = [['Rows in the file', q.rows_in_file], ['Duplicate rows removed', q.duplicate_rows_removed], ['Rows used', q.rows_used],
      ['Rows with no producer or consumer role', q.unknown_role_rows], ['External partners', q.external_partners],
      ['Transmission queues listed / required', `${fmt(q.transmission_queues_inventoried)} / ${fmt(q.transmission_queues_required)}`],
      ['Remote queues pointing nowhere', q.dangling_remote_queues.length], ['Queue names defined with two types', q.type_collisions.length],
      ['Apps with conflicting attribute values', q.attribute_conflicts.length]];
    const cand = R.optimiser.candidates;
    box.replaceChildren(
      h('div', { class: 'section' }, h('h3', {}, v.passed ? 'Every check passed' : 'Some checks failed'),
        h('p', {}, 'An independent validator rebuilds the target from its own rows and checks MQ structure, every as-is flow, and each hard rule.'),
        h('ul', { class: 'checks' }, ...checks)),
      R.warnings.length ? h('div', { class: 'section' }, h('h3', {}, `Notes to review (${R.warnings.length})`), h('ul', { class: 'notes' }, ...R.warnings.slice(0, 200).map((w) => h('li', {}, w)))) : null,
      h('div', { class: 'section' }, h('h3', {}, 'Input data'), h('dl', { class: 'facts', style: 'max-width:520px' }, ...facts.flatMap(([k, val]) => [h('dt', {}, k), h('dd', {}, fmt(val))]))),
      h('div', { class: 'section' }, h('h3', {}, 'How the target was chosen'),
        h('p', {}, 'Louvain clustering ran at several resolutions, from the configured rules and from a stricter start. The candidate with the lowest score won.'),
        h('table', {}, h('thead', {}, h('tr', {}, h('th', {}, 'Start'), h('th', { class: 'num' }, 'Resolution'), h('th', { class: 'num' }, 'Score after clustering'), h('th', { class: 'num' }, 'Final score'), h('th', { class: 'num' }, 'Queue managers'), h('th', { class: 'num' }, 'Moves'))),
          h('tbody', {}, ...cand.map((c) => h('tr', {}, h('td', {}, cap(c.start)), h('td', { class: 'num' }, c.resolution), h('td', { class: 'num' }, c.after_clustering), h('td', { class: 'num' }, c.final), h('td', { class: 'num' }, c.queue_managers), h('td', { class: 'num' }, c.moves)))))));
  }
}

function renderApps() {
  const R = S.result, A = S.apps;
  const box = $('#tab-apps');
  let rows = R.decisions;
  const q = A.q.trim().toLowerCase();
  if (q) rows = rows.filter((d) => d.app.toLowerCase().includes(q) || d.name.toLowerCase().includes(q) || d.to.toLowerCase().includes(q) || d.from.some((f) => f.toLowerCase().includes(q)));
  if (A.show === 'moved') rows = rows.filter((d) => !d.stays);
  if (A.show === 'kept') rows = rows.filter((d) => d.stays);
  if (A.attr && A.value) rows = rows.filter((d) => (d.attrs[A.attr] || '(blank)') === A.value);
  const key = { app: (d) => d.app, from: (d) => d.from.length, to: (d) => d.to, partners: (d) => (d.partners ? d.partners_on_same_qm / d.partners : 2) }[A.sort];
  rows = [...rows].sort((a, b) => { const x = key(a), y = key(b); return (x < y ? -1 : x > y ? 1 : 0) * A.dir; });
  const per = 100, pages = Math.max(1, Math.ceil(rows.length / per));
  A.page = clamp(A.page, 0, pages - 1);
  const page = rows.slice(A.page * per, A.page * per + per);
  const attrs = R.graph.attributes;
  const rerender = () => { A.page = 0; renderApps(); };
  const th = (k, t, cls = '') => h('th', { class: `sortable ${cls}`, 'aria-sort': A.sort === k ? (A.dir > 0 ? 'ascending' : 'descending') : null,
    onclick: () => { A.dir = A.sort === k ? -A.dir : 1; A.sort = k; renderApps(); } }, t + (A.sort === k ? (A.dir > 0 ? ' ▲' : ' ▼') : ''));
  const search = h('input', { type: 'search', placeholder: 'Search apps or queue managers', value: A.q, 'aria-label': 'Search applications' });
  search.addEventListener('input', () => { A.q = search.value; clearTimeout(S._qt); S._qt = setTimeout(() => { rerender(); const el = $('#tab-apps input[type=search]'); el.focus(); el.setSelectionRange(el.value.length, el.value.length); }, 200); });
  const attrSel = h('select', { 'aria-label': 'Filter by attribute', onchange: (e) => { A.attr = e.target.value; A.value = ''; rerender(); } },
    h('option', { value: '' }, 'Any attribute'), ...attrs.map((a) => h('option', { value: a, selected: A.attr === a }, label(a))));
  const valSel = A.attr ? h('select', { 'aria-label': 'Attribute value', onchange: (e) => { A.value = e.target.value; rerender(); } },
    h('option', { value: '' }, 'Any value'), ...(X.values[A.attr] || []).map(([v, n]) => h('option', { value: v, selected: A.value === v }, `${v} (${n})`))) : null;
  const show = h('select', { 'aria-label': 'Show', onchange: (e) => { A.show = e.target.value; rerender(); } },
    ...[['all', 'All apps'], ['moved', 'Apps that move'], ['kept', 'Apps keeping a current queue manager']].map(([v, t]) => h('option', { value: v, selected: A.show === v }, t)));
  box.replaceChildren(h('div', { class: 'section', style: 'max-width:none' },
    h('h3', {}, `${plural(rows.length, 'application')}`),
    h('div', { class: 'filters' }, search, show, attrSel, valSel),
    h('table', {}, h('thead', {}, h('tr', {}, th('app', 'App'), h('th', {}, 'Name'), th('from', 'As-is queue managers'), th('to', 'Target'), th('partners', 'Partners on the same QM', 'num'), h('th', {}, 'Why'))),
      h('tbody', {}, ...page.map((d) => h('tr', { class: 'clickable', onclick: () => { selectTab('topology'); select({ kind: 'app', id: d.app }, true); } },
        h('td', { class: 'id' }, d.app), h('td', {}, d.name),
        h('td', { class: 'id' }, d.from.length > 3 ? `${d.from.slice(0, 3).join(', ')} and ${d.from.length - 3} more` : d.from.join(', ')),
        h('td', { class: 'id' }, d.to), h('td', { class: 'num' }, d.partners ? `${d.partners_on_same_qm} of ${d.partners}` : 'None'),
        h('td', { class: 'muted' }, d.reason))))),
    h('div', { class: 'pager' }, h('button', { class: 'btn small', type: 'button', disabled: A.page === 0, onclick: () => { A.page--; renderApps(); } }, 'Previous'),
      `Page ${A.page + 1} of ${pages}`, h('button', { class: 'btn small', type: 'button', disabled: A.page >= pages - 1, onclick: () => { A.page++; renderApps(); } }, 'Next'))));
}

// ---------------------------------------------------------------------------------------------
// downloads
function download(name, text, type = 'text/plain') {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = h('a', { href: url, download: name });
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
  $('#downloads').open = false;
}

function decisionsCsv() {
  const R = S.result;
  const attrs = R.graph.attributes;
  const q = (v) => { const s = String(v ?? ''); return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s; };
  const head = ['app', 'name', 'as_is_queue_managers', 'target_queue_manager', 'keeps_current_qm', 'partners', 'partners_on_same_qm', 'reason', ...attrs];
  const lines = [head.join(',')];
  for (const d of R.decisions) lines.push([d.app, d.name, d.from.join(' '), d.to, d.stays ? 'yes' : 'no', d.partners, d.partners_on_same_qm, d.reason, ...attrs.map((a) => d.attrs[a])].map(q).join(','));
  return lines.join('\n') + '\n';
}

function renderDownloads() {
  const R = S.result;
  const item = (title, desc, fn) => h('button', { type: 'button', onclick: fn }, title, h('small', {}, desc));
  const report = () => { const { files, ...rest } = R; return JSON.stringify(rest, null, 1); };
  $('#download-menu').replaceChildren(
    item('Target topology (CSV)', 'Same columns as your input', () => download('target_topology.csv', R.files['target_topology.csv'], 'text/csv')),
    item('Channels (CSV)', 'Sender and receiver pairs', () => download('target_channels.csv', R.files['target_channels.csv'], 'text/csv')),
    item('MQSC script', 'DEFINE commands per queue manager', () => download('target.mqsc', R.files['target.mqsc'])),
    item('Decisions (CSV)', 'Where each app goes and why', () => download('decisions.csv', decisionsCsv(), 'text/csv')),
    item('Full report (JSON)', 'Scores, rules, checks, decisions', () => download('report.json', report(), 'application/json')),
    item('Configuration (YAML)', 'Rerun with the command line tool', () => download('config.yaml', JSON.stringify(S.ran, null, 2) + '\n', 'text/yaml')));
  $('#downloads summary').setAttribute('aria-disabled', 'false');
}

// ---------------------------------------------------------------------------------------------
// tabs and wiring
function selectTab(name) {
  for (const b of document.querySelectorAll('.tabs [role=tab]')) b.setAttribute('aria-selected', String(b.dataset.tab === name));
  for (const t of ['topology', 'score', 'rules', 'apps', 'checks']) $(`#tab-${t}`).hidden = t !== name;
}

function wire() {
  const file = $('#file');
  $('#pick-file').addEventListener('click', () => file.click());
  file.addEventListener('change', () => { const f = file.files[0]; if (f) choose({ kind: 'file', file: f, name: f.name }); file.value = ''; });
  const sample = () => choose({ kind: 'sample', name: 'Sample estate' });
  $('#use-sample').addEventListener('click', sample);
  $('#empty-sample').addEventListener('click', async () => { await sample(); if (S.info?.ready) run(); });
  const drop = $('#drop');
  drop.addEventListener('dragover', (e) => { e.preventDefault(); drop.classList.add('over'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('over'));
  drop.addEventListener('drop', (e) => { e.preventDefault(); drop.classList.remove('over'); const f = e.dataTransfer.files[0]; if (f) choose({ kind: 'file', file: f, name: f.name }); });
  $('#run').addEventListener('click', run);
  for (const b of document.querySelectorAll('.tabs [role=tab]')) b.addEventListener('click', () => selectTab(b.dataset.tab));
  for (const b of document.querySelectorAll('#view-mode button')) {
    b.addEventListener('click', () => {
      S.view = b.dataset.v;
      for (const x of document.querySelectorAll('#view-mode button')) x.setAttribute('aria-pressed', String(x === b));
      buildViews(false);
    });
  }
  $('#colour').addEventListener('change', (e) => { S.colour = e.target.value; buildViews(true); });
  $('#show-edges').addEventListener('change', (e) => { S.showEdges = e.target.checked; buildViews(true); });
  const mw = $('#min-weight');
  mw.addEventListener('input', () => { S.minWeight = parseInt(mw.value, 10); $('#min-weight-out').textContent = mw.value; clearTimeout(S._mw); S._mw = setTimeout(() => buildViews(true), 60); });
  $('#link-cams').addEventListener('change', (e) => { S.link = e.target.checked; if (S.link && views) views.target.followCamera(views.as_is); });
  $('#find').addEventListener('submit', (e) => { e.preventDefault(); find($('#find-q').value); });
  $('#reset-view').addEventListener('click', () => { if (views) { views.as_is.fitView(); views.target.fitView(); } });
  document.addEventListener('click', (e) => { const d = $('#downloads'); if (d.open && !d.contains(e.target)) d.open = false; });
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => buildViews(true));
}

wire();
