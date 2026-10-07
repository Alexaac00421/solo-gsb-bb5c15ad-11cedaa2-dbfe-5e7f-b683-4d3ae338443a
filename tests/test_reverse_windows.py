"""反向通行时间窗测试。

覆盖：进入时刻判定方向、跨窗边界走完、半开边界、封闭对两个方向的
全程约束、节点等待、避台阶、与停靠/汇合的组合、字典序平局、
导入校验（无效/重叠窗拒绝整次导入并保留旧版本）与版本快照。
"""
import os
import tempfile
import unittest
from pathlib import Path

from app.db import (
    ClosureRow,
    EdgeRow,
    ReverseWindowRow,
    Snapshot,
    connect,
    current_version,
    get_snapshot,
    init_db,
    publish_closures,
    publish_graph,
)
from app.routing import (
    MeetingVisitor,
    NoMeetingCandidateError,
    NoRouteError,
    RouteEngine,
    Stop,
)
from app.schemas import ValidationError, normalize_graph


def make_engine(nodes, edges, windows=(), closures=()):
    return RouteEngine(
        Snapshot(
            version_id=1,
            nodes=list(nodes),
            edges=[EdgeRow(*e) for e in edges],
            closures=[ClosureRow(*c) for c in closures],
            reverse_windows=[ReverseWindowRow(*w) for w in windows],
        )
    )


# (id, src, dst, seconds, has_stairs)
def E(eid, u, v, secs, stairs=False):
    return (eid, u, v, secs, stairs)


# (edge_id, start, end)
def W(eid, start, end):
    return (eid, start, end)


class ReverseDirectionTests(unittest.TestCase):
    def setUp(self):
        # A --E1(100s)--> B，反向窗 [200,300)
        self.eng = make_engine(["A", "B"], [E("E1", "A", "B", 100)], [W("E1", 200, 300)])

    def test_forward_unchanged_outside_window(self):
        r = self.eng.plan("A", "B", 0)
        self.assertEqual(r.arrival_time, 100)
        (s,) = r.segments
        self.assertEqual((s.from_node, s.to_node), ("A", "B"))

    def test_reverse_inside_window(self):
        r = self.eng.plan("B", "A", 200)
        (s,) = r.segments
        self.assertEqual((s.from_node, s.to_node), ("B", "A"))
        self.assertEqual((s.enter_time, s.leave_time), (200, 300))

    def test_reverse_waits_at_node_until_window_opens(self):
        r = self.eng.plan("B", "A", 50)
        (s,) = r.segments
        self.assertEqual(s.enter_time, 200)
        self.assertEqual(s.waiting_seconds, 150)
        self.assertEqual(r.arrival_time, 300)

    def test_no_reverse_without_later_window(self):
        # 300 已在窗外，之后再无反向窗
        with self.assertRaises(NoRouteError):
            self.eng.plan("B", "A", 300)
        with self.assertRaises(NoRouteError):
            self.eng.plan("B", "A", 1000)

    def test_half_open_boundaries(self):
        # 恰好在 start 进入：反向允许
        r = self.eng.plan("B", "A", 199)
        self.assertEqual(r.segments[0].enter_time, 200)
        # 恰好在 end 就绪：反向不可入，窗外也无后续窗
        with self.assertRaises(NoRouteError):
            self.eng.plan("B", "A", 300)
        # 恰好在 start 之前原方向可入（通行区间不与窗相交于 start）
        r = self.eng.plan("A", "B", 100)
        self.assertEqual(r.segments[0].enter_time, 100)
        # 恰好在 end 进入原方向：立即允许
        r = self.eng.plan("A", "B", 300)
        self.assertEqual((r.segments[0].enter_time, r.arrival_time), (300, 400))

    def test_forward_blocked_inside_window_must_wait(self):
        r = self.eng.plan("A", "B", 250)
        (s,) = r.segments
        self.assertEqual(s.enter_time, 300)      # 等窗结束
        self.assertEqual(s.leave_time, 400)
        self.assertEqual(s.waiting_seconds, 50)

    def test_direction_decided_at_entry_then_may_cross_boundary(self):
        # 250 在窗内反向进入，通行到 350（窗 300 已结束）仍走完反向
        r = self.eng.plan("B", "A", 250)
        self.assertEqual(r.arrival_time, 350)
        self.assertEqual((r.segments[0].from_node, r.segments[0].to_node), ("B", "A"))
        # 原方向 250 进入须等到 300；不会在途中“变回”原方向
        r2 = self.eng.plan("A", "B", 250)
        self.assertEqual(r2.arrival_time, 400)

    def test_multi_edge_path_mixed_directions(self):
        # A->B E1(10)，B->C E2(10)；E2 反向窗 [100,200)
        eng = make_engine(
            ["A", "B", "C"],
            [E("E1", "A", "B", 10), E("E2", "B", "C", 10)],
            [W("E2", 100, 200)],
        )
        # C 0 出发：等窗到 100 反向 E2 到 B(110)；B->A 原方向无反向配置不可反
        with self.assertRaises(NoRouteError):
            eng.plan("C", "A", 0)
        r = eng.plan("C", "B", 0)
        self.assertEqual((r.segments[0].from_node, r.segments[0].to_node), ("C", "B"))
        self.assertEqual((r.segments[0].enter_time, r.arrival_time), (100, 110))

    def test_avoid_stairs_applies_to_reverse(self):
        eng = make_engine(["A", "B"], [E("E1", "A", "B", 10, stairs=True)], [W("E1", 100, 200)])
        with self.assertRaises(NoRouteError):
            eng.plan("B", "A", 100, avoid_stairs=True)
        r = eng.plan("B", "A", 100, avoid_stairs=False)
        self.assertTrue(r.segments[0].has_stairs)

    def test_multiple_adjacent_windows(self):
        # 相接的两扇窗 [200,300)[300,400)：300 仍可反向进入
        eng = make_engine(
            ["A", "B"], [E("E1", "A", "B", 100)],
            [W("E1", 200, 300), W("E1", 300, 400)],
        )
        r = eng.plan("B", "A", 300)
        self.assertEqual(r.segments[0].enter_time, 300)
        # 290 进入跨 300 窗边界（进入时在第一扇窗内）
        r = eng.plan("B", "A", 290)
        self.assertEqual((r.segments[0].enter_time, r.arrival_time), (290, 390))


class ClosureInteractionTests(unittest.TestCase):
    def test_closure_blocks_reverse_entry_whole_interval(self):
        # 反向窗 [200,300)，封闭 [250,350)：
        # 200 进入则 [200,300) 与封闭重叠 -> 推迟到 350，已出窗 -> 不可行
        eng = make_engine(
            ["A", "B"], [E("E1", "A", "B", 100)],
            [W("E1", 200, 300)], [("E1", 250, 350)],
        )
        with self.assertRaises(NoRouteError):
            eng.plan("B", "A", 200)

    def test_closure_wait_can_skip_to_next_window(self):
        # 窗 [200,300)、[400,500)；封闭 [200,350)
        # 200 进入被封闭推迟到 350（出窗），等下一窗 400 进入，500 到
        eng = make_engine(
            ["A", "B"], [E("E1", "A", "B", 100)],
            [W("E1", 200, 300), W("E1", 400, 500)],
            [("E1", 200, 350)],
        )
        r = eng.plan("B", "A", 200)
        self.assertEqual((r.segments[0].enter_time, r.arrival_time), (400, 500))

    def test_closure_finishing_exactly_at_window_end(self):
        # 封闭 [100,250)：200 就绪时反向进入会与封闭重叠 -> 250 进入仍在窗内
        eng = make_engine(
            ["A", "B"], [E("E1", "A", "B", 100)],
            [W("E1", 200, 300)], [("E1", 100, 250)],
        )
        r = eng.plan("B", "A", 200)
        self.assertEqual((r.segments[0].enter_time, r.arrival_time), (250, 350))


class LexTieWithReverseTests(unittest.TestCase):
    def test_reverse_edges_participate_in_lexicographic_tie(self):
        # S 出发到 T：
        #   原方向线 S->A(E2,10) -> A->T(E4,100) = 110
        #   反向线：T->B 的边 E3(110) 配反向窗 [0,1000)，S->B E1(50)
        #   S->B(50) 后反向 E3：50 在窗内进入 -> 160
        # 封闭 E4 [10,60) 把原方向线拉平到 160；序列 (E1,E3) < (E2,E4)
        edges = [
            E("E2", "S", "A", 10),
            E("E4", "A", "T", 100),
            E("E1", "S", "B", 50),
            E("E3", "T", "B", 110),   # 注意原方向 T->B，窗内可 B->T
        ]
        eng = make_engine(
            ["S", "A", "B", "T"], edges,
            [W("E3", 0, 1000)], [("E4", 10, 60)],
        )
        r = eng.plan("S", "T", 0)
        self.assertEqual(r.arrival_time, 160)
        self.assertEqual([s.edge_id for s in r.segments], ["E1", "E3"])
        # 反向段实际方向是 B -> T
        self.assertEqual((r.segments[1].from_node, r.segments[1].to_node), ("B", "T"))


class StopsWithReverseTests(unittest.TestCase):
    def test_stop_visit_across_reverse_edge(self):
        # A->B E1(10)，反向窗 [100,200)
        # B 0 出发往返 B：反向到 A(110)，停靠 [150,160] 停留 20 -> 170；
        # 170 仍在窗内，原方向须等窗结束 200 进入 -> 210
        eng = make_engine(["A", "B"], [E("E1", "A", "B", 10)], [W("E1", 100, 200)])
        r = eng.plan("B", "B", 0, stops=(Stop("A", 150, 160, 20),))
        self.assertEqual(r.arrival_time, 210)
        self.assertEqual(
            [(s.from_node, s.to_node, s.enter_time) for s in r.segments],
            [("B", "A", 100), ("A", "B", 200)],
        )
        (visit,) = r.stops
        self.assertEqual((visit.arrival_time, visit.start_time, visit.end_time), (110, 150, 170))

    def test_missed_stop_window_via_reverse_is_no_route(self):
        eng = make_engine(["A", "B"], [E("E1", "A", "B", 10)], [W("E1", 100, 200)])
        with self.assertRaises(NoRouteError):
            eng.plan("B", "B", 0, stops=(Stop("A", 100, 105, 0),))


class MeetingWithReverseTests(unittest.TestCase):
    def test_meeting_candidates_evaluate_reverse_arrivals(self):
        # A->B E1(60)，反向窗 [100,200)
        eng = make_engine(["A", "B"], [E("E1", "A", "B", 60)], [W("E1", 100, 200)])
        plan = eng.plan_meeting(
            (MeetingVisitor("alice", "A", 0, False),
             MeetingVisitor("bob", "B", 0, False)),
            ("A", "B"), 300,
        )
        # 候选 A：alice 0（起点）、bob 160（反向）-> 160
        # 候选 B：alice 60、bob 0 -> 60 胜出
        self.assertEqual(plan.node, "B")
        self.assertEqual(plan.meeting_time, 60)

    def test_meeting_reverse_arrival_after_latest_rejected(self):
        eng = make_engine(["A", "B"], [E("E1", "A", "B", 60)], [W("E1", 100, 200)])
        with self.assertRaises(NoMeetingCandidateError):
            eng.plan_meeting(
                (MeetingVisitor("alice", "A", 0, False),
                 MeetingVisitor("bob", "B", 0, False)),
                ("A",), 150,
            )


class SchemaValidationTests(unittest.TestCase):
    def _edge(self, **kw):
        base = {"id": "E1", "from": "A", "to": "B",
                "travel_seconds": 100, "has_stairs": False}
        base.update(kw)
        return base

    def test_absent_and_empty_windows_normalize_empty(self):
        g = normalize_graph({"nodes": ["A", "B"], "edges": [self._edge()]})
        self.assertEqual(g["edges"][0]["reverse_windows"], [])
        g = normalize_graph(
            {"nodes": ["A", "B"], "edges": [self._edge(reverse_windows=[])]})
        self.assertEqual(g["edges"][0]["reverse_windows"], [])

    def test_windows_sorted_and_kept_as_epoch_pairs(self):
        g = normalize_graph({"nodes": ["A", "B"], "edges": [self._edge(
            reverse_windows=[
                {"start": "2026-10-05T09:00:00Z", "end": "2026-10-05T10:00:00Z"},
                {"start": 1000, "end": 2000},
            ])]})
        self.assertEqual(
            g["edges"][0]["reverse_windows"],
            [(1000, 2000), (1791190800, 1791194400)],
        )

    def test_invalid_windows_rejected(self):
        bad_payloads = [
            [{"start": 100, "end": 100}],                       # start == end
            [{"start": 200, "end": 100}],                       # start > end
            [{"start": 0, "end": 100}, {"start": 50, "end": 150}],  # 重叠
            [{"start": "bad", "end": 100}],                     # 时间格式
            [42],                                               # 项非对象
            "not-a-list",                                       # 非数组
            [{"start": 0}],                                     # 缺 end
            [{"start": 0, "end": -10}],                         # 负纪元秒
        ]
        for windows in bad_payloads:
            with self.assertRaises(ValidationError):
                normalize_graph(
                    {"nodes": ["A", "B"], "edges": [self._edge(reverse_windows=windows)]}
                )

    def test_adjacent_window_endpoints_allowed(self):
        g = normalize_graph({"nodes": ["A", "B"], "edges": [self._edge(
            reverse_windows=[{"start": 0, "end": 100}, {"start": 100, "end": 200}])]})
        self.assertEqual(len(g["edges"][0]["reverse_windows"]), 2)

    def test_null_windows_treated_as_absent(self):
        g = normalize_graph(
            {"nodes": ["A", "B"], "edges": [self._edge(reverse_windows=None)]})
        self.assertEqual(g["edges"][0]["reverse_windows"], [])


class GraphPublishTests(unittest.TestCase):
    GRAPH = {
        "nodes": ["A", "B"],
        "edges": [
            {"id": "E1", "from": "A", "to": "B", "travel_seconds": 100,
             "has_stairs": False,
             "reverse_windows": [{"start": 100, "end": 200}, {"start": 300, "end": 400}]},
        ],
    }

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["APP_DB_PATH"] = str(Path(self.tmp.name) / "test.db")
        self.conn = connect(Path(os.environ["APP_DB_PATH"]))
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()
        os.environ.pop("APP_DB_PATH", None)

    def test_windows_persisted_and_snapshotted(self):
        v = publish_graph(self.conn, self.GRAPH)
        snap = get_snapshot(self.conn)
        self.assertEqual(
            [(w.edge_id, w.start, w.end) for w in snap.reverse_windows],
            [("E1", 100, 200), ("E1", 300, 400)],
        )
        self.assertEqual(get_snapshot(self.conn, v).reverse_windows, snap.reverse_windows)

    def test_invalid_windows_reject_whole_import_keep_old(self):
        v1 = publish_graph(self.conn, self.GRAPH)
        bad = {
            "nodes": ["A", "B"],
            "edges": [
                {"id": "E1", "from": "A", "to": "B", "travel_seconds": 100,
                 "reverse_windows": [{"start": 0, "end": 100}, {"start": 50, "end": 150}]},
            ],
        }
        with self.assertRaises(ValidationError):
            publish_graph(self.conn, bad)
        self.assertEqual(current_version(self.conn), v1)
        self.assertEqual(len(get_snapshot(self.conn).reverse_windows), 2)

    def test_windows_replaced_on_republish(self):
        publish_graph(self.conn, self.GRAPH)
        # 新图不带反向窗：整体替换为空
        v2 = publish_graph(self.conn, {
            "nodes": ["A", "B"],
            "edges": [{"id": "E1", "from": "A", "to": "B",
                       "travel_seconds": 100, "has_stairs": False}],
        })
        self.assertEqual(get_snapshot(self.conn).reverse_windows, [])
        # 旧版本快照仍保留旧窗
        self.assertEqual(len(get_snapshot(self.conn, v2 - 1).reverse_windows), 2)

    def test_closures_republish_copies_windows(self):
        publish_graph(self.conn, self.GRAPH)
        publish_closures(self.conn, {"closures": []})
        self.assertEqual(len(get_snapshot(self.conn).reverse_windows), 2)


if __name__ == "__main__":
    unittest.main()
