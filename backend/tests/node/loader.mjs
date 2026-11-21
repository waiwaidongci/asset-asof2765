// 依赖桩 loader：把前端 ESM 的相对导入替换为内存中的桩模块。
import {pathToFileURL} from 'node:url';
import path from 'node:path';

const root = path.resolve(import.meta.dirname, '../../../frontend/src');
const url = rel => pathToFileURL(path.join(root, rel)).href;

export const stubs = {
  [url('utils/dom.js')]: `
    class Node {
      constructor(tag) { this.tag = tag; this.props = {}; this.children = []; this.textContent = ''; this.className = ''; }
      append(...kids) { for (const k of kids.flat(Infinity)) if (k) this.children.push(k); }
      replaceChildren(...kids) { this.children = kids.flat(Infinity).filter(Boolean); }
      addEventListener() {} setAttribute(k, v) { this.props[k] = v; }
      get isConnected() { return true; }
      queryText() { return [this.textContent, ...this.children.flatMap(c => c.queryText ? c.queryText() : [])].filter(Boolean).join(' '); }
    }
    export const el = (tag, props = {}, ...kids) => { const n = new Node(tag); Object.assign(n.props, props); n.append(kids); return n; };
    export const h = (lvl, text) => { const n = new Node('h' + lvl); n.textContent = text; return n; };
    export const p = (text, cls) => { const n = new Node('p'); n.textContent = text || ''; if (cls) n.className = cls; return n; };
    export const button = (text, attrs = {}) => { const n = new Node('button'); n.textContent = text; Object.assign(n.props, attrs); return n; };
    export const link = (text, href, attrs = {}) => { const n = new Node('a'); n.textContent = text; n.props.href = href; Object.assign(n.props, attrs); return n; };`,
  [url('components/common/panels.js')]: `
    const OUTCOME = {confirmed:'盘到', disputed:'待核', not_counted:'未盘到',
      lent_misscan:'借出误扫', retired_misscan:'退役误扫', unknown_misscan:'无效号误扫',
      queued:'排队重算', running:'正在计算', complete:'对账完成', failed:'计算失败'};
    export const panel = (title, ...kids) => { const n = {tag:'section', textContent: typeof title === 'string' ? title : '', children: [],
      queryText() { return [this.textContent, ...this.children.flatMap(c => c.queryText ? c.queryText() : [])].filter(Boolean).join(' '); },
      append(k) { this.children.push(...k.flat(Infinity).filter(Boolean)); }}; n.append(kids); return n; };
    export const pill = (text, st) => {
      const label = OUTCOME[st] || text;
      return {tag:'span', textContent: label, props:{'data-state':st}, children:[],
        querySelectorText(){ return label; }, queryText(){ return label; }};
    };
    export const empty = msg => ({tag:'div', textContent:msg, children:[], queryText(){return msg;}});
    export const metrics = entries => ({tag:'dl', entries, queryText(){ return entries.map(e => e.label + ':' + e.value).join(' '); }});`,
  [url('components/common/table.js')]: `
    export const table = (cols, rows, opts = {}) => {
      const cell = (col, row) => {
        if (!col.render) return String(row[col.key] ?? '');
        const node = col.render(row);
        return node && node.querySelectorText ? node.querySelectorText() : (node?.textContent ?? '');
      };
      const text = rows.map(row => cols.map(col => cell(col, row)).join(' ')).join(' ');
      return {tag:'table', rows, caption: opts.caption, queryText(){ return (opts.caption || '') + ' ' + text; }};
    };`,
  [url('components/common/fields.js')]: `export const feedback = () => ({tag:'p', textContent:'', queryText(){return '';}});`,
  [url('hooks/action.js')]: `
    export const handler = (control, fb, task) => async event => { event && event.preventDefault && event.preventDefault(); await task(event); };
    export const poll = async () => null;`,
  [url('constants/enums.js')]: `
    export const STOCKTAKE_STATUS = {queued:'排队重算', running:'正在计算', complete:'对账完成', failed:'计算失败'};
    export const STOCKTAKE_OUTCOME = {confirmed:'盘到', disputed:'待核', not_counted:'未盘到',
      lent_misscan:'借出误扫', retired_misscan:'退役误扫', unknown_misscan:'无效号误扫'};
    export const TOOL_STATUS = {in_custody:'在管', checked_out:'借出', retired:'退役'};`,
  [url('api/http.js')]: `export const request = async () => {};`,
  [url('stores/workspace.js')]: `export const workspace = globalThis.__workspace;`,
  [url('stores/session.js')]: `
    export const session = {
      get canWrite() { return globalThis.__role !== 'viewer'; },
      get canReview() { return globalThis.__role !== 'viewer'; },
      get actor() { return {role: globalThis.__role || 'admin', username: globalThis.__role || 'admin'}; },
    };`,
};

export async function resolve(specifier, context, next) {
  return next(specifier, context);
}

export async function load(moduleUrl, context, next) {
  if (stubs[moduleUrl]) return {format: 'module', shortCircuit: true, source: stubs[moduleUrl]};
  return next(moduleUrl, context);
}
