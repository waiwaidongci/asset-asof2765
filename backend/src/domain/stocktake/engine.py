"""模糊标签扫码与月末工具快照的二部图对账引擎。

业务口径
--------
- 左点为一次扫码记录（旧标签可读出多个候选工具号），右点为月末快照中「在管」的工具；
  借出、退役工具不参与配对，仅作为误扫依据。
- 目标是尽量多配：求最大基数匹配（Hopcroft–Karp）。
- 只有在**所有**最大匹配里都取同一条边的配对才记为「盘到」；可替换的配对按交替环、
  交替增广链的连通结构整组列为「待核」。
- 判定依据为有向残量图上的强连通分量（Tarjan）与超源可达性：

  * 已匹配边 (u,v)：u 与 v 同强连通分量，或 u 可由空闲扫码侧到达，则存在别的最大匹配
    不用这条边，属于争议；否则该边被所有最大匹配共用，记为盘到。
  * 未匹配边 (u,v)：u 与 v 同强连通分量，或 u 可由空闲扫码侧到达，则能进入某个最大匹配，
    属于争议备选；否则任何最大匹配都不会采用，仅作候选依据。
  * 全部争议边按无向连通关系成组，组内重新分配不改变最大配对件数。

引擎只依赖 Python 标准库，便于在没有第三方依赖的环境下单测。
"""

from collections import deque
from dataclasses import dataclass, field

# 工具生命周期状态（与借还台账口径一致）。
IN_CUSTODY = "in_custody"
CHECKED_OUT = "checked_out"
RETIRED = "retired"
TOOL_STATUSES = (IN_CUSTODY, CHECKED_OUT, RETIRED)
TOOL_STATUS_LABELS = {IN_CUSTODY: "在管", CHECKED_OUT: "借出", RETIRED: "退役"}

# 扫码对账结论。
CONFIRMED = "confirmed"          # 盘到：所有最优配法一致
DISPUTED = "disputed"            # 待核：组内存在等价的不同配法
NOT_COUNTED = "not_counted"      # 未盘到：在管工具没有任何扫码能配上
LENT_MISSCAN = "lent_misscan"    # 借出误扫：候选号全部指向借出工具
RETIRED_MISSCAN = "retired_misscan"  # 退役误扫：候选号含退役工具且无在管候选
UNKNOWN_MISSCAN = "unknown_misscan"   # 无效号误扫：候选号在月末快照中不存在
OUTCOME_LABELS = {
    CONFIRMED: "盘到",
    DISPUTED: "待核",
    NOT_COUNTED: "未盘到",
    LENT_MISSCAN: "借出误扫",
    RETIRED_MISSCAN: "退役误扫",
    UNKNOWN_MISSCAN: "无效号误扫",
}

_MAX_ID_LENGTH = 80
_MAX_NAME_LENGTH = 120
_MAX_CANDIDATES = 20


@dataclass(frozen=True)
class Tool:
    tool_id: str
    name: str | None
    status: str


@dataclass(frozen=True)
class Scan:
    scan_id: str
    tag_read: str | None
    candidates: tuple[str, ...]
    source: str | None = None
    scanned_at: str | None = None


@dataclass
class _Graph:
    n_left: int
    n_right: int
    adjacency: list[list[int]]          # 左点 -> 右点列表（候选去重后）
    pair_left: list[int | None]         # 左点当前匹配的右点
    pair_right: list[int | None]        # 右点当前匹配的左点
    components: list[int]               # 每个节点（含超源、超汇）的强连通分量号
    reachable_from_source: list[bool]   # 节点是否可由超源到达
    reaches_sink: list[bool]            # 节点是否可到达超汇
    source: int                         # 超源节点编号
    sink: int                           # 超汇节点编号


def _identifier(raw, label, index):
    if not isinstance(raw, str):
        raise ValueError(f"第 {index} 条记录的{label}须为字符串。")
    value = raw.strip()
    if not 1 <= len(value) <= _MAX_ID_LENGTH:
        raise ValueError(f"第 {index} 条记录的{label}须为 1 至 {_MAX_ID_LENGTH} 个字符。")
    return value


def validate_tools(rows):
    """校验月末快照工具列表，返回按工具号去重后的规范序列。"""
    if not isinstance(rows, (list, tuple)) or not rows:
        raise ValueError("月末快照为空，请先提供至少一件工具。")
    tools: list[Tool] = []
    seen: set[str] = set()
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ValueError(f"第 {index} 行工具快照格式有误。")
        tool_id = _identifier(row.get("tool_id"), "工具编号", index)
        if tool_id in seen:
            raise ValueError(f"工具编号 {tool_id} 在月末快照中重复出现。")
        status = row.get("status")
        if status not in TOOL_STATUSES:
            raise ValueError(f"工具 {tool_id} 的状态须为 in_custody、checked_out 或 retired。")
        name = row.get("name")
        if name is not None:
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= _MAX_NAME_LENGTH:
                raise ValueError(f"工具 {tool_id} 的名称须为 1 至 {_MAX_NAME_LENGTH} 个字符。")
            name = name.strip()
        seen.add(tool_id)
        tools.append(Tool(tool_id, name, status))
    return tools


def validate_scans(rows):
    """校验扫码记录，返回规范扫码序列；同一扫码内重复候选号只保留一次。"""
    if not isinstance(rows, (list, tuple)) or not rows:
        raise ValueError("扫码记录为空，请至少录入一条扫码。")
    scans: list[Scan] = []
    seen_scan: set[str] = set()
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ValueError(f"第 {index} 条扫码格式有误。")
        scan_id = _identifier(row.get("scan_id"), "扫码编号", index)
        if scan_id in seen_scan:
            raise ValueError(f"扫码编号 {scan_id} 重复，请核对补扫记录。")
        seen_scan.add(scan_id)
        raw_candidates = row.get("candidates")
        if not isinstance(raw_candidates, (list, tuple)) or not raw_candidates:
            raise ValueError(f"扫码 {scan_id} 至少需要一个候选工具号。")
        if len(raw_candidates) > _MAX_CANDIDATES:
            raise ValueError(f"扫码 {scan_id} 的候选号最多 {_MAX_CANDIDATES} 个。")
        candidates: list[str] = []
        local_seen: set[str] = set()
        for candidate in raw_candidates:
            value = _identifier(candidate, "候选工具号", index)
            if value not in local_seen:
                local_seen.add(value)
                candidates.append(value)
        tag_read = row.get("tag_read")
        if tag_read is not None and (not isinstance(tag_read, str) or not tag_read.strip()):
            raise ValueError(f"扫码 {scan_id} 的原始标签读数不能为空字符串。")
        tag_read = tag_read.strip() if isinstance(tag_read, str) else None
        source = row.get("source")
        source = source.strip() if isinstance(source, str) and source.strip() else None
        scanned_at = row.get("scanned_at")
        scanned_at = scanned_at.strip() if isinstance(scanned_at, str) and scanned_at.strip() else None
        scans.append(Scan(scan_id, tag_read, tuple(candidates), source, scanned_at))
    return scans


def _hopcroft_karp(adjacency, n_right):
    """返回最大匹配及配对数；左点、右点均以 0 起编号。"""
    n_left = len(adjacency)
    pair_left: list[int | None] = [None] * n_left
    pair_right: list[int | None] = [None] * n_right
    depth: list[int] = [-1] * n_left

    def breadth():
        queue = deque()
        for u in range(n_left):
            if pair_left[u] is None:
                depth[u] = 0
                queue.append(u)
            else:
                depth[u] = -1
        found = False
        while queue:
            u = queue.popleft()
            for v in adjacency[u]:
                mate = pair_right[v]
                if mate is None:
                    found = True
                elif depth[mate] == -1:
                    depth[mate] = depth[u] + 1
                    queue.append(mate)
        return found

    def depth_first(u):
        for v in adjacency[u]:
            mate = pair_right[v]
            if mate is None or (depth[mate] == depth[u] + 1 and depth_first(mate)):
                pair_left[u] = v
                pair_right[v] = u
                return True
        depth[u] = -1
        return False

    matching_size = 0
    while breadth():
        for u in range(n_left):
            if pair_left[u] is None and depth_first(u):
                matching_size += 1
    return pair_left, pair_right, matching_size


def _tarjan(graph_adjacency, source):
    """迭代版 Tarjan，返回各节点强连通分量号（编号不具业务含义，仅比较是否相同）。"""
    total = len(graph_adjacency)
    index_counter = 0
    stack: list[int] = []
    on_stack = [False] * total
    indices = [-1] * total
    lowlink = [0] * total
    components = [-1] * total
    component_counter = 0

    # 显式栈帧：(节点, 下一条待访问邻边的位置)
    for root in [source, *[node for node in range(total) if node != source]]:
        if indices[root] != -1:
            continue
        indices[root] = lowlink[root] = index_counter
        index_counter += 1
        stack.append(root)
        on_stack[root] = True
        work = [(root, 0)]
        while work:
            node, edge_index = work[-1]
            if edge_index < len(graph_adjacency[node]):
                nxt = graph_adjacency[node][edge_index]
                work[-1] = (node, edge_index + 1)
                if indices[nxt] == -1:
                    indices[nxt] = lowlink[nxt] = index_counter
                    index_counter += 1
                    stack.append(nxt)
                    on_stack[nxt] = True
                    work.append((nxt, 0))
                elif on_stack[nxt]:
                    lowlink[node] = min(lowlink[node], indices[nxt])
            else:
                work.pop()
                if lowlink[node] == indices[node]:
                    while True:
                        member = stack.pop()
                        on_stack[member] = False
                        components[member] = component_counter
                        if member == node:
                            break
                    component_counter += 1
                if work:
                    parent = work[-1][0]
                    lowlink[parent] = min(lowlink[parent], lowlink[node])
    return components


def _build_graph(adjacency, pair_left, pair_right):
    n_left = len(adjacency)
    n_right = len(pair_right)
    source = n_left + n_right
    sink = source + 1
    total = sink + 1
    directed: list[list[int]] = [[] for _ in range(total)]
    # 超源只指向空闲左点：交替增广链必须从没有配对的扫码出发。
    for u in range(n_left):
        if pair_left[u] is None:
            directed[source].append(u)
    # 空闲右点指向超汇：捕捉「已匹配左点 -> 空闲右点」的可翻转交替路径。
    for v in range(n_right):
        if pair_right[v] is None:
            directed[n_left + v].append(sink)
    for u, neighbors in enumerate(adjacency):
        for v in neighbors:
            right_node = n_left + v
            if pair_left[u] == v:
                # 匹配边在残量图中只保留右→左方向。
                directed[right_node].append(u)
            else:
                # 非匹配边保留左→右方向，二者共同构成交替路径。
                directed[u].append(right_node)
    components = _tarjan(directed, source)
    reachable_from_source = [False] * total
    reachable_from_source[source] = True
    queue = deque([source])
    while queue:
        node = queue.popleft()
        for nxt in directed[node]:
            if not reachable_from_source[nxt]:
                reachable_from_source[nxt] = True
                queue.append(nxt)
    # 反图上从超汇出发，等价于正图中能到达超汇的节点集合。
    reaches_sink = [False] * total
    reaches_sink[sink] = True
    reverse: list[list[int]] = [[] for _ in range(total)]
    for node, neighbors in enumerate(directed):
        for nxt in neighbors:
            reverse[nxt].append(node)
    queue = deque([sink])
    while queue:
        node = queue.popleft()
        for nxt in reverse[node]:
            if not reaches_sink[nxt]:
                reaches_sink[nxt] = True
                queue.append(nxt)
    return _Graph(n_left, n_right, adjacency, pair_left, pair_right, components,
                  reachable_from_source, reaches_sink, source, sink)


def _edge_kind(graph: _Graph, u, v):
    """返回边在最大匹配集合中的角色：essential / optional / unusable。"""
    right_node = graph.n_left + v
    same_component = graph.components[u] == graph.components[right_node]
    from_free_side = graph.reachable_from_source[u]
    to_free_side = graph.reaches_sink[right_node]
    matched = graph.pair_left[u] == v
    if matched:
        # 匹配边仅当无法经任何交替环或交替路径被替换时，才被所有最大匹配共用。
        if same_component or from_free_side or to_free_side:
            return "optional"
        return "essential"
    if same_component or from_free_side or to_free_side:
        return "optional"
    return "unusable"


def _candidate_resolution(candidates, tools_by_id):
    """逐条说明候选号在月末快照中的去向，作为误扫与配对的依据。"""
    resolution = []
    for candidate in candidates:
        tool = tools_by_id.get(candidate)
        if tool is None:
            resolution.append({"candidate": candidate, "kind": "unknown",
                               "label": "无效号", "detail": "月末快照中不存在该工具号"})
        elif tool.status == IN_CUSTODY:
            resolution.append({"candidate": candidate, "kind": IN_CUSTODY,
                               "label": TOOL_STATUS_LABELS[IN_CUSTODY],
                               "detail": "在管工具，可参与配对"})
        else:
            resolution.append({"candidate": candidate, "kind": tool.status,
                               "label": TOOL_STATUS_LABELS[tool.status],
                               "detail": f"{TOOL_STATUS_LABELS[tool.status]}工具不参与实盘配对"})
    return resolution


def reconcile(tool_rows, scan_rows):
    """对月末快照与扫码记录执行最大匹配对账，返回盘到、待核组、未盘到与误扫及依据。"""
    tools = validate_tools(tool_rows)
    scans = validate_scans(scan_rows)
    tools_by_id = {tool.tool_id: tool for tool in tools}
    custody_tools = [tool for tool in tools if tool.status == IN_CUSTODY]
    right_index = {tool.tool_id: v for v, tool in enumerate(custody_tools)}

    adjacency: list[list[int]] = []
    scan_edges: list[list[tuple[str, int]]] = []
    for scan in scans:
        edges: list[tuple[str, int]] = []
        seen_right: set[int] = set()
        for candidate in scan.candidates:
            v = right_index.get(candidate)
            if v is not None and v not in seen_right:
                seen_right.add(v)
                edges.append((candidate, v))
        adjacency.append([v for _, v in edges])
        scan_edges.append(edges)

    pair_left, pair_right, matching_size = _hopcroft_karp(adjacency, len(custody_tools))
    graph = _build_graph(adjacency, pair_left, pair_right)

    # 每条边标注角色；争议边用于成组。
    edge_roles: dict[tuple[int, int], str] = {}
    for u, edges in enumerate(scan_edges):
        for candidate, v in edges:
            edge_roles[(u, v)] = _edge_kind(graph, u, v)

    # 争议组：由全部 optional 边构成的无向连通分量。
    undirected: dict[int, set[int]] = {}
    for (u, v), role in edge_roles.items():
        if role != "optional":
            continue
        right_node = graph.n_left + v
        undirected.setdefault(u, set()).add(right_node)
        undirected.setdefault(right_node, set()).add(u)
    group_by_node: dict[int, int] = {}
    groups: list[dict] = []
    for start in undirected:
        if start in group_by_node:
            continue
        group_id = len(groups)
        members_left: set[int] = set()
        members_right: set[int] = set()
        queue = deque([start])
        group_by_node[start] = group_id
        while queue:
            node = queue.popleft()
            if node < graph.n_left:
                members_left.add(node)
            else:
                members_right.add(node - graph.n_left)
            for nxt in undirected[node]:
                if nxt not in group_by_node:
                    group_by_node[nxt] = group_id
                    queue.append(nxt)
        groups.append({"id": group_id, "left": members_left, "right": members_right})

    confirmed_pairs = []
    for u, v in enumerate(pair_left):
        if v is not None and edge_roles[(u, v)] == "essential":
            confirmed_pairs.append((u, v))

    scan_view = {}
    tool_view = {}

    # —— 盘到（所有最优配法一致的配对）——
    for u, v in confirmed_pairs:
        scan = scans[u]
        tool = custody_tools[v]
        scan_view[u] = {
            "scan_id": scan.scan_id, "tag_read": scan.tag_read, "outcome": CONFIRMED,
            "outcome_label": OUTCOME_LABELS[CONFIRMED], "tool_id": tool.tool_id,
            "group_id": None, "source": scan.source, "scanned_at": scan.scanned_at,
            "candidates": _candidate_resolution(scan.candidates, tools_by_id),
            "basis": (f"最大匹配共 {matching_size} 件；该配对不在任何交替环或空闲侧交替链上，"
                      f"所有 {matching_size} 件规模的最优配法都将扫码 {scan.scan_id} 配给 {tool.tool_id}。"),
        }
        tool_view[v] = {
            "tool_id": tool.tool_id, "name": tool.name, "status": IN_CUSTODY,
            "status_label": TOOL_STATUS_LABELS[IN_CUSTODY], "outcome": CONFIRMED,
            "outcome_label": OUTCOME_LABELS[CONFIRMED], "scan_id": scan.scan_id, "group_id": None,
            "basis": f"扫码 {scan.scan_id} 的候选号包含本工具，且该配对被所有最优配法一致采用。",
        }

    # —— 待核争议组 ——
    group_views = []
    for group in groups:
        left_members = sorted(group["left"])
        right_members = sorted(group["right"])
        group_id = group["id"]
        optional_edges = sorted(
            (scans[u].scan_id, custody_tools[v].tool_id)
            for (u, v), role in edge_roles.items()
            if role == "optional" and group_by_node.get(u) == group_id
        )
        internal_max = sum(1 for v in right_members if pair_right[v] is not None)
        group_view = {
            "group_id": group_id,
            "scan_ids": [scans[u].scan_id for u in left_members],
            "tool_ids": [custody_tools[v].tool_id for v in right_members],
            "scan_count": len(left_members),
            "tool_count": len(right_members),
            "optional_pairs": [{"scan_id": s, "tool_id": t} for s, t in optional_edges],
            "max_pairs_in_group": internal_max,
            "basis": (
                f"组内 {len(left_members)} 条扫码与 {len(right_members)} 件在管工具经交替环/交替链连通，"
                f"存在多种等价最优配法，组内最多可配 {internal_max} 件；任一强行指定都会牺牲同组其他配对，"
                "故整组待人工核示。"
            ),
        }
        group_views.append(group_view)
        for u in left_members:
            scan = scans[u]
            matched_v = pair_left[u]
            scan_view[u] = {
                "scan_id": scan.scan_id, "tag_read": scan.tag_read, "outcome": DISPUTED,
                "outcome_label": OUTCOME_LABELS[DISPUTED],
                "tool_id": custody_tools[matched_v].tool_id if matched_v is not None else None,
                "group_id": group_id, "source": scan.source, "scanned_at": scan.scanned_at,
                "candidates": _candidate_resolution(scan.candidates, tools_by_id),
                "basis": f"属于待核组 #{group_id + 1}：候选号可在组内互换，最优配法不唯一。",
            }
        for v in right_members:
            tool = custody_tools[v]
            mate = pair_right[v]
            tool_view[v] = {
                "tool_id": tool.tool_id, "name": tool.name, "status": IN_CUSTODY,
                "status_label": TOOL_STATUS_LABELS[IN_CUSTODY], "outcome": DISPUTED,
                "outcome_label": OUTCOME_LABELS[DISPUTED],
                "scan_id": scans[mate].scan_id if mate is not None else None,
                "group_id": group_id,
                "basis": f"属于待核组 #{group_id + 1}：多条扫码候选号相互连通，无法唯一盘到。",
            }

    # —— 未盘到：在管工具没有任何扫码候选指向，或只剩不可采用的边 ——
    for v, tool in enumerate(custody_tools):
        if v in tool_view:
            continue
        blockers = sorted(
            scans[u].scan_id for u in range(len(scans))
            if any(edge_roles.get((u, vv)) == "unusable" for _, vv in scan_edges[u] if vv == v)
        )
        if blockers:
            basis = (f"虽有扫码 {('、'.join(blockers))} 的候选号指向本工具，但这些边无法进入任何最大匹配"
                     "（采用会减少总盘到件数），按最优口径记为未盘到。")
        else:
            basis = "月末快照中本工具为在管，但没有任何一条扫码的候选号指向它，记为未盘到。"
        tool_view[v] = {
            "tool_id": tool.tool_id, "name": tool.name, "status": IN_CUSTODY,
            "status_label": TOOL_STATUS_LABELS[IN_CUSTODY], "outcome": NOT_COUNTED,
            "outcome_label": OUTCOME_LABELS[NOT_COUNTED], "scan_id": None, "group_id": None,
            "basis": basis,
        }

    # —— 误扫：没有任何在管候选的扫码 ——
    for u, scan in enumerate(scans):
        if u in scan_view:
            continue
        resolution = _candidate_resolution(scan.candidates, tools_by_id)
        kinds = {item["kind"] for item in resolution}
        if RETIRED in kinds:
            outcome, reason = RETIRED_MISSCAN, "候选号含已退役工具，退役资产不参与实盘配对"
        elif CHECKED_OUT in kinds:
            outcome, reason = LENT_MISSCAN, "候选号全部指向借出未还工具，站内实物盘点无法配对"
        else:
            outcome, reason = UNKNOWN_MISSCAN, "候选号在月末快照中均不存在，属无效标签读数"
        scan_view[u] = {
            "scan_id": scan.scan_id, "tag_read": scan.tag_read, "outcome": outcome,
            "outcome_label": OUTCOME_LABELS[outcome], "tool_id": None, "group_id": None,
            "source": scan.source, "scanned_at": scan.scanned_at, "candidates": resolution,
            "basis": f"{reason}；候选去向：" + "、".join(
                f"{item['candidate']}（{item['label']}）" for item in resolution),
        }

    confirmed_count = len(confirmed_pairs)
    disputed_scan_count = sum(group["scan_count"] for group in group_views)
    misscan_counts = {
        RETIRED_MISSCAN: sum(1 for item in scan_view.values() if item["outcome"] == RETIRED_MISSCAN),
        LENT_MISSCAN: sum(1 for item in scan_view.values() if item["outcome"] == LENT_MISSCAN),
        UNKNOWN_MISSCAN: sum(1 for item in scan_view.values() if item["outcome"] == UNKNOWN_MISSCAN),
    }
    result = {
        "matching_size": matching_size,
        "metrics": {
            "custody_tools": len(custody_tools),
            "total_scans": len(scans),
            "confirmed": confirmed_count,
            "disputed_groups": len(group_views),
            "disputed_scans": disputed_scan_count,
            "not_counted": sum(1 for item in tool_view.values() if item["outcome"] == NOT_COUNTED),
            "retired_misscan": misscan_counts[RETIRED_MISSCAN],
            "lent_misscan": misscan_counts[LENT_MISSCAN],
            "unknown_misscan": misscan_counts[UNKNOWN_MISSCAN],
            "lent_tools": sum(1 for tool in tools if tool.status == CHECKED_OUT),
            "retired_tools": sum(1 for tool in tools if tool.status == RETIRED),
        },
        "confirmed": [scan_view[u] for u, _ in sorted(confirmed_pairs, key=lambda pair: pair[0])],
        "groups": group_views,
        "scans": [scan_view[u] for u in range(len(scans))],
        "tools": [tool_view[v] for v in range(len(custody_tools))],
        "offline_tools": [
            {"tool_id": tool.tool_id, "name": tool.name, "status": tool.status,
             "status_label": TOOL_STATUS_LABELS[tool.status],
             "basis": "借出/退役工具不在站内实盘范围内，仅用于误扫判定。"}
            for tool in tools if tool.status != IN_CUSTODY
        ],
    }
    result["headline"] = (
        f"月末在管 {len(custody_tools)} 件，扫码 {len(scans)} 条；最优配法盘到 {confirmed_count} 件，"
        f"待核组 {len(group_views)} 个（涉 {disputed_scan_count} 条扫码），"
        f"未盘到 {result['metrics']['not_counted']} 件，误扫 "
        f"{sum(misscan_counts.values())} 条。"
    )
    return result
