// 扫码实盘页面冒烟测试：轻量 DOM 桩 + 依赖桩，验证渲染分支与 API 调用。
// 运行：node --import ./tests/node/register.mjs --test tests/node/stocktake-page.test.js
import test from 'node:test';
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
import path from 'node:path';

const root = path.resolve(import.meta.dirname, '../../../frontend/src');
const pageUrl = pathToFileURL(path.join(root, 'pages/stocktake.js'));

const calls = [];
function makeResult() {
  return {
    headline: '月末在管 3 件，扫码 6 条；最优配法盘到 1 件，待核组 1 个。',
    metrics: {custody_tools: 3, total_scans: 6, confirmed: 1, disputed_groups: 1, disputed_scans: 2,
      not_counted: 0, retired_misscan: 1, lent_misscan: 1, unknown_misscan: 1, lent_tools: 1, retired_tools: 1},
    confirmed: [],
    groups: [{group_id: 0, scan_ids: ['S1', 'S2'], tool_ids: ['GQ-01', 'GQ-02'], scan_count: 2, tool_count: 2,
      max_pairs_in_group: 2,
      optional_pairs: [{scan_id: 'S1', tool_id: 'GQ-01'}, {scan_id: 'S1', tool_id: 'GQ-02'}],
      basis: '组内 2 条扫码与 2 件在管工具经交替环连通。'}],
    scans: [
      {scan_id: 'S1', outcome: 'disputed', tool_id: 'GQ-01', group_id: 0,
        candidates: [{candidate: 'GQ-01', kind: 'in_custody', label: '在管', detail: 'x'}], basis: '争议'},
      {scan_id: 'S3', outcome: 'confirmed', tool_id: 'GQ-03', group_id: null,
        candidates: [{candidate: 'GQ-03', kind: 'in_custody', label: '在管', detail: 'x'}], basis: '唯一'},
      {scan_id: 'S4', outcome: 'lent_misscan', tool_id: null, group_id: null,
        candidates: [{candidate: 'GQ-04', kind: 'checked_out', label: '借出', detail: 'x'}], basis: '借出误扫'},
      {scan_id: 'S5', outcome: 'retired_misscan', tool_id: null, group_id: null,
        candidates: [{candidate: 'GQ-05', kind: 'retired', label: '退役', detail: 'x'}], basis: '退役误扫'},
      {scan_id: 'S6', outcome: 'unknown_misscan', tool_id: null, group_id: null,
        candidates: [{candidate: 'GHOST', kind: 'unknown', label: '无效号', detail: 'x'}], basis: '无效号'},
    ],
    tools: [{tool_id: 'GQ-01', name: '电钻', outcome: 'disputed', scan_id: 'S1', group_id: 0, basis: '争议'},
      {tool_id: 'GQ-03', name: '修枝剪', outcome: 'confirmed', scan_id: 'S3', group_id: null, basis: '唯一'}],
    offline_tools: [{tool_id: 'GQ-04', name: '砂光机', status: 'checked_out'},
      {tool_id: 'GQ-05', name: '踏梯', status: 'retired'}],
  };
}

const detail = () => ({id: 1, name: '11 月实盘', snapshot_date: '2026-11-30', status: 'complete',
  status_label: '对账完成', version: 2, last_error: null, result: makeResult(), resolutions: [],
  scan_ledger: [{scan_id: 'S1', candidates: ['GQ-01', 'GQ-02'], input_version: 1, source: null}], tools: []});

const workspace = {
  stocktakeBatches: [{id: 1, name: '11 月实盘', snapshot_date: '2026-11-30', status: 'complete',
    status_label: '对账完成', version: 2, scan_count: 6, updated_at: '2026-11-30T10:00:00Z'}],
  stocktakeDetail: async id => { calls.push(['detail', id]); return detail(); },
  stocktakeCreate: async data => { calls.push(['create', data]); return {id: 9}; },
  stocktakeAppend: async (id, scans) => { calls.push(['append', id, scans]); return {added: scans.length}; },
  stocktakeRebuild: async id => { calls.push(['rebuild', id]); },
  stocktakeResolve: async (id, data) => { calls.push(['resolve', id, data]); },
};

globalThis.__workspace = workspace;
globalThis.__calls = calls;

async function loadPage() {
  const module = await import(pageUrl);
  return module.stocktakePage;
}

test('无 id 时渲染批次列表与新建表单', async () => {
  globalThis.__role = 'admin';
  global.location.hash = '#stocktake';
  const stocktakePage = await loadPage();
  const node = await stocktakePage({id: null, filter: ''}, async () => {});
  const text = node.queryText();
  assert.match(text, /扫码实盘批次/);
  assert.match(text, /新建批次/);
  assert.match(text, /2026-11-30/);
  assert.match(text, /对账口径/);
});

test('详情页渲染指标、待核组、全部误扫结论与核示表单', async () => {
  globalThis.__role = 'admin';
  global.location.hash = '#stocktake/1';
  const stocktakePage = await loadPage();
  const node = await stocktakePage({id: 1, filter: ''}, async () => {});
  const text = node.queryText();
  assert.match(text, /待核组 #1/);
  assert.match(text, /借出误扫/);
  assert.match(text, /退役误扫/);
  assert.match(text, /无效号误扫/);
  assert.match(text, /扫码流水/);
  assert.match(text, /月末在管:3/);
  assert.match(text, /提交人工核示/);
  assert.ok(calls.some(([kind, id]) => kind === 'detail' && id === 1));
});

test('未完成批次展示重算提示而非结论表', async () => {
  globalThis.__role = 'admin';
  const original = workspace.stocktakeDetail;
  workspace.stocktakeDetail = async () => ({...detail(), status: 'queued', result: null});
  const stocktakePage = await loadPage();
  const node = await stocktakePage({id: 1, filter: ''}, async () => {});
  assert.match(node.queryText(), /后台正在重算/);
  workspace.stocktakeDetail = original;
});

test('只读角色不渲染补扫与核示表单', async () => {
  globalThis.__role = 'viewer';
  const stocktakePage = await loadPage();
  const node = await stocktakePage({id: 1, filter: 'lent_misscan'}, async () => {});
  const text = node.queryText();
  assert.doesNotMatch(text, /提交并发补扫/);
  assert.doesNotMatch(text, /提交人工核示/);
  assert.match(text, /等待复核主管人工核示/);
  globalThis.__role = 'admin';
});
