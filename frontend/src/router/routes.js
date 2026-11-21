export const pages = ['overview', 'inputs', 'run', 'results', 'review', 'delivery', 'stocktake'];

export function route() {
  const [page, id, tail] = location.hash.slice(1).split('/');
  const known = pages.includes(page) ? page : 'overview';
  return {
    page: known,
    id: Number(id) || null,
    // stocktake 页面第三段为结论筛选；其余页面沿用分页偏移。
    offset: known === 'stocktake' ? 0 : Math.max(0, Number(tail) || 0),
    filter: known === 'stocktake' ? (tail || '') : '',
  };
}
