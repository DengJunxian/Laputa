"""Self-contained static report containing only the already scoped read model."""
import base64
import hashlib

from civitas.storage.commit_store import json_bytes


_JS = r"""
const data = JSON.parse(document.querySelector('#view-data').textContent);
const el = (tag, value, cls) => {
  const node = document.createElement(tag);
  if (value !== undefined) node.textContent = value;
  if (cls) node.className = cls;
  return node;
};
const ui = Object.fromEntries(['title','run','scope','tick','previous','next','meta','metrics',
  'tabs','section','count','filter','grid','empty','comparison','comparison-grid','baseline',
  'arm-tools','arm','download','missing','workspace','actor-tools','actor'].map(id => [id, document.getElementById(id)]));
const paired = Boolean(data.periods);
const periods = paired ? data.periods : data.views.map(view => ({tick:view.cursor.tick}));
const firstAfterBaseline = data.baseline ? periods.findIndex(p => p.tick > data.baseline.tick) : -1;
let index = paired ? Math.max(0,firstAfterBaseline) : periods.length - 1;
let arm = 0;
let active = null;
const currentView = () => paired ? periods[index].arms[arm].view : data.views[index];
const states = {committed:'已提交',completed:'已完成',checkpointed:'已保存检查点',
                failed:'运行失败',cancelled:'已取消',projection_failed:'投影失败',
                uncommitted:'此期无提交',unrecorded_observe:'此期没有留存主体观察',
                actor_unregistered:'此期未登记该主体'};
const text = value => value === null || value === undefined ? '—' : String(value);
const subject = data.audience === 'agent';
ui.title.textContent = paired ? (subject ? '主体观察对照' : '联合世界对照') : (subject ? '主体观察' : '运行检查');
ui.scope.textContent = subject ? data.actor_id : '研究者';
for (const {tick} of periods) {
  const option = el('option', '第 ' + tick + ' 期');
  option.value = tick;
  ui.tick.append(option);
}
if (paired) {
  for (const [slot,{label}] of data.arms.entries()) {
    const button = el('button',label);
    button.type = 'button';
    button.addEventListener('click', () => { arm = slot; show(); });
    ui.arm.append(button);
  }
  ui['arm-tools'].hidden = false;
  ui.actor.addEventListener('change', drawTable);
  ui.download.addEventListener('click', () => {
    const frame = periods[index].arms[arm];
    if (!frame.export) return;
    const body = frame.exact_export ?? JSON.stringify({
      schema_version:data.schema_version, authority:false, audience:data.audience,
      actor_id:data.actor_id, run_id:data.arms[arm].id, views:[frame.view]
    }) + '\n';
    const href = URL.createObjectURL(new Blob([body],{type:'application/json'}));
    const link = el('a');
    link.href = href;
    link.download = frame.export;
    link.click();
    setTimeout(() => URL.revokeObjectURL(href),1000);
  });
}
function setPeriod(change) {
  index = Math.max(0, Math.min(periods.length - 1, index + change));
  show();
}
ui.previous.addEventListener('click', () => setPeriod(-1));
ui.next.addEventListener('click', () => setPeriod(1));
ui.tick.addEventListener('change', () => { index = ui.tick.selectedIndex; show(); });
ui.filter.addEventListener('input', () => drawTable());
const comparisonMetrics = [
  ['wages_paid_minor','实付工资','货币'], ['produced_units','产出','件'], ['consumed_units','消费','件'],
  ['deposit_claims_minor','期末存款','货币'], ['bank_reserves_minor','期末准备金','货币'],
  ['disclosure_recipients','本期银行通知触达','人'], ['disclosure_paid_minor','银行披露实付','货币'],
  ['support_disbursed_minor','国库支持拨付','货币'], ['support_recovered_minor','国库支持收回','货币'],
  ['treasury_program_net_outlay_minor','国库政策净支出（税前）','货币'], ['treasury_cash_minor','期末国库现金','货币']
];
function drawComparison(period) {
  const head = el('thead'), title = el('tr'), body = el('tbody');
  for (const label of ['第 ' + period.tick + ' 期', ...data.arms.map(a => a.label)]) {
    const th = el('th',label);
    th.scope = 'col';
    title.append(th);
  }
  for (const [i,frame] of period.arms.entries()) {
    const status = frame.view ? frame.view.cursor.source_commit_hash : states[frame.status];
    const cell = el('small',frame.view ? status.slice(0,12) : status);
    cell.title = status;
    title.children[i+1].append(cell);
  }
  head.append(title);
  for (const [key,label,unit] of comparisonMetrics) {
    const row = el('tr'), name = el('th',label);
    name.scope = 'row';
    name.append(el('small',unit));
    row.append(name,...period.arms.map(frame => el('td',text(frame.view?.comparison_metrics[key]))));
    body.append(row);
  }
  ui['comparison-grid'].replaceChildren(head,body);
  ui.comparison.hidden = false;
  const {baseline,root_seeds} = data;
  const roots = root_seeds.filter(seed => seed !== null);
  const sample = roots.length ? '随机根 ' + [...new Set(roots)].map(String).join('／') + '。' : '';
  const status = {matched:'经济状态匹配',different:'经济状态有差异',unavailable:'缺少提交'};
  ui.baseline.textContent = (baseline ?
    '第 ' + baseline.tick + ' 期基线 · ' + status[baseline.status] + '（比较时排除制度配置摘要）。' : '') +
    sample + '金额以最小货币单位计。';
}
function show() {
  const view = currentView();
  ui.tick.selectedIndex = index;
  ui.previous.disabled = index === 0;
  ui.next.disabled = index === periods.length - 1;
  ui.run.textContent = paired ? data.arms[arm].id : data.run_id;
  ui.workspace.hidden = !view;
  ui.missing.hidden = Boolean(view);
  if (paired) {
    const period = periods[index], current = period.arms[arm];
    for (const [slot,button] of [...ui.arm.children].entries()) {
      button.setAttribute('aria-pressed', String(slot === arm));
    }
    if (!subject) drawComparison(period);
    ui.download.hidden = !current.export;
    ui.missing.textContent = data.arms[arm].label + ' · 第 ' + period.tick + ' 期 · ' + (states[current.status] || '');
  }
  ui.meta.replaceChildren();
  if (!view) return;
  for (const [label,value] of [
    ['提交',view.cursor.source_commit_hash], ['状态摘要',view.cursor.state_hash],
    ...(subject ? [['观察摘要',view.observation_hash]] :
        [['状态',states[view.status] || text(view.status)],['数据',view.data_origin.startsWith('synthetic')?'合成':view.data_origin],
         ['模型',view.model_versions.join('、') || '规则'],
         ...(paired ? [['决策席位',text(view.metrics.find(([name]) => name === '当期主体')?.[1])]] : [])])
  ]) {
    const row = el('div');
    const val = el('dd',value.length === 64 ? value.slice(0,16) : value);
    val.title = value;
    row.append(el('dt',label),val);
    ui.meta.append(row);
  }
  ui.metrics.replaceChildren();
  for (const [label,value,unit] of view.metrics || []) {
    const item = el('div',undefined,'metric');
    item.append(el('span',label),el('strong',value),el('small',unit));
    ui.metrics.append(item);
  }
  ui.metrics.hidden = paired || !view.metrics?.length;
  if (paired && !subject) {
    const priorActor = ui.actor.value;
    const actors = view.sections.find(s => s.key === 'accounts').rows.map(row => row[0]);
    ui.actor.replaceChildren(el('option','全部账户'));
    ui.actor.firstChild.value = '';
    for (const actor of actors) ui.actor.append(el('option',actor));
    if (actors.includes(priorActor)) ui.actor.value = priorActor;
  }
  if (!view.sections.some(s => s.key === active)) active = view.sections[0].key;
  drawTabs();
  drawTable();
}
function drawTabs() {
  ui.tabs.replaceChildren();
  for (const {key,label} of currentView().sections) {
    const button = el('button',label);
    button.type = 'button';
    button.setAttribute('aria-pressed', String(key === active));
    button.addEventListener('click', () => {
      active = key;
      ui.filter.value = '';
      drawTabs();
      [...ui.tabs.children].find(item => item.getAttribute('aria-pressed') === 'true').focus();
      drawTable();
    });
    ui.tabs.append(button);
  }
}
function makeCell(value) {
  const td = el('td');
  if (typeof value === 'object' && value !== null) {
    if (!Object.keys(value).length) td.textContent = '—';
    else {
      const details = el('details');
      const title = Array.isArray(value) ? Object.keys(value).length + ' 项' : '展开明细';
      details.append(el('summary',title),el('pre',JSON.stringify(value,null,2)));
      td.append(details);
    }
  } else td.textContent = text(value);
  return td;
}
function drawTable() {
  const section = currentView().sections.find(s => s.key === active);
  const query = ui.filter.value.trim().toLocaleLowerCase();
  const filterAccounts = paired && !subject && active === 'accounts';
  ui['actor-tools'].hidden = !filterAccounts;
  const actor = filterAccounts ? ui.actor.value : '';
  const rows = section.rows.filter(row => (!actor || row[0] === actor) &&
    (!query || JSON.stringify(row).toLocaleLowerCase().includes(query)));
  ui.section.textContent = section.label;
  ui.count.textContent = rows.length + ' 条';
  const header = el('tr');
  for (const name of section.columns) {
    const th = el('th',name);
    th.scope = 'col';
    header.append(th);
  }
  const head = el('thead'), body = el('tbody');
  head.append(header);
  for (const row of rows) {
    const tr = el('tr');
    tr.append(...row.map(makeCell));
    body.append(tr);
  }
  ui.grid.replaceChildren(head,body);
  ui.empty.hidden = rows.length > 0;
  ui.empty.textContent = query ? '没有记录符合筛选条件。' : '此游标没有这类记录。';
}
show();
"""

_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'sha256-%%HASH%%'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>EcoSim · 只读检查</title>
<style>
:root { color-scheme:light; --ink:#122633; --muted:#445d6c; --canvas:#f2f6f7; --line:#c9d8de;
  --accent:#135f69; --aside:#e4f0f0; --paper:#fff; --nav:#092c39; font:15px/1.5 system-ui,sans-serif; }
*,*::before,*::after { box-sizing:border-box }
body { margin:0; background:var(--canvas); color:var(--ink) }
[hidden] { display:none !important }
::selection { background:#b2e4dd; color:#0b2e3a }
button,select,input { font:inherit; color:inherit }
button,select { cursor:pointer }
button:disabled { opacity:.48; cursor:default }
:focus-visible { outline:3px solid #15878b; outline-offset:3px }
header { color:#f1fbff; background:var(--nav); min-height:64px; padding:0 max(24px,calc((100% - 1280px)/2));
  display:flex; gap:20px; align-items:center; justify-content:space-between; flex-wrap:wrap }
.brand { font-size:1.18rem; font-weight:760; letter-spacing:.025em }
.header-context { display:flex; align-items:center; gap:14px; font-size:.84rem; overflow-wrap:anywhere }
#run { color:#c1d8df }
#scope { border-left:1px solid #4b727f; padding-left:14px }
main { max-width:1280px; padding:34px 28px; margin:auto }
.hero { display:flex; align-items:flex-end; justify-content:space-between; gap:20px; margin-bottom:24px }
h1,h2 { margin:0; line-height:1.2 }
h1 { font-size:2.1rem; letter-spacing:-.025em }
h2 { font-size:1.3rem }
.timeline { display:flex; gap:8px; align-items:center; flex-wrap:wrap }
.timeline label,.toolbar label { color:var(--muted); font-size:.85rem }
select,button,input { border:1px solid var(--line); background:var(--paper); border-radius:5px; min-height:36px; padding:5px 10px }
button:not(:disabled):hover,select:hover { border-color:var(--accent) }
dl#meta { display:flex; flex-wrap:wrap; gap:8px 28px; margin:0 0 22px; padding:13px 0;
  border-block:1px solid var(--line) }
#meta div { display:flex; align-items:center; gap:8px }
dt { font-size:.83rem; color:var(--muted) }
dd { margin:0; font-variant-numeric:tabular-nums; font-size:.88rem }
.metrics { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); border-block:1px solid var(--line);
  margin:0 0 26px; background:var(--aside) }
.metric { padding:16px 18px; display:grid; grid-template-columns:auto 1fr; gap:0 8px; align-items:baseline }
.metric + .metric { border-left:1px solid var(--line) }
.metric span { grid-column:1/-1; color:var(--muted); font-size:.85rem }
.metric strong { font-variant-numeric:tabular-nums; font-size:1.5rem; letter-spacing:-.025em; overflow-wrap:anywhere }
.metric small { color:var(--muted); font-size:.74rem }
.workspace { display:grid; grid-template-columns:170px minmax(0,1fr); gap:20px; align-items:start }
nav { display:grid; gap:4px }
nav button { background:transparent; text-align:left; border-color:transparent }
nav button[aria-pressed=true] { background:var(--aside); border-color:#9fbdbf; color:#124c55; font-weight:700 }
article { background:var(--paper); border:1px solid var(--line); border-radius:6px; min-width:0 }
.toolbar { min-height:67px; display:flex; gap:16px; justify-content:space-between; align-items:center;
  padding:14px 16px; flex-wrap:wrap; border-bottom:1px solid var(--line) }
.toolbar > div { display:flex; align-items:center; gap:12px }
#count { color:var(--muted); font-size:.85rem; font-variant-numeric:tabular-nums }
input { max-width:min(250px,100%) }
.scroll { max-width:100%; overflow:auto }
table { width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums; font-size:.86rem }
td,th { border-bottom:1px solid #e3ebef; text-align:left; vertical-align:top; padding:10px 14px; overflow-wrap:anywhere }
th { background:#edf4f5; color:#254653; font-weight:650 }
td { min-width:85px }
tr:last-child td { border-bottom:0 }
tbody tr:hover { background:#f6fafb }
details { min-width:90px }
summary { color:var(--accent); cursor:pointer }
pre { white-space:pre-wrap; overflow-wrap:anywhere; max-width:44ch; margin:8px 0 0; font-size:.82rem }
#empty { padding:14px 16px; color:var(--muted); margin:0 }
.comparison { margin-bottom:23px }
.comparison table { table-layout:fixed; background:var(--paper); border-block:1px solid var(--line) }
.comparison th:first-child { width:48% }
.comparison thead th { border-bottom-color:var(--line) }
.comparison th:not(:first-child),.comparison td { text-align:right }
.comparison th small { color:var(--muted); font-size:.75rem; font-weight:400; margin-inline-start:8px }
.comparison thead small { display:block; margin:2px 0 0; font-variant-numeric:tabular-nums }
.comparison tbody th { background:transparent; font-weight:540 }
.comparison td { min-width:0 }
.comparison-note,#missing { color:var(--muted); margin:0 0 12px; font-size:.88rem }
.arm-tools { display:flex; gap:8px 12px; align-items:center; flex-wrap:wrap; margin:0 0 14px }
.arm-tools span { color:var(--muted); font-size:.85rem }
#arm { display:flex; flex-wrap:wrap; gap:4px }
#arm button[aria-pressed=true] { background:var(--aside); border-color:#9fbdbf; color:#124c55; font-weight:650 }
.arm-tools #download { margin-inline-start:auto; color:var(--accent) }
#actor { max-width:100% }
.toolbar #actor-tools { flex-wrap:wrap }
@media (max-width:760px) {
  header { padding:10px 18px; gap:6px }
  main { padding:26px 15px }
  .hero { align-items:flex-start; flex-direction:column }
  .workspace { grid-template-columns:minmax(0,1fr); gap:8px }
  nav { display:flex; flex-wrap:wrap }
  .metrics { grid-template-columns:repeat(2,minmax(0,1fr)) }
  .metric:nth-child(3) { border-left:0 }
  .metric:nth-child(n+3) { border-top:1px solid var(--line) }
  td,th { padding:9px 10px }
  .comparison tbody th small { display:block; margin:0 }
}
</style>
</head>
<body>
<header><span class="brand">EcoSim</span><div class="header-context"><span id="run"></span><span id="scope"></span></div></header>
<main>
  <section class="hero" aria-label="提交选择">
    <h1 id="title">运行检查</h1>
    <div class="timeline">
      <label for="tick">提交期</label>
      <button id="previous" type="button" aria-label="上一期">←</button>
      <select id="tick"></select>
      <button id="next" type="button" aria-label="下一期">→</button>
    </div>
  </section>
  <section id="comparison" class="comparison" aria-label="两臂当期指标" hidden>
    <p id="baseline" class="comparison-note"></p>
    <table id="comparison-grid"></table>
  </section>
  <div id="arm-tools" class="arm-tools" hidden>
    <span id="arm-label">详查运行</span><div id="arm" role="group" aria-labelledby="arm-label"></div>
    <button id="download" type="button" hidden>下载本期 JSON</button>
  </div>
  <dl id="meta" aria-label="提交出处"></dl>
  <p id="missing" role="status" hidden></p>
  <div id="metrics" class="metrics" aria-label="当期指标"></div>
  <div id="workspace" class="workspace">
    <nav id="tabs" aria-label="数据类别"></nav>
    <article aria-labelledby="section">
      <div class="toolbar">
        <div><h2 id="section"></h2><span id="count" aria-live="polite"></span></div>
        <div id="actor-tools" hidden><label for="actor">账户主体</label><select id="actor"></select></div>
        <div><label for="filter">筛选</label><input id="filter" type="search" autocomplete="off"></div>
      </div>
      <div class="scroll" tabindex="0" role="region" aria-labelledby="section"><table id="grid"></table></div>
      <p id="empty" hidden></p>
    </article>
  </div>
  <noscript>请启用 JavaScript 以切换提交和筛选记录。</noscript>
</main>
<script id="view-data" type="application/json">%%DATA%%</script>
<script>%%SCRIPT%%</script>
</body>
</html>
"""


def _browser_integers(value):
    if type(value) is int and abs(value) > (1 << 53)-1:
        return str(value)
    if isinstance(value, dict):
        return {k:_browser_integers(v) for k,v in value.items()}
    if isinstance(value, (tuple,list)):
        return [_browser_integers(v) for v in value]
    return value


def render(data):
    # Prevent a data value such as </script> from ending the inert JSON block.
    payload = json_bytes(_browser_integers(data)).decode().strip()
    payload = payload.replace('&',r'\u0026').replace('<',r'\u003c').replace('>',r'\u003e')
    digest = base64.b64encode(hashlib.sha256(_JS.encode()).digest()).decode()
    return _HTML.replace('%%HASH%%',digest).replace('%%SCRIPT%%',_JS).replace('%%DATA%%',payload)
