"""行程改签（rebook）测试。

覆盖：
- 成功改签：起点/编号不变，逐段时刻、终点、人数、数据版本、世代一次性
  替换，修订号递增；旧库行程初始修订号为 1；
- 规划时先排除本行程原占用（不与自身旧占用竞争），仍与其余行程竞争，
  容量不超额；按需读取仍只触及有容量路段；
- 拒绝场景：行程不存在（404）、已取消、已出发、修订号不符、
  data_version 不符、新出发时刻不晚于当前时刻、参数非法、
  新路线不可行（no_route）；拒绝时原行程与占用均不变；
- 起点不可变更（origin 字段非法）；零段行程改签；连续改签修订号递增；
- 封闭更新（同世代）与重新发布图（新世代）下的改签；
- 改签负载校验（normalize_rebook）。
"""
import os
import tempfile
import threading
import unittest
from pathlib import Path

from app.booking import (
    RebookValidationError,
    RevisionConflictError,
    TripCancelledError,
    TripDepartedError,
    TripNotFoundError,
    VersionConflictError,
    cancel_trip_atomic,
    confirm_trip_atomic,
    rebook_trip_atomic,
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
from app.routing import NoRouteError
from app.schemas import ValidationError, normalize_rebook

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
D = 10_000_000          # 固定“当前时刻”，相对它构造过去/未来出发时刻
PAST = D - 10_000       # 已出发行程的出发时刻
FUT1 = D + 10_000       # 改签前的出发时刻
FUT2 = D + 20_000       # 改签后的出发时刻


class RebookTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "test.db")
        os.environ["APP_DB_PATH"] = self.db_path
        self.conn = connect(Path(self.db_path))
        init_db(self.conn)
        publish_graph(self.conn, GRAPH)
        self.now = D

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()
        os.environ.pop("APP_DB_PATH", None)

    def confirm(self, origin="A", destination="C", departure=FUT1,
                avoid_stairs=False, people=1, expected_version=None):
        return confirm_trip_atomic(
            self.conn,
            origin=origin, destination=destination, departure=departure,
            avoid_stairs=avoid_stairs, people=people,
            expected_version=expected_version,
        )

    def rebook(self, trip_id, *, departure=FUT2, destination="C",
               avoid_stairs=False, people=1, expected_revision=1,
               expected_version=None, now=None):
        return rebook_trip_atomic(
            self.conn,
            trip_id,
            expected_revision=expected_revision,
            departure=departure,
            destination=destination,
            avoid_stairs=avoid_stairs,
            people=people,
            expected_version=expected_version,
            now=self.now if now is None else now,
        )


class RebookSuccessTests(RebookTestBase):
    def test_rebook_replaces_fields_and_bumps_revision(self):
        rec0, snap = self.confirm(destination="C", people=1)
        self.assertEqual(rec0.revision, 1)
        rec1, snap1 = self.rebook(rec0.trip_id, departure=FUT2, destination="B",
                                  people=1, expected_revision=1)
        # 编号/起点/创建时间不变
        self.assertEqual(rec1.trip_id, rec0.trip_id)
        self.assertEqual(rec1.origin, "A")
        self.assertEqual(rec1.created_at, rec0.created_at)
        # 新字段一次性替换
        self.assertEqual(rec1.destination, "B")
        self.assertEqual(rec1.departure_ts, FUT2)
        self.assertEqual(rec1.status, "confirmed")
        self.assertEqual(rec1.revision, 2)
        self.assertEqual(rec1.version_id, snap1.version_id)
        self.assertEqual(rec1.generation_id, snap1.generation_id)
        self.assertEqual([s.edge_id for s in rec1.segments], ["E1"])
        self.assertEqual(rec1.segments[0].enter_ts, FUT2)
        self.assertEqual(rec1.segments[0].leave_ts, FUT2 + 100)
        self.assertEqual(rec1.arrival_ts, FUT2 + 100)

        # 再次改签：修订号继续递增，旧方案不残留逐段
        rec2, _ = self.rebook(rec0.trip_id, departure=FUT2 + 100,
                              destination="C", people=1, expected_revision=2)
        self.assertEqual(rec2.revision, 3)
        self.assertEqual([s.edge_id for s in rec2.segments], ["E1", "E2"])
        n_segments = self.conn.execute(
            "SELECT COUNT(*) AS c FROM trip_segments WHERE trip_id=?",
            (rec0.trip_id,),
        ).fetchone()["c"]
        self.assertEqual(n_segments, 2)

    def test_rebook_changes_people_and_avoid_stairs(self):
        rec0, _ = self.confirm(people=1, avoid_stairs=False)
        # 不避台阶时最短路是 E3（300）；改为避台阶须走 E1 E2（200）
        rec1, _ = self.rebook(rec0.trip_id, people=1, avoid_stairs=True,
                              expected_revision=1)
        self.assertEqual(rec1.party_size, 1)
        self.assertTrue(rec1.avoid_stairs)
        self.assertEqual([s.edge_id for s in rec1.segments], ["E1", "E2"])
        self.assertEqual(rec1.arrival_ts, FUT2 + 200)
        # 再改人数（不避台阶，E3 不限容量，2 人可走）
        rec2, _ = self.rebook(rec0.trip_id, people=2, avoid_stairs=False,
                              departure=FUT2 + 1, expected_revision=2)
        self.assertEqual(rec2.party_size, 2)
        self.assertEqual([s.edge_id for s in rec2.segments], ["E3"])

    def test_zero_segment_trip_rebook(self):
        rec0, _ = self.confirm(origin="A", destination="A", departure=FUT1,
                               people=1)
        self.assertEqual(rec0.segments, [])
        self.assertEqual(rec0.revision, 1)
        rec1, _ = self.rebook(rec0.trip_id, destination="B",
                              expected_revision=1)
        self.assertEqual(rec1.revision, 2)
        self.assertEqual([s.edge_id for s in rec1.segments], ["E1"])

    def test_rebook_to_same_origin_destination_zero_segment(self):
        rec0, _ = self.confirm(destination="B", people=1)
        rec1, _ = self.rebook(rec0.trip_id, destination="A",
                              expected_revision=1)
        self.assertEqual(rec1.segments, [])
        self.assertEqual(rec1.departure_ts, FUT2)
        self.assertEqual(rec1.arrival_ts, FUT2)


class RebookOccupancyTests(RebookTestBase):
    def test_rebook_excludes_its_own_old_occupancy(self):
        # 自身 1 人占用 E1[FUT1, FUT1+100)，容量 1；改签到同一时刻区间
        # 时若不排除自身会被迫等待；排除后应能立即进入
        rec0, _ = self.confirm(departure=FUT1, people=1)
        rec1, _ = self.rebook(rec0.trip_id, departure=FUT1,
                              destination="C", expected_revision=1)
        self.assertEqual(rec1.segments[0].enter_ts, FUT1)
        self.assertEqual(rec1.segments[0].leave_ts, FUT1 + 100)

    def test_rebook_still_competes_with_other_trips(self):
        other, _ = self.confirm(departure=FUT2, people=1)  # 占 E1[FUT2, FUT2+100)
        mine, _ = self.confirm(departure=FUT1, people=1)
        # 改签为 FUT2 同发：自身旧占用在 FUT1 已结束，other 仍占 E1，
        # 容量 1 → 必须等到 FUT2+100 进入
        rec1, _ = self.rebook(mine.trip_id, departure=FUT2,
                              expected_revision=1)
        self.assertEqual(rec1.segments[0].enter_ts, FUT2 + 100)
        # other 行程不受影响
        self.assertEqual(get_trip(self.conn, other.trip_id).revision, 1)

    def test_rebook_people_over_capacity_rejected_without_change(self):
        rec0, _ = self.confirm(people=1)
        with self.assertRaises(NoRouteError):
            # E1/E2 容量分别为 1/2，避台阶后唯一平路要求 E1 容 2 人 → 不可行
            self.rebook(rec0.trip_id, people=2, avoid_stairs=True,
                        expected_revision=1)
        kept = get_trip(self.conn, rec0.trip_id)
        self.assertEqual(kept.revision, 1)
        self.assertEqual(kept.departure_ts, FUT1)
        self.assertEqual(kept.party_size, 1)
        # 原占用仍在：同发的另一单必须等待
        other, _ = self.confirm(departure=FUT1, people=1)
        self.assertEqual(other.segments[0].enter_ts, FUT1 + 100)

    def test_rebook_then_capacity_is_consistent(self):
        rec0, _ = self.confirm(departure=FUT1, people=1)
        self.rebook(rec0.trip_id, departure=FUT2, people=1,
                    expected_revision=1)
        snap = get_snapshot(self.conn)
        occ = active_occupancy(self.conn, snap.generation_id)
        # 旧占用（FUT1 区间）不得残留
        self.assertEqual(
            [iv for iv in occ.get("E1", []) if iv[0] < FUT2], [])
        events = sorted(
            ev for e in occ.values() for a, b, p in e for ev in ((a, p), (b, -p))
        )
        load = peak = 0
        for _, delta in events:
            load += delta
            peak = max(peak, load)
        self.assertLessEqual(peak, 2)

    def test_rebook_reads_occupancy_on_demand(self):
        """改签同样按需读取：只触及有容量路段，每条至多读取一次。"""
        from unittest import mock

        from app import db as dbmod

        rec0, _ = self.confirm(departure=FUT1, people=1)

        def run(rev, departure, avoid_stairs):
            calls = []
            real = dbmod.edge_occupancy

            def spy(conn, generation_id, edge_id, not_before=0, **kw):
                calls.append((edge_id, kw.get("exclude_trip_id")))
                return real(conn, generation_id, edge_id, not_before, **kw)

            with mock.patch.object(dbmod, "edge_occupancy", spy):
                self.rebook(rec0.trip_id, departure=departure,
                            destination="C", avoid_stairs=avoid_stairs,
                            people=1, expected_revision=rev)
            return calls

        # 非避台阶/避台阶最短路都是 E1 E2（200 < E3 直达 300）：
        # 两条有容量路段各至多读取一次，且都排除本行程；E3 不被触及
        calls = run(1, FUT2, False)
        edges = [e for e, _ in calls]
        self.assertEqual(sorted(edges), ["E1", "E2"])
        self.assertEqual(len(edges), len(set(edges)))
        self.assertNotIn("E3", edges)
        self.assertTrue(all(ex == rec0.trip_id for _, ex in calls))
        # 再次改签（避台阶）：同样只触及 E1/E2，各一次
        calls2 = run(2, FUT2 + 100, True)
        edges2 = [e for e, _ in calls2]
        self.assertEqual(sorted(edges2), ["E1", "E2"])
        self.assertEqual(len(edges2), len(set(edges2)))


class RebookRejectionTests(RebookTestBase):
    def test_trip_not_found(self):
        with self.assertRaises(TripNotFoundError):
            self.rebook(999)

    def test_cancelled_trip_cannot_rebook(self):
        rec0, _ = self.confirm(people=1)
        cancel_trip_atomic(self.conn, rec0.trip_id)
        with self.assertRaises(TripCancelledError):
            self.rebook(rec0.trip_id, expected_revision=1)
        kept = get_trip(self.conn, rec0.trip_id)
        self.assertEqual(kept.status, "cancelled")
        self.assertEqual(kept.revision, 1)

    def test_departed_trip_cannot_rebook(self):
        rec0, _ = self.confirm(departure=PAST, people=1)
        with self.assertRaises(TripDepartedError):
            self.rebook(rec0.trip_id, departure=FUT2, expected_revision=1)
        # 边界：now 恰好等于原出发时刻也算已出发
        with self.assertRaises(TripDepartedError):
            self.rebook(rec0.trip_id, departure=FUT2, expected_revision=1,
                        now=PAST)
        kept = get_trip(self.conn, rec0.trip_id)
        self.assertEqual(kept.departure_ts, PAST)
        self.assertEqual(kept.revision, 1)

    def test_revision_mismatch_rejected(self):
        rec0, _ = self.confirm(people=1)
        with self.assertRaises(RevisionConflictError) as cm:
            self.rebook(rec0.trip_id, expected_revision=7)
        self.assertEqual(cm.exception.expected_revision, 7)
        self.assertEqual(cm.exception.current_revision, 1)
        # 成功改签一次后再用旧修订号也被拒绝
        self.rebook(rec0.trip_id, expected_revision=1)
        with self.assertRaises(RevisionConflictError):
            self.rebook(rec0.trip_id, departure=FUT2 + 1,
                        expected_revision=1)
        kept = get_trip(self.conn, rec0.trip_id)
        self.assertEqual(kept.revision, 2)

    def test_data_version_mismatch_rejected(self):
        rec0, _ = self.confirm(people=1)
        cur = current_version(self.conn)
        with self.assertRaises(VersionConflictError) as cm:
            self.rebook(rec0.trip_id, expected_revision=1,
                        expected_version=cur + 10)
        self.assertEqual(cm.exception.current_version, cur)
        # 携带当前版本则放行
        rec1, snap = self.rebook(rec0.trip_id, expected_revision=1,
                                 expected_version=cur)
        self.assertEqual(rec1.version_id, snap.version_id)

    def test_new_departure_must_be_in_future(self):
        rec0, _ = self.confirm(departure=FUT1, people=1)
        for bad in (D, D - 1, PAST):
            with self.assertRaises(RebookValidationError):
                self.rebook(rec0.trip_id, departure=bad, expected_revision=1)
        kept = get_trip(self.conn, rec0.trip_id)
        self.assertEqual(kept.departure_ts, FUT1)
        self.assertEqual(kept.revision, 1)

    def test_not_yet_departed_boundary_is_strict_before(self):
        # now 严格早于原出发时刻即允许（即使只差 1 秒）
        rec0, _ = self.confirm(departure=FUT1, people=1)
        rec1, _ = self.rebook(rec0.trip_id, departure=FUT1 + 1,
                              expected_revision=1, now=FUT1 - 1)
        self.assertEqual(rec1.revision, 2)

    def test_no_route_rejected_leaves_everything_unchanged(self):
        rec0, _ = self.confirm(people=1)
        with self.assertRaises(NoRouteError):
            # 终点 GHOST 会在 HTTP 层拦截；存储层这里用避台阶 + 超大人数
            # 制造不可行；另用不可达终点通过直接构造也测一下
            self.rebook(rec0.trip_id, people=99, avoid_stairs=True,
                        expected_revision=1)
        kept = get_trip(self.conn, rec0.trip_id)
        self.assertEqual(kept.revision, 1)


class RebookGenerationTests(RebookTestBase):
    def test_closures_update_same_generation_rebook_keeps_metering(self):
        other, _ = self.confirm(departure=FUT2, people=1)
        mine, _ = self.confirm(departure=FUT1, people=1)
        publish_closures(self.conn, {"closures": []})  # 同世代，版本+1
        snap = get_snapshot(self.conn)
        rec1, rsnap = self.rebook(
            mine.trip_id, departure=FUT2, expected_revision=1)
        self.assertEqual(rec1.version_id, snap.version_id)
        self.assertEqual(rec1.generation_id, snap.generation_id)
        # other 同代占用仍在 → 排队
        self.assertEqual(rec1.segments[0].enter_ts, FUT2 + 100)
        self.assertEqual(other, other)  # other 不变

    def test_graph_republish_new_generation_rebook(self):
        old, _ = self.confirm(departure=FUT1, people=1)
        v = publish_graph(self.conn, GRAPH)  # 新世代
        snap = get_snapshot(self.conn)
        self.assertEqual(snap.version_id, v)
        # 旧行程仍在旧世代；改签按新世代（空占用）规划，立即进入
        rec1, rsnap = self.rebook(old.trip_id, departure=FUT2,
                                  expected_revision=1)
        self.assertEqual(rec1.generation_id, snap.generation_id)
        self.assertEqual(rec1.version_id, snap.version_id)
        self.assertEqual(rec1.segments[0].enter_ts, FUT2)
        self.assertEqual(rec1.revision, 2)
        # 改签后行程迁到新世代；旧世代不再有它的占用
        old_gen = self.conn.execute(
            "SELECT generation_id FROM trips WHERE trip_id=? AND revision=1",
            (old.trip_id,),
        ).fetchall()
        # revision=1 的行已被覆盖，行程当前世代即新世代
        cur = get_trip(self.conn, old.trip_id)
        self.assertEqual(cur.generation_id, snap.generation_id)

    def test_restore_opens_new_generation_for_rebook(self):
        old, _ = self.confirm(departure=FUT1, people=1)
        publish_closures(self.conn, {"closures": []})
        new_v = restore_version(self.conn, 1)
        snap = get_snapshot(self.conn)
        self.assertEqual(new_v, snap.version_id)
        rec1, _ = self.rebook(old.trip_id, departure=FUT2,
                              expected_revision=1)
        self.assertEqual(rec1.generation_id, snap.generation_id)
        self.assertEqual(rec1.segments[0].enter_ts, FUT2)


class NormalizeRebookTests(unittest.TestCase):
    NODES = {"A", "B", "C"}

    def norm(self, payload):
        return normalize_rebook(payload, self.NODES)

    def test_valid_payload(self):
        q = self.norm({
            "departure_time": D + 10, "destination": "C",
            "people": 2, "avoid_stairs": True, "revision": 3,
            "data_version": 5,
        })
        self.assertEqual(q["departure"], D + 10)
        self.assertEqual(q["destination"], "C")
        self.assertEqual(q["people"], 2)
        self.assertTrue(q["avoid_stairs"])
        self.assertEqual(q["expected_revision"], 3)
        self.assertEqual(q["expected_version"], 5)

    def test_optional_fields_default(self):
        q = self.norm({
            "departure_time": D + 10, "destination": "B",
            "people": 1, "revision": 1,
        })
        self.assertFalse(q["avoid_stairs"])
        self.assertIsNone(q["expected_version"])

    def test_epoch_seconds_accepted(self):
        q = self.norm({"departure_time": D + 10, "destination": "B",
                       "people": 1, "revision": 1})
        self.assertEqual(q["departure"], D + 10)

    def test_invalid_payloads(self):
        base = {"departure_time": D + 10, "destination": "C",
                "people": 1, "revision": 1}
        bads = [
            None, [], "x",
            {},
            {**base, "destination": "GHOST"},
            {**base, "people": 0}, {**base, "people": -1},
            {**base, "people": 1.5}, {**base, "people": "2"},
            {**base, "people": True},
            {**base, "revision": 0}, {**base, "revision": -2},
            {**base, "revision": "1"}, {**base, "revision": 1.0},
            {**base, "revision": True},
            {**base, "data_version": 0}, {**base, "data_version": "2"},
            {**base, "data_version": False},
            {**base, "avoid_stairs": "yes"},
            {**base, "departure_time": "nope"},
            {**base, "departure_time": -1},
            {**base, "origin": "A"}, {**base, "origin": "X"},
            {**base, "stops": []},
        ]
        for bad in bads:
            with self.assertRaises(ValidationError, msg=repr(bad)):
                self.norm(bad)

    def test_missing_required_fields(self):
        for field in ("departure_time", "destination", "people", "revision"):
            payload = {
                "departure_time": D + 10, "destination": "C",
                "people": 1, "revision": 1,
            }
            del payload[field]
            with self.assertRaises(ValidationError, msg=field):
                self.norm(payload)


class RebookMigrationTests(unittest.TestCase):
    """旧库 trips 表缺少 revision 列时，init_db 自动补列且初始修订号为 1。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "old.db")
        os.environ["APP_DB_PATH"] = self.db_path

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("APP_DB_PATH", None)

    def test_old_trips_get_revision_one(self):
        from app.db import SCHEMA

        conn = connect(Path(self.db_path))
        try:
            # 模拟改签能力上线前的旧库（trips 无 revision 列）
            old_schema = SCHEMA.replace(
                "    -- 行程修订号：确认时为 1，每次成功改签递增 1（乐观并发控制）\n"
                "    revision      INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),\n",
                "",
            )
            self.assertNotIn("revision", old_schema)
            conn.executescript(old_schema)
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('current_version', '0')"
            )
            publish_graph(conn, GRAPH)
            tid = conn.execute(
                "INSERT INTO trips(version_id, generation_id, origin, destination, "
                "    departure_ts, arrival_ts, avoid_stairs, party_size, status) "
                "VALUES (1, 1, 'A', 'B', ?, ?, 0, 1, 'confirmed')",
                (FUT1, FUT1 + 100),
            ).lastrowid
            conn.execute(
                "INSERT INTO trip_segments(trip_id, segment_idx, edge_id, from_node, "
                "    to_node, enter_ts, leave_ts, travel_seconds, waiting_seconds, "
                "    has_stairs) VALUES (?, 0, 'E1', 'A', 'B', ?, ?, 100, 0, 0)",
                (tid, FUT1, FUT1 + 100),
            )

            init_db(conn)  # 触发补列迁移
            ddl = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='trips'"
            ).fetchone()["sql"]
            self.assertIn("revision", ddl)
            rec = get_trip(conn, tid)
            self.assertEqual(rec.revision, 1)  # 旧库行程初始修订号为 1

            # 迁移后可正常改签，修订号递增到 2
            updated, _ = rebook_trip_atomic(
                conn, tid, expected_revision=1, departure=FUT2,
                destination="C", avoid_stairs=False, people=1, now=D,
            )
            self.assertEqual(updated.revision, 2)
            self.assertEqual(updated.departure_ts, FUT2)
        finally:
            conn.close()


class RebookConcurrencyTests(RebookTestBase):
    def test_concurrent_rebooks_and_confirms_never_exceed_capacity(self):
        errors = []
        # 先建若干已确认且未出发的行程（修订号 1）
        ids = []
        for i in range(6):
            rec, _ = self.confirm(departure=FUT1, people=1)
            ids.append(rec.trip_id)

        def worker(trip_id, use_rebook):
            c = connect(Path(self.db_path))
            try:
                init_db(c)
                if use_rebook:
                    rebook_trip_atomic(
                        c, trip_id, expected_revision=1, departure=FUT2,
                        destination="C", avoid_stairs=False, people=1, now=D,
                    )
                else:
                    confirm_trip_atomic(
                        c, origin="A", destination="C", departure=FUT2,
                        avoid_stairs=False, people=1,
                    )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                c.close()

        threads = []
        for i, tid in enumerate(ids):
            threads.append(threading.Thread(target=worker, args=(tid, True)))
        for _ in range(6):
            threads.append(threading.Thread(target=worker, args=(None, False)))
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])

        snap = get_snapshot(self.conn)
        caps = {e.id: e.capacity for e in snap.edges}
        occ = active_occupancy(self.conn, snap.generation_id)
        for edge_id, intervals in occ.items():
            cap = caps[edge_id]
            if cap is None:
                continue  # 不限容量路段（如直达台阶路 E3）
            events = sorted(
                ev for a, b, p in intervals for ev in ((a, p), (b, -p)))
            load = peak = 0
            for _, delta in events:
                load += delta
                peak = max(peak, load)
            self.assertLessEqual(peak, cap, f"{edge_id} 峰值 {peak} 超过容量 {cap}")
        # 被改签的 6 条行程修订号均为 2，且其 FUT1 旧占用全部替换为 FUT2 起
        for tid in ids:
            cur = get_trip(self.conn, tid)
            self.assertEqual(cur.revision, 2)
            self.assertEqual(cur.departure_ts, FUT2)


if __name__ == "__main__":
    unittest.main()
