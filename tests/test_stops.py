"""按顺序停靠查询测试：窗口语义、顺序约束、全程字典序、输入校验。"""
import unittest

from app.db import ClosureRow, EdgeRow, Snapshot
from app.routing import NoRouteError, RouteEngine, Stop
from app.schemas import ValidationError, normalize_query


def make_engine(nodes, edges, closures=()):
    return RouteEngine(
        Snapshot(
            version_id=1,
            nodes=list(nodes),
            edges=[EdgeRow(*e) for e in edges],
            closures=[ClosureRow(*c) for c in closures],
        )
    )


# (id, src, dst, seconds, has_stairs)
def E(eid, u, v, secs, stairs=False):
    return (eid, u, v, secs, stairs)


def S(node, earliest, latest, stay):
    return Stop(node, earliest, latest, stay)


class StopWindowTests(unittest.TestCase):
    def setUp(self):
        # S --E1(10s)--> A --E2(20s)--> T
        self.engine = make_engine(
            ["S", "A", "T"], [E("E1", "S", "A", 10), E("E2", "A", "T", 20)]
        )

    def plan(self, stops, departure=0):
        return self.engine.plan("S", "T", departure, stops=tuple(stops))

    def test_arrive_before_window_waits_for_open(self):
        r = self.plan([S("A", 100, 200, 30)])
        (visit,) = r.stops
        self.assertEqual(visit.node, "A")
        self.assertEqual(visit.arrival_time, 10)   # 抵达
        self.assertEqual(visit.start_time, 100)    # 等窗开放后开始
        self.assertEqual(visit.end_time, 130)      # 停留 30 秒
        # 结束停留后才走 E2
        self.assertEqual(r.segments[1].enter_time, 130)
        self.assertEqual(r.segments[1].waiting_seconds, 0)
        self.assertEqual(r.arrival_time, 150)

    def test_arrive_inside_window_starts_immediately(self):
        r = self.plan([S("A", 5, 200, 30)])
        (visit,) = r.stops
        self.assertEqual((visit.arrival_time, visit.start_time, visit.end_time), (10, 10, 40))
        self.assertEqual(r.arrival_time, 60)

    def test_arrive_exactly_at_latest_is_allowed(self):
        r = self.plan([S("A", 0, 10, 5)])
        self.assertEqual(r.stops[0].start_time, 10)
        self.assertEqual(r.arrival_time, 35)

    def test_arrive_after_latest_is_no_route(self):
        with self.assertRaises(NoRouteError):
            self.plan([S("A", 0, 9, 5)])

    def test_zero_stay_is_allowed(self):
        r = self.plan([S("A", 0, 1000, 0)])
        self.assertEqual(r.stops[0].end_time, 10)
        self.assertEqual(r.arrival_time, 30)

    def test_closure_wait_after_stay_ends(self):
        # E2 在 [40,100) 封闭：停留 10→30 结束后在 A 等到 100 进入 E2
        eng = make_engine(
            ["S", "A", "T"],
            [E("E1", "S", "A", 10), E("E2", "A", "T", 20)],
            [("E2", 40, 100)],
        )
        r = eng.plan("S", "T", 0, stops=(S("A", 0, 100, 20),))
        self.assertEqual(r.stops[0].end_time, 30)
        self.assertEqual(r.segments[1].enter_time, 100)
        self.assertEqual(r.segments[1].waiting_seconds, 70)
        self.assertEqual(r.arrival_time, 120)


class StopOrderTests(unittest.TestCase):
    def test_passing_future_stop_does_not_count(self):
        # S->B->A->B->T；第 1 站是 A，途经 B（第 2 站）不算完成
        edges = [
            E("E1", "S", "B", 10),
            E("E2", "B", "A", 10),
            E("E3", "A", "B", 10),
            E("E4", "B", "T", 10),
        ]
        eng = make_engine(["S", "A", "B", "T"], edges)
        r = eng.plan("S", "T", 0, stops=(S("A", 0, 1000, 5), S("B", 0, 1000, 5)))
        v1, v2 = r.stops
        self.assertEqual((v1.node, v1.arrival_time, v1.end_time), ("A", 20, 25))
        # 10 时刻曾途经 B，但第 2 站必须在完成 A 之后才算
        self.assertEqual((v2.node, v2.arrival_time, v2.end_time), ("B", 35, 40))
        self.assertEqual(r.arrival_time, 50)

    def test_origin_is_first_stop(self):
        eng = make_engine(
            ["S", "A", "T"], [E("E1", "S", "A", 10), E("E2", "A", "T", 20)]
        )
        r = eng.plan("S", "T", 0, stops=(S("S", 50, 100, 10),))
        (visit,) = r.stops
        self.assertEqual((visit.node, visit.arrival_time, visit.start_time, visit.end_time),
                         ("S", 0, 50, 60))
        self.assertEqual(r.segments[0].enter_time, 60)
        self.assertEqual(r.arrival_time, 90)

    def test_origin_stop_window_missed_is_no_route(self):
        eng = make_engine(["S", "T"], [E("E1", "S", "T", 10)])
        with self.assertRaises(NoRouteError):
            eng.plan("S", "T", 100, stops=(S("S", 0, 50, 0),))

    def test_destination_is_last_stop(self):
        eng = make_engine(
            ["S", "A"], [E("E1", "S", "A", 10)]
        )
        r = eng.plan("S", "A", 0, stops=(S("A", 100, 200, 30),))
        # 最终到达 = 完成最后一站停留的时刻
        self.assertEqual(r.arrival_time, 130)
        self.assertEqual(r.stops[0].end_time, 130)
        self.assertEqual([s.edge_id for s in r.segments], ["E1"])

    def test_chained_stops_on_same_node(self):
        eng = make_engine(
            ["S", "A", "T"], [E("E1", "S", "A", 10), E("E2", "A", "T", 20)]
        )
        r = eng.plan("S", "T", 0, stops=(S("A", 0, 100, 5), S("A", 0, 100, 7)))
        v1, v2 = r.stops
        self.assertEqual((v1.arrival_time, v1.start_time, v1.end_time), (10, 10, 15))
        self.assertEqual((v2.arrival_time, v2.start_time, v2.end_time), (15, 15, 22))
        self.assertEqual(r.arrival_time, 42)

    def test_destination_reached_before_all_stops_keeps_searching(self):
        # 路径先经过终点 T，再完成停靠后回到 T
        edges = [
            E("E1", "S", "T", 10),
            E("E2", "T", "A", 10),
            E("E3", "A", "T", 10),
        ]
        eng = make_engine(["S", "A", "T"], edges)
        r = eng.plan("S", "T", 0, stops=(S("A", 0, 1000, 5),))
        self.assertEqual([s.edge_id for s in r.segments], ["E1", "E2", "E3"])
        self.assertEqual(r.stops[0].end_time, 25)
        self.assertEqual(r.arrival_time, 35)

    def test_unreachable_stop_is_no_route(self):
        eng = make_engine(
            ["S", "A", "X"], [E("E1", "S", "A", 10)]
        )
        with self.assertRaises(NoRouteError):
            eng.plan("S", "A", 0, stops=(S("X", 0, 1000, 0),))

    def test_unreachable_destination_after_stops_is_no_route(self):
        eng = make_engine(
            ["S", "A", "X"], [E("E1", "S", "A", 10)]
        )
        with self.assertRaises(NoRouteError):
            eng.plan("S", "X", 0, stops=(S("A", 0, 1000, 0),))

    def test_avoid_stairs_applies_to_stop_legs(self):
        edges = [
            E("E1", "S", "A", 10, stairs=True),
            E("E3", "S", "A", 30),
            E("E2", "A", "T", 10),
        ]
        eng = make_engine(["S", "A", "T"], edges)
        r = eng.plan("S", "T", 0, avoid_stairs=True, stops=(S("A", 0, 1000, 0),))
        self.assertEqual([s.edge_id for s in r.segments], ["E3", "E2"])
        self.assertEqual(r.stops[0].arrival_time, 30)


class GlobalLexTieTests(unittest.TestCase):
    def test_window_flattening_uses_full_sequence_lex(self):
        # E1(50s) 与 E0(90s) 都到 A；窗口 [100,200] 把两者结束停留时刻都拉平到 100
        # 逐段独立破平局会选 E1（到达更早），全程字典序应选 ("E0","E2")
        edges = [
            E("E1", "S", "A", 50),
            E("E0", "S", "A", 90),
            E("E2", "A", "T", 10),
        ]
        eng = make_engine(["S", "A", "T"], edges)
        r = eng.plan("S", "T", 0, stops=(S("A", 100, 200, 0),))
        self.assertEqual(r.arrival_time, 110)
        self.assertEqual([s.edge_id for s in r.segments], ["E0", "E2"])

    def test_lex_tie_spanning_two_stop_legs(self):
        # 两腿各有两条等时路径，窗口拉平后全程序列 ("E1","E3") 字典序最小
        edges = [
            E("E2", "S", "A", 10),
            E("E1", "S", "A", 20),
            E("E4", "A", "B", 10),
            E("E3", "A", "B", 20),
            E("E5", "B", "T", 10),
        ]
        eng = make_engine(["S", "A", "B", "T"], edges)
        stops = (S("A", 100, 200, 0), S("B", 200, 300, 0))
        r = eng.plan("S", "T", 0, stops=stops)
        # 两腿结束时刻都被窗口拉平：A 站 100 结束，B 站 200 结束，最终 210
        self.assertEqual(r.arrival_time, 210)
        self.assertEqual([s.edge_id for s in r.segments], ["E1", "E3", "E5"])


class StopQueryValidationTests(unittest.TestCase):
    NODES = {"S", "A", "T"}
    BASE = {"origin": "S", "destination": "T", "departure_time": "2026-10-05T08:00:00Z"}

    def query(self, stops):
        payload = dict(self.BASE)
        if stops is not None:
            payload["stops"] = stops
        return normalize_query(payload, self.NODES)

    def valid_stop(self, **kw):
        stop = {
            "node": "A",
            "earliest_start": "2026-10-05T09:00:00Z",
            "latest_start": "2026-10-05T10:00:00Z",
            "stay_seconds": 60,
        }
        stop.update(kw)
        return stop

    def test_no_stops_key_or_empty_list_keeps_legacy_shape(self):
        self.assertEqual(self.query(None)["stops"], [])
        self.assertEqual(self.query([])["stops"], [])

    def test_valid_stop_parses(self):
        q = self.query([self.valid_stop()])
        (stop,) = q["stops"]
        self.assertEqual(stop["node"], "A")
        self.assertEqual(stop["stay"], 60)
        self.assertLess(stop["earliest"], stop["latest"])

    def test_unknown_stop_node_rejected(self):
        with self.assertRaises(ValidationError):
            self.query([self.valid_stop(node="GHOST")])

    def test_inverted_window_rejected(self):
        with self.assertRaises(ValidationError):
            self.query([self.valid_stop(earliest_start="2026-10-05T11:00:00Z",
                                        latest_start="2026-10-05T10:00:00Z")])

    def test_equal_window_bounds_allowed(self):
        q = self.query([self.valid_stop(earliest_start="2026-10-05T09:00:00Z",
                                        latest_start="2026-10-05T09:00:00Z")])
        self.assertEqual(q["stops"][0]["earliest"], q["stops"][0]["latest"])

    def test_bad_window_time_format_rejected(self):
        with self.assertRaises(ValidationError):
            self.query([self.valid_stop(earliest_start="not-a-time")])

    def test_invalid_stay_seconds_rejected(self):
        for bad in (-1, 1.5, True, "10", None):
            with self.assertRaises(ValidationError, msg=f"stay_seconds={bad!r}"):
                self.query([self.valid_stop(stay_seconds=bad)])

    def test_zero_stay_seconds_valid(self):
        q = self.query([self.valid_stop(stay_seconds=0)])
        self.assertEqual(q["stops"][0]["stay"], 0)

    def test_missing_stop_fields_rejected(self):
        for field in ("node", "earliest_start", "latest_start", "stay_seconds"):
            stop = self.valid_stop()
            del stop[field]
            with self.assertRaises(ValidationError, msg=f"missing {field}"):
                self.query([stop])

    def test_stops_must_be_list(self):
        for bad in ({}, "A", 1, True):
            with self.assertRaises(ValidationError, msg=f"stops={bad!r}"):
                self.query(bad)

    def test_stop_entry_must_be_object(self):
        with self.assertRaises(ValidationError):
            self.query(["A"])


class NoStopCompatibilityTests(unittest.TestCase):
    def test_plan_without_stops_unchanged(self):
        eng = make_engine(
            ["S", "A", "T"], [E("E1", "S", "A", 10), E("E2", "A", "T", 20)]
        )
        r_default = eng.plan("S", "T", 0)
        r_empty = eng.plan("S", "T", 0, stops=())
        for r in (r_default, r_empty):
            self.assertEqual(r.stops, ())
            self.assertEqual(r.arrival_time, 30)
            self.assertEqual([s.edge_id for s in r.segments], ["E1", "E2"])


if __name__ == "__main__":
    unittest.main()
