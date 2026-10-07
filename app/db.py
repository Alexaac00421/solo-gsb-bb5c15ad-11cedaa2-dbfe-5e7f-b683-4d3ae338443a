"""SQLite 持久层。

版本模型
--------
每次导入（步道图或封闭记录）生成一个新版本号：

- 新版本完整复制上一版本的节点/路段/封闭记录，再覆盖被更新的那部分；
- ``meta`` 表中的 ``current_version`` 指针在同一事务内切换；
- 查询通过 :func:`get_snapshot` 在开始时读取一次指针，整次请求固定使用该版本。

导入失败时事务整体回滚，旧版本保持不动。

版本恢复（:func:`restore_version`）把指定历史版本的四类数据整体复制为一个
**新的**生效版本：版本号继续递增、历史版本原样保留，绝不回拨指针。恢复与
导入共用同一 ``BEGIN IMMEDIATE`` 写事务，彼此串行，生效顺序即提交顺序。
"""
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import NamedTuple, Optional

from . import config
from .schemas import ValidationError, normalize_closures, normalize_graph

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS versions (
    version_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    kind           TEXT NOT NULL CHECK (kind IN ('graph', 'closures', 'restore')),
    created_at     INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    -- 图发布世代：步道图发布/版本恢复时取新版本号；封闭记录更新沿用旧值。
    -- 行程容量按世代独立计量。
    generation_id  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS nodes (
    version_id  INTEGER NOT NULL,
    node_id     TEXT NOT NULL,
    PRIMARY KEY (version_id, node_id)
);

CREATE TABLE IF NOT EXISTS edges (
    version_id      INTEGER NOT NULL,
    edge_id         TEXT NOT NULL,
    from_node       TEXT NOT NULL,
    to_node         TEXT NOT NULL,
    travel_seconds  INTEGER NOT NULL CHECK (travel_seconds > 0),
    has_stairs      INTEGER NOT NULL CHECK (has_stairs IN (0, 1)),
    -- 正整数通行容量；NULL 表示不限容量
    capacity        INTEGER CHECK (capacity IS NULL OR capacity > 0),
    PRIMARY KEY (version_id, edge_id)
);

CREATE TABLE IF NOT EXISTS closures (
    version_id  INTEGER NOT NULL,
    closure_id  INTEGER NOT NULL,
    edge_id     TEXT NOT NULL,
    start_ts    INTEGER NOT NULL,
    end_ts      INTEGER NOT NULL CHECK (end_ts > start_ts),
    PRIMARY KEY (version_id, closure_id)
);

CREATE TABLE IF NOT EXISTS reverse_windows (
    version_id  INTEGER NOT NULL,
    edge_id     TEXT NOT NULL,
    window_idx  INTEGER NOT NULL,
    start_ts    INTEGER NOT NULL,
    end_ts      INTEGER NOT NULL CHECK (end_ts > start_ts),
    PRIMARY KEY (version_id, edge_id, window_idx)
);

-- 已确认行程（含已取消行程：取消只置状态，行与逐段记录保留可查询）
CREATE TABLE IF NOT EXISTS trips (
    trip_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id    INTEGER NOT NULL,   -- 确认时固定的数据版本
    generation_id INTEGER NOT NULL,   -- 确认时所属图发布世代（容量计量口径）
    origin        TEXT NOT NULL,
    destination   TEXT NOT NULL,
    departure_ts  INTEGER NOT NULL,
    arrival_ts    INTEGER NOT NULL,
    avoid_stairs  INTEGER NOT NULL CHECK (avoid_stairs IN (0, 1)),
    party_size    INTEGER NOT NULL CHECK (party_size > 0),
    status        TEXT NOT NULL CHECK (status IN ('confirmed', 'cancelled')),
    -- 行程修订号：确认时为 1，每次成功改签递增 1（乐观并发控制）
    revision      INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_at    INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    cancelled_at  INTEGER
);

-- 逐段实际占用：半开区间 [enter_ts, leave_ts)，方向也一并存档
CREATE TABLE IF NOT EXISTS trip_segments (
    trip_id         INTEGER NOT NULL,
    segment_idx     INTEGER NOT NULL,
    edge_id         TEXT NOT NULL,
    from_node       TEXT NOT NULL,
    to_node         TEXT NOT NULL,
    enter_ts        INTEGER NOT NULL,
    leave_ts        INTEGER NOT NULL CHECK (leave_ts > enter_ts),
    travel_seconds  INTEGER NOT NULL,
    waiting_seconds INTEGER NOT NULL,
    has_stairs      INTEGER NOT NULL CHECK (has_stairs IN (0, 1)),
    PRIMARY KEY (trip_id, segment_idx)
);

CREATE INDEX IF NOT EXISTS idx_edges_from     ON edges(version_id, from_node);
CREATE INDEX IF NOT EXISTS idx_closures_edge ON closures(version_id, edge_id);
CREATE INDEX IF NOT EXISTS idx_reverse_edge  ON reverse_windows(version_id, edge_id);
CREATE INDEX IF NOT EXISTS idx_trips_gen     ON trips(generation_id, status);
CREATE INDEX IF NOT EXISTS idx_trip_seg_trip ON trip_segments(trip_id);
-- 行程确认按路段按需读取占用：(edge_id, leave_ts) 支撑
-- “指定路段、leave_ts 大于某时刻”的范围定位，读取量只取决于
-- 该路段上的占用行数，与其他路段的行程数量无关。
CREATE INDEX IF NOT EXISTS idx_trip_seg_edge ON trip_segments(edge_id, leave_ts);
"""


class EdgeRow(NamedTuple):
    id: str
    src: str
    dst: str
    seconds: int
    has_stairs: bool
    capacity: Optional[int] = None  # None 表示不限容量


class ClosureRow(NamedTuple):
    edge_id: str
    start: int
    end: int


class ReverseWindowRow(NamedTuple):
    edge_id: str
    start: int
    end: int


class Snapshot(NamedTuple):
    """单次查询使用的不可变数据版本。"""

    version_id: int
    nodes: list[str]
    edges: list[EdgeRow]
    closures: list[ClosureRow]
    reverse_windows: list[ReverseWindowRow] = []
    generation_id: Optional[int] = None

    @property
    def node_set(self) -> set[str]:
        return set(self.nodes)


class TripRecord(NamedTuple):
    """一条已确认/已取消行程及其逐段占用（按段序）。"""

    trip_id: int
    version_id: int
    generation_id: int
    origin: str
    destination: str
    departure_ts: int
    arrival_ts: int
    avoid_stairs: bool
    party_size: int
    status: str
    created_at: int
    cancelled_at: Optional[int]
    segments: list["TripSegmentRecord"]
    revision: int = 1


class TripSegmentRecord(NamedTuple):
    segment_idx: int
    edge_id: str
    from_node: str
    to_node: str
    enter_ts: int
    leave_ts: int
    travel_seconds: int
    waiting_seconds: int
    has_stairs: bool


def connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path is not None else config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """建表并写入初始指针（不存在任何已发布版本时）。"""
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('current_version', '0')"
    )
    _migrate_old_schema(conn)


def _migrate_old_schema(conn: sqlite3.Connection) -> None:
    """旧库原地升级：versions 增加世代列、edges 增加容量列。

    - ``versions``：旧库的 ``kind`` CHECK 可能不含 ``'restore'``，且缺少
      ``generation_id`` 列——CHECK 无法就地修改，按 SQLite 惯例
      “建新表 → 复制 → 删旧表 → 改名”迁移；历史行的世代按发布语义回填：
      步道图发布与版本恢复各自开启新世代，封闭记录更新沿用前一世代。
    - ``edges``：缺少 ``capacity`` 列时直接 ``ALTER TABLE ADD COLUMN``
      （既有版本容量缺省为不限）。
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='versions'"
    ).fetchone()
    need_versions = (
        row is None
        or "'restore'" not in row["sql"]
        or "generation_id" not in row["sql"]
    )
    if need_versions:
        with transaction(conn):
            conn.execute("DROP TABLE IF EXISTS versions_new")
            conn.execute(
                "CREATE TABLE versions_new ("
                "    version_id    INTEGER PRIMARY KEY AUTOINCREMENT,"
                "    kind          TEXT NOT NULL CHECK (kind IN ('graph', 'closures', 'restore')),"
                "    created_at    INTEGER NOT NULL DEFAULT (strftime('%s','now')),"
                "    generation_id INTEGER NOT NULL"
                ")"
            )
            old_rows = conn.execute(
                "SELECT version_id, kind, created_at FROM versions ORDER BY version_id"
            ).fetchall()
            generation = 0
            for r in old_rows:
                if r["kind"] in ("graph", "restore"):
                    generation = int(r["version_id"])
                conn.execute(
                    "INSERT INTO versions_new(version_id, kind, created_at, generation_id) "
                    "VALUES (?, ?, ?, ?)",
                    (r["version_id"], r["kind"], r["created_at"], generation),
                )
            conn.execute("DROP TABLE versions")
            conn.execute("ALTER TABLE versions_new RENAME TO versions")

    erow = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='edges'"
    ).fetchone()
    if erow is not None and "capacity" not in erow["sql"]:
        with transaction(conn):
            conn.execute(
                "ALTER TABLE edges ADD COLUMN capacity INTEGER "
                "CHECK (capacity IS NULL OR capacity > 0)"
            )

    # 行程修订号：改签能力新增列；旧库行程初始修订号为 1。
    trow = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='trips'"
    ).fetchone()
    if trow is not None and "revision" not in trow["sql"]:
        with transaction(conn):
            conn.execute(
                "ALTER TABLE trips ADD COLUMN revision INTEGER NOT NULL "
                "DEFAULT 1 CHECK (revision > 0)"
            )


@contextmanager
def transaction(conn: sqlite3.Connection):
    """立即失败的写事务，避免与其他导入互相等待。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT value FROM meta WHERE key='current_version'"
    ).fetchone()
    return int(row["value"]) if row is not None else 0


def _generation_of(conn: sqlite3.Connection, version_id: int) -> int:
    """读取某版本所属的图发布世代。"""
    row = conn.execute(
        "SELECT generation_id FROM versions WHERE version_id=?", (version_id,)
    ).fetchone()
    return int(row["generation_id"]) if row is not None else 0


def _copy_version(conn: sqlite3.Connection, old: int, new: int, kind: str, generation: int) -> None:
    conn.execute(
        "INSERT INTO versions(version_id, kind, generation_id) VALUES (?, ?, ?)",
        (new, kind, generation),
    )
    if old:
        conn.execute("INSERT INTO nodes(version_id, node_id) SELECT ?, node_id FROM nodes WHERE version_id=?", (new, old))
        conn.execute(
            "INSERT INTO edges(version_id, edge_id, from_node, to_node, travel_seconds, has_stairs, capacity) "
            "SELECT ?, edge_id, from_node, to_node, travel_seconds, has_stairs, capacity FROM edges WHERE version_id=?",
            (new, old),
        )
        conn.execute(
            "INSERT INTO closures(version_id, closure_id, edge_id, start_ts, end_ts) "
            "SELECT ?, closure_id, edge_id, start_ts, end_ts FROM closures WHERE version_id=?",
            (new, old),
        )
        conn.execute(
            "INSERT INTO reverse_windows(version_id, edge_id, window_idx, start_ts, end_ts) "
            "SELECT ?, edge_id, window_idx, start_ts, end_ts FROM reverse_windows "
            "WHERE version_id=?",
            (new, old),
        )


def publish_graph(conn: sqlite3.Connection, payload) -> int:
    """原子发布新步道图。

    校验失败抛出 :class:`ValidationError` 并完整回滚；旧封闭记录被保留，
    其中引用了不在新图中路段的记录会自动丢弃。
    """
    graph = normalize_graph(payload)
    new_edge_ids = {e["id"] for e in graph["edges"]}

    with transaction(conn):
        old = _current_version(conn)
        new = int(
            conn.execute("SELECT COALESCE(MAX(version_id), 0) + 1 AS v FROM versions").fetchone()["v"]
        )
        # 步道图发布开启新的图发布世代（世代号即新版本号）
        _copy_version(conn, old, new, "graph", new)

        # 用新版本的图整体替换
        conn.execute("DELETE FROM nodes WHERE version_id=?", (new,))
        conn.execute("DELETE FROM edges WHERE version_id=?", (new,))
        conn.executemany(
            "INSERT INTO nodes(version_id, node_id) VALUES (?, ?)",
            [(new, n) for n in graph["nodes"]],
        )
        conn.executemany(
            "INSERT INTO edges(version_id, edge_id, from_node, to_node, travel_seconds, has_stairs, capacity) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (new, e["id"], e["src"], e["dst"], e["seconds"], 1 if e["has_stairs"] else 0,
                 e["capacity"])
                for e in graph["edges"]
            ],
        )
        # 反向时间窗随新图整体替换（校验已在事务前完成）
        conn.execute("DELETE FROM reverse_windows WHERE version_id=?", (new,))
        conn.executemany(
            "INSERT INTO reverse_windows(version_id, edge_id, window_idx, start_ts, end_ts) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (new, e["id"], idx, start, end)
                for e in graph["edges"]
                for idx, (start, end) in enumerate(e["reverse_windows"])
            ],
        )
        # 丢弃悬挂封闭记录
        conn.execute("DELETE FROM closures WHERE version_id=? AND edge_id NOT IN (%s)" %
                     (",".join("?" * len(new_edge_ids)) if new_edge_ids else "''"),
                     (new, *sorted(new_edge_ids)) if new_edge_ids else (new,))

        conn.execute(
            "UPDATE meta SET value=? WHERE key='current_version'", (str(new),)
        )
    return new


def publish_closures(conn: sqlite3.Connection, payload) -> int:
    """原子整体替换封闭记录。必须先发布过步道图。"""
    with transaction(conn):
        old = _current_version(conn)
        if not old:
            # 回滚由 contextmanager 完成
            raise ValueError("尚未导入步道图，无法发布封闭记录")
        known_edges = {
            r["edge_id"]
            for r in conn.execute("SELECT edge_id FROM edges WHERE version_id=?", (old,))
        }
        closures = normalize_closures(payload, known_edges)

        new = int(
            conn.execute("SELECT COALESCE(MAX(version_id), 0) + 1 AS v FROM versions").fetchone()["v"]
        )
        # 封闭记录更新不开启新世代：沿用当前版本的图发布世代
        generation = _generation_of(conn, old)
        _copy_version(conn, old, new, "closures", generation)
        conn.execute("DELETE FROM closures WHERE version_id=?", (new,))
        conn.executemany(
            "INSERT INTO closures(version_id, closure_id, edge_id, start_ts, end_ts) "
            "VALUES (?, ?, ?, ?, ?)",
            [(new, i, c["edge_id"], c["start"], c["end"]) for i, c in enumerate(closures)],
        )
        conn.execute(
            "UPDATE meta SET value=? WHERE key='current_version'", (str(new),)
        )
    return new


class VersionNotFoundError(ValueError):
    """恢复目标版本不存在（恢复被拒绝，生效版本不变）。"""


def restore_version(conn: sqlite3.Connection, target: int) -> int:
    """把历史版本 ``target`` 整体恢复为新的生效版本。

    在单个写事务中完成：校验目标版本存在 → 分配递增的新版本号 → 完整复制
    目标版本的节点/路段/封闭记录/反向通行窗 → 切换 ``current_version`` 指针。
    目标版本本身原样保留（历史仍可追溯），指针只前进不回拨。

    目标不存在时抛出 :class:`VersionNotFoundError` 并整体回滚，生效版本不变；
    目标版本号非法（非正整数）时抛出 :class:`ValidationError`。
    """
    if isinstance(target, bool) or not isinstance(target, int) or target <= 0:
        raise ValidationError(f"恢复目标版本号必须是正整数，收到 {target!r}")
    with transaction(conn):
        exists = conn.execute(
            "SELECT 1 FROM versions WHERE version_id=?", (target,)
        ).fetchone()
        if exists is None:
            raise VersionNotFoundError(f"版本 v{target} 不存在，无法恢复")
        new = int(
            conn.execute("SELECT COALESCE(MAX(version_id), 0) + 1 AS v FROM versions").fetchone()["v"]
        )
        # 版本恢复开启新的图发布世代：旧世代行程独立计量、仍可查询和取消
        _copy_version(conn, target, new, "restore", new)
        conn.execute(
            "UPDATE meta SET value=? WHERE key='current_version'", (str(new),)
        )
    return new


def get_snapshot(conn: sqlite3.Connection, version_id: Optional[int] = None) -> Optional[Snapshot]:
    """读取某个版本（默认当前版本）的完整数据。无已发布版本返回 None。"""
    if version_id is None:
        version_id = _current_version(conn)
    if not version_id:
        return None
    exists = conn.execute(
        "SELECT 1 FROM versions WHERE version_id=?", (version_id,)
    ).fetchone()
    if exists is None:
        return None

    nodes = [r["node_id"] for r in conn.execute(
        "SELECT node_id FROM nodes WHERE version_id=? ORDER BY node_id", (version_id,))]
    edges = [
        EdgeRow(r["edge_id"], r["from_node"], r["to_node"], r["travel_seconds"],
                bool(r["has_stairs"]), r["capacity"])
        for r in conn.execute(
            "SELECT edge_id, from_node, to_node, travel_seconds, has_stairs, capacity "
            "FROM edges WHERE version_id=?", (version_id,))
    ]
    closures = [
        ClosureRow(r["edge_id"], r["start_ts"], r["end_ts"])
        for r in conn.execute(
            "SELECT edge_id, start_ts, end_ts FROM closures WHERE version_id=? "
            "ORDER BY closure_id", (version_id,))
    ]
    reverse_windows = [
        ReverseWindowRow(r["edge_id"], r["start_ts"], r["end_ts"])
        for r in conn.execute(
            "SELECT edge_id, start_ts, end_ts FROM reverse_windows WHERE version_id=? "
            "ORDER BY edge_id, window_idx", (version_id,))
    ]
    generation = _generation_of(conn, version_id)
    return Snapshot(version_id, nodes, edges, closures, reverse_windows, generation)


def current_version(conn: sqlite3.Connection) -> int:
    return _current_version(conn)


# ----------------------------------------------------------------------
# 行程（容量确认）
# ----------------------------------------------------------------------
def active_occupancy(conn: sqlite3.Connection, generation_id: int) -> dict[str, list[tuple[int, int, int]]]:
    """某图发布世代内全部未取消行程的逐段占用（**全量视图**）。

    返回 ``{edge_id: [(enter_ts, leave_ts, party_size), ...]}``，仅含
    **当前世代**且 ``status='confirmed'`` 的行程；已取消行程不占容量，
    其他世代（重新发布/版本恢复后）独立计量。

    该全量读取只用于校验与测试；行程确认本身**不**调用本函数，而是
    通过 :func:`edge_occupancy` 按需读取寻路实际触及的路段占用，
    避免读取量随未触及路段上的行程数线性增长。
    """
    rows = conn.execute(
        "SELECT ts.edge_id AS edge_id, ts.enter_ts AS enter_ts, "
        "       ts.leave_ts AS leave_ts, t.party_size AS party_size "
        "FROM trip_segments ts "
        "JOIN trips t ON t.trip_id = ts.trip_id "
        "WHERE t.generation_id=? AND t.status='confirmed'",
        (generation_id,),
    ).fetchall()
    occupancy: dict[str, list[tuple[int, int, int]]] = {}
    for r in rows:
        occupancy.setdefault(r["edge_id"], []).append(
            (r["enter_ts"], r["leave_ts"], r["party_size"])
        )
    for intervals in occupancy.values():
        intervals.sort()
    return occupancy


# 单路段占用查询：CROSS JOIN 固定以 trip_segments 为外侧表，
# 经 idx_trip_seg_edge 按 (edge_id, leave_ts) 范围定位，再按行程主键
# 回表过滤世代与状态——读取行数只取决于该路段在该时刻之后的占用量。
EDGE_OCCUPANCY_SQL = (
    "SELECT ts.enter_ts AS enter_ts, ts.leave_ts AS leave_ts, "
    "       t.party_size AS party_size "
    "FROM trip_segments ts CROSS JOIN trips t ON t.trip_id = ts.trip_id "
    "WHERE ts.edge_id=? AND ts.leave_ts > ? "
    "  AND t.generation_id=? AND t.status='confirmed' "
    "ORDER BY ts.enter_ts"
)

# 改签专用变体：在上述条件上额外排除本行程原占用（一次性替换语义：
# 重新规划只与其余行程竞争容量，不能与自身旧占用互相阻挡）。
EDGE_OCCUPANCY_EXCLUDE_SQL = (
    "SELECT ts.enter_ts AS enter_ts, ts.leave_ts AS leave_ts, "
    "       t.party_size AS party_size "
    "FROM trip_segments ts CROSS JOIN trips t ON t.trip_id = ts.trip_id "
    "WHERE ts.edge_id=? AND ts.leave_ts > ? "
    "  AND t.generation_id=? AND t.status='confirmed' "
    "  AND t.trip_id != ? "
    "ORDER BY ts.enter_ts"
)


def edge_occupancy(
    conn: sqlite3.Connection,
    generation_id: int,
    edge_id: str,
    not_before: int = 0,
    *,
    exclude_trip_id: Optional[int] = None,
) -> list[tuple[int, int, int]]:
    """某图发布世代内**指定路段**的未取消占用（行程确认按需读取）。

    返回按进入时刻排序的 ``[(enter_ts, leave_ts, party_size), ...]``，
    仅含当前世代且 ``status='confirmed'`` 的行程在该路段上的逐段占用。
    ``leave_ts <= not_before`` 的占用按半开区间语义不可能阻挡不早于
    ``not_before`` 进入的行程（端点相接不冲突），读取时直接过滤。
    给出 ``exclude_trip_id`` 时（改签）排除该行程自身的原占用——改签
    在同一事务内“先释放原占用、再确认新路线”，新规划不得与自身旧
    占用竞争。

    查询经 ``idx_trip_seg_edge`` 索引按路段定位，只触及该路段的占用
    行：未触及路段上的行程增长不影响本查询的读取量。调用方须在写
    事务内读取，以与并发确认/取消/发布/恢复严格串行。
    """
    if exclude_trip_id is None:
        rows = conn.execute(
            EDGE_OCCUPANCY_SQL, (edge_id, not_before, generation_id)
        ).fetchall()
    else:
        rows = conn.execute(
            EDGE_OCCUPANCY_EXCLUDE_SQL,
            (edge_id, not_before, generation_id, exclude_trip_id),
        ).fetchall()
    return [(r["enter_ts"], r["leave_ts"], r["party_size"]) for r in rows]


def insert_trip(
    conn: sqlite3.Connection,
    *,
    version_id: int,
    generation_id: int,
    origin: str,
    destination: str,
    departure_ts: int,
    arrival_ts: int,
    avoid_stairs: bool,
    party_size: int,
    segments: list[tuple],
) -> int:
    """在当前事务内原子写入一条已确认行程及逐段占用，返回行程编号。

    ``segments`` 为
    ``(edge_id, from_node, to_node, enter_ts, leave_ts,
       travel_seconds, waiting_seconds, has_stairs)``
    元组列表，按段序给出。
    """
    cur = conn.execute(
        "INSERT INTO trips(version_id, generation_id, origin, destination, "
        "    departure_ts, arrival_ts, avoid_stairs, party_size, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'confirmed')",
        (version_id, generation_id, origin, destination, departure_ts,
         arrival_ts, 1 if avoid_stairs else 0, party_size),
    )
    trip_id = int(cur.lastrowid)
    conn.executemany(
        "INSERT INTO trip_segments(trip_id, segment_idx, edge_id, from_node, to_node, "
        "    enter_ts, leave_ts, travel_seconds, waiting_seconds, has_stairs) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (trip_id, idx, edge_id, from_node, to_node, enter_ts, leave_ts,
             travel_seconds, waiting_seconds, 1 if has_stairs else 0)
            for idx, (edge_id, from_node, to_node, enter_ts, leave_ts,
                      travel_seconds, waiting_seconds, has_stairs) in enumerate(segments)
        ],
    )
    return trip_id


def get_trip(conn: sqlite3.Connection, trip_id: int) -> Optional[TripRecord]:
    """按编号查询行程（含已取消行程；不存在返回 None）。"""
    row = conn.execute("SELECT * FROM trips WHERE trip_id=?", (trip_id,)).fetchone()
    if row is None:
        return None
    segments = [
        TripSegmentRecord(
            r["segment_idx"], r["edge_id"], r["from_node"], r["to_node"],
            r["enter_ts"], r["leave_ts"], r["travel_seconds"],
            r["waiting_seconds"], bool(r["has_stairs"]),
        )
        for r in conn.execute(
            "SELECT * FROM trip_segments WHERE trip_id=? ORDER BY segment_idx",
            (trip_id,),
        )
    ]
    return TripRecord(
        trip_id=row["trip_id"],
        version_id=row["version_id"],
        generation_id=row["generation_id"],
        origin=row["origin"],
        destination=row["destination"],
        departure_ts=row["departure_ts"],
        arrival_ts=row["arrival_ts"],
        avoid_stairs=bool(row["avoid_stairs"]),
        party_size=row["party_size"],
        status=row["status"],
        created_at=row["created_at"],
        cancelled_at=row["cancelled_at"],
        segments=segments,
        revision=int(row["revision"]) if "revision" in row.keys() else 1,
    )


def replace_trip(
    conn: sqlite3.Connection,
    trip_id: int,
    *,
    version_id: int,
    generation_id: int,
    destination: str,
    departure_ts: int,
    arrival_ts: int,
    avoid_stairs: bool,
    party_size: int,
    segments: list[tuple],
) -> int:
    """在当前事务内**一次性替换**行程的逐段时刻、人数、数据版本与世代。

    改签专用：行程编号、起点与创建时间保持不变；旧逐段占用全部删除，
    写入重新规划得到的新逐段占用，行程修订号 ``revision`` 递增 1。
    必须在外层写事务中调用（占用读取、规划与替换须与并发确认/取消/
    发布严格串行）。返回新的修订号。
    """
    conn.execute(
        "UPDATE trips SET version_id=?, generation_id=?, destination=?, "
        "    departure_ts=?, arrival_ts=?, avoid_stairs=?, party_size=?, "
        "    revision=revision+1 "
        "WHERE trip_id=? AND status='confirmed'",
        (version_id, generation_id, destination, departure_ts, arrival_ts,
         1 if avoid_stairs else 0, party_size, trip_id),
    )
    conn.execute("DELETE FROM trip_segments WHERE trip_id=?", (trip_id,))
    conn.executemany(
        "INSERT INTO trip_segments(trip_id, segment_idx, edge_id, from_node, to_node, "
        "    enter_ts, leave_ts, travel_seconds, waiting_seconds, has_stairs) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (trip_id, idx, edge_id, from_node, to_node, enter_ts, leave_ts,
             travel_seconds, waiting_seconds, 1 if has_stairs else 0)
            for idx, (edge_id, from_node, to_node, enter_ts, leave_ts,
                      travel_seconds, waiting_seconds, has_stairs) in enumerate(segments)
        ],
    )
    row = conn.execute(
        "SELECT revision FROM trips WHERE trip_id=?", (trip_id,)
    ).fetchone()
    return int(row["revision"])


def cancel_trip(conn: sqlite3.Connection, trip_id: int, cancelled_at: int) -> bool:
    """在当前事务内把行程标记为取消。

    返回 ``True`` 表示本次调用实际释放了占用（之前为已确认）；
    返回 ``False`` 表示行程此前已是取消状态（重复取消不重复释放）。
    """
    row = conn.execute(
        "SELECT status FROM trips WHERE trip_id=?", (trip_id,)
    ).fetchone()
    if row is None:
        return False
    if row["status"] == "cancelled":
        return False
    conn.execute(
        "UPDATE trips SET status='cancelled', cancelled_at=? WHERE trip_id=?",
        (cancelled_at, trip_id),
    )
    return True


def export_json(conn: sqlite3.Connection, path: Path) -> None:
    """辅助：把当前版本导出为 JSON（CLI 排错用）。"""
    snap = get_snapshot(conn)
    if snap is None:
        raise RuntimeError("当前没有已发布的数据版本")
    path.write_text(
        json.dumps(
            {
                "version_id": snap.version_id,
                "nodes": snap.nodes,
                "edges": [e._asdict() | {"has_stairs": e.has_stairs} for e in snap.edges],
                "closures": [c._asdict() for c in snap.closures],
                "reverse_windows": [w._asdict() for w in snap.reverse_windows],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
