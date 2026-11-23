'use strict';

const ui = id => document.getElementById(id);
const token = location.hash.slice(1);
const workspace = ['/labor','/withdrawal'].find(prefix => location.pathname.startsWith(prefix+'/')) ?? '';
const stateNames = {frozen:'对照归档', ready:'已冻结', running:'运行中', pausing:'等待期界暂停', paused:'已暂停',
  cancelling:'等待期界取消', cancelled:'已取消', interrupted:'服务中断', completed:'已完成', failed:'运行失败'};
const goodsMetrics = [['wages_paid_minor','实付工资'],['produced_units','产出（件）'],['consumed_units','消费（件）'],
  ['deposit_claims_minor','存款'],['bank_reserves_minor','准备金'],['disclosure_recipients','银行通知触达（户）'],
  ['treasury_cash_minor','国库现金']];
const securityMetrics = [['security_close_minor','成交收盘价'],['security_volume_units','成交量（份）'],
  ['security_fill_count','成交笔数']];
const jointMetrics = [['security_mark_minor','证券标价'],['realized_close_minor','成交收盘价'],
  ['consumed_units','消费（件）'],['firm_sales_minor','企业销售额'],['wages_paid_minor','实付工资'],
  ['bank_reserves_minor','银行准备金'],['treasury_cash_minor','国库现金']];
const jointArms = {flat_weighted:'无冲击 · 权重 2', shock_weighted:'有冲击 · 权重 2'};
const supplierMetrics = [['visible_quotes','授权报价'],['supplier','选择的供应商'],
  ['settled_units','实付采购量（件）'],['buyer_debit_minor','买方实付'],
  ['supplier_credit_minor','卖方实收'],['attempts','归档尝试'],['successful_attempts','成功尝试'],
  ['fallbacks','规则回退'],['failed_attempts','失败尝试'],['external_api_calls','外部调用']];
const isSecurityView = view => Object.hasOwn(view?.comparison_metrics ?? {}, 'security_close_minor');
let jobs = [], selected = null, role = null, currentExport = null, readRevision = 0, busy = false;
let listSignature = '', compareSignature = '', canGrant = false, supplierPreview = false;
let requestedStatus = 0, appliedStatus = 0;
let choiceArchive = false, choicePreviews = [], interventionTemplates = [];
let registration = null, registrationBusy = false;
let jointTemplate = null;
const terminalRegistration = new Set(['completed','failed','interrupted']);
const registrationStates = {...stateNames, empty:'待登记', registering:'正在冻结归档', ready:'已登记',
  not_started:'未启动', incomplete:'未完成', completed:'已完成', failed:'失败'};
const registrationFailures = {registering:'登记中断或失败', running:'运行中断或审计失败',
  comparison:'两臂未通过完成验证'};
const pending = new Map();

function node(tag, text) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  return element;
}

function notice(message, error = false) {
  ui('message').textContent = message;
  ui('message').classList.toggle('error', error);
}

async function request(path, body) {
  const headers = {Authorization: 'Bearer ' + token};
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const response = await fetch(workspace + path, {method: body === undefined ? 'GET' : 'POST', headers, body});
  if (!response.ok) {
    const issue = await response.json();
    const error = new Error(issue.error);
    error.status = response.status;
    throw error;
  }
  return response;
}

function options(select, first, values) {
  const old = select.value;
  select.replaceChildren(new Option(first, ''), ...values.map(([key,label]) => new Option(label,key)));
  if ([...select.options].some(option => option.value === old)) select.value = old;
}

function render() {
  ui('freeze').disabled = ui('fork-freeze').disabled = ui('template-freeze').disabled = busy;
  ui('joint-freeze').disabled = busy;
  const job = jobs.find(row => row.id === selected);
  ui('empty').hidden = Boolean(job) || role !== 'researcher';
  ui('run').hidden = !job;
  const signature = JSON.stringify(jobs.map(({id,label,status}) => [id,label,status,id === selected]));
  if (role === 'researcher' && signature !== listSignature) {
    listSignature = signature;
    const active = document.activeElement?.dataset.job;
    const controls = jobs.map(row => {
      const button = node('button');
      button.dataset.job = row.id;
      button.setAttribute('aria-pressed', String(row.id === selected));
      button.append(node('strong',row.label), node('small',stateNames[row.status] ?? row.status));
      button.addEventListener('click', () => { selectJob(row.id, true); });
      return button;
    });
    ui('jobs').replaceChildren(...controls);
    controls.find(button => button.dataset.job === active)?.focus();
  }
  if (!job) return;
  ui('run-title').textContent = job.label;
  ui('state').textContent = stateNames[job.status] ?? job.status;
  ui('state').dataset.state = job.status;
  ui('cursor').textContent = job.tick === null ? '未启动' : `已提交 ${job.tick} / ${job.terminal_tick} 期` +
    (job.commit_hash ? `  ·  ${job.commit_hash.slice(0,12)}` : '');
  ui('cursor').title = job.commit_hash ?? '';
  ui('controls').hidden = role !== 'researcher';
  ui('fork').hidden = supplierPreview || role !== 'researcher' || !['paused','completed'].includes(job.status);
  ui('start').disabled = busy || job.status !== 'ready';
  ui('pause').disabled = busy || job.status !== 'running';
  ui('resume').disabled = busy || !['paused','failed','interrupted'].includes(job.status);
  ui('cancel').disabled = busy || !['ready','running','pausing','paused','failed','interrupted'].includes(job.status);
  ui('grant').hidden = !canGrant || role !== 'researcher';
  if (ui('tick').value === '' && job.tick !== null) ui('tick').value = job.tick;
  if (ui('fork-tick').value === '' && job.tick !== null) ui('fork-tick').value = job.tick;
  const otherJobs = jobs.filter(row => row.id !== selected).map(row => [row.id,row.label]);
  if (JSON.stringify(otherJobs) !== compareSignature) {
    compareSignature = JSON.stringify(otherJobs);
    options(ui('compare'), '无', otherJobs);
  }
  if (job.error && job.status === 'failed') notice(job.error, true);
}

function clearView() {
  readRevision += 1;
  currentExport = null;
  canGrant = false;
  ui('download').disabled = true;
  ui('download').textContent = '导出 JSON';
  ui('grant').hidden = true;
  ui('observer-link').hidden = true;
  ui('result').replaceChildren();
  ui('view-status').textContent = '';
  ui('view-status').removeAttribute('title');
}

function selectJob(key, read) {
  if (!jobs.some(job => job.id === key)) return;
  selected = key;
  ui('until').value = '';
  ui('fork-tick').value = '';
  ui('fork').open = false;
  notice('');
  clearView();
  render();
  if (read && ui('tick').value !== '') readView();
}

async function refresh() {
  const revision = ++requestedStatus;
  const response = await request('/api/status');
  const data = await response.json();
  if (revision < appliedStatus) return;
  appliedStatus = revision;
  const first = role === null;
  role = data.role;
  if (first && data.research_preview && role === 'researcher') {
    const navigation = ui('preview-nav');
    for (const link of navigation.querySelectorAll('a')) {
      if (link.pathname === workspace+'/') link.setAttribute('aria-current','page');
      link.hash = token;
    }
    navigation.hidden = false;
  }
  choiceArchive = Boolean(data.choice_archive);
  if (first && role === 'researcher' && data.intervention_templates?.length) {
    interventionTemplates = data.intervention_templates;
    ui('template-kind').replaceChildren(...interventionTemplates.map(row => new Option(row.label,row.kind)));
    ui('intervention-templates').hidden = false;
    renderTemplate();
  }
  const incoming = data.withdrawal_registration ?? data.labor_registration;
  if (incoming) {
    const firstReview = registration === null;
    registration = incoming;
    if (firstReview) {
      const {proposal} = registration;
      const labor = Boolean(data.labor_registration);
      ui('registration-title').textContent = labor ? '登记岗位对照' : '登记提款对照';
      const endpoint = labor ? `岗位第 ${proposal.offer_tick} 期发布 · 主指标为新合同实付工资`
        : `核验通知第 ${proposal.notice_tick} 期 · 主指标为截至期末的实际支付金额`;
      ui('registration-window').textContent = `根种子 ${proposal.root_seed} · 执行第 1–${proposal.final_tick} 期 · ${endpoint}`;
      ui('registration-config').textContent = JSON.stringify(proposal.arms,null,2);
    }
    renderRegistration();
    ui('scope').textContent = '研究者';
    ui('layout').classList.add('observer');
    ui('registration-panel').hidden = false;
    if (!data.choice_previews) return;
  }
  if (data.choice_previews) {
    if (!ui('choice-panel').hidden) return;
    choicePreviews = data.choice_previews;
    ui('scope').textContent = '研究者';
    ui('layout').classList.add('observer');
    ui('choice-panel').hidden = false;
    const kinds = ui('choice-kind');
    kinds.replaceChildren(...choicePreviews.map(row => new Option(row.label,row.kind)));
    chooseExperiment();
    return;
  }
  supplierPreview = data.supplier_preview;
  jobs = data.jobs;
  if (first) {
    ui('researcher').hidden = role !== 'researcher';
    ui('layout').classList.toggle('observer', role === 'observer');
    ui('actor-label').hidden = ui('compare-label').hidden = role !== 'researcher';
    ui('scope').textContent = role === 'researcher' ? '研究者' : data.actor;
    if (role === 'researcher') ui('config').value = data.template;
    jointTemplate = data.joint_template;
    if (jointTemplate) {
      ui('joint-template').hidden = false;
      ui('joint-template-info').textContent = `${jointTemplate.ticks} 期 · ${jointTemplate.config_hash.slice(0,12)}`;
      ui('joint-template-info').title = jointTemplate.config_hash;
    }
    ui('joint-pilot').hidden = !data.joint_pilot;
    selected = jobs.at(-1)?.id ?? null;
  }
  render();
  if (first && role === 'observer' && ui('tick').value !== '') readView();
}

function renderRegistration() {
  const {phase,plan_hash,archive_hash,proposal,arms,denominators,failure} = registration;
  ui('registration-phase').textContent = phase === 'completed' ? '已完成独立审计' : registrationStates[phase];
  ui('registration-phase').dataset.state = phase;
  ui('registration-plan').textContent = plan_hash ? `计划散列 ${plan_hash} · 合成归档散列 ${archive_hash}` : '';
  ui('registration-register').disabled = registrationBusy || phase !== 'empty';
  ui('registration-start').disabled = registrationBusy || phase !== 'ready';
  ui('registration-register').hidden = phase !== 'empty' && phase !== 'registering';
  ui('registration-start').hidden = terminalRegistration.has(phase);
  for (const [side,arm] of Object.entries(arms)) {
    ui(`registration-${side}-state`).textContent = registrationStates[arm.state];
    const cursor = ui(`registration-${side}-cursor`);
    cursor.textContent = arm.tick === null ? '—' : `${arm.tick} / ${proposal.final_tick}`;
    cursor.title = arm.commit_hash ?? '';
  }
  const d = denominators;
  ui('registration-denominators').textContent = `登记 ${d.assigned_arms} · 已启动 ${d.started_arms} · 完成 ${d.completed_arms} · 失败 ${d.failed_arms} · 未完成 ${d.incomplete_arms}`;
  ui('registration-failure').textContent = failure ? registrationFailures[failure] : '';
}

async function sendRegistration(action) {
  if (registrationBusy) return;
  const expected_hash = action === 'register' ? registration.template_hash : registration.plan_hash;
  registrationBusy = true;
  renderRegistration();
  try {
    const kind = Object.hasOwn(registration.proposal,'offer_tick') ? 'labor' : 'withdrawal';
    await request(`/api/${kind}-registration`,JSON.stringify({action,expected_hash}));
    notice('');
    await refresh();
  } catch (error) {
    notice(error.message,true);
  } finally {
    registrationBusy = false;
    renderRegistration();
  }
}
ui('registration-register').addEventListener('click', () => sendRegistration('register'));
ui('registration-start').addEventListener('click', () => sendRegistration('start'));

async function send(action, fields, rawConfig) {
  if (busy) return;
  busy = true;
  render();
  const signature = JSON.stringify({action, ...fields, rawConfig});
  const requestId = pending.get(signature) ?? crypto.randomUUID().replaceAll('-','');
  pending.set(signature, requestId);
  const envelope = JSON.stringify({action, request_id:requestId, ...fields});
  const body = rawConfig === undefined ? envelope : envelope.slice(0,-1) + ',"config":' + rawConfig + '}';
  try {
    const receipt = await (await request('/api/control', body)).json();
    pending.delete(signature);
    await refresh();
    if (['freeze','fork','freeze-intervention','freeze-joint-template'].includes(action)) {
      selectJob(receipt.job, false);
      if (action === 'freeze') ui('configure').open = false;
    }
    notice({freeze:'已冻结配置。','freeze-intervention':'已冻结处理臂。','freeze-joint-template':'已冻结联合配置。',
      fork:'已冻结分支。',start:'已启动。',resume:'已请求恢复。',
      pause:'将在当前期完成后暂停。',cancel:'已提交取消请求。'}[action]);
  } catch (error) {
    if (error.status && error.status < 500) pending.delete(signature);
    notice(error.message, true);
  } finally {
    busy = false;
    render();
  }
}

function makeTable(columns, rows) {
  const wrap = node('div');
  wrap.className = 'table-wrap';
  wrap.tabIndex = 0;
  const table = node('table');
  const header = node('tr');
  for (const column of columns) {
    const th = node('th',column);
    th.scope = 'col';
    header.append(th);
  }
  const head = node('thead'), body = node('tbody');
  head.append(header);
  for (const row of rows) {
    const line = node('tr');
    for (const value of row) {
      const cell = node('td');
      if (value instanceof HTMLElement) {
        cell.append(value);
      } else if (value !== null && typeof value === 'object') {
        const details = node('details');
        const kinds = {labor_income_shock:'缩时与实付凭据', bank_statement:'银行账单',
          closed_social_context:'社会情境', fiscal_announcement:'财政公告', security_wealth:'可见证券估值'};
        const label = kinds[value.kind] ?? '查看详情';
        details.open = value.kind === 'labor_income_shock';
        details.append(node('summary',label),node('pre',JSON.stringify(value,null,2)));
        cell.append(details);
      } else cell.textContent = value ?? '—';
      line.append(cell);
    }
    body.append(line);
  }
  table.append(head, body);
  wrap.append(table);
  return wrap;
}

function network(view, typed) {
  const details = node('details');
  const edges = typed ?? view.communication_edges;
  const title = typed ? '该期网络与传播' : '通信关系';
  details.append(node('summary',`${title}（${edges.length} 条）`));
  if (!edges.length) return details;
  const canvas = node('canvas');
  canvas.width = 640;
  canvas.height = 320;
  canvas.setAttribute('aria-hidden','true');
  const actors = [...new Set(edges.flatMap(edge => edge.slice(0,2)))].sort();
  const positions = new Map(actors.map((actor,index) => {
    const angle = 2 * Math.PI * index / actors.length;
    return [actor,[320 + 264*Math.cos(angle), 160 + 134*Math.sin(angle)]];
  }));
  const ctx = canvas.getContext('2d');
  ctx.strokeStyle = '#a1adab';
  ctx.beginPath();
  for (const [source,target] of edges) {ctx.moveTo(...positions.get(source)); ctx.lineTo(...positions.get(target));}
  ctx.stroke();
  ctx.fillStyle = '#08655a';
  for (const [x,y] of positions.values()) {ctx.beginPath(); ctx.arc(x,y,3,0,2*Math.PI); ctx.fill();}
  details.append(canvas, typed ? makeTable(['来源','目标','关系','敞口、份额或消息'],
    edges.map(([source,target,kind,value]) => [source,target,edgeNames[kind] ?? kind,value]))
    : makeTable(['来源','目标'], edges));
  return details;
}

function ledgerAudit(key, cursor, label) {
  const revision = readRevision;
  const details = node('details');
  details.className = 'ledger-audit';
  const status = node('p');
  status.className = 'notice';
  status.setAttribute('role', 'status');
  status.tabIndex = -1;
  const tables = node('div');
  const controls = node('div');
  controls.className = 'actions';
  const build = node('button', '校验账本');
  const previous = node('button', '上一页');
  const next = node('button', '下一页');
  previous.hidden = next.hidden = true;
  controls.append(build, previous, next);
  details.append(node('summary', label ? `${label} · 账本核对` : '账本核对'), controls, status, tables);

  const requestCursor = {job:key, tick:cursor.tick, commit:cursor.source_commit_hash};
  let currentPage = 1, lastPage = 1;
  async function read(page, create = false) {
    build.disabled = previous.disabled = next.disabled = true;
    status.classList.remove('error');
    status.textContent = create ? '正在核对分录和账户…' : '正在读取账户页…';
    try {
      const response = create
        ? await request('/api/research-index', JSON.stringify(requestCursor))
        : await request('/api/research-page?' + new URLSearchParams({...requestCursor, page}));
      const data = await response.json();
      if (revision !== readRevision) return;
      if (data.cursor?.tick !== cursor.tick || data.cursor.source_commit_hash !== cursor.source_commit_hash ||
          data.cursor.state_hash !== cursor.state_hash || data.accounts?.page !== page) {
        throw new Error('核对结果与所选提交不一致，请重新查看提交。');
      }
      const {metrics, denominators, accounts} = data;
      currentPage = accounts.page;
      lastPage = Math.ceil(accounts.total / accounts.page_size);
      const positions = Object.hasOwn(accounts.rows[0] ?? {}, 'security_units');
      tables.replaceChildren(
        makeTable(['账本复算','数值'], [['现金总额',metrics.cash_minor],['产出（件）',metrics.produced_units],
          ['消费（件）',metrics.consumed_units],['主体数',metrics.actor_count],['决策数',denominators.decisions],
          ['回执数',denominators.receipts]]),
        makeTable(['账户','现金','库存（件）','应收工资','存款资产','准备金',
          ...(positions ? ['证券份额','贷款负债'] : [])], accounts.rows.map(row =>
          [row.account_id,row.cash_minor,row.goods_units,row.wage_receivable_minor,row.deposit_asset_minor,
            row.bank_reserve_minor, ...(positions ? [row.security_units,row.loan_payable_minor] : [])])));
      status.textContent = `第 ${cursor.tick} 期 · 第 ${currentPage} / ${lastPage} 页 · ${accounts.total} 个账户`;
      status.title = cursor.source_commit_hash;
      build.hidden = true;
      previous.hidden = next.hidden = lastPage === 1;
      if (create) status.focus({preventScroll:true});
    } catch (error) {
      if (revision !== readRevision) return;
      status.textContent = error.message;
      status.classList.add('error');
    } finally {
      build.disabled = false;
      previous.disabled = currentPage <= 1;
      next.disabled = currentPage >= lastPage;
    }
  }
  build.addEventListener('click', () => read(1, true));
  previous.addEventListener('click', () => read(currentPage - 1));
  next.addEventListener('click', () => read(currentPage + 1));
  return details;
}

const interventionMetrics = {
  wages_paid_minor:'实付工资', affected_wage_paid_minor:'指定住户实付工资',
  produced_units:'产出（件）', consumed_units:'消费（件）', bank_reserves_minor:'准备金',
  disclosure_recipients:'通知当期触达（户）',
  queued_minor:'接通知后入队', withdrawals_paid_minor:'当期提款实付', outstanding_minor:'期末未付',
  fundamental_minor:'基本面价格', security_close_minor:'成交收盘价', security_volume_units:'成交量（份）',
  security_paid_minor:'成交实付', tax_collected_minor:'实缴税', transfers_paid_minor:'实付转移',
  disclosure_paid_minor:'公告成本', treasury_cash_minor:'国库现金',
};
const edgeNames = {communication:'通信联系', deposit:'存款债权', credit:'贷款敞口',
  fill:'实际成交', delivered_message:'已见消息', employment:'当期已履约雇佣', holdings:'证券持仓'};

function jointRelations(layers) {
  const details = node('details'), label = node('label','关系层'), select = node('select');
  const body = node('div');
  details.append(node('summary','多层关系'), label, body);
  label.append(select);
  select.replaceChildren(...Object.keys(layers).map(key => new Option(edgeNames[key],key)));
  function update() {
    const {edges, total, limit} = layers[select.value];
    const note = edges.length < total ? `显示 ${total} 条中的 ${limit} 条，按来源和目标 ID 排序。`
      : `${total} 条关系`;
    const graph = network(null, edges);
    graph.open = true;
    graph.querySelector('summary').textContent = edgeNames[select.value];
    body.replaceChildren(node('p',note), graph);
  }
  select.addEventListener('change', update);
  update();
  return details;
}

function timelineTable(periods, onSelect) {
  return makeTable(['期次','提交', ...jointMetrics.map(([,label]) => label),'分企业销售额'],
    periods.map(({cursor,metrics,sales_by_firm_minor}) => {
      const period = onSelect ? node('button',`第 ${cursor.tick} 期`) : cursor.tick;
      if (onSelect) period.addEventListener('click', () => onSelect(cursor.tick));
      const hash = node('code',cursor.source_commit_hash.slice(0,12));
      hash.title = cursor.source_commit_hash;
      return [period, hash, ...jointMetrics.map(([key]) => metrics[key]), sales_by_firm_minor];
    }));
}

async function showJointTimeline(key, target) {
  try {
    const data = await (await request('/api/timeline?' + new URLSearchParams({job:key}))).json();
    if (!target.isConnected) return;
    if (!data.periods.length) {
      target.textContent = '等待首个结算期提交。';
      return;
    }
    target.replaceChildren(timelineTable(data.periods, tick => {ui('tick').value = tick; readView();}));
  } catch (error) { if (target.isConnected) target.textContent = error.message; }
}

function interventionView(view) {
  const {plan, timeline, individuals, ledger, network: edges} = view.intervention_evolution;
  const block = node('section');
  block.className = 'intervention-evidence';
  block.dataset.commit = view.cursor.source_commit_hash;
  block.append(node('h3', plan.label + ' · 演化证据'));
  const columns = Object.keys(interventionMetrics).filter(key =>
    timeline.some(row => row.metrics[key] !== null && row.metrics[key] !== undefined));
  block.append(makeTable(['期次','提交', ...columns.map(key => interventionMetrics[key])],
    timeline.map(row => [row.cursor.tick, row.cursor.source_commit_hash.slice(0,12),
      ...columns.map(key => row.metrics[key])])));
  const individual = node('details');
  individual.open = true;
  individual.append(node('summary','个体的实际触达、选择与回执'), makeTable(
    ['主体','授权观察 ID','可见干预事实','选择','执行回执'],
    individuals.map(row => [row.actor,row.observation_id,[...row.facts,...row.feedback_facts],
      {意图:row.intents, 参数:row.choice, 预防储蓄:row.fiscal_precaution},
      row.receipts.map(r => [r.outcome,r.executed_quantity,r.cash_delta_minor,r.ledger_tx_ids])])));
  const relation = network(view, edges);
  const accounting = node('details');
  accounting.append(node('summary','已记账的干预链'), makeTable(['事务','事件','实付与后续状态'],
    ledger.map(row => [row.tx_id,row.event,row.details])));
  block.append(individual, relation, accounting);
  return block;
}

function renderTemplate() {
  const template = interventionTemplates.find(row => row.kind === ui('template-kind').value);
  const {change_tick,settlement_tick,feedback_tick,config_hash,dose} =
    template.arms.find(row => row.arm === ui('template-arm').value);
  const doseLabel = {
    income: () => `指定两户工时 ${dose.work_minutes} 分钟`,
    liquidity: () => `${dose.bank} 首次提款 ${dose.withdrawal_minor}`,
    revaluation: () => `基本面下调 ${dose.fundamental_drop_minor}`,
    fiscal: () => `税率 ${dose.rate_bps / 100}% · 每户转移 ${dose.transfer_per_household_minor}`,
  }[template.kind]();
  ui('template-window').textContent =
    `${doseLabel} · 第 ${change_tick} 期施加 · 第 ${settlement_tick} 期结算观察 · 第 ${feedback_tick} 期反馈 · ${config_hash.slice(0,12)}`;
}

function showView(data, other, key, otherKey, tick) {
  const result = ui('result');
  const view = data.views?.[0];
  const partner = other?.views?.[0];
  const metrics = supplierPreview ? supplierMetrics
    : isSecurityView(view ?? partner) ? securityMetrics : goodsMetrics;
  const label = id => jobs.find(job => job.id === id)?.label ?? '';
  if (otherKey) {
    result.append(makeTable(['同一期指标',label(key),label(otherKey)], metrics.map(([id,name]) =>
      [name,view?.comparison_metrics?.[id],partner?.comparison_metrics?.[id]])));
    if (!partner) result.append(node('p','对照运行在该期暂无提交。'));
  }
  if (!view) {
    ui('view-status').textContent = data.status === 'unrecorded_observe' ? '该提交没有留存主体观察。' : '该期暂无提交。';
    return;
  }
  const unit = data.audience === 'researcher' ? '合成世界，金额以最小货币单位计。' : '';
  ui('view-status').textContent = `第 ${tick} 期 · ${view.cursor.source_commit_hash.slice(0,12)}　${unit}`;
  ui('view-status').title = view.cursor.source_commit_hash;
  if (view.actors) options(ui('actor'), '研究者', view.actors.map(id => [id,id]));
  if (!otherKey && view.comparison_metrics) {
    result.append(makeTable(['指标','数值'], metrics.map(([id,name]) => [name,view.comparison_metrics[id]])));
  }
  if (data.audience === 'researcher' && role === 'researcher' && !supplierPreview) {
    if (!isSecurityView(view)) result.append(ledgerAudit(key, view.cursor, otherKey ? label(key) : ''));
    if (partner && !isSecurityView(partner)) result.append(ledgerAudit(otherKey, partner.cursor, label(otherKey)));
  }
  if (view.intervention_evolution) result.append(interventionView(view));
  if (partner?.intervention_evolution) result.append(interventionView(partner));
  if (view.joint_world) {
    const timeline = node('details'), target = node('div','正在读取提交期次…');
    timeline.open = true;
    timeline.append(node('summary','期次时间轴'), target);
    result.append(timeline, jointRelations(view.relation_layers));
    showJointTimeline(key,target);
  }
  if (!view.intervention_evolution && view.communication_edges) result.append(network(view));
  for (const section of view.sections) {
    const details = node('details');
    details.open = data.audience === 'agent' && section.key === 'market';
    details.append(node('summary',section.label), makeTable(section.columns,section.rows));
    result.append(details);
  }
  if (view.researcher_settlement) {
    result.append(node('h3','研究者可见 · 提交后的结果'));
    for (const section of view.researcher_settlement) {
      const details = node('details');
      details.open = section.key === 'actor_decision';
      details.append(node('summary',section.label), makeTable(section.columns,section.rows));
      result.append(details);
    }
  }
}

async function readView() {
  clearView();
  if (ui('tick').value === '' || selected === null) return;
  const revision = readRevision;
  const key = selected, tick = Number(ui('tick').value), actor = role === 'researcher' ? ui('actor').value : '';
  const otherKey = role === 'researcher' && !actor ? ui('compare').value : '';
  const query = new URLSearchParams({job:key, tick});
  if (actor) query.set('actor',actor);
  ui('view-status').textContent = '正在读取已提交状态…';
  try {
    const tasks = [request('/api/view?' + query).then(response => response.json())];
    if (otherKey) tasks.push(request('/api/view?' + new URLSearchParams({job:otherKey,tick})).then(response => response.json()));
    const [data, other] = await Promise.all(tasks);
    if (readRevision !== revision) return;
    showView(data, other, key, otherKey, tick);
    if (data.status === 'available') {
      const atSupplierSlot = Object.hasOwn(data.views?.[0]?.comparison_metrics ?? {}, 'attempts');
      if (!choiceArchive && (!supplierPreview || (otherKey && atSupplierSlot))) {
        currentExport = otherKey ? '/api/pair-download?' + new URLSearchParams({job:key, compare:otherKey, tick})
          : '/api/download?' + query;
        ui('download').textContent = otherKey ? '导出对照' : '导出 JSON';
        ui('download').disabled = false;
      }
      canGrant = Boolean(actor);
      ui('grant').hidden = !canGrant;
    }
  } catch (error) {
    if (readRevision === revision) {ui('view-status').textContent = ''; notice(error.message,true);}
  }
}

ui('template-kind').addEventListener('change', renderTemplate);
ui('template-arm').addEventListener('change', renderTemplate);
ui('template-freeze').addEventListener('click', () => send('freeze-intervention',
  {kind:ui('template-kind').value, side:ui('template-arm').value}));
ui('validate').addEventListener('click', async () => {
  try {
    const data = await (await request('/api/validate', ui('config').value)).json();
    notice(`配置有效 · ${data.ticks} 期 · ${data.config_hash.slice(0,12)}`);
  } catch (error) { notice(error.message,true); }
});
ui('freeze').addEventListener('click', () => send('freeze', {label:ui('run-label').value}, ui('config').value));
ui('joint-freeze').addEventListener('click', () => send('freeze-joint-template', {expected_hash:jointTemplate.config_hash}));
for (const action of ['start','resume','pause','cancel']) {
  ui(action).addEventListener('click', () => send(action, {job:selected,
    ...(['start','resume'].includes(action) ? {until:ui('until').value === '' ? null : Number(ui('until').value)} : {})}));
}
ui('intervention').addEventListener('change', () => { ui('shift-fields').hidden = ui('intervention').value !== 'shift'; });
ui('fork-freeze').addEventListener('click', () => {
  const tick = ui('fork-tick').value === '' ? null : Number(ui('fork-tick').value);
  const intervention = ui('intervention').value === 'shift' ? {kind:'labor_income_shock', shock:{tick:tick === null ? null : tick+1,
    household_indices:ui('workers').value.split(',').map(value => value.trim() === '' ? null : Number(value)),
    work_minutes:Number(ui('minutes').value)}} : null;
  send('fork',{job:selected,tick,label:ui('fork-label').value,intervention});
});
ui('tick').addEventListener('input', clearView);
for (const name of ['actor','compare']) ui(name).addEventListener('change', clearView);
ui('read-view').addEventListener('click', readView);
async function downloadCurrent(path = currentExport) {
  const revision = readRevision;
  if (!path) return;
  try {
    const response = await request(path);
    const body = await response.blob();
    if (revision !== readRevision) return;
    const url = URL.createObjectURL(body);
    const link = node('a');
    link.href = url;
    link.download = response.headers.get('Content-Disposition').split('"')[1];
    link.click();
    setTimeout(() => URL.revokeObjectURL(url),1000);
  } catch (error) { if (revision === readRevision) notice(error.message,true); }
}
ui('download').addEventListener('click', () => downloadCurrent());
ui('joint-pilot-download').addEventListener('click', () => downloadCurrent('/api/joint-pilot-download'));
ui('joint-pilot').addEventListener('toggle', async () => {
  if (!ui('joint-pilot').open) return;
  const status = ui('joint-pilot-status'), result = ui('joint-pilot-result');
  if (result.childElementCount) return;
  status.textContent = '正在核对两臂归档…';
  status.classList.remove('error');
  try {
    const data = await (await request('/api/joint-pilot')).json();
    const provenance = node('details');
    provenance.append(node('summary','来源与执行状态'), makeTable(['处理臂','数据来源','随机根','代码提交','源码哈希','运行状态'],
      data.arms.map(arm => [jointArms[arm.arm],arm.data_origin,arm.seed,arm.commit,arm.source_hash,arm.run_status])));
    const trajectories = data.arms.map(arm => {
      const group = node('details');
      group.append(node('summary',jointArms[arm.arm]),timelineTable(arm.periods));
      return group;
    });
    result.replaceChildren(provenance,...trajectories, node('h3','逐期差值 · 有冲击减无冲击'),
      makeTable(['期次',...jointMetrics.map(([,label]) => label)], data.differences.map(row =>
        [row.tick,...jointMetrics.map(([key]) => row.metrics[key])])));
    status.textContent = '— 表示该期缺少可比值。';
    ui('joint-pilot-download').disabled = false;
  } catch (error) { status.textContent = error.message; status.classList.add('error'); }
});
ui('grant').addEventListener('click', async () => {
  const revision = readRevision;
  try {
    const data = await (await request('/api/grant', JSON.stringify({job:selected,actor:ui('actor').value}))).json();
    if (revision !== readRevision) return;
    ui('observer-link').href = data.url;
    ui('observer-link').hidden = false;
  } catch (error) { notice(error.message,true); }
});


const choiceLabels = ['规则', '固定合成归档'];
const queueLabels = {queued:'排队中', expired:'已到期', paid:'已付清', resolution:'进入处置'};
const denominatorLabels = {
  assigned_arms:'登记处理臂', completed_arms:'完成处理臂', planned_seats:'登记席位',
  reached_seats:'实际触达', delivered_notice_seats:'通知送达', paired_opportunities:'配对机会', unreached_seats:'未触达',
  ineligible_seats:'无资格', eligible_unreached_seats:'合格未触达', unobserved_at_offer:'岗位期未提交',
  unobserved_at_notice:'通知期未提交', attempted_intents:'提款意图', admitted_intents:'实际入队',
  seats_with_payment:'实付席位', rejected_intents:'拒绝提款', insufficient_deposit:'存款不足',
  attempted_applications:'实际申请', rejected_applications:'拒绝申请', normal_waits:'正常等待', hired:'登记席位获聘',
  model_attempts:'归档尝试', successful_model_attempts:'成功尝试', failed_model_attempts:'失败尝试',
  rule_fallbacks:'规则回退', failed_execution_attempts:'执行失败', natural_stops:'自然终止'
};

function chooseExperiment() {
  const experiment = choicePreviews.find(row => row.kind === ui('choice-kind').value);
  const periods = ui('choice-tick');
  periods.replaceChildren(...experiment.periods.map(tick => new Option(`第 ${tick} 期`,tick)));
  periods.value = experiment.periods.includes(experiment.default_tick) ? experiment.default_tick : experiment.periods.at(-1);
  readChoice();
}

function showChoice(view, finalTick) {
  const {kind,tick,report} = view;
  const arms = [report.arms.rule, report.arms.archive];
  const cursors = arms.map(arm => arm.commits.find(row => row.tick === tick));
  const atPeriod = (fn) => arms.map((arm,i) => cursors[i] ? fn(arm) : null);
  const decisionTick = kind === 'withdrawal' ? report.notice_tick : report.terms.offered_tick;
  const decision = (field) => tick === decisionTick ? atPeriod(arm => arm[field]) : [null,null];
  const rows = [['提交散列', ...cursors.map(cursor => cursor?.source_commit_hash.slice(0,12))]];
  const units = '金额为最小货币单位。';
  let summary;
  if (kind === 'withdrawal') {
    const event = arm => arm.settlements.find(row => row.tick === tick);
    rows.push(['核验通知触达', ...decision('exposed')],
      ['当期申请金额', ...decision('requested_minor')], ['当期入队金额', ...decision('queued_minor')],
      ['当期实付', ...atPeriod(arm => event(arm)?.paid_minor ?? 0)],
      ['当期未付余额', ...atPeriod(arm => event(arm)?.outstanding_minor ?? (tick===decisionTick ? arm.queued_minor : null))],
      ['队列状态', ...atPeriod(arm => event(arm) ? queueLabels[event(arm).status]
        : tick===decisionTick && arm.queued_minor ? '已入队' : null)]);
    const unpaid = report.status === 'completed' ? '期末未付余额' : '末次提交未付余额';
    summary = [['累计申请','requested_minor'],['累计入队','queued_minor'],
      ['累计实付','paid_minor'],[unpaid,'outstanding_minor']];
  } else {
    const work = arm => arm.work_and_wages.find(row => row.tick === tick);
    const contract = arm => {
      if (tick < decisionTick) return null;
      if (!arm.hired) return '无登记合同';
      if (tick < report.terms.start_tick) return '待生效';
      if (arm.activation_failed) return '生效失败';
      return tick < report.terms.end_tick_exclusive ? '有效' : '已到期';
    };
    rows.push(['候选资格', ...decision('eligible')], ['岗位触达', ...decision('reached')],
      ['申请', ...decision('attempted_applications')], ['正常等待', ...decision('normal_waits')],
      ['登记席位获聘', ...decision('hired')], ['登记合同状态', ...atPeriod(contract)], ['当期新合同工时（分钟）', ...atPeriod(arm => work(arm)?.minutes ?? 0)],
      ['当期新合同实付', ...atPeriod(arm => work(arm)?.paid_minor ?? 0)]);
    summary = [['新合同实付工资','paid_contract_wages_minor'],
      ['后窗全来源实付工资','total_seat_wages_after_offer_minor']];
  }
  const table = makeTable(['当期指标',...choiceLabels], rows);
  const hashes = table.querySelectorAll('tbody tr:first-child td');
  cursors.forEach((cursor,i) => { if (cursor) hashes[i+1].title = cursor.source_commit_hash; });
  const scope = kind === 'withdrawal' ? `第 ${decisionTick}–${finalTick} 期 · ${units}`
    : `第 ${decisionTick} 期开放 · 合同第 ${report.terms.start_tick}–${report.terms.end_tick_exclusive-1} 期 · 每期 ${report.terms.wage_minor} · 工资后窗第 ${report.terms.start_tick}–${finalTick} 期 · ${units}`;
  const counts = Object.entries(denominatorLabels)
    .filter(([key]) => Object.hasOwn(report.denominators,key)).map(([key,label]) => [label,report.denominators[key]]);
  const details = node('details');
  details.append(node('summary','执行分母'), makeTable(['统计口径','计数'],counts));
  ui('choice-result').replaceChildren(table, node('p',scope),
    makeTable([report.status === 'completed' ? '冻结窗口指标' : '已提交范围指标',...choiceLabels],summary.map(([label,key]) => [label,...arms.map(arm => arm[key])])),
    details);
  ui('choice-status').textContent = `第 ${tick} 期 · ${report.status==='completed' ? '对照已完成' : '对照未通过'}`;
}

async function readChoice() {
  const revision = ++readRevision;
  currentExport = null;
  ui('choice-result').replaceChildren();
  ui('choice-download').disabled = true;
  ui('choice-grant').disabled = true;
  ui('choice-link').hidden = true;
  ui('choice-status').textContent = '正在读取冻结对照…';
  notice('');
  const kind = ui('choice-kind').value, tick = Number(ui('choice-tick').value);
  const query = new URLSearchParams({kind,tick});
  try {
    const view = await (await request('/api/choice-view?'+query)).json();
    if (revision !== readRevision) return;
    if (view.kind !== kind || view.tick !== tick) throw new Error('对照与所选实验或期次不一致。');
    const experiment = choicePreviews.find(row => row.kind === kind);
    showChoice(view,experiment.final_tick);
    currentExport = '/api/choice-download?'+query;
    ui('choice-download').disabled = false;
    ui('choice-grant').disabled = false;
  } catch (error) {
    if (revision === readRevision) {
      ui('choice-status').textContent = '';
      notice(error.message,true);
    }
  }
}

ui('choice-kind').addEventListener('change', chooseExperiment);
ui('choice-tick').addEventListener('change', readChoice);
ui('choice-download').addEventListener('click', () => downloadCurrent());
ui('choice-arm').addEventListener('change', () => { ui('choice-link').hidden = true; });
ui('choice-grant').addEventListener('click', async () => {
  const revision = readRevision;
  const kind = ui('choice-kind').value, side = ui('choice-arm').value;
  try {
    const grant = await (await request('/api/choice-grant', JSON.stringify({kind,side}))).json();
    if (revision !== readRevision || side !== ui('choice-arm').value) return;
    ui('choice-link').href = grant.url;
    ui('choice-link').hidden = false;
  } catch (error) { if (revision === readRevision) notice(error.message,true); }
});

async function poll() {
  try { await refresh(); }
  catch (error) { notice(error.message,true); }
  if (!choiceArchive || (registration && !terminalRegistration.has(registration.phase))) setTimeout(poll,1200);
}
if (token) poll();
else notice('请打开本地服务提供的研究者链接或授权的主体只读链接。',true);
