"""容量确认（行程预订）测试。

覆盖：
- 路段容量字段的导入/预检校验（缺省不限、正整数、bool/float/0 拒绝）；
- 半开占用区间、同发等待、端点相接不超额、人数超容拒绝；
- 逐段等待（链式路段流水）、与封闭/反向窗/避台阶的组合；
- 原子确认、查询、取消幂等（重复取消不重复释放）；
- 并发确认不超额（写事务串行）；
- 封闭更新沿用世代、重新发布/版本恢复开启新世代且旧行程可查可取消；
- 数据版本冲突不写入；普通路线查询不受容量影响（语义不变）。
"""
import os
import tempfile
import threading
import unittest
from pathlib import Path

from app.booking import (
    VersionConflictError,
    cancel_trip_atomic,
    confirm_trip_atomic,
)
from app.db import (
    active_occupancy,
    connect,
    current_version,
    get_snapshot,
    get_trip,
    init_db,
    publish_closures,
    publish_graph,
    restore_version,
)
from app.routing import NoRouteError, RouteEngine, build_engine
from app.db import ClosureRow, EdgeRow, Snapshot
from app.schemas import ValidationError, normalize_booking, normalize_graph

GRAPH_CAP = {
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
D = 1000000  # 固定出发纪元秒，避开边界干扰


class BookingTestBase(unittest.TestCase):
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

    def publish(self, graph=GRAPH_CAP, closures=None):
        v = publish_graph(self.conn, graph)
        if closures is not None:
            publish_closures(self.conn, closures)
        return v

    def confirm(self, origin="A", destination="C", departure=D,
                avoid_stairs=False, people=1, expected_version=None):
        return confirm_trip_atomic(
            self.conn,
            origin=origin, destination=destination, departure=departure,
            avoid_stairs=avoid_stairs, people=people,
            expected_version=expected_version,
        )


class CapacitySchemaTests(unittest.TestCase):
    def test_capacity_optional_defaults_unlimited(self):
        graph = normalize_graph({
            "nodes": ["A", "B"],
            "edges": [{"id": "E1", "from": "A", "to": "B",
                       "travel_seconds": 10, "has_stairs": False}],
        })
        self.assertIsNone(graph["edges"][0]["capacity"])

    def test_capacity_null_means_unlimited(self):
        graph = normalize_graph({
            "nodes": ["A", "B"],
            "edges": [{"id": "E1", "from": "A", "to": "B",
                       "travel_seconds": 10, "has_stairs": False,
                       "capacity": None}],
        })
        self.assertIsNone(graph["edges"][0]["capacity"])

    def test_capacity_must_be_positive_int(self):
        for bad in (0, -1, 1.5, True, "3", 2.0):
            with self.assertRaises(ValidationError):
                normalize_graph({
                    "nodes": ["A", "B"],
                    "edges": [{"id": "E1", "from": "A", "to": "B",
                               "travel_seconds": 10, "has_stairs": False,
                               "capacity": bad}],
                })

    def test_capacity_persisted_and_copied(self):
        with tempfile.TemporaryDirectory() as td:
            os.environ["APP_DB_PATH"] = str(Path(td) / "x.db")
            try:
                conn = connect(Path(os.environ["APP_DB_PATH"]))
                init_db(conn)
                publish_graph(conn, GRAPH_CAP)
                snap = get_snapshot(conn)
                caps = {e.id: e.capacity for e in snap.edges}
                self.assertEqual(caps, {"E1": 1, "E2": 2, "E3": None})
                # 封闭更新复制版本时容量随图保留
                publish_closures(conn, {"closures": []})
                snap2 = get_snapshot(conn)
                self.assertEqual({e.id: e.capacity for e in snap2.edges}, caps)
                conn.close()
            finally:
                os.environ.pop("APP_DB_PATH", None)


class CapacityEngineTests(unittest.TestCase):
    """直接对引擎注入占用，校验容量等待与 FIFO。"""

    def engine(self, occupancy=None, closures=(), caps=None):
        edges = [
            EdgeRow("E1", "A", "B", 100, False, (caps or {}).get("E1", 1)),
            EdgeRow("E2", "B", "C", 100, False, (caps or {}).get("E2", 1)),
        ]
        snap = Snapshot(1, ["A", "B", "C"], edges,
                        [ClosureRow(*c) for c in closures], [], 1)
        # 传入空 dict 即“容量确认模式”（区别于 None 的普通查询模式）
        return build_engine(snap, {} if occupancy is None else occupancy)

    def test_no_occupancy_unchanged(self):
        r = self.engine().plan("A", "C", 0)
        self.assertEqual([(s.edge_id, s.enter_time, s.leave_time) for s in r.segments],
                         [("E1", 0, 100), ("E2", 100, 200)])

    def test_half_open_boundary_allows_back_to_back(self):
        # 既有占用 [0,100)：新行程恰好在 100 进入不超额
        r = self.engine({"E1": [(0, 100, 1)]}).plan("A", "B", 0)
        self.assertEqual(r.segments[0].enter_time, 100)
        self.assertEqual(r.segments[0].waiting_seconds, 100)

    def test_overlap_forces_wait_until_leave(self):
        r = self.engine({"E1": [(50, 150, 1)]}).plan("A", "B", 0)
        self.assertEqual(r.segments[0].enter_time, 150)

    def test_party_size_aggregates_within_capacity(self):
        # 容量 2：既有 1 人占用 [0,100)，再来 1 人可同时进入；再来 2 人须等待
        eng = self.engine({"E1": [(0, 100, 1)]}, caps={"E1": 2, "E2": 1})
        r1 = eng.plan("A", "B", 0, party_size=1)
        self.assertEqual(r1.segments[0].enter_time, 0)
        r2 = eng.plan("A", "B", 0, party_size=2)
        self.assertEqual(r2.segments[0].enter_time, 100)

    def test_party_larger_than_capacity_unreachable(self):
        # 避台阶后只剩容量 1 的 E1+E2；2 人团体恒不可用
        with self.assertRaises(NoRouteError):
            self.engine(caps={"E1": 1, "E2": 1}).plan(
                "A", "C", 0, avoid_stairs=True, party_size=2)

    def test_wait_at_each_segment_pipeline(self):
        # 两条容量 1 的边，已有行程 0 出发：后来者在每条边上各等 100
        r = self.engine({"E1": [(0, 100, 1)], "E2": [(100, 200, 1)]}).plan(
            "A", "C", 0)
        self.assertEqual([(s.edge_id, s.enter_time, s.leave_time) for s in r.segments],
                         [("E1", 100, 200), ("E2", 200, 300)])

    def test_later_departure_never_arrives_earlier_fifo(self):
        eng = self.engine({"E1": [(0, 100, 1)]})
        early = eng.plan("A", "B", 0)
        late = eng.plan("A", "B", 50)
        self.assertGreaterEqual(
            late.arrival_time, early.arrival_time)

    def test_capacity_with_closure_combined(self):
        # 容量 1：占用 [0,100)；E1 另有封闭 [100,200)
        # 0 出发：容量推到 100，但 100 起封闭 -> 再等到 200 进入
        eng = self.engine({"E1": [(0, 100, 1)]}, closures=[("E1", 100, 200)])
        r = eng.plan("A", "B", 0)
        self.assertEqual(r.segments[0].enter_time, 200)

    def test_capacity_with_reverse_window(self):
        # B->A 反向窗 [0,500)；容量 1，已有正向占用 [100,200) 对反向同样计数
        edges = [EdgeRow("E1", "A", "B", 100, False, 1)]
        from app.db import ReverseWindowRow
        snap = Snapshot(1, ["A", "B"], edges, [],
                        [ReverseWindowRow("E1", 0, 500)], 1)
        eng = build_engine(snap, {"E1": [(100, 200, 1)]})
        r = eng.plan("B", "A", 0)
        # 0 进入反向时占用 [100,200) 与 [0,100) 不重叠 → 可立即走
        self.assertEqual(r.segments[0].enter_time, 0)
        # 50 进入会与 [100,200) 重叠 → 等到 200（窗仍开着）
        r2 = eng.plan("B", "A", 50)
        self.assertEqual(r2.segments[0].enter_time, 200)

    def test_unlimited_edge_ignores_occupancy(self):
        edges = [EdgeRow("E1", "A", "B", 100, False, None)]
        snap = Snapshot(1, ["A", "B"], edges, [], [], 1)
        eng = build_engine(snap, {"E1": [(0, 100, 50)]})
        r = eng.plan("A", "B", 0, party_size=1000)
        self.assertEqual(r.segments[0].enter_time, 0)

    def test_avoid_stairs_still_applies(self):
        # E3 是台阶直达，避台阶后只能走 E1+E2
        edges = [
            EdgeRow("E1", "A", "B", 100, False, 1),
            EdgeRow("E2", "B", "C", 100, False, 1),
            EdgeRow("E3", "A", "C", 300, True, None),
        ]
        snap = Snapshot(1, ["A", "B", "C"], edges, [], [], 1)
        eng = build_engine(snap, None)
        r = eng.plan("A", "C", 0, avoid_stairs=True)
        self.assertEqual([s.edge_id for s in r.segments], ["E1", "E2"])


class ConfirmFlowTests(BookingTestBase):
    def test_confirm_returns_id_segments_and_versions(self):
        self.publish()
        record, snap = self.confirm()
        self.assertEqual(record.version_id, snap.version_id)
        self.assertEqual(record.generation_id, snap.generation_id)
        self.assertEqual(record.status, "confirmed")
        self.assertEqual(record.party_size, 1)
        self.assertEqual([s.edge_id for s in record.segments], ["E1", "E2"])
        self.assertEqual(record.segments[0].enter_ts, D)
        self.assertEqual(record.segments[0].leave_ts, D + 100)

    def test_confirm_zero_segment_trip_origin_equals_destination(self):
        self.publish()
        record, _ = self.confirm(origin="A", destination="A")
        self.assertEqual(record.segments, [])
        self.assertEqual(record.arrival_ts, D)

    def test_second_confirmation_waits_for_capacity(self):
        self.publish()
        t1, _ = self.confirm(departure=D)
        t2, _ = self.confirm(departure=D)
        # E1/E2 容量：E1=1 → t2 全程后移 100
        self.assertEqual(t2.segments[0].enter_ts, D + 100)
        self.assertEqual(t2.segments[1].enter_ts, D + 200)
        self.assertEqual(t2.arrival_ts, D + 300)

    def test_no_capacity_feasible_route_rejects_without_occupancy_left(self):
        # 单人容量 1 的边被持续占用至很晚，且无替代路径 → 拒绝
        self.publish(graph={
            "nodes": ["A", "B"],
            "edges": [{"id": "E1", "from": "A", "to": "B", "travel_seconds": 10,
                       "has_stairs": False, "capacity": 1}],
        })
        t1, _ = self.confirm(origin="A", destination="B", departure=0)
        self.assertEqual(t1.trip_id, 1)
        with self.assertRaises(NoRouteError):
            # 人数 2 超过容量 1，唯一路径不可用
            self.confirm(origin="A", destination="B", departure=0, people=2)
        # 拒绝不留占用：仍可确认 1 人新行程（排在 t1 之后）
        t2, _ = self.confirm(origin="A", destination="B", departure=0, people=1)
        self.assertEqual(t2.segments[0].enter_ts, 10)

    def test_get_trip_by_id(self):
        self.publish()
        record, _ = self.confirm()
        fetched = get_trip(self.conn, record.trip_id)
        self.assertEqual(fetched.trip_id, record.trip_id)
        self.assertEqual(len(fetched.segments), 2)
        self.assertIsNone(get_trip(self.conn, 99999))

    def test_cancel_releases_capacity_and_is_idempotent(self):
        self.publish()
        t1, _ = self.confirm(departure=D)
        t2, _ = self.confirm(departure=D)  # 被推迟到 D+100
        self.assertEqual(t2.segments[0].enter_ts, D + 100)

        record, released = cancel_trip_atomic(self.conn, t1.trip_id)
        self.assertTrue(released)
        self.assertEqual(record.status, "cancelled")
        self.assertIsNotNone(record.cancelled_at)

        # 重复取消：幂等，不重复释放
        record2, released2 = cancel_trip_atomic(self.conn, t1.trip_id)
        self.assertFalse(released2)
        self.assertEqual(record2.status, "cancelled")

        # 容量已释放：新行程可立即进入
        t3, _ = self.confirm(departure=D)
        self.assertEqual(t3.segments[0].enter_ts, D)

    def test_cancel_unknown_trip_returns_none(self):
        self.publish()
        self.assertIsNone(cancel_trip_atomic(self.conn, 424242))

    def test_occupancy_excludes_cancelled_and_other_generations(self):
        self.publish()
        t1, _ = self.confirm(departure=D)
        gen = get_snapshot(self.conn).generation_id
        occ = active_occupancy(self.conn, gen)
        self.assertEqual(len(occ["E1"]), 1)
        cancel_trip_atomic(self.conn, t1.trip_id)
        self.assertEqual(active_occupancy(self.conn, gen), {})
        # 其他世代查不到本行程的占用
        self.assertEqual(active_occupancy(self.conn, gen + 999), {})


class BookingPayloadTests(BookingTestBase):
    def norm(self, payload):
        self.publish()
        return normalize_booking(payload, get_snapshot(self.conn).node_set)

    def test_valid_payload(self):
        q = self.norm({
            "origin": "A", "destination": "C",
            "departure_time": D, "avoid_stairs": True,
            "people": 3, "data_version": 1,
        })
        self.assertEqual(q["people"], 3)
        self.assertTrue(q["avoid_stairs"])
        self.assertEqual(q["expected_version"], 1)

    def test_data_version_optional(self):
        q = self.norm({"origin": "A", "destination": "C",
                       "departure_time": D, "people": 1})
        self.assertIsNone(q["expected_version"])

    def test_people_must_be_positive_int(self):
        for bad in (0, -2, 1.0, "2", True, None):
            with self.assertRaises(ValidationError):
                self.norm({"origin": "A", "destination": "C",
                           "departure_time": D, "people": bad})

    def test_missing_required_fields(self):
        for payload in (
            {"origin": "A", "destination": "C", "departure_time": D},
            {"origin": "A", "departure_time": D, "people": 1},
        ):
            with self.assertRaises(ValidationError):
                self.norm(payload)

    def test_unknown_nodes_and_bad_time(self):
        with self.assertRaises(ValidationError):
            self.norm({"origin": "Z", "destination": "C",
                       "departure_time": D, "people": 1})
        with self.assertRaises(ValidationError):
            self.norm({"origin": "A", "destination": "C",
                       "departure_time": "not-a-time", "people": 1})

    def test_stops_rejected_in_booking(self):
        with self.assertRaises(ValidationError):
            self.norm({"origin": "A", "destination": "C",
                       "departure_time": D, "people": 1,
                       "stops": [{"node": "B"}]})

    def test_bad_expected_version(self):
        for bad in (0, -1, "1", 1.5, True):
            with self.assertRaises(ValidationError):
                self.norm({"origin": "A", "destination": "C",
                           "departure_time": D, "people": 1,
                           "data_version": bad})


class VersionConflictTests(BookingTestBase):
    def test_stale_version_conflicts_and_writes_nothing(self):
        v1 = self.publish()
        publish_closures(self.conn, {"closures": []})  # v2，同世代
        before = len(self.conn.execute("SELECT * FROM trips").fetchall())
        with self.assertRaises(VersionConflictError) as ctx:
            self.confirm(expected_version=v1)
        self.assertEqual(ctx.exception.expected_version, v1)
        self.assertEqual(ctx.exception.current_version, current_version(self.conn))
        after = len(self.conn.execute("SELECT * FROM trips").fetchall())
        self.assertEqual(before, after)  # 无任何写入

    def test_matching_version_confirms(self):
        self.publish()
        v = current_version(self.conn)
        record, _ = self.confirm(expected_version=v)
        self.assertEqual(record.version_id, v)


class GenerationTests(BookingTestBase):
    def test_closures_update_keeps_generation_and_metering(self):
        self.publish()
        t1, _ = self.confirm(departure=D)  # 占 E1[D,D+100)
        gen_before = get_snapshot(self.conn).generation_id
        publish_closures(self.conn, {"closures": []})
        snap = get_snapshot(self.conn)
        self.assertEqual(snap.generation_id, gen_before)
        # 同世代：t1 的占用仍计入
        t2, _ = self.confirm(departure=D)
        self.assertEqual(t2.segments[0].enter_ts, D + 100)

    def test_graph_republish_starts_independent_generation(self):
        self.publish()
        t1, _ = self.confirm(departure=D)
        old_generation = get_snapshot(self.conn).generation_id
        publish_graph(self.conn, GRAPH_CAP)  # 新图（内容相同）→ 新世代
        snap = get_snapshot(self.conn)
        self.assertNotEqual(snap.generation_id, old_generation)
        # 新世代独立计量：立即进入，不受 t1 影响
        t2, _ = self.confirm(departure=D)
        self.assertEqual(t2.segments[0].enter_ts, D)
        # 旧行程仍可查询、可取消
        fetched = get_trip(self.conn, t1.trip_id)
        self.assertEqual(fetched.status, "confirmed")
        _, released = cancel_trip_atomic(self.conn, t1.trip_id)
        self.assertTrue(released)

    def test_restore_starts_new_generation(self):
        v1 = self.publish()
        publish_graph(self.conn, GRAPH_CAP)  # v2 新世代
        t_old, _ = self.confirm(departure=D)  # 在 v2 世代占用
        new_v = restore_version(self.conn, v1)
        snap = get_snapshot(self.conn)
        self.assertEqual(snap.version_id, new_v)
        self.assertEqual(snap.generation_id, new_v)
        # 恢复出的新世代独立计量
        t_new, _ = self.confirm(departure=D)
        self.assertEqual(t_new.segments[0].enter_ts, D)
        # 旧行程仍可查可取消
        self.assertEqual(get_trip(self.conn, t_old.trip_id).status, "confirmed")
        cancel_trip_atomic(self.conn, t_old.trip_id)

    def test_migration_backfills_generations(self):
        """旧库 versions 无 generation_id 时按发布语义回填。"""
        self.publish()  # v1 graph -> gen1
        publish_closures(self.conn, {"closures": []})  # v2 -> gen1
        publish_graph(self.conn, GRAPH_CAP)  # v3 graph -> gen3
        # 直接删除列重建，模拟旧库升级
        self.conn.execute("BEGIN IMMEDIATE")
        self.conn.execute("ALTER TABLE versions RENAME TO versions_old")
        self.conn.execute(
            "CREATE TABLE versions (version_id INTEGER PRIMARY KEY, kind TEXT, created_at INTEGER)"
        )
        self.conn.execute(
            "INSERT INTO versions SELECT version_id, kind, created_at FROM versions_old"
        )
        self.conn.execute("DROP TABLE versions_old")
        self.conn.execute("COMMIT")
        # init_db 的迁移应重建并回填世代
        init_db(self.conn)
        gens = {
            r["version_id"]: r["generation_id"]
            for r in self.conn.execute("SELECT version_id, generation_id FROM versions")
        }
        self.assertEqual(gens, {1: 1, 2: 1, 3: 3})


class ConcurrentConfirmTests(BookingTestBase):
    def test_concurrent_confirms_never_exceed_capacity(self):
        # 容量 1 的单一路段，N 个并发确认各 1 人、同一时刻出发
        graph = {
            "nodes": ["A", "B"],
            "edges": [{"id": "E1", "from": "A", "to": "B", "travel_seconds": 10,
                       "has_stairs": False, "capacity": 1}],
        }
        self.publish(graph=graph)
        n = 8
        results = [None] * n
        errors = []

        def worker(i):
            # 每线程独立连接（与 HTTP 请求模型一致）
            c = connect(Path(self.db_path))
            try:
                init_db(c)
                record, _ = confirm_trip_atomic(
                    c, origin="A", destination="B", departure=0,
                    avoid_stairs=False, people=1,
                )
                results[i] = record
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                c.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])

        # 不变量：任意整数时刻 E1 上的占用人数 <= 1
        intervals = [
            (s.enter_ts, s.leave_ts)
            for rec in results
            for s in rec.segments
        ]
        self.assertEqual(len(intervals), n)
        events = sorted(
            [x for a, b in intervals for x in ((a, 1), (b, -1))],
            key=lambda e: (e[0], e[1]),
        )
        load = peak = 0
        for _, delta in events:
            load += delta
            peak = max(peak, load)
        self.assertEqual(peak, 1)
        # 且每人都拿到一个互不相同的可行出发槽 [10k, 10k+10)
        enters = sorted(a for a, _ in intervals)
        self.assertEqual(enters, [10 * i for i in range(n)])


class PlainRouteUnaffectedTests(BookingTestBase):
    def test_route_engine_without_occupancy_ignores_capacity(self):
        # E1 容量 1；无占用的普通查询引擎下，100 人团体也不受容量约束
        graph = {
            "nodes": ["A", "B"],
            "edges": [{"id": "E1", "from": "A", "to": "B", "travel_seconds": 10,
                       "has_stairs": False, "capacity": 1}],
        }
        self.publish(graph=graph)
        snap = get_snapshot(self.conn)
        engine = build_engine(snap)  # 不传 occupancy：普通路线查询模式
        r = engine.plan("A", "B", D, party_size=100)
        self.assertEqual([s.edge_id for s in r.segments], ["E1"])
        self.assertEqual(r.arrival_time, D + 10)


if __name__ == "__main__":
    unittest.main()
