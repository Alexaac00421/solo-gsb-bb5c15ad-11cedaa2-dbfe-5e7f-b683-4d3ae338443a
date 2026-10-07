"""版本恢复测试：整体恢复为新版本、历史可追溯、拒绝非法目标、并发串行、旧库迁移。"""
import os
import tempfile
import threading
import unittest
from pathlib import Path

from app.db import (
    SCHEMA,
    VersionNotFoundError,
    connect,
    current_version,
    get_snapshot,
    init_db,
    publish_closures,
    publish_graph,
    restore_version,
)
from app.schemas import ValidationError, normalize_restore

GRAPH_V1 = {
    "nodes": ["A", "B", "C"],
    "edges": [
        {"id": "E1", "from": "A", "to": "B", "travel_seconds": 10, "has_stairs": False},
        {"id": "E2", "from": "B", "to": "C", "travel_seconds": 20, "has_stairs": True,
         "reverse_windows": [
             {"start": "2026-10-05T11:00:00Z", "end": "2026-10-05T12:00:00Z"}]},
    ],
}
GRAPH_V2 = {
    "nodes": ["A", "C"],
    "edges": [
        {"id": "E3", "from": "A", "to": "C", "travel_seconds": 30, "has_stairs": False},
    ],
}
CLOSURES_V1 = {
    "closures": [
        {"edge_id": "E1", "start": "2026-10-05T08:00:00Z", "end": "2026-10-05T09:00:00Z"}
    ]
}


def snapshot_content(snap):
    """可比较的快照内容（不含版本号）。"""
    return (
        tuple(snap.nodes),
        tuple(sorted(snap.edges)),
        tuple(sorted(snap.closures)),
        tuple(sorted(snap.reverse_windows)),
    )


class RestoreTestBase(unittest.TestCase):
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


class RestoreTests(RestoreTestBase):
    def test_restore_creates_new_version_with_source_content(self):
        publish_graph(self.conn, GRAPH_V1)          # v1（含反向窗）
        publish_closures(self.conn, CLOSURES_V1)    # v2
        publish_graph(self.conn, GRAPH_V2)          # v3（误发布）
        self.assertEqual(current_version(self.conn), 3)

        new = restore_version(self.conn, 2)
        self.assertEqual(new, 4)                    # 版本号继续递增
        self.assertEqual(current_version(self.conn), 4)

        restored = get_snapshot(self.conn)
        source = get_snapshot(self.conn, 2)
        # 节点/路段/封闭记录/反向通行窗整体与来源版本一致
        self.assertEqual(snapshot_content(restored), snapshot_content(source))
        self.assertEqual(len(restored.reverse_windows), 1)
        self.assertEqual(len(restored.closures), 1)

    def test_restore_keeps_history_and_never_rewinds_pointer(self):
        publish_graph(self.conn, GRAPH_V1)          # v1
        publish_closures(self.conn, CLOSURES_V1)    # v2
        publish_graph(self.conn, GRAPH_V2)          # v3
        restore_version(self.conn, 1)               # v4 = v1 的内容

        # 历史版本（含被覆盖的 v3）仍可追溯
        self.assertEqual(snapshot_content(get_snapshot(self.conn, 3)),
                         snapshot_content(get_snapshot(self.conn, 3)))
        snap3 = get_snapshot(self.conn, 3)
        self.assertEqual([e.id for e in snap3.edges], ["E3"])
        snap1 = get_snapshot(self.conn, 1)
        self.assertEqual([e.id for e in snap1.edges], ["E1", "E2"])
        # 生效版本是新的 v4，而不是把指针拨回 v1
        self.assertEqual(current_version(self.conn), 4)
        kind = self.conn.execute(
            "SELECT kind FROM versions WHERE version_id=4").fetchone()["kind"]
        self.assertEqual(kind, "restore")

    def test_restore_current_version_is_allowed(self):
        publish_graph(self.conn, GRAPH_V1)          # v1
        new = restore_version(self.conn, 1)         # 恢复当前版本自身
        self.assertEqual(new, 2)
        self.assertEqual(snapshot_content(get_snapshot(self.conn, 2)),
                         snapshot_content(get_snapshot(self.conn, 1)))

    def test_restore_nonexistent_version_rejected(self):
        publish_graph(self.conn, GRAPH_V1)          # v1
        with self.assertRaises(VersionNotFoundError):
            restore_version(self.conn, 999)
        # 生效版本不变，且没有遗留新版本行
        self.assertEqual(current_version(self.conn), 1)
        count = self.conn.execute("SELECT COUNT(*) AS c FROM versions").fetchone()["c"]
        self.assertEqual(count, 1)

    def test_restore_on_empty_db_rejected(self):
        with self.assertRaises(VersionNotFoundError):
            restore_version(self.conn, 1)
        self.assertEqual(current_version(self.conn), 0)

    def test_restore_invalid_target_rejected(self):
        publish_graph(self.conn, GRAPH_V1)
        for bad in (0, -3, True, False, "1", 1.5, None):
            with self.assertRaises(ValidationError):
                restore_version(self.conn, bad)
        self.assertEqual(current_version(self.conn), 1)

    def test_restore_after_closures_replaced(self):
        publish_graph(self.conn, GRAPH_V1)                       # v1
        publish_closures(self.conn, CLOSURES_V1)                 # v2
        publish_closures(self.conn, {"closures": []})            # v3 清空封闭
        self.assertEqual(len(get_snapshot(self.conn).closures), 0)
        restore_version(self.conn, 2)                            # v4
        snap = get_snapshot(self.conn)
        self.assertEqual([c.edge_id for c in snap.closures], ["E1"])


class NormalizeRestoreTests(unittest.TestCase):
    def test_valid_payload(self):
        self.assertEqual(normalize_restore({"version": 2}), 2)

    def test_invalid_payloads(self):
        for bad in (
            None, [], "2", 2,                # 包络不是对象
            {},                              # 缺 version
            {"version": 0}, {"version": -1},  # 非正整数
            {"version": True}, {"version": "2"}, {"version": 2.0},
            {"version": None},
        ):
            with self.assertRaises(ValidationError, msg=repr(bad)):
                normalize_restore(bad)


class RestoreConcurrencyTests(RestoreTestBase):
    def test_restore_and_publish_are_serialized(self):
        """恢复与发布并发：写事务串行，版本号单调递增，每个版本都是完整数据。"""
        publish_graph(self.conn, GRAPH_V1)  # v1
        rounds = 5
        errors = []

        def do_restore():
            conn = connect(Path(self.db_path))
            try:
                for _ in range(rounds):
                    restore_version(conn, 1)
            except Exception as exc:  # pragma: no cover - 失败时记录
                errors.append(exc)
            finally:
                conn.close()

        def do_publish():
            conn = connect(Path(self.db_path))
            try:
                for _ in range(rounds):
                    publish_closures(conn, CLOSURES_V1)
            except Exception as exc:  # pragma: no cover - 失败时记录
                errors.append(exc)
            finally:
                conn.close()

        barrier = threading.Barrier(2)

        def synced(fn):
            def wrapper():
                barrier.wait()
                fn()
            return wrapper

        threads = [threading.Thread(target=synced(do_restore)),
                   threading.Thread(target=synced(do_publish))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        # 最终生效顺序明确：10 次写操作串行提交，版本号连续递增到 11
        self.assertEqual(current_version(self.conn), 1 + 2 * rounds)
        # 每个历史版本都是完整数据（查询不会读到拼接状态）
        for v in range(1, 2 + 2 * rounds):
            snap = get_snapshot(self.conn, v)
            self.assertIsNotNone(snap, v)
            self.assertEqual(sorted(e.id for e in snap.edges), ["E1", "E2"], v)
            self.assertEqual(snap.nodes, ["A", "B", "C"], v)
            self.assertEqual(len(snap.reverse_windows), 1, v)


class RestoreMigrationTests(unittest.TestCase):
    """旧库（versions.kind CHECK 不含 'restore'）在 init_db 时自动迁移。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "old.db")
        os.environ["APP_DB_PATH"] = self.db_path

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("APP_DB_PATH", None)

    def test_old_schema_db_is_migrated_and_restorable(self):
        old_schema = SCHEMA.replace("'graph', 'closures', 'restore'",
                                    "'graph', 'closures'")
        self.assertNotIn("'restore'", old_schema)
        conn = connect(Path(self.db_path))
        try:
            # 模拟旧版本服务创建的库
            conn.executescript(old_schema)
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('current_version', '0')"
            )
            publish_graph(conn, GRAPH_V1)        # v1
            publish_closures(conn, CLOSURES_V1)  # v2

            init_db(conn)  # 触发迁移
            ddl = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='versions'"
            ).fetchone()["sql"]
            self.assertIn("'restore'", ddl)

            new = restore_version(conn, 1)
            self.assertEqual(new, 3)
            kinds = [r["kind"] for r in conn.execute(
                "SELECT kind FROM versions ORDER BY version_id")]
            self.assertEqual(kinds, ["graph", "closures", "restore"])
            self.assertEqual(snapshot_content(get_snapshot(conn, 3)),
                             snapshot_content(get_snapshot(conn, 1)))
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
