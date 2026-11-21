"""桂桥扫码实盘演示数据：月末快照与含候选号、误扫、争议连通的扫码账。"""

# 月末快照：3 件在管、1 件借出、1 件退役。
DEMO_TOOLS = [
    {"tool_id": "GQ-01", "name": "充电电钻", "status": "in_custody"},
    {"tool_id": "GQ-02", "name": "数字万用表", "status": "in_custody"},
    {"tool_id": "GQ-03", "name": "园艺修枝剪", "status": "in_custody"},
    {"tool_id": "GQ-04", "name": "手持砂光机", "status": "checked_out"},
    {"tool_id": "GQ-05", "name": "折叠踏梯", "status": "retired"},
]

# 扫码账：
# - S1/S2 互为候选构成交替环，形成待核组；
# - S3 唯一候选 GQ-03，所有最优配法一致，记盘到；
# - S4 扫到借出、S5 扫到退役、S6 为无效号，分别归类误扫。
DEMO_SCANS = [
    {"scan_id": "S-1001", "tag_read": "GQ01/GQ02", "candidates": ["GQ-01", "GQ-02"],
     "source": "北门固定岗", "scanned_at": "2026-11-30T09:02"},
    {"scan_id": "S-1002", "tag_read": "GQ02/GQ01", "candidates": ["GQ-02", "GQ-01"],
     "source": "北门固定岗", "scanned_at": "2026-11-30T09:05"},
    {"scan_id": "S-1003", "tag_read": "GQ03", "candidates": ["GQ-03"],
     "source": "南门移动岗", "scanned_at": "2026-11-30T09:11"},
    {"scan_id": "S-1004", "tag_read": "GQ04", "candidates": ["GQ-04"],
     "source": "货架补扫", "scanned_at": "2026-11-30T09:18"},
    {"scan_id": "S-1005", "tag_read": "GQ05", "candidates": ["GQ-05"],
     "source": "后门补扫", "scanned_at": "2026-11-30T09:24"},
    {"scan_id": "S-1006", "tag_read": "GQ99", "candidates": ["GQ-99"],
     "source": "南门移动岗", "scanned_at": "2026-11-30T09:31"},
]


def demo_tools():
    return [dict(row) for row in DEMO_TOOLS]


def demo_scans():
    return [dict(row) for row in DEMO_SCANS]
