"""联合预检测试：错误收集、原数组位置、候选图交叉校验、版本不变。"""
import os
import tempfile
import unittest
from pathlib import Path

from app.db import (
    connect,
    current_version,
    get_snapshot,
    init_db,
    publish_closures,
    publish_graph,
)
from app.precheck import precheck
from app.schemas import ValidationError

GRAPH = {
    "nodes": ["GATE", "PLAZA", "LAKE", "TOWER"],
    "edges": [
        {"id": "E1", "from": "GATE", "to": "PLAZA", "travel_seconds": 180, "has_stairs": False},
        {"id": "E2", "from": "PLAZA", "to": "LAKE", "travel_seconds": 300, "has_stairs": False},
        {"id": "E3", "from": "LAKE", "to": "TOWER", "travel_seconds": 240, "has_stairs": False,
         "reverse_windows": [
             {"start": "2026-10-05T11:00:00Z", "end": "2026-10-05T13:00:00Z"},
             {"start": "2026-10-05T14:00:00Z", "end": "2026-10-05T16:00:00Z"}]},
    ],
}
CLOSURES = {
    "closures": [
        {"edge_id": "E2", "start": "2026-10-05T10:00:00Z", "end": "2026-10-05T11:00:00Z"}
    ]
}


def located(report):
    """(source, path, index) 三元组集合，便于断言错误位置。"""
    return {(e["source"], e["path"], e["index"]) for e in report["errors"]}


class PrecheckPassTests(unittest.TestCase):
    def test_valid_payload_passes_and_counts(self):
        report = precheck({"graph": GRAPH, "closures": CLOSURES})
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["counts"], {
            "nodes": 4, "edges": 3, "closures": 1, "reverse_windows": 2,
        })

    def test_counts_zero_reverse_windows_when_absent(self):
        graph = {"nodes": ["A", "B"],
                 "edges": [{"id": "E1", "from": "A", "to": "B",
                            "travel_seconds": 10, "has_stairs": False}]}
        report = precheck({"graph": graph, "closures": {"closures": []}})
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["counts"], {
            "nodes": 2, "edges": 1, "closures": 0, "reverse_windows": 0,
        })

    def test_empty_graph_and_closures_pass(self):
        report = precheck({"graph": {"nodes": [], "edges": []},
                           "closures": {"closures": []}})
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["counts"], {
            "nodes": 0, "edges": 0, "closures": 0, "reverse_windows": 0,
        })

    def test_envelope_validation(self):
        for bad in ("nope", [], None, 42):
            with self.assertRaises(ValidationError):
                precheck(bad)
        with self.assertRaises(ValidationError):
            precheck({"closures": CLOSURES})          # 缺 graph
        with self.assertRaises(ValidationError):
            precheck({"graph": GRAPH})                # 缺 closures


class GraphErrorTests(unittest.TestCase):
    def test_graph_errors_collected_with_positions(self):
        bad_graph = {
            "nodes": ["A", "A", ""],
            "edges": [
                {"id": "E1", "from": "A", "to": "GHOST",
                 "travel_seconds": 0, "has_stairs": False},
                {"id": "E1", "from": "A", "to": "A",
                 "travel_seconds": 10, "has_stairs": "yes"},
                "not-an-object",
                {"id": "E2", "from": "A", "to": "A", "travel_seconds": 5,
                 "reverse_windows": [
                     {"start": "2026-10-05T11:00:00Z", "end": "2026-10-05T11:00:00Z"},
                     {"start": "2026-10-05T12:00:00Z", "end": "2026-10-05T13:00:00Z"},
                     {"start": "2026-10-05T12:30:00Z", "end": "2026-10-05T14:00:00Z"}]},
            ],
        }
        report = precheck({"graph": bad_graph, "closures": {"closures": []}})
        self.assertFalse(report["ok"])
        self.assertNotIn("counts", report)
        got = located(report)
        expected = {
            ("graph", "nodes[1]", 1),                          # 节点重复
            ("graph", "nodes[2]", 2),                          # 空节点名
            ("graph", "edges[0].to", 0),                       # 引用未定义节点
            ("graph", "edges[0].travel_seconds", 0),           # 非正秒数
            ("graph", "edges[1].id", 1),                       # 路段编号重复
            ("graph", "edges[1].has_stairs", 1),               # 非布尔
            ("graph", "edges[2]", 2),                          # 非对象
            ("graph", "edges[3].reverse_windows[0]", 0),       # start == end
            ("graph", "edges[3].reverse_windows", 3),          # 窗重叠
        }
        self.assertTrue(expected <= got, f"缺少: {expected - got}")
        # 全部错误都来自 graph，closures 为空数组无错误
        self.assertTrue(all(s == "graph" for s, _, _ in got))
        messages = [e["message"] for e in report["errors"]]
        self.assertTrue(any("节点重复" in m for m in messages))
        self.assertTrue(any("路段编号重复" in m for m in messages))
        self.assertTrue(any("重叠" in m for m in messages))

    def test_nodes_not_array_skips_edge_node_reference(self):
        report = precheck({
            "graph": {"nodes": "not-a-list",
                      "edges": [{"id": "E1", "from": "A", "to": "GHOST",
                                 "travel_seconds": 10, "has_stairs": False}]},
            "closures": {"closures": []},
        })
        self.assertFalse(report["ok"])
        got = located(report)
        self.assertIn(("graph", "nodes", None), got)
        # 节点集合不可判定：不误报路段的节点引用
        self.assertFalse(any("未定义的节点" in e["message"] for e in report["errors"]))

    def test_graph_not_object(self):
        report = precheck({"graph": [1, 2], "closures": {"closures": []}})
        self.assertEqual(located(report), {("graph", "", None)})

    def test_reverse_window_item_errors(self):
        graph = {
            "nodes": ["A", "B"],
            "edges": [{"id": "E1", "from": "A", "to": "B",
                       "travel_seconds": 10, "has_stairs": False,
                       "reverse_windows": [
                           {"end": "2026-10-05T12:00:00Z"},                # 缺 start
                           {"start": "not-a-time",
                            "end": "2026-10-05T12:00:00Z"},                 # 时间非法
                           "junk",                                          # 非对象
                           {"start": "2026-10-05T12:00:00Z",
                            "end": "2026-10-05T13:00:00Z"}]}],
        }
        report = precheck({"graph": graph, "closures": {"closures": []}})
        got = located(report)
        expected = {
            ("graph", "edges[0].reverse_windows[0].start", 0),
            ("graph", "edges[0].reverse_windows[1].start", 1),
            ("graph", "edges[0].reverse_windows[2]", 2),
        }
        self.assertTrue(expected <= got, f"缺少: {expected - got}")
        # 唯一有效的窗不构成重叠
        self.assertFalse(any("重叠" in e["message"] for e in report["errors"]))

    def test_reverse_windows_not_array(self):
        graph = {
            "nodes": ["A", "B"],
            "edges": [{"id": "E1", "from": "A", "to": "B",
                       "travel_seconds": 10, "has_stairs": False,
                       "reverse_windows": "junk"}],
        }
        report = precheck({"graph": graph, "closures": {"closures": []}})
        self.assertEqual(located(report),
                         {("graph", "edges[0].reverse_windows", 0)})


class ClosureErrorTests(unittest.TestCase):
    def test_closure_refs_checked_against_candidate_graph(self):
        # 候选图含 E1/E2/E3；封闭引用 ZZ → 报错并给出原数组位置
        payload = {"graph": GRAPH,
                   "closures": {"closures": [
                       {"edge_id": "E1", "start": "2026-10-05T08:00:00Z",
                        "end": "2026-10-05T09:00:00Z"},
                       {"edge_id": "ZZ", "start": "2026-10-05T08:00:00Z",
                        "end": "2026-10-05T09:00:00Z"}]}}
        report = precheck(payload)
        self.assertFalse(report["ok"])
        self.assertEqual(located(report), {("closures", "closures[1].edge_id", 1)})
        self.assertIn("不存在的路段", report["errors"][0]["message"])

    def test_closure_own_errors_collected(self):
        payload = {"graph": GRAPH,
                   "closures": {"closures": [
                       "junk",
                       {"edge_id": "E1"},                                   # 缺 start/end
                       {"edge_id": "E1", "start": "oops",
                        "end": "2026-10-05T09:00:00Z"},                     # 时间非法
                       {"edge_id": "E1", "start": "2026-10-05T10:00:00Z",
                        "end": "2026-10-05T09:00:00Z"}]}}                   # start >= end
        report = precheck(payload)
        got = located(report)
        expected = {
            ("closures", "closures[0]", 0),
            ("closures", "closures[1].start", 1),
            ("closures", "closures[1].end", 1),
            ("closures", "closures[2].start", 2),
            ("closures", "closures[3]", 3),
        }
        self.assertEqual(got, expected)

    def test_invalid_graph_suppresses_closure_edge_reference_only(self):
        bad_graph = {"nodes": ["A", "B"],
                     "edges": [{"id": "E1", "from": "A", "to": "B",
                                "travel_seconds": 0, "has_stairs": False}]}
        payload = {"graph": bad_graph,
                   "closures": {"closures": [
                       # 图无效：引用不可判定，不误报
                       {"edge_id": "NOPE", "start": "2026-10-05T08:00:00Z",
                        "end": "2026-10-05T09:00:00Z"},
                       # 自身时间区间错误仍报告
                       {"edge_id": "NOPE", "start": "2026-10-05T10:00:00Z",
                        "end": "2026-10-05T09:00:00Z"}]}}
        report = precheck(payload)
        self.assertFalse(report["ok"])
        got = located(report)
        self.assertIn(("graph", "edges[0].travel_seconds", 0), got)
        self.assertIn(("closures", "closures[1]", 1), got)
        self.assertFalse(any("不存在的路段" in e["message"] for e in report["errors"]))

    def test_closures_envelope_errors_reported_when_graph_invalid(self):
        bad_graph = {"nodes": ["A"], "edges": "junk"}
        report = precheck({"graph": bad_graph, "closures": "nope"})
        got = located(report)
        self.assertIn(("graph", "edges", None), got)
        self.assertIn(("closures", "", None), got)
        report2 = precheck({"graph": bad_graph, "closures": {"closures": "nope"}})
        self.assertIn(("closures", "closures", None), located(report2))


class PrecheckVersionTests(unittest.TestCase):
    """预检不改变当前数据版本。"""

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

    def test_precheck_does_not_change_version(self):
        publish_graph(self.conn, GRAPH)
        publish_closures(self.conn, CLOSURES)
        before = current_version(self.conn)
        snap_before = get_snapshot(self.conn)

        # 通过与不通过的预检都不改变版本
        ok_report = precheck({"graph": GRAPH, "closures": CLOSURES})
        self.assertTrue(ok_report["ok"])
        bad_report = precheck({"graph": {"nodes": [], "edges": "junk"},
                               "closures": {"closures": [{"edge_id": "ZZ"}]}})
        self.assertFalse(bad_report["ok"])

        self.assertEqual(current_version(self.conn), before)
        snap_after = get_snapshot(self.conn)
        self.assertEqual(snap_after, snap_before)

    def test_closure_refs_use_candidate_graph_not_database(self):
        # 库中当前图只有 E1；候选图有 E9，封闭引用 E9 → 预检通过
        publish_graph(self.conn, {
            "nodes": ["A", "B"],
            "edges": [{"id": "E1", "from": "A", "to": "B",
                       "travel_seconds": 10, "has_stairs": False}],
        })
        candidate = {
            "nodes": ["X", "Y"],
            "edges": [{"id": "E9", "from": "X", "to": "Y",
                       "travel_seconds": 10, "has_stairs": False}],
        }
        report = precheck({"graph": candidate,
                           "closures": {"closures": [
                               {"edge_id": "E9",
                                "start": "2026-10-05T08:00:00Z",
                                "end": "2026-10-05T09:00:00Z"}]}})
        self.assertTrue(report["ok"], report)
        # 反过来：封闭引用库中存在但候选图中没有的 E1 → 报错
        report2 = precheck({"graph": candidate,
                            "closures": {"closures": [
                                {"edge_id": "E1",
                                 "start": "2026-10-05T08:00:00Z",
                                 "end": "2026-10-05T09:00:00Z"}]}})
        self.assertFalse(report2["ok"])
        self.assertEqual(located(report2), {("closures", "closures[0].edge_id", 0)})


if __name__ == "__main__":
    unittest.main()
