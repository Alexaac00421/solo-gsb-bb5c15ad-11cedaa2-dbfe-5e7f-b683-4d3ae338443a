"""行程确认“按需读取占用”测试。

覆盖：
- 寻路只读取实际触及的有容量路段占用；未触及路段上的行程增长不使
  单次确认的占用读取量线性增长；
- 按需视图与全量预加载（旧行为）的规划结果完全一致（半开区间、
  容量等待、最早到达与全程编号序列平局不变）；
- 不晚于出发时刻即结束的占用在读取时被过滤（半开区间端点相接）；
- 占用查询经 (edge_id, leave_ts) 索引定位，不整表扫描；
- 旧库自动补建索引（CREATE INDEX IF NOT EXISTS），数据不受影响；
- 并发确认/取消/封闭更新混合压力下不超额、不混用世代。
"""
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from app import db
from app.booking import DemandOccupancy, cancel_trip_atomic, confirm_trip_atomic
from app.db import (
    EDGE_OCCUPANCY_SQL,
    active_occupancy,
    connect,
    get_snapshot,
    init_db,
    publish_closures,
    publish_graph,
)
from app.routing import build_engine

# 两条互不相干的步道：E1(A->B) 与 E9(X->Y)，容量均为 1
GRAPH_TWO_TRAILS = {
    "nodes": ["A", "B", "X", "Y"],
    "edges": [
        {"id": "E1", "from": "A", "to": "B", "travel_seconds": 10,
         "has_stairs": False, "capacity": 1},
        {"id": "E9", "from": "X", "to": "Y", "travel_seconds": 10,
         "has_stairs": False, "capacity": 1},
    ],
}
D0 = 1_000_000_000  # E1 上确认的出发时刻（与 E9 的占用区间错开）


class OnDemandTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "test.db")
        os.environ["APP_DB_PATH"] = self.db_path
        self.conn = connect(Path(self.db_path))
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()
        os.environ.pop("APP_DB_PATH", None)

    def confirm(self, **kwargs):
        return confirm_trip_atomic(self.conn, **kwargs)

    @staticmethod
    def occupancy_spy():
        """包装 db.edge_occupancy 的计数间谍（确认路径与引擎共用该入口）。"""
        calls = {"rows": 0, "edges": []}
        real = db.edge_occupancy

        def spy(conn, generation_id, edge_id, not_before=0, exclude_trip_id=None):
            rows = real(conn, generation_id, edge_id, not_before, exclude_trip_id)
            calls["rows"] += len(rows)
            calls["edges"].append(edge_id)
            return rows

        return calls, spy


class ReadVolumeTests(OnDemandTestBase):
    """未触及路段的行程增长不得使单次确认的读取量线性增长。"""

    def _measure_confirm_reads(self, background_trips: int):
        """在 E9 上累积 background_trips 条已确认行程后，在 E1 上确认
        一条新行程，返回 (读取统计, 行程记录)。"""
        with tempfile.TemporaryDirectory() as td:
            conn = connect(Path(td) / "bench.db")
            init_db(conn)
            publish_graph(conn, GRAPH_TWO_TRAILS)
            # 高峰期：E9 上累积大量已确认行程（同一世代）
            for _ in range(background_trips):
                confirm_trip_atomic(
                    conn, origin="X", destination="Y", departure=0,
                    avoid_stairs=False, people=1,
                )
            # E1 上先确认 2 条，使被测确认自身也有占用可读
            for _ in range(2):
                confirm_trip_atomic(
                    conn, origin="A", destination="B", departure=D0,
                    avoid_stairs=False, people=1,
                )
            calls, spy = self.occupancy_spy()
            with mock.patch.object(db, "edge_occupancy", spy):
                record, _ = confirm_trip_atomic(
                    conn, origin="A", destination="B", departure=D0,
                    avoid_stairs=False, people=1,
                )
            conn.close()
            return calls, record

    def test_untouched_edge_trips_do_not_inflate_reads(self):
        stats = {}
        for k in (0, 30, 120):
            calls, record = self._measure_confirm_reads(k)
            stats[k] = calls["rows"]
            # 只触及 E1：E9 上的 k 条行程完全不被读取
            self.assertEqual(set(calls["edges"]), {"E1"})
            # 每条触及的路段在一次确认内至多读取一次（逐路段缓存）
            self.assertEqual(len(calls["edges"]), len(set(calls["edges"])))
            # 读取行数 = E1 自身的 2 条占用，与 k 无关
            self.assertEqual(calls["rows"], 2)
            # 新行程排在两条既有占用之后（容量等待语义不变）
            self.assertEqual(record.segments[0].enter_ts, D0 + 20)
        # 读量不随未触及路段上的行程数增长
        self.assertEqual(len(set(stats.values())), 1)

    def test_zero_segment_trip_never_reads_unreachable_edges(self):
        publish_graph(self.conn, GRAPH_TWO_TRAILS)
        for _ in range(5):
            self.confirm(origin="X", destination="Y", departure=0,
                         avoid_stairs=False, people=1)
        calls, spy = self.occupancy_spy()
        with mock.patch.object(db, "edge_occupancy", spy):
            record, _ = self.confirm(origin="A", destination="A", departure=D0,
                                     avoid_stairs=False, people=1)
        self.assertEqual(record.segments, [])
        # 起点即终点：搜索仍只触及 A 可达的路段，E9 上的行程不被读取
        self.assertNotIn("E9", calls["edges"])

    def test_occupancy_ending_at_or_before_departure_is_not_read(self):
        publish_graph(self.conn, GRAPH_TWO_TRAILS)
        # 既有占用 E1 [0,10)
        self.confirm(origin="A", destination="B", departure=0,
                     avoid_stairs=False, people=1)

        # 出发时刻 5：占用 [0,10) 相关 → 读到 1 行，等到 10 进入
        calls, spy = self.occupancy_spy()
        with mock.patch.object(db, "edge_occupancy", spy):
            rec2, _ = self.confirm(origin="A", destination="B", departure=5,
                                   avoid_stairs=False, people=1)
        self.assertEqual(calls["rows"], 1)
        self.assertEqual(rec2.segments[0].enter_ts, 10)

        # 出发时刻 20：既有占用 leave_ts<=20 全部不可能阻挡（半开端点
        # 相接允许），读取时直接过滤 → 0 行，立即进入
        calls2, spy2 = self.occupancy_spy()
        with mock.patch.object(db, "edge_occupancy", spy2):
            rec3, _ = self.confirm(origin="A", destination="B", departure=20,
                                   avoid_stairs=False, people=1)
        self.assertEqual(calls2["rows"], 0)
        self.assertEqual(rec3.segments[0].enter_ts, 20)


class LazyEngineTests(unittest.TestCase):
    """引擎层：可调用占用源按需读取，且与预加载 dict 结果一致。"""

    def snap(self):
        from app.db import ClosureRow, EdgeRow, ReverseWindowRow, Snapshot
        edges = [
            EdgeRow("E1", "A", "B", 100, False, 1),
            EdgeRow("E2", "B", "C", 100, False, 1),
            EdgeRow("E3", "A", "C", 300, True, None),
        ]
        return Snapshot(1, ["A", "B", "C"], edges,
                        [ClosureRow("E2", 150, 180)],
                        [ReverseWindowRow("E1", 0, 40)], 1)

    def test_callable_source_fetches_only_touched_capacity_edges(self):
        occ = {"E1": [(0, 100, 1)], "E2": [(100, 200, 1)], "E3": [(0, 300, 1)]}
        fetched = []

        def source(edge_id):
            fetched.append(edge_id)
            return occ.get(edge_id, ())

        route = build_engine(self.snap(), source).plan("A", "C", 0)
        # E1 容量 1 被占 [0,100) → 等至 100；E2 被占 [100,200) → 等至 200
        self.assertEqual(
            [(s.edge_id, s.enter_time, s.leave_time) for s in route.segments],
            [("E1", 100, 200), ("E2", 200, 300)],
        )
        # E3 不限容量：不触发读取；E1/E2 各只读取一次（两阶段+重放缓存）
        self.assertNotIn("E3", fetched)
        self.assertEqual(sorted(fetched), ["E1", "E2"])

    def test_callable_source_matches_preloaded_dict(self):
        occ = {"E1": [(0, 100, 1)], "E2": [(100, 200, 1)]}
        snap = self.snap()
        eager = build_engine(snap, occ).plan("A", "C", 0)
        lazy = build_engine(snap, lambda e: occ.get(e, ())).plan("A", "C", 0)
        self.assertEqual(eager, lazy)
        # 避台阶（E3 台阶直达被排除）与不同出发时刻下同样一致
        for dep in (0, 37, 150):
            e = build_engine(snap, occ).plan("A", "C", dep, avoid_stairs=True)
            l = build_engine(snap, lambda eid: occ.get(eid, ())).plan(
                "A", "C", dep, avoid_stairs=True)
            self.assertEqual(e, l)


class EquivalenceTests(OnDemandTestBase):
    """按需视图（新）与全量预加载（旧行为）在同一数据上规划结果一致。"""

    GRAPH = {
        "nodes": ["A", "B", "C"],
        "edges": [
            {"id": "E1", "from": "A", "to": "B", "travel_seconds": 100,
             "has_stairs": False, "capacity": 1},
            {"id": "E2", "from": "B", "to": "C", "travel_seconds": 100,
             "has_stairs": False, "capacity": 2},
            {"id": "E3", "from": "A", "to": "C", "travel_seconds": 300,
             "has_stairs": True},
        ],
    }

    def test_demand_view_matches_full_preload(self):
        publish_graph(self.conn, self.GRAPH)
        # 累积若干确认行程（含容量等待与避台阶）
        for kwargs in (
            dict(departure=0, people=1),
            dict(departure=0, people=1),
            dict(departure=50, people=2),
            dict(departure=0, people=1, avoid_stairs=True),
        ):
            self.confirm(origin="A", destination="C",
                         avoid_stairs=kwargs.get("avoid_stairs", False),
                         departure=kwargs["departure"], people=kwargs["people"])
        snap = get_snapshot(self.conn)
        full = active_occupancy(self.conn, snap.generation_id)
        for dep, people, avoid in ((0, 1, False), (37, 2, False),
                                   (100, 1, True), (250, 2, False)):
            eager = build_engine(snap, full).plan(
                "A", "C", dep, avoid_stairs=avoid, party_size=people)
            demand = DemandOccupancy(self.conn, snap.generation_id,
                                     not_before=dep)
            lazy = build_engine(snap, demand).plan(
                "A", "C", dep, avoid_stairs=avoid, party_size=people)
            self.assertEqual(eager, lazy)
            # 只读取触及的有容量路段（E1/E2），E3 不限容量从不读取
            self.assertLessEqual(set(demand.edges_read), {"E1", "E2"})
            # 读取行数 = 触及路段上 leave_ts > 出发时刻的占用条数
            expected_rows = sum(
                1 for e in demand.edges_read for u in full.get(e, ())
                if u[1] > dep
            )
            self.assertEqual(demand.rows_read, expected_rows)


class QueryPlanTests(OnDemandTestBase):
    def test_edge_occupancy_query_uses_index_without_scans(self):
        publish_graph(self.conn, GRAPH_TWO_TRAILS)
        for _ in range(50):
            self.confirm(origin="X", destination="Y", departure=0,
                         avoid_stairs=False, people=1)
        plan = self.conn.execute(
            "EXPLAIN QUERY PLAN " + EDGE_OCCUPANCY_SQL, ("E1", 0, 1, None, None)
        ).fetchall()
        detail = " | ".join(r["detail"] for r in plan)
        # 按 (edge_id, leave_ts) 索引范围定位 + 行程主键回表，无整表扫描
        self.assertIn("idx_trip_seg_edge", detail)
        self.assertNotIn("SCAN", detail.upper())

    def test_old_db_gets_edge_index_on_init(self):
        # 模拟旧库：索引缺失时 init_db 自动补建，数据不受影响
        publish_graph(self.conn, GRAPH_TWO_TRAILS)
        self.confirm(origin="A", destination="B", departure=0,
                     avoid_stairs=False, people=1)
        self.conn.execute("DROP INDEX IF EXISTS idx_trip_seg_edge")
        init_db(self.conn)
        row = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_trip_seg_edge'"
        ).fetchone()
        self.assertIsNotNone(row)
        # 旧行程数据仍在，确认语义不变
        rec, _ = self.confirm(origin="A", destination="B", departure=0,
                              avoid_stairs=False, people=1)
        self.assertEqual(rec.segments[0].enter_ts, 10)


class MixedConcurrencyTests(OnDemandTestBase):
    """并发确认/取消/封闭更新混合压力：不超额、不混用世代。"""

    def test_mixed_load_never_exceeds_capacity(self):
        publish_graph(self.conn, GRAPH_TWO_TRAILS)
        errors = []

        def worker(origin, destination, count, cancel_odd=False):
            for i in range(count):
                c = connect(Path(self.db_path))
                try:
                    init_db(c)
                    record, _ = confirm_trip_atomic(
                        c, origin=origin, destination=destination,
                        departure=0, avoid_stairs=False, people=1,
                    )
                    if cancel_odd and i % 2:
                        cancel_trip_atomic(c, record.trip_id)
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)
                finally:
                    c.close()

        threads = [
            threading.Thread(target=worker, args=("A", "B", 6)),
            threading.Thread(target=worker, args=("X", "Y", 6)),
            threading.Thread(target=worker, args=("A", "B", 6, True)),
        ]
        for t in threads:
            t.start()
        # 封闭更新与确认并发：沿用同一世代，已确认行程继续计量
        publish_closures(self.conn, {"closures": []})
        for t in threads:
            t.join()
        self.assertEqual(errors, [])

        # 不变量：最终世代内未取消行程在每条边上的峰值占用 <= 容量 1
        snap = get_snapshot(self.conn)
        occupancy = active_occupancy(self.conn, snap.generation_id)
        for edge_id, intervals in occupancy.items():
            events = sorted(
                [ev for a, b, p in intervals for ev in ((a, p), (b, -p))],
            )
            load = peak = 0
            for _, delta in events:
                load += delta
                peak = max(peak, load)
            self.assertLessEqual(peak, 1, f"{edge_id} 峰值占用 {peak} 超额")


if __name__ == "__main__":
    unittest.main()
