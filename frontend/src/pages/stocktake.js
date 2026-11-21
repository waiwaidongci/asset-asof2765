import {feedback} from '../components/common/fields.js';
import {empty, metrics, panel, pill} from '../components/common/panels.js';
import {table} from '../components/common/table.js';
import {STOCKTAKE_OUTCOME, STOCKTAKE_STATUS, TOOL_STATUS} from '../constants/enums.js';
import {handler, poll} from '../hooks/action.js';
import {session} from '../stores/session.js';
import {workspace} from '../stores/workspace.js';
import {button, el, h, link, p} from '../utils/dom.js';
import {date} from '../utils/format.js';

const FILTERS = [
  ['', '全部结论'], ['confirmed', '盘到'], ['disputed', '待核'], ['not_counted', '未盘到'],
  ['lent_misscan', '借出误扫'], ['retired_misscan', '退役误扫'], ['unknown_misscan', '无效号误扫'],
];

function outcomePill(outcome) {
  return pill(STOCKTAKE_OUTCOME[outcome] || outcome, outcome);
}

function parseToolLines(text) {
  // 每行：工具编号,名称,状态；名称可空，状态取 in_custody/checked_out/retired。
  const tools = [];
  text.split(/\r?\n/).forEach((line, index) => {
    const trimmed = line.trim();
    if (!trimmed) return;
    const parts = trimmed.split(',').map(part => part.trim());
    if (parts.length < 2 || !parts[0] || !parts[2]) {
      throw new Error(`第 ${index + 1} 行格式应为：工具编号,名称,状态（名称可留空）`);
    }
    if (!Object.keys(TOOL_STATUS).includes(parts[2])) {
      throw new Error(`第 ${index + 1} 行状态须为 in_custody、checked_out 或 retired`);
    }
    tools.push({tool_id: parts[0], name: parts[1] || null, status: parts[2]});
  });
  if (!tools.length) throw new Error('请至少录入一件月末快照工具');
  return tools;
}

function parseScanLines(text) {
  // 每行：扫码编号,候选号1|候选号2|...；可选第三列来源。
  const scans = [];
  text.split(/\r?\n/).forEach((line, index) => {
    const trimmed = line.trim();
    if (!trimmed) return;
    const parts = trimmed.split(',').map(part => part.trim());
    if (parts.length < 2 || !parts[0] || !parts[1]) {
      throw new Error(`第 ${index + 1} 行格式应为：扫码编号,候选号（多个用 | 分隔）`);
    }
    const candidates = parts[1].split('|').map(item => item.trim()).filter(Boolean);
    if (!candidates.length) throw new Error(`第 ${index + 1} 行至少需要一个候选号`);
    scans.push({scan_id: parts[0], candidates, source: parts[2] || null});
  });
  if (!scans.length) throw new Error('请至少录入一条扫码');
  return scans;
}

function batchForm(refresh) {
  const message = feedback();
  const submit = button('建立月末实盘批次', {type: 'submit', class: 'primary'});
  return el('form', {class: 'fields', onSubmit: handler(submit, message, async event => {
    const data = new FormData(event.target);
    const tools = parseToolLines(data.get('tools_text'));
    const item = await workspace.stocktakeCreate({
      name: data.get('name'), snapshot_date: data.get('snapshot_date'), tools,
    });
    await refresh();
    location.hash = `#stocktake/${item.id}`;
  })},
    el('label', {class: 'field'}, el('span', {}, '批次名称'),
      el('input', {name: 'name', required: true, maxlength: 120, placeholder: '例：11 月月末扫码实盘'})),
    el('label', {class: 'field'}, el('span', {}, '月末快照日期'),
      el('input', {name: 'snapshot_date', type: 'date', required: true})),
    el('label', {class: 'field'}, el('span', {}, '月末工具快照（每行：编号,名称,状态）'),
      el('textarea', {name: 'tools_text', rows: 7, required: true,
        placeholder: 'GQ-01,充电电钻,in_custody\nGQ-04,砂光机,checked_out\nGQ-05,踏梯,retired'})),
    el('div', {class: 'actions'}, submit), message);
}

function appendForm(batch, refresh) {
  const message = feedback();
  const submit = button('提交并发补扫', {type: 'submit', class: 'primary'});
  return el('form', {class: 'fields', onSubmit: handler(submit, message, async event => {
    const data = new FormData(event.target);
    const scans = parseScanLines(data.get('scans_text'));
    await workspace.stocktakeAppend(batch.id, scans);
    event.target.reset();
    await refresh();
  })},
    p('补扫只追加流水并触发整批重算；同编号扫码会被拒绝。新扫码若连通旧争议组，合并后统一重算。', 'subtle'),
    el('label', {class: 'field'}, el('span', {}, '扫码记录（每行：扫码编号,候选号用 | 分隔,来源可选）'),
      el('textarea', {name: 'scans_text', rows: 5, required: true,
        placeholder: 'S7,GQ-01|GQ-02,北门补扫\nS8,GQ-03,南门补扫'})),
    el('div', {class: 'actions'}, submit), message);
}

function candidateList(candidates) {
  return el('ul', {class: 'candidate-list'}, candidates.map(item =>
    el('li', {class: `candidate candidate-${item.kind}`},
      el('strong', {}, item.candidate), ` ${item.label}：${item.detail}`)));
}

function resolveForm(group, batchId, refresh) {
  const message = feedback();
  const submit = button('提交人工核示', {type: 'submit', class: 'quiet'});
  return el('form', {class: 'fields resolve-form', onSubmit: handler(submit, message, async event => {
    const data = new FormData(event.target);
    const [scanId, toolId] = String(data.get('pair')).split('=>');
    await workspace.stocktakeResolve(batchId, {
      group_id: group.group_id, scan_id: scanId, tool_id: toolId, note: data.get('note'),
    });
    await refresh();
  })},
    el('label', {class: 'field'}, el('span', {}, '核示配对（仅可在组内候选关系中选择）'),
      el('select', {name: 'pair', required: true}, group.optional_pairs.map(pair =>
        el('option', {value: `${pair.scan_id}=>${pair.tool_id}`}, `${pair.scan_id} → ${pair.tool_id}`)))),
    el('label', {class: 'field'}, el('span', {}, '核示依据'),
      el('textarea', {name: 'note', rows: 2, required: true, maxlength: 500})),
    el('div', {class: 'actions'}, submit), message);
}

function groupPanel(group, batch, resolutions, refresh) {
  const resolution = resolutions.find(item => item.group_id === group.group_id);
  return panel(`待核组 #${group.group_id + 1}（${group.scan_count} 条扫码 · ${group.tool_count} 件工具 · 组内最多 ${group.max_pairs_in_group} 件）`,
    p(group.basis, 'subtle'),
    table([
      {key: 'scan_id', label: '扫码'}, {key: 'tool_id', label: '可配工具号'},
    ], group.optional_pairs, {caption: '组内等价候选配对', pageSize: 8}),
    resolution
      ? el('div', {class: 'decision'}, p(`已核示：${resolution.scan_id} → ${resolution.tool_id}（${resolution.author}）`),
          p(resolution.note, 'subtle'))
      : (session.canReview ? resolveForm(group, batch.id, refresh) : p('等待复核主管人工核示。', 'subtle')));
}

function findingsPanel(batch, filter, setFilter, refresh) {
  const tabs = el('div', {class: 'filter-tabs', role: 'tablist'},
    FILTERS.map(([value, label]) => el('button', {
      type: 'button', class: value === filter ? 'tab active' : 'tab', 'aria-pressed': value === filter,
      onClick: () => setFilter(value),
    }, label)));
  if (batch.status !== 'complete' || !batch.result) {
    return panel('盘点结论', tabs, empty('后台正在重算，完成后展示盘到、待核与误扫依据。'));
  }
  const scanColumns = [
    {key: 'scan_id', label: '扫码编号'},
    {label: '结论', render: row => outcomePill(row.outcome)},
    {key: 'tool_id', label: '当前配给工具'},
    {label: '候选号依据', render: row => candidateList(row.candidates)},
    {key: 'basis', label: '判定依据'},
  ];
  const toolColumns = [
    {key: 'tool_id', label: '工具编号'}, {key: 'name', label: '名称'},
    {label: '结论', render: row => outcomePill(row.outcome)},
    {key: 'scan_id', label: '关联扫码'}, {key: 'basis', label: '判定依据'},
  ];
  const result = batch.result;
  const scans = filter ? result.scans.filter(item => item.outcome === filter) : result.scans;
  const showMissing = !filter || filter === 'not_counted';
  const missingTools = result.tools.filter(item => item.outcome === 'not_counted');
  const misscanSummary = !filter && el('div', {class: 'misscan-summary'},
    ...[['lent_misscan', '借出误扫'], ['retired_misscan', '退役误扫'], ['unknown_misscan', '无效号误扫']]
      .map(([key, label]) => el('button', {type: 'button', class: 'tab', onClick: () => setFilter(key)},
        `${label} ${result.metrics[key]} 条`)));
  return panel('盘点结论与依据', tabs, misscanSummary,
    el('div', {class: 'table-wrap'}, table(scanColumns, scans, {caption: '扫码结论', pageSize: 10})),
    showMissing && el('div', {class: 'table-wrap'},
      table(toolColumns, missingTools, {caption: '未盘到工具', empty: '没有未盘到的在管工具。', pageSize: 10})),
    filter === 'lent_misscan' && p('借出工具不在站内实盘范围；对应扫码只作误扫留痕，不计入盘到。', 'subtle'),
    filter === 'retired_misscan' && p('退役工具生命周期已终止；扫到退役标签按退役误扫处理。', 'subtle'));
}

export async function stocktakePage(route, refresh) {
  const batches = workspace.stocktakeBatches;
  if (!route.id) {
    const list = table([
      {label: '批次', render: item => link(item.name, `#stocktake/${item.id}`)},
      {key: 'snapshot_date', label: '快照日期'},
      {label: '状态', render: item => pill(item.status_label, item.status)},
      {key: 'version', label: '版本'}, {key: 'scan_count', label: '扫码条数'},
      {label: '更新时间', render: item => date(item.updated_at)},
    ], batches, {caption: '实盘批次', empty: '还没有盘点批次，请在下方建立。'});
    return el('div', {class: 'two-col'},
      el('div', {class: 'wide-panel'}, panel('扫码实盘批次', list)),
      el('aside', {class: 'sidebar-panel'},
        session.canWrite ? panel('新建批次', batchForm(refresh))
          : panel('新建批次', p('只读角色可查看批次与结论，录入需分析员或主管权限。', 'subtle')),
        panel('对账口径', el('ul', {class: 'method-list'}, [
          '每条扫码可带多个候选号，每件在管工具最多配一次，按最大基数尽量多配。',
          '只有所有最优配法都一致的配对记为盘到；有争议的成组列入待核。',
          '新扫码连通旧争议组时整组重算合并，其余组照用。',
          '并发补扫的旧计算不会覆盖新账；中断续跑不重复计件。',
        ].map(text => el('li', {}, text))))));
  }

  const batch = await workspace.stocktakeDetail(route.id);
  const filter = route.filter || '';
  const setFilter = value => { location.hash = `#stocktake/${batch.id}/${value || ''}`; };
  const finished = item => item.status === 'complete' || item.status === 'failed';
  if (!finished(batch)) {
    const original = location.hash;
    const status = el('p', {role: 'status'}, '后台正在重算，完成后自动展示最新结论。');
    poll(() => workspace.stocktakeDetail(batch.id), finished, () => location.hash === original)
      .then(item => { if (item && status.isConnected && location.hash === original) refresh(); })
      .catch(error => { if (status.isConnected) status.textContent = error.message; });
  }
  const result = batch.result;
  const metricCards = result ? metrics([
    {label: '月末在管', value: result.metrics.custody_tools},
    {label: '盘到', value: result.metrics.confirmed, detail: '所有最优配法一致'},
    {label: '待核组', value: result.metrics.disputed_groups, detail: `涉 ${result.metrics.disputed_scans} 条扫码`},
    {label: '未盘到', value: result.metrics.not_counted},
    {label: '误扫', value: result.metrics.lent_misscan + result.metrics.retired_misscan + result.metrics.unknown_misscan,
      detail: `借出 ${result.metrics.lent_misscan} · 退役 ${result.metrics.retired_misscan} · 无效 ${result.metrics.unknown_misscan}`},
  ]) : p(batch.last_error || '等待后台计算。', 'feedback');

  const rebuildMessage = feedback();
  const rebuildButton = button('手动触发重算', {class: 'quiet'});
  rebuildButton.addEventListener('click', handler(rebuildButton, rebuildMessage, async () => {
    await workspace.stocktakeRebuild(batch.id);
    await refresh();
  }));
  const rebuild = session.canWrite && rebuildButton;

  return el('div', {},
    el('div', {class: 'result-head'},
      p(`${batch.name} · 快照日 ${batch.snapshot_date} · 版本 v${batch.version}`, 'eyeline'),
      h(2, result ? result.headline : '扫码实盘对账进行中'),
      el('div', {class: 'actions'}, pill(batch.status_label, batch.status),
        link('返回批次列表', '#stocktake', {class: 'quiet'}), rebuild)),
    metricCards,
    el('div', {class: 'two-col'},
      el('div', {class: 'wide-panel'},
        findingsPanel(batch, filter, setFilter, refresh),
        result && result.groups.length
          ? el('div', {}, ...result.groups.map(group =>
              groupPanel(group, batch, batch.resolutions, refresh)))
          : panel('待核争议组', empty('没有待核组，所有配对要么唯一盘到，要么属于误扫。'))),
      el('aside', {class: 'sidebar-panel'},
        session.canWrite ? panel('并发补扫', appendForm(batch, refresh)) : null,
        panel('扫码流水', table([
          {key: 'scan_id', label: '扫码'},
          {label: '候选号', render: row => (row.candidates || []).join('、')},
          {key: 'input_version', label: '入账版本'}, {key: 'source', label: '来源'},
        ], batch.scan_ledger, {caption: '扫码流水（只追加）', empty: '暂无扫码。', pageSize: 8})),
        panel('借出/退役工具', table([
          {key: 'tool_id', label: '工具编号'}, {key: 'name', label: '名称'},
          {label: '状态', render: row => TOOL_STATUS[row.status]},
        ], result ? result.offline_tools : [], {caption: '不参与配对，仅用于误扫判定', pageSize: 8})))));
}
