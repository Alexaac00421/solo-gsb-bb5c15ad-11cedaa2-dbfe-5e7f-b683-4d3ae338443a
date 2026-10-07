"""原子发布、版本快照、校验拒绝测试。"""
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
from app.schemas import ValidationError

GRAPH_V1 = {
    "nodes": ["A", "B", "C"],
    "edges": [
        {"id": "E1", "from": "A", "to": "B", "travel_seconds": 10, "has_stairs": False},
        {"id": "E2", "from": "B", "to": "C", "travel_seconds": 20, "has_stairs": True},
    ],
}
CLOSURES_V1 = {
    "closures": [
        {"edge_id": "E1", "start": "2026-10-05T08:00:00Z", "end": "2026-10-05T09:00:00Z"}
    ]
}


class DBTestBase(unittest.TestCase):
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


class PublishTests(DBTestBase):
    def test_publish_graph_creates_version(self):
        v = publish_graph(self.conn, GRAPH_V1)
        self.assertEqual(v, 1)
        snap = get_snapshot(self.conn)
        self.assertEqual(snap.version_id, 1)
        self.assertEqual(snap.nodes, ["A", "B", "C"])
        self.assertEqual(len(snap.edges), 2)

    def test_closures_require_graph_first(self):
        with self.assertRaises(ValueError):
            publish_closures(self.conn, CLOSURES_V1)
        self.assertEqual(current_version(self.conn), 0)

    def test_graph_update_is_atomic_and_keeps_old_on_rejection(self):
        v1 = publish_graph(self.conn, GRAPH_V1)
        bad_graph = {
            "nodes": ["A"],
            "edges": [{"id": "X1", "from": "A", "to": "GHOST",
                       "travel_seconds": 5, "has_stairs": False}],
        }
        with self.assertRaises(ValidationError):
            publish_graph(self.conn, bad_graph)
        self.assertEqual(current_version(self.conn), v1)
        snap = get_snapshot(self.conn)
        self.assertEqual([e.id for e in snap.edges], ["E1", "E2"])

    def test_non_positive_seconds_rejected(self):
        for secs in (0, -10):
            bad = {"nodes": ["A", "B"],
                   "edges": [{"id": "X", "from": "A", "to": "B",
                              "travel_seconds": secs, "has_stairs": False}]}
            with self.assertRaises(ValidationError):
                publish_graph(self.conn, bad)
        # 浮点 / bool 也必须拒绝
        for secs in (1.5, True, "10"):
            bad = {"nodes": ["A", "B"],
                   "edges": [{"id": "X", "from": "A", "to": "B",
                              "travel_seconds": secs, "has_stairs": False}]}
            with self.assertRaises(ValidationError):
                publish_graph(self.conn, bad)

    def test_duplicate_node_or_edge_rejected(self):
        with self.assertRaises(ValidationError):
            publish_graph(self.conn, {
                "nodes": ["A", "A"], "edges": []})
        with self.assertRaises(ValidationError):
            publish_graph(self.conn, {
                "nodes": ["A", "B"],
                "edges": [
                    {"id": "X", "from": "A", "to": "B", "travel_seconds": 1, "has_stairs": False},
                    {"id": "X", "from": "B", "to": "A", "travel_seconds": 1, "has_stairs": False},
                ]})

    def test_invalid_closure_interval_rejected(self):
        publish_graph(self.conn, GRAPH_V1)
        cases = [
            {"closures": [{"edge_id": "E1",
                           "start": "2026-10-05T09:00:00Z",
                           "end": "2026-10-05T09:00:00Z"}]},
            {"closures": [{"edge_id": "E1",
                           "start": "2026-10-05T10:00:00Z",
                           "end": "2026-10-05T09:00:00Z"}]},
            {"closures": [{"edge_id": "MISSING",
                           "start": "2026-10-05T08:00:00Z",
                           "end": "2026-10-05T09:00:00Z"}]},
        ]
        for payload in cases:
            with self.assertRaises(ValidationError):
                publish_closures(self.conn, payload)
        self.assertEqual(current_version(self.conn), 1)  # 旧版本仍在

    def test_closures_replace_and_version_advances(self):
        publish_graph(self.conn, GRAPH_V1)  # v1
        v2 = publish_closures(self.conn, CLOSURES_V1)
        self.assertEqual(v2, 2)
        snap = get_snapshot(self.conn)
        # 新版本保留图，覆盖封闭
        self.assertEqual(len(snap.edges), 2)
        self.assertEqual(len(snap.closures), 1)

        v3 = publish_closures(self.conn, {"closures": []})
        self.assertEqual(v3, 3)
        snap3 = get_snapshot(self.conn)
        self.assertEqual(len(snap3.closures), 0)
        self.assertEqual(len(snap3.edges), 2)

        # 旧版本仍可按版本号读取（单次查询固定版本）
        snap_old = get_snapshot(self.conn, 2)
        self.assertEqual(len(snap_old.closures), 1)

    def test_graph_republish_drops_dangling_closures_but_keeps_valid(self):
        publish_graph(self.conn, GRAPH_V1)
        publish_closures(self.conn, {
            "closures": [
                {"edge_id": "E1", "start": "2026-10-05T08:00:00Z", "end": "2026-10-05T09:00:00Z"},
                {"edge_id": "E2", "start": "2026-10-05T08:00:00Z", "end": "2026-10-05T09:00:00Z"},
            ]})
        # 新图不再包含 E1，但包含 E2
        new_graph = {
            "nodes": ["B", "C"],
            "edges": [{"id": "E2", "from": "B", "to": "C",
                       "travel_seconds": 20, "has_stairs": True}],
        }
        publish_graph(self.conn, new_graph)
        snap = get_snapshot(self.conn)
        self.assertEqual([c.edge_id for c in snap.closures], ["E2"])

    def test_empty_graph_is_valid(self):
        v = publish_graph(self.conn, {"nodes": [], "edges": []})
        snap = get_snapshot(self.conn, v)
        self.assertEqual(snap.nodes, [])

    def test_persistence_across_connections(self):
        publish_graph(self.conn, GRAPH_V1)
        conn2 = connect(Path(self.db_path))
        try:
            self.assertEqual(current_version(conn2), 1)
            self.assertEqual(len(get_snapshot(conn2).edges), 2)
        finally:
            conn2.close()


if __name__ == "__main__":
    unittest.main()
