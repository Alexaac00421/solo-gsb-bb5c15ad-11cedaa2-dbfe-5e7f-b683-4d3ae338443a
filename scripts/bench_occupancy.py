#!/usr/bin/env python3
"""容量确认“按需读取占用”的性能验证（仅用标准库，直接走存储层）。

场景
====
两条互不相干的步道：E1(A->B) 与 E9(X->Y)，容量均为 1。
先在 E9 上累积 K 条已确认行程（模拟高峰期其他步道上的大量确认），
随后在 E1 上确认一条新行程，统计本次确认**实际读取的占用行数**与
触及的路段，并与旧版“预加载整个世代全部占用”的读取量对照。

预期：无论 K 多大，E1 上的确认只读取 E1 的占用（行数恒定），
即“未触及路段的行程增长不使单次确认的占用读取量线性增长”。

用法::

    python3 scripts/bench_occupancy.py [K ...]   # 默认: 0 200 2000

K 可任意加大（如 20000）；堆积阶段逐条确认，耗时随 K 增长。
"""
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.booking import confirm_trip_atomic  # noqa: E402

GRAPH = {
    "nodes": ["A", "B", "X", "Y"],
    "edges": [
        {"id": "E1", "from": "A", "to": "B", "travel_seconds": 10,
         "has_stairs": False, "capacity": 1},
        {"id": "E9", "from": "X", "to": "Y", "travel_seconds": 10,
         "has_stairs": False, "capacity": 1},
    ],
}
D0 = 10**9  # E1 上确认的出发时刻（与 E9 的占用区间错开）


def measure(k: int) -> None:
    with tempfile.TemporaryDirectory() as td:
        conn = db.connect(Path(td) / "bench.db")
        db.init_db(conn)
        db.publish_graph(conn, GRAPH)
        # 高峰期：E9 上累积 K 条已确认行程（同一世代）
        for _ in range(k):
            confirm_trip_atomic(conn, origin="X", destination="Y",
                                departure=0, avoid_stairs=False, people=1)
        # E1 上先确认 2 条，使被测确认自身也有占用可读
        for _ in range(2):
            confirm_trip_atomic(conn, origin="A", destination="B",
                                departure=D0, avoid_stairs=False, people=1)

        reads = {"rows": 0, "edges": set()}
        real = db.edge_occupancy

        def spy(conn_, generation_id, edge_id, not_before=0):
            rows = real(conn_, generation_id, edge_id, not_before)
            reads["rows"] += len(rows)
            reads["edges"].add(edge_id)
            return rows

        db.edge_occupancy = spy
        try:
            t0 = time.perf_counter()
            confirm_trip_atomic(conn, origin="A", destination="B",
                                departure=D0, avoid_stairs=False, people=1)
            elapsed_ms = (time.perf_counter() - t0) * 1000
        finally:
            db.edge_occupancy = real

        # 旧版行为对照：预加载整个世代的全部占用
        snap = db.get_snapshot(conn)
        full = db.active_occupancy(conn, snap.generation_id)
        full_rows = sum(len(v) for v in full.values())
        print(f"K={k:<6} 触及路段={sorted(reads['edges'])} "
              f"按需读取={reads['rows']} 行 | 旧版预加载={full_rows} 行 "
              f"| 本次确认耗时={elapsed_ms:.1f} ms")
        conn.close()


def main() -> int:
    ks = [int(a) for a in sys.argv[1:]] or [0, 200, 2000]
    print("在 E9 上累积 K 条已确认行程后，在 E1 上做一次确认：")
    for k in ks:
        measure(k)
    print("\n预期：按需读取行数不随 K 增长（只读 E1 的占用）；"
          "旧版预加载行数随 K 线性增长。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
