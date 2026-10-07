"""行程改签（reschedule）测试。

覆盖：
- 成功改签：起点与编号不变，终点/出发时刻/人数/避台阶可改，修订号递增，
  逐段时刻、人数、data_version、generation_id 一次性替换；
- 先排除本行程原占用再规划（自身不阻挡新路线），其他行程占用照常计量；
- 封闭更新（同世代）与重新发布/恢复（新世代）后改签的版本/世代更新；
- 旧库行程初始修订号为 1（迁移补列）；
- 拒绝：行程不存在、已取消、已出发、修订号不符、data_version 不符、
  新出发时刻不晚于当前时刻、参数非法、新路线不可行——原行程及占用不变；
- 并发改签/取消串行：修订号冲突，容量不超额；
- 查询与取消字段/行为兼容（新增 revision_no 为增量字段）。
"""
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from app.booking import (
    DepartureInPastError,
    TripNotFoundError,
    TripNotModifiableError,
    VersionConflictError,
    confirm_trip_atomic,
    reschedule_trip_atomic,
)
from app.db import (
    RevisionConflictError,
    active_occupancy,
    connect,
    get_snapshot,
    get_trip,
    init_db,
    publish_closures,
    publish_graph,
    restore_version,
)
from app.routing import NoRouteError
from app.schemas import ValidationError, normalize_reschedule

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
NOW = 1_000_000          # 固定“当前时刻”，所有出发时刻均在未来


class RescheduleTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "test.db")
        os.environ["APP_DB_PATH"] = self.db_path
        self.conn = connect(Path(self.db_path))
        init_db(self.conn)
        publish_graph(self.conn, GRAPH_CAP)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()
        os.environ.pop("APP_DB_PATH", None)

    def confirm(self, origin="A", destination="C", departure=NOW + 1000,
                avoid_stairs=False, people=1, expected_version=None):
        return confirm_trip_atomic(
            self.conn,
            origin=origin, destination=destination, departure=departure,
            avoid_stairs=avoid_stairs, people=people,
            expected_version=expected_version,
        )

    def reschedule(self, trip_id, *, revision=1, destination="C",
                   departure=NOW + 2000, avoid_stairs=False, people=1,
                   expected_version=None, now=NOW):
        return reschedule_trip_atomic(
            self.conn,
            trip_id,
            expected_revision=revision,
            destination=destination,
            departure=departure,
            avoid_stairs=avoid_stairs,
            people=people,
            expected_version=expected_version,
            now=now,
        )


class RescheduleSuccessTests(RescheduleTestBase):
    def test_basic_reschedule_replaces_plan_and_bumps_revision(self):
        rec, _ = self.confirm(departure=NOW + 1000, people=1)
        self.assertEqual(rec.revision_no, 1)

        # 改为 2 人：E1/E2 容量受限（E1=1 对 2 人恒不可用），
        # 不避台阶时改走不限容量的台阶直边 E3（300 秒）
        new_rec, snap = self.reschedule(
            rec.trip_id, revision=1, destination="C",
            departure=NOW + 3000, people=2,
        )
        # 编号与起点不变
        self.assertEqual(new_rec.trip_id, rec.trip_id)
        self.assertEqual(new_rec.origin, "A")
        # 新终点、新人数、新逐段时刻一次性替换
        self.assertEqual(new_rec.destination, "C")
        self.assertEqual(new_rec.party_size, 2)
        self.assertEqual([s.edge_id for s in new_rec.segments], ["E3"])
        self.assertEqual(new_rec.segments[0].enter_ts, NOW + 3000)
        self.assertEqual(new_rec.segments[0].leave_ts, NOW + 3300)
        self.assertEqual(new_rec.departure_ts, NOW + 3000)
        self.assertEqual(new_rec.arrival_ts, NOW + 3300)
        # 版本/世代取当前快照，修订号递增
        self.assertEqual(new_rec.version_id, snap.version_id)
        self.assertEqual(new_rec.generation_id, snap.generation_id)
        self.assertEqual(new_rec.revision_no, 2)
        self.assertEqual(new_rec.status, "confirmed")
        # 旧的两段逐段行已被整体替换为新的一段
        self.assertEqual(len(new_rec.segments), 1)

    def test_repeated_reschedules_increment_revision(self):
        rec, _ = self.confirm(departure=NOW + 1000)
        for i in range(3):
            rec, _ = self.reschedule(rec.trip_id, revision=i + 1,
                                     departure=NOW + 2000 + i)
            self.assertEqual(rec.revision_no, i + 2)
        self.assertEqual(rec.revision_no, 4)
        self.assertEqual(rec.departure_ts, NOW + 2002)

    def test_avoid_stairs_change_is_honored(self):
        rec, _ = self.confirm(avoid_stairs=False, departure=NOW + 1000)
        # 改签为避台阶：E3 含台阶不可走，只能 E1+E2
        new_rec, _ = self.reschedule(rec.trip_id, revision=1,
                                     avoid_stairs=True, people=1)
        self.assertEqual([s.edge_id for s in new_rec.segments], ["E1", "E2"])

    def test_zero_segment_trip_reschedule(self):
        rec, _ = self.confirm(origin="A", destination="A", departure=NOW + 1000)
        self.assertEqual(rec.segments, [])
        new_rec, _ = self.reschedule(rec.trip_id, revision=1, destination="B")
        self.assertEqual([s.edge_id for s in new_rec.segments], ["E1"])
        self.assertEqual(new_rec.revision_no, 2)

    def test_reschedule_to_same_origin_destination_keeps_zero_segments(self):
        rec, _ = self.confirm(departure=NOW + 1000)
        new_rec, _ = self.reschedule(rec.trip_id, revision=1,
                                     destination="A", departure=NOW + 5000)
        self.assertEqual(new_rec.segments, [])
        self.assertEqual(new_rec.arrival_ts, NOW + 5000)


class RescheduleOccupancyTests(RescheduleTestBase):
    def test_own_old_occupancy_does_not_block_new_route(self):
        # 容量 1：本行程原占用 E1[NOW+1000, NOW+1100)，改签回同一时段
        # 也应可行——先排除自身原占用
        rec, snap = self.confirm(departure=NOW + 1000, people=1)
        new_rec, _ = self.reschedule(rec.trip_id, revision=1,
                                     departure=NOW + 1000)
        self.assertEqual(new_rec.segments[0].enter_ts, NOW + 1000)
        # 旧占用行已删除：该世代在该时段只剩改签后的 1 份占用
        occ = active_occupancy(self.conn, snap.generation_id)
        self.assertEqual(occ["E1"], [(NOW + 1000, NOW + 1100, 1)])

    def test_other_trips_still_metered(self):
        # 另一行程占 E1[NOW+3000, NOW+3100)；t1 改签到该时刻须等待
        other, _ = self.confirm(departure=NOW + 3000, people=1)
        rec, _ = self.confirm(departure=NOW + 1000, people=1)
        new_rec, _ = self.reschedule(rec.trip_id, revision=1,
                                     departure=NOW + 3000)
        self.assertEqual(new_rec.segments[0].enter_ts, NOW + 3100)
        self.assertEqual(new_rec.segments[0].waiting_seconds, 100)
        # 两行程都在，互不覆盖
        self.assertEqual(get_trip(self.conn, other.trip_id).revision_no, 1)

    def test_party_size_change_metered_against_others(self):
        # 专用图：E1(A->B)/E2(B->C) 容量均为 2，2 人团体可走
        graph = {
            "nodes": ["A", "B", "C"],
            "edges": [
                {"id": "E1", "from": "A", "to": "B", "travel_seconds": 100,
                 "has_stairs": False, "capacity": 2},
                {"id": "E2", "from": "B", "to": "C", "travel_seconds": 100,
                 "has_stairs": False, "capacity": 2},
            ],
        }
        publish_graph(self.conn, graph)  # 新世代，从空占用开始
        rec, _ = self.confirm(departure=NOW + 1000, people=1)
        # 第一次改签仍为 1 人：E1[4000,4100)、E2[4100,4200)
        new_rec, _ = self.reschedule(rec.trip_id, revision=1,
                                     destination="C", departure=NOW + 4000,
                                     people=1)
        self.assertEqual(new_rec.segments[0].enter_ts, NOW + 4000)
        # 另一 1 人从 B 于 4100 进入 E2：1+1=2 恰好不超额，占用 [4100,4200)
        other, _ = self.confirm(origin="B", destination="C",
                                departure=NOW + 4100, people=1)
        self.assertEqual(other.segments[0].enter_ts, NOW + 4100)
        # 第二次改签改为 2 人（排除自身 1 人后，他人仍占 1 人）：
        # E1 无他人可 4000 进入；E2 上 2 人 + 1 人 > 2 → 在 B 等到 4200
        new_rec2, _ = self.reschedule(rec.trip_id, revision=2,
                                      departure=NOW + 4000, people=2)
        segs = [(s.edge_id, s.enter_ts) for s in new_rec2.segments]
        self.assertEqual(segs, [("E1", NOW + 4000), ("E2", NOW + 4200)])
        self.assertEqual(new_rec2.party_size, 2)
        # 另一行程不受影响
        self.assertEqual(get_trip(self.conn, other.trip_id).revision_no, 1)

    def test_no_route_leaves_old_trip_and_occupancy_untouched(self):
        rec, snap = self.confirm(departure=NOW + 1000, people=1)
        before = get_trip(self.conn, rec.trip_id)
        # 2 人 > E1 容量 1，避台阶后唯一路径不可用 → 404 语义，回滚
        with self.assertRaises(NoRouteError):
            self.reschedule(rec.trip_id, revision=1,
                            avoid_stairs=True, people=2,
                            departure=NOW + 5000)
        after = get_trip(self.conn, rec.trip_id)
        self.assertEqual(after, before)  # 行与逐段占用完全不变
        self.assertEqual(after.revision_no, 1)
        occ = active_occupancy(self.conn, snap.generation_id)
        self.assertEqual(occ["E1"], [(NOW + 1000, NOW + 1100, 1)])


class RescheduleVersionGenerationTests(RescheduleTestBase):
    def test_closures_update_same_generation_then_reschedule(self):
        rec, _ = self.confirm(departure=NOW + 1000)
        gen_before = get_snapshot(self.conn).generation_id
        v2 = publish_closures(self.conn, {"closures": []})
        new_rec, snap = self.reschedule(rec.trip_id, revision=1,
                                        departure=NOW + 2000)
        self.assertEqual(new_rec.version_id, v2)
        self.assertEqual(new_rec.generation_id, gen_before)  # 封闭更新不换世代
        self.assertEqual(snap.generation_id, gen_before)

    def test_graph_republish_new_generation_then_reschedule(self):
        rec, old_snap = self.confirm(departure=NOW + 1000)
        old_gen = old_snap.generation_id
        new_v = publish_graph(self.conn, GRAPH_CAP)  # 新世代
        new_rec, snap = self.reschedule(rec.trip_id, revision=1,
                                        departure=NOW + 2000)
        self.assertEqual(new_rec.version_id, new_v)
        self.assertEqual(new_rec.generation_id, new_v)
        self.assertNotEqual(new_rec.generation_id, old_gen)
        # 改签后行程不再计入旧世代
        self.assertEqual(active_occupancy(self.conn, old_gen), {})

    def test_restore_then_reschedule_uses_new_generation(self):
        v1 = get_snapshot(self.conn).version_id
        rec, _ = self.confirm(departure=NOW + 1000)
        publish_graph(self.conn, GRAPH_CAP)
        restored_v = restore_version(self.conn, v1)
        new_rec, snap = self.reschedule(rec.trip_id, revision=1,
                                        departure=NOW + 2000)
        self.assertEqual(new_rec.version_id, restored_v)
        self.assertEqual(new_rec.generation_id, restored_v)
        self.assertEqual(snap.generation_id, restored_v)

    def test_expected_version_must_match(self):
        rec, _ = self.confirm(departure=NOW + 1000)
        publish_closures(self.conn, {"closures": []})  # 版本前进
        before = get_trip(self.conn, rec.trip_id)
        with self.assertRaises(VersionConflictError) as ctx:
            self.reschedule(rec.trip_id, revision=1, expected_version=1)
        self.assertEqual(ctx.exception.expected_version, 1)
        self.assertEqual(ctx.exception.current_version, 2)
        # 版本冲突不写入
        self.assertEqual(get_trip(self.conn, rec.trip_id), before)

    def test_matching_expected_version_reschedules(self):
        rec, _ = self.confirm(departure=NOW + 1000)
        v = get_snapshot(self.conn).version_id
        new_rec, _ = self.reschedule(rec.trip_id, revision=1,
                                     expected_version=v, departure=NOW + 2000)
        self.assertEqual(new_rec.version_id, v)
        self.assertEqual(new_rec.revision_no, 2)


class RescheduleRejectionTests(RescheduleTestBase):
    def test_unknown_trip(self):
        with self.assertRaises(TripNotFoundError):
            self.reschedule(99999, departure=NOW + 2000)

    def test_cancelled_trip_rejected(self):
        from app.booking import cancel_trip_atomic
        rec, _ = self.confirm(departure=NOW + 1000)
        cancel_trip_atomic(self.conn, rec.trip_id)
        with self.assertRaises(TripNotModifiableError) as ctx:
            self.reschedule(rec.trip_id, revision=1, departure=NOW + 2000)
        self.assertEqual(ctx.exception.reason, "行程已取消")
        self.assertEqual(get_trip(self.conn, rec.trip_id).status, "cancelled")

    def test_departed_trip_rejected_half_open_boundary(self):
        rec, _ = self.confirm(departure=NOW + 1000)
        # 当前时刻恰为出发时刻：已出发（半开）
        with self.assertRaises(TripNotModifiableError):
            self.reschedule(rec.trip_id, revision=1,
                            departure=NOW + 2000, now=NOW + 1000)
        # 出发前一秒尚可改签
        new_rec, _ = self.reschedule(rec.trip_id, revision=1,
                                     departure=NOW + 2000, now=NOW + 999)
        self.assertEqual(new_rec.revision_no, 2)

    def test_revision_mismatch_rejected_and_untouched(self):
        rec, _ = self.confirm(departure=NOW + 1000)
        before = get_trip(self.conn, rec.trip_id)
        for bad_rev in (2, 3):
            with self.assertRaises(RevisionConflictError):
                self.reschedule(rec.trip_id, revision=bad_rev,
                                departure=NOW + 2000)
        self.assertEqual(get_trip(self.conn, rec.trip_id), before)

    def test_new_departure_must_be_strictly_after_now(self):
        rec, _ = self.confirm(departure=NOW + 1000)
        for dep in (NOW, NOW - 1):
            with self.assertRaises(DepartureInPastError):
                self.reschedule(rec.trip_id, revision=1, departure=dep)
        # 恰好晚 1 秒允许
        new_rec, _ = self.reschedule(rec.trip_id, revision=1,
                                     departure=NOW + 1)
        self.assertEqual(new_rec.revision_no, 2)

    def test_origin_missing_from_current_graph_is_no_route(self):
        rec, _ = self.confirm(departure=NOW + 1000)
        # 重新发布一张不含起点 A 的图（新世代）
        publish_graph(self.conn, {
            "nodes": ["X", "Y"],
            "edges": [{"id": "Z1", "from": "X", "to": "Y",
                       "travel_seconds": 10, "has_stairs": False}],
        })
        with self.assertRaises(NoRouteError):
            self.reschedule(rec.trip_id, revision=1, departure=NOW + 2000)


class RescheduleSchemaTests(unittest.TestCase):
    NODES = {"A", "B", "C"}

    def norm(self, payload):
        return normalize_reschedule(payload, self.NODES)

    def base(self, **over):
        payload = {
            "revision_no": 1,
            "departure_time": NOW + 2000,
            "destination": "C",
            "people": 2,
            "avoid_stairs": False,
        }
        payload.update(over)
        return payload

    def test_valid(self):
        q = self.norm(self.base(data_version=3))
        self.assertEqual(q["revision"], 1)
        self.assertEqual(q["people"], 2)
        self.assertEqual(q["destination"], "C")
        self.assertEqual(q["expected_version"], 3)
        self.assertIsNone(q["trip_id"])

    def test_trip_id_optional_in_body(self):
        q = self.norm(self.base(trip_id=7))
        self.assertEqual(q["trip_id"], 7)

    def test_missing_required_fields(self):
        for field in ("revision_no", "departure_time", "destination", "people"):
            payload = self.base()
            del payload[field]
            with self.assertRaises(ValidationError):
                self.norm(payload)

    def test_origin_forbidden(self):
        with self.assertRaises(ValidationError):
            self.norm(self.base(origin="B"))

    def test_stops_forbidden(self):
        with self.assertRaises(ValidationError):
            self.norm(self.base(stops=[]))

    def test_bad_revision(self):
        for bad in (0, -1, 1.0, "1", True, None):
            with self.assertRaises(ValidationError):
                self.norm(self.base(revision_no=bad))

    def test_bad_people(self):
        for bad in (0, -2, 1.5, "2", True, None):
            with self.assertRaises(ValidationError):
                self.norm(self.base(people=bad))

    def test_unknown_destination_and_bad_time(self):
        with self.assertRaises(ValidationError):
            self.norm(self.base(destination="Z"))
        with self.assertRaises(ValidationError):
            self.norm(self.base(departure_time="nope"))

    def test_avoid_stairs_must_be_bool(self):
        with self.assertRaises(ValidationError):
            self.norm(self.base(avoid_stairs="false"))

    def test_bad_data_version(self):
        for bad in (0, -1, "1", 1.5, True):
            with self.assertRaises(ValidationError):
                self.norm(self.base(data_version=bad))


class RescheduleConcurrencyTests(RescheduleTestBase):
    def test_concurrent_reschedules_only_one_wins_per_revision(self):
        rec, _ = self.confirm(departure=NOW + 1000)
        n = 6
        results = []
        errors = []

        def worker(i):
            c = connect(Path(self.db_path))
            try:
                init_db(c)
                new_rec, _ = reschedule_trip_atomic(
                    c, rec.trip_id,
                    expected_revision=1,
                    destination="C",
                    departure=NOW + 3000 + i,
                    avoid_stairs=False,
                    people=1,
                    now=NOW,
                )
                results.append(new_rec)
            except RevisionConflictError as exc:
                errors.append(exc)
            finally:
                c.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 恰好一个改签成功，其余全部修订号冲突
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), n - 1)
        final = get_trip(self.conn, rec.trip_id)
        self.assertEqual(final.revision_no, 2)
        self.assertEqual(len(final.segments), 2)

    def test_concurrent_cancel_and_reschedule(self):
        rec, _ = self.confirm(departure=NOW + 1000)
        outcome = {}

        def do_reschedule():
            c = connect(Path(self.db_path))
            try:
                init_db(c)
                try:
                    reschedule_trip_atomic(
                        c, rec.trip_id, expected_revision=1,
                        destination="C", departure=NOW + 3000,
                        avoid_stairs=False, people=1, now=NOW,
                    )
                    outcome["reschedule"] = "ok"
                except (RevisionConflictError, TripNotModifiableError):
                    outcome["reschedule"] = "conflict"
            finally:
                c.close()

        barrier = threading.Barrier(2)

        def synced(fn):
            def w():
                barrier.wait()
                fn()
            return w

        t1 = threading.Thread(target=synced(do_reschedule))
        t1.start()
        barrier.wait()
        # 主线程与工作线程竞争取消
        from app.booking import cancel_trip_atomic
        cancel_trip_atomic(self.conn, rec.trip_id)
        t1.join()
        final = get_trip(self.conn, rec.trip_id)
        if outcome["reschedule"] == "ok":
            # 改签先提交：随后取消应把改签后的行程取消掉
            self.assertEqual(final.status, "cancelled")
        else:
            self.assertEqual(final.status, "cancelled")
            self.assertEqual(final.revision_no, 1)


class RescheduleMigrationTests(unittest.TestCase):
    """旧库 trips 无 revision_no 列时自动补列，初始修订号为 1。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["APP_DB_PATH"] = str(Path(self.tmp.name) / "old.db")
        self.conn = connect(Path(os.environ["APP_DB_PATH"]))
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()
        os.environ.pop("APP_DB_PATH", None)

    def test_old_trips_get_revision_one(self):
        publish_graph(self.conn, GRAPH_CAP)
        # 先确认一条行程（旧库时代创建）
        rec, _ = confirm_trip_atomic(
            self.conn, origin="A", destination="C",
            departure=int(time.time()) + 10_000,
            avoid_stairs=False, people=1,
        )
        # 模拟旧库：移除修订号列（SQLite 需重建表）
        self.conn.execute("BEGIN IMMEDIATE")
        self.conn.execute("ALTER TABLE trips RENAME TO trips_old")
        self.conn.execute(
            "CREATE TABLE trips ("
            "trip_id INTEGER PRIMARY KEY AUTOINCREMENT, version_id INTEGER NOT NULL, "
            "generation_id INTEGER NOT NULL, origin TEXT NOT NULL, destination TEXT NOT NULL, "
            "departure_ts INTEGER NOT NULL, arrival_ts INTEGER NOT NULL, "
            "avoid_stairs INTEGER NOT NULL, party_size INTEGER NOT NULL, "
            "status TEXT NOT NULL, created_at INTEGER NOT NULL, cancelled_at INTEGER)"
        )
        self.conn.execute(
            "INSERT INTO trips(trip_id, version_id, generation_id, origin, destination, "
            "departure_ts, arrival_ts, avoid_stairs, party_size, status, created_at) "
            "SELECT trip_id, version_id, generation_id, origin, destination, departure_ts, "
            "arrival_ts, avoid_stairs, party_size, status, created_at FROM trips_old"
        )
        self.conn.execute("DROP TABLE trips_old")
        self.conn.execute("COMMIT")

        init_db(self.conn)  # 触发补列迁移
        old = get_trip(self.conn, rec.trip_id)
        self.assertEqual(old.revision_no, 1)
        # 旧行程可以按修订 1 改签
        new_rec, _ = reschedule_trip_atomic(
            self.conn, rec.trip_id, expected_revision=1, destination="B",
            departure=int(time.time()) + 1000, avoid_stairs=False, people=1,
        )
        self.assertEqual(new_rec.revision_no, 2)


class CompatFieldsTests(RescheduleTestBase):
    def test_query_and_cancel_still_work_and_include_revision(self):
        rec, _ = self.confirm(departure=NOW + 1000)
        fetched = get_trip(self.conn, rec.trip_id)
        self.assertEqual(fetched.revision_no, 1)
        new_rec, _ = self.reschedule(rec.trip_id, revision=1,
                                     departure=NOW + 2000)
        self.assertEqual(new_rec.revision_no, 2)
        # 改签后的行程仍可查询、可取消，取消释放其（新）占用
        from app.booking import cancel_trip_atomic
        cancelled, released = cancel_trip_atomic(self.conn, rec.trip_id)
        self.assertTrue(released)
        self.assertEqual(cancelled.status, "cancelled")
        self.assertEqual(cancelled.revision_no, 2)
        gen = get_snapshot(self.conn).generation_id
        self.assertEqual(active_occupancy(self.conn, gen), {})


if __name__ == "__main__":
    unittest.main()
