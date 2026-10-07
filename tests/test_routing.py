"""通行规划引擎测试：时间区间语义、等待、字典序选路、台阶规避。"""
import unittest

from app.db import ClosureRow, EdgeRow, Snapshot
from app.routing import NoRouteError, RouteEngine


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


class OverlapBoundaryTests(unittest.TestCase):
    def setUp(self):
        # 单条边 S -> T，通行 60 秒
        self.engine = make_engine(["S", "T"], [E("E1", "S", "T", 60)])

    def plan_with(self, closures, departure=0):
        eng = make_engine(["S", "T"], [E("E1", "S", "T", 60)], closures)
        return eng.plan("S", "T", departure)

    def test_no_closure(self):
        r = self.engine.plan("S", "T", 0)
        self.assertEqual(r.arrival_time, 60)
        self.assertEqual([s.edge_id for s in r.segments], ["E1"])

    def test_arrive_exactly_at_closure_start_is_allowed(self):
        # 通行区间 [0,60)，封闭 [60,120)：到达时刻恰好封闭开始，不重叠
        r = self.plan_with([("E1", 60, 120)])
        self.assertEqual(r.arrival_time, 60)
        self.assertEqual(r.segments[0].waiting_seconds, 0)

    def test_depart_exactly_at_closure_end_is_allowed(self):
        # 封闭 [0,60)：在 S 等候到 60 进入，[60,120) 不重叠
        r = self.plan_with([("E1", 0, 60)])
        self.assertEqual(r.segments[0].enter_time, 60)
        self.assertEqual(r.segments[0].leave_time, 120)
        self.assertEqual(r.segments[0].waiting_seconds, 60)
        self.assertEqual(r.arrival_time, 120)

    def test_closure_inside_transit_interval_forces_wait(self):
        # 封闭 [30,90) 切过 [0,60)，必须等到 90 再走
        r = self.plan_with([("E1", 30, 90)])
        self.assertEqual(r.segments[0].enter_time, 90)
        self.assertEqual(r.arrival_time, 150)

    def test_departure_inside_closure_forces_wait(self):
        r = self.plan_with([("E1", 100, 200)], departure=120)
        self.assertEqual(r.segments[0].enter_time, 200)
        self.assertEqual(r.arrival_time, 260)

    def test_two_adjacent_closures(self):
        r = self.plan_with([("E1", 30, 60), ("E1", 0, 30)])
        self.assertEqual(r.segments[0].enter_time, 60)
        self.assertEqual(r.arrival_time, 120)

    def test_overlapping_closures(self):
        r = self.plan_with([("E1", 10, 80), ("E1", 0, 50)])
        self.assertEqual(r.segments[0].enter_time, 80)
        self.assertEqual(r.arrival_time, 140)

    def test_waiting_happens_at_node_not_mid_edge(self):
        # 封闭恰好在出发后第 1 秒开始且跨越原到达时刻：不能先走到一半
        r = self.plan_with([("E1", 1, 500)])
        self.assertEqual(r.segments[0].enter_time, 500)
        self.assertEqual(r.segments[0].waiting_seconds, 500)


class EarliestArrivalTests(unittest.TestCase):
    def test_directed_edges_respected(self):
        eng = make_engine(["A", "B"], [E("E1", "A", "B", 10)])
        eng.plan("A", "B", 0)  # 正向可行
        with self.assertRaises(NoRouteError):
            eng.plan("B", "A", 0)

    def test_chooses_fastest_path(self):
        edges = [
            E("E1", "S", "A", 10),
            E("E2", "A", "T", 10),
            E("E3", "S", "T", 100),
        ]
        eng = make_engine(["S", "A", "T"], edges)
        r = eng.plan("S", "T", 0)
        self.assertEqual(r.arrival_time, 20)
        self.assertEqual(r.segments[0].waiting_seconds, 0)

    def test_waiting_can_make_slower_path_win(self):
        # 短路但封闭：S-A 5s，A-T 5s；E_AT 在 [5,100) 封闭，等到 100 → 105
        # 直路 S-T 50s 不封 → 50 到达
        edges = [E("E1", "S", "A", 5), E("E2", "A", "T", 5), E("E3", "S", "T", 50)]
        eng = make_engine(["S", "A", "T"], edges, [("E2", 5, 100)])
        r = eng.plan("S", "T", 0)
        self.assertEqual(r.arrival_time, 50)
        self.assertEqual([s.edge_id for s in r.segments], ["E3"])

    def test_segment_times_chain(self):
        edges = [E("E1", "S", "A", 10), E("E2", "A", "T", 20)]
        eng = make_engine(["S", "A", "T"], edges, [("E2", 10, 35)])
        r = eng.plan("S", "T", 0)
        s1, s2 = r.segments
        self.assertEqual((s1.enter_time, s1.leave_time), (0, 10))
        self.assertEqual((s2.enter_time, s2.leave_time), (35, 55))
        self.assertEqual(s2.waiting_seconds, 25)
        self.assertEqual(r.arrival_time, 55)


class LexicographicTieTests(unittest.TestCase):
    def _engine(self, closures=()):
        # S -> A -> T: E2(10s), E4(100s)
        # S -> B -> T: E1(50s), E3(110s)
        # 若 E4 被封闭推迟到 60 进入，则两条路线都在 160 到达
        edges = [
            E("E2", "S", "A", 10),
            E("E4", "A", "T", 100),
            E("E1", "S", "B", 50),
            E("E3", "B", "T", 110),
        ]
        return make_engine(["S", "A", "B", "T"], edges, closures)

    def test_pure_equal_arrival_lex_smallest(self):
        # 不封闭时：A 线 110 到，B 线 160 到 → 最早 110，无平局
        r = self._engine().plan("S", "T", 0)
        self.assertEqual(r.arrival_time, 110)
        self.assertEqual([s.edge_id for s in r.segments], ["E2", "E4"])

    def test_wait_flattens_arrival_lex_smallest(self):
        # E4 在 [10,60) 封闭：A 线 60+100=160；B 线 50+110=160
        # 序列 (E1,E3) 字典序小于 (E2,E4)
        r = self._engine([("E4", 10, 60)]).plan("S", "T", 0)
        self.assertEqual(r.arrival_time, 160)
        self.assertEqual([s.edge_id for s in r.segments], ["E1", "E3"])

    def test_lex_compare_is_string_order(self):
        # E10 与 E2 按字符串比较："E10" < "E2"
        edges = [
            E("E10", "S", "A", 10), E("E20", "A", "T", 10),
            E("E2", "S", "B", 10), E("E9", "B", "T", 10),
        ]
        eng = make_engine(["S", "A", "B", "T"], edges)
        r = eng.plan("S", "T", 0)
        self.assertEqual(r.arrival_time, 20)
        self.assertEqual([s.edge_id for s in r.segments], ["E10", "E20"])


class StairsTests(unittest.TestCase):
    def test_avoid_stairs_filters_edges(self):
        edges = [
            E("E1", "S", "A", 10, stairs=True),
            E("E2", "A", "T", 10, stairs=False),
            E("E3", "S", "T", 100, stairs=False),
        ]
        eng = make_engine(["S", "A", "T"], edges)
        r_stairs = eng.plan("S", "T", 0, avoid_stairs=False)
        self.assertEqual([s.edge_id for s in r_stairs.segments], ["E1", "E2"])
        r_flat = eng.plan("S", "T", 0, avoid_stairs=True)
        self.assertEqual([s.edge_id for s in r_flat.segments], ["E3"])
        self.assertTrue(all(not s.has_stairs for s in r_flat.segments))

    def test_avoid_stairs_no_route(self):
        edges = [E("E1", "S", "T", 10, stairs=True)]
        eng = make_engine(["S", "T"], edges)
        with self.assertRaises(NoRouteError):
            eng.plan("S", "T", 0, avoid_stairs=True)

    def test_origin_equals_destination(self):
        eng = make_engine(["S", "T"], [E("E1", "S", "T", 10)])
        r = eng.plan("S", "S", 100)
        self.assertEqual(r.arrival_time, 100)
        self.assertEqual(r.segments, ())


if __name__ == "__main__":
    unittest.main()
