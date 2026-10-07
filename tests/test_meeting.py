"""多人汇合点规划测试：候选筛选、三级择优、到达后等待、负载校验。"""
import unittest

from app.db import ClosureRow, EdgeRow, Snapshot
from app.routing import (
    MeetingVisitor,
    NoMeetingCandidateError,
    RouteEngine,
)
from app.schemas import ValidationError, normalize_meeting
from app.timeutil import parse_time


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


def V(origin, dep=0, avoid=False, vid=None, index=0):
    return MeetingVisitor(index if vid is None else vid, origin, dep, avoid)


#   A ──E1(10s)──> X ──E2(10s)──> C
#   B ──E3(20s)──> X
BASIC_NODES = ["A", "B", "X", "C"]
BASIC_EDGES = [
    E("E1", "A", "X", 10),
    E("E2", "X", "C", 10),
    E("E3", "B", "X", 20),
]


class MeetingSelectionTests(unittest.TestCase):
    def setUp(self):
        self.engine = make_engine(BASIC_NODES, BASIC_EDGES)

    def test_picks_earliest_meeting_time(self):
        # X：A 10 到、B 20 到 → 汇合 20；C：A 20 到、B 不可达
        plan = self.engine.plan_meeting(
            (V("A", index=0), V("B", index=1)), ("X", "C"), 1000
        )
        self.assertEqual(plan.node, "X")
        self.assertEqual(plan.meeting_time, 20)
        self.assertEqual(plan.total_arrival_seconds, 30)

    def test_unreachable_candidate_is_skipped(self):
        # C 对 B 不可达；X 可行 → 选 X，不报错
        plan = self.engine.plan_meeting(
            (V("A", index=0), V("B", index=1)), ("C", "X"), 1000
        )
        self.assertEqual(plan.node, "X")

    def test_arrival_after_deadline_rejects_candidate(self):
        # X 需 B 20 时刻抵达，最晚 19 → 无合格候选
        with self.assertRaises(NoMeetingCandidateError):
            self.engine.plan_meeting(
                (V("A", index=0), V("B", index=1)), ("X",), 19
            )

    def test_arrival_exactly_at_deadline_is_allowed(self):
        plan = self.engine.plan_meeting(
            (V("A", index=0), V("B", index=1)), ("X",), 20
        )
        self.assertEqual(plan.meeting_time, 20)

    def test_meeting_time_breaks_tie_before_sum(self):
        # P 汇合 100、和 200；Q 汇合 90（和 180）→ 汇合时刻优先选 Q
        edges = [
            E("E1", "A", "P", 100),
            E("E2", "B", "P", 100),
            E("E3", "A", "Q", 90),
            E("E4", "B", "Q", 90),
        ]
        eng = make_engine(["A", "B", "P", "Q"], edges)
        plan = eng.plan_meeting(
            (V("A", index=0), V("B", index=1)), ("P", "Q"), 1000
        )
        self.assertEqual(plan.node, "Q")
        self.assertEqual(plan.meeting_time, 90)

    def test_sum_breaks_tie_when_meeting_time_equal(self):
        # P：两人都 100 到（和 200）；Q：一人 100、一人 50（和 150），汇合都 100
        edges = [
            E("E1", "A", "P", 100),
            E("E2", "B", "P", 100),
            E("E3", "A", "Q", 100),
            E("E4", "B", "Q", 50),
        ]
        eng = make_engine(["A", "B", "P", "Q"], edges)
        plan = eng.plan_meeting(
            (V("A", index=0), V("B", index=1)), ("P", "Q"), 1000
        )
        self.assertEqual(plan.node, "Q")
        self.assertEqual(plan.meeting_time, 100)
        self.assertEqual(plan.total_arrival_seconds, 150)

    def test_node_id_lex_breaks_final_tie(self):
        # P 与 Q 两人都 100 到 → 汇合时刻、总和相同，按节点编号选 P
        edges = [
            E("E1", "A", "P", 100),
            E("E2", "B", "P", 100),
            E("E3", "A", "Q", 100),
            E("E4", "B", "Q", 100),
        ]
        eng = make_engine(["A", "B", "P", "Q"], edges)
        plan = eng.plan_meeting(
            (V("A", index=0), V("B", index=1)), ("Q", "P"), 1000
        )
        self.assertEqual(plan.node, "P")

    def test_string_lex_node_ids(self):
        # "N10" < "N2"（字符串字典序）
        edges = [
            E("E1", "A", "N10", 10),
            E("E2", "B", "N10", 10),
            E("E3", "A", "N2", 10),
            E("E4", "B", "N2", 10),
        ]
        eng = make_engine(["A", "B", "N10", "N2"], edges)
        plan = eng.plan_meeting(
            (V("A", index=0), V("B", index=1)), ("N2", "N10"), 1000
        )
        self.assertEqual(plan.node, "N10")

    def test_wait_after_arrival_seconds(self):
        plan = self.engine.plan_meeting(
            (V("A", index=1), V("B", index=2)), ("X",), 1000
        )
        waits = {
            a.visitor.id: plan.meeting_time - a.route.arrival_time for a in plan.arrivals
        }
        self.assertEqual(waits, {1: 10, 2: 0})  # A 早到 10 秒，B 最晚到 0 秒

    def test_visitor_starting_at_candidate(self):
        # B 直接从 X 出发：抵达即出发时刻，零路段
        plan = self.engine.plan_meeting(
            (V("A", index=0), V("X", dep=15, index=1)), ("X",), 1000
        )
        self.assertEqual(plan.meeting_time, 15)
        (a0, a1) = plan.arrivals
        self.assertEqual(a0.route.segments[0].edge_id, "E1")
        self.assertEqual(a1.route.segments, ())
        self.assertEqual(a1.route.arrival_time, 15)

    def test_per_visitor_departure_times(self):
        # A 0 时刻出发 10 到；B 50 时刻出发 70 到 → 汇合 70
        plan = self.engine.plan_meeting(
            (V("A", dep=0, index=0), V("B", dep=50, index=1)), ("X",), 1000
        )
        self.assertEqual(plan.meeting_time, 70)

    def test_avoid_stairs_per_visitor(self):
        # A 有台阶捷径 E5（5s），平路 E1（10s）；避台阶访客走 E1
        edges = BASIC_EDGES + [E("E5", "A", "X", 5, stairs=True)]
        eng = make_engine(BASIC_NODES, edges)
        plan = eng.plan_meeting(
            (V("A", dep=0, avoid=True, vid="a"), V("B", dep=0, vid="b")),
            ("X",), 1000,
        )
        (a0,) = [a for a in plan.arrivals if a.visitor.origin == "A"]
        self.assertEqual([s.edge_id for s in a0.route.segments], ["E1"])
        self.assertTrue(all(not s.has_stairs for s in a0.route.segments))

    def test_avoid_stairs_can_make_candidate_infeasible(self):
        # A 到 X 只有台阶路，避台阶 → 无合格候选
        edges = [
            E("E1", "A", "X", 10, stairs=True),
            E("E3", "B", "X", 20),
        ]
        eng = make_engine(BASIC_NODES, edges)
        with self.assertRaises(NoMeetingCandidateError):
            eng.plan_meeting(
                (V("A", avoid=True, index=0), V("B", index=1)), ("X",), 1000
            )

    def test_closure_wait_reflected_in_arrival(self):
        # E3 在 [0, 60) 封闭：B 在起点等到 60 进入，80 到 X
        eng = make_engine(BASIC_NODES, BASIC_EDGES, [("E3", 0, 60)])
        plan = eng.plan_meeting(
            (V("A", index=0), V("B", index=1)), ("X",), 1000
        )
        self.assertEqual(plan.meeting_time, 80)

    def test_arrivals_follow_request_order(self):
        plan = self.engine.plan_meeting(
            (V("B", vid="bob"), V("A", vid="alice")), ("X",), 1000
        )
        self.assertEqual([a.visitor.id for a in plan.arrivals], ["bob", "alice"])


VALID_PAYLOAD = {
    "visitors": [
        {"id": "alice", "origin": "A",
         "departure_time": "2026-10-05T09:00:00Z", "avoid_stairs": False},
        {"origin": "B", "departure_time": "2026-10-05T09:00:00Z"},
    ],
    "candidates": ["X", "C"],
    "latest_meeting_time": "2026-10-05T12:00:00Z",
}
NODES = {"A", "B", "X", "C"}


class MeetingValidationTests(unittest.TestCase):
    def normalize(self, payload):
        return normalize_meeting(payload, NODES)

    def test_valid_payload(self):
        q = self.normalize(VALID_PAYLOAD)
        self.assertEqual([v["id"] for v in q["visitors"]], ["alice", 2])
        self.assertEqual(q["candidates"], ["X", "C"])
        self.assertEqual(
            q["latest_meeting_time"], parse_time("2026-10-05T12:00:00Z")
        )

    def test_default_avoid_stairs_false(self):
        q = self.normalize(VALID_PAYLOAD)
        self.assertFalse(q["visitors"][1]["avoid_stairs"])

    def test_payload_must_be_object(self):
        with self.assertRaises(ValidationError):
            self.normalize(["not", "object"])

    def test_missing_top_level_fields(self):
        for field in ("visitors", "candidates", "latest_meeting_time"):
            bad = dict(VALID_PAYLOAD)
            del bad[field]
            with self.assertRaises(ValidationError, msg=f"missing {field}"):
                self.normalize(bad)

    def test_visitors_must_be_list(self):
        for bad in ({}, "A", 1, True):
            p = dict(VALID_PAYLOAD, visitors=bad)
            with self.assertRaises(ValidationError, msg=f"visitors={bad!r}"):
                self.normalize(p)

    def test_at_least_two_visitors(self):
        p = dict(VALID_PAYLOAD, visitors=[VALID_PAYLOAD["visitors"][0]])
        with self.assertRaises(ValidationError):
            self.normalize(p)

    def test_visitor_missing_fields(self):
        for field in ("origin", "departure_time"):
            v = dict(VALID_PAYLOAD["visitors"][0])
            del v[field]
            p = dict(VALID_PAYLOAD, visitors=[v, VALID_PAYLOAD["visitors"][1]])
            with self.assertRaises(ValidationError, msg=f"missing {field}"):
                self.normalize(p)

    def test_unknown_visitor_origin(self):
        p = dict(VALID_PAYLOAD, visitors=[
            {"origin": "GHOST", "departure_time": "2026-10-05T09:00:00Z"},
            VALID_PAYLOAD["visitors"][1],
        ])
        with self.assertRaises(ValidationError):
            self.normalize(p)

    def test_bad_visitor_time(self):
        p = dict(VALID_PAYLOAD, visitors=[
            {"origin": "A", "departure_time": "not-a-time"},
            VALID_PAYLOAD["visitors"][1],
        ])
        with self.assertRaises(ValidationError):
            self.normalize(p)

    def test_bad_avoid_stairs_type(self):
        p = dict(VALID_PAYLOAD, visitors=[
            {"origin": "A", "departure_time": "2026-10-05T09:00:00Z",
             "avoid_stairs": "yes"},
            VALID_PAYLOAD["visitors"][1],
        ])
        with self.assertRaises(ValidationError):
            self.normalize(p)

    def test_duplicate_visitor_ids(self):
        p = dict(VALID_PAYLOAD, visitors=[
            {"id": "x", "origin": "A", "departure_time": "2026-10-05T09:00:00Z"},
            {"id": "x", "origin": "B", "departure_time": "2026-10-05T09:00:00Z"},
        ])
        with self.assertRaises(ValidationError):
            self.normalize(p)

    def test_candidates_must_be_nonempty_list(self):
        for bad in ({}, "X", 1, True):
            with self.assertRaises(ValidationError, msg=f"candidates={bad!r}"):
                self.normalize(dict(VALID_PAYLOAD, candidates=bad))
        with self.assertRaises(ValidationError):
            self.normalize(dict(VALID_PAYLOAD, candidates=[]))

    def test_unknown_candidate_node(self):
        with self.assertRaises(ValidationError):
            self.normalize(dict(VALID_PAYLOAD, candidates=["GHOST"]))

    def test_duplicate_candidates(self):
        with self.assertRaises(ValidationError):
            self.normalize(dict(VALID_PAYLOAD, candidates=["X", "X"]))

    def test_bad_latest_meeting_time(self):
        with self.assertRaises(ValidationError):
            self.normalize(dict(VALID_PAYLOAD, latest_meeting_time="noon"))

    def test_epoch_seconds_accepted(self):
        q = self.normalize(
            dict(VALID_PAYLOAD,
                 visitors=[
                     {"origin": "A", "departure_time": 0},
                     {"origin": "B", "departure_time": 10},
                 ],
                 latest_meeting_time=1000)
        )
        self.assertEqual(q["visitors"][0]["departure"], 0)
        self.assertEqual(q["latest_meeting_time"], 1000)


if __name__ == "__main__":
    unittest.main()
