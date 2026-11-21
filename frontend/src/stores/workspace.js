import {request, saveFile} from '../api/http.js';
import {session} from './session.js';

const call = (path, body) => request(path, {token:session.token, body});
export const workspace = {
  spec: null,
  datasets: [],
  runs: [],
  stocktakeBatches: [],
  async init() { this.spec = await request('/spec'); },
  async refresh() {
    const [datasets, runs, stocktakeBatches] = await Promise.all([
      call('/datasets'), call('/runs'), call('/stocktake/batches'),
    ]);
    this.datasets = datasets;
    this.runs = runs;
    this.stocktakeBatches = stocktakeBatches;
  },
  getDataset: (id, offset = 0) => call(`/datasets/${id}?offset=${offset}`),
  getRun: id => call(`/runs/${id}`),
  upload: data => call('/datasets', data),
  start: data => call('/runs', data),
  review: (id, data) => call(`/runs/${id}/review`, data),
  audit: () => call('/audit'),
  schedules: () => call('/schedules'),
  deliveries: () => call('/deliveries'),
  schedule: data => call('/schedules', data),
  disable: id => call(`/schedules/${id}/disable`, {}),
  stocktakeCreate: data => call('/stocktake/batches', data),
  stocktakeDetail: id => call(`/stocktake/batches/${id}`),
  stocktakeAppend: (id, scans) => call(`/stocktake/batches/${id}/scans`, {scans}),
  stocktakeRebuild: id => call(`/stocktake/batches/${id}/rebuild`, {}),
  stocktakeFindings: (id, outcome) =>
    call(`/stocktake/batches/${id}/findings${outcome ? `?outcome=${outcome}` : ''}`),
  stocktakeResolve: (id, data) => call(`/stocktake/batches/${id}/resolve`, data),
  download: (path, filename) => saveFile(path, session.token, filename),
};
