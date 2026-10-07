"""内置示例数据，首次启动自动导入。

园区示意：

    GATE ──E1──> PLAZA ──E2──> HILL(台阶)
                  │  ╲           │
                  E3  ╲E6(台阶)  E4(台阶,施工封闭)
                  ▼    ╲         ▼
                 LAKE ──E5──── TOWER

10:00–11:00 之间 E4 封闭施工。

高峰容量保障（行程确认 ``POST /api/trips``）：主干道 E1/E3/E5 各限
2 人（同时占用计），其余路段缺省不限；超容确认会在节点等待前段
行程离开，无法等到空位时被明确拒绝。
"""

SAMPLE_GRAPH = {
    "nodes": ["GATE", "PLAZA", "HILL", "LAKE", "TOWER"],
    "edges": [
        {"id": "E1", "from": "GATE",  "to": "PLAZA", "travel_seconds": 180, "has_stairs": False, "capacity": 2},
        {"id": "E2", "from": "PLAZA", "to": "HILL",  "travel_seconds": 240, "has_stairs": True},
        {"id": "E3", "from": "PLAZA", "to": "LAKE",  "travel_seconds": 300, "has_stairs": False, "capacity": 2},
        {"id": "E4", "from": "HILL",  "to": "TOWER", "travel_seconds": 120, "has_stairs": True},
        {"id": "E5", "from": "LAKE",  "to": "TOWER", "travel_seconds": 240, "has_stairs": False, "capacity": 2},
        {"id": "E6", "from": "PLAZA", "to": "TOWER", "travel_seconds": 600, "has_stairs": True},
    ],
}

SAMPLE_CLOSURES = {
    "closures": [
        {
            "edge_id": "E4",
            "start": "2026-10-05T10:00:00Z",
            "end": "2026-10-05T11:00:00Z",
        }
    ]
}
