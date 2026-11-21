export const ROLE = {admin:'复核主管', analyst:'分析员', viewer:'只读查看'};
export const STATUS = {queued:'排队中', running:'正在计算', complete:'计算完成', failed:'运行失败'};
export const REVIEW = {pending:'待复核', accepted:'已通过', returned:'已退回'};
export const STOCKTAKE_STATUS = {queued:'排队重算', running:'正在计算', complete:'对账完成', failed:'计算失败'};
export const STOCKTAKE_OUTCOME = {
  confirmed:'盘到', disputed:'待核', not_counted:'未盘到',
  lent_misscan:'借出误扫', retired_misscan:'退役误扫', unknown_misscan:'无效号误扫',
};
export const TOOL_STATUS = {in_custody:'在管', checked_out:'借出', retired:'退役'};
