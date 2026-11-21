// 测试引导：注册 DOM 环境与 ESM loader 钩子。
import {register} from 'node:module';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

// 极简 DOM 全局桩（页面只用到 createElement/getElementById 与少量属性）。
class StubNode {
  constructor(tag) {
    this.tag = tag; this.props = {}; this.children = []; this.textContent = ''; this.className = '';
  }
  append(...kids) { for (const k of kids.flat(Infinity)) if (k) this.children.push(k); }
  replaceChildren(...kids) { this.children = kids.flat(Infinity).filter(Boolean); }
  addEventListener() {}
  setAttribute(k, v) { this.props[k] = v; }
  get isConnected() { return true; }
  queryText() {
    return [this.textContent, ...this.children.flatMap(c => c.queryText ? c.queryText() : [])]
      .filter(Boolean).join(' ');
  }
}
global.document = {
  createElement: tag => new StubNode(tag),
  getElementById: () => new StubNode('div'),
  body: {dataset: {}},
};
global.location = {hash: '#stocktake'};
global.sessionStorage = {getItem: () => null, setItem: () => {}, removeItem: () => {}};
global.setTimeout = () => 0;

register(pathToFileURL(path.join(import.meta.dirname, 'loader.mjs')));
