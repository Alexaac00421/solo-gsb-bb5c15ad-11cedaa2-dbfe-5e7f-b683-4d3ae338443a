"""高峰时段有容量保障的行程确认。

容量语义
========
- 步道图每条路段可携带正整数通行容量 ``capacity``，缺省/``null`` 不限；
- 已确认行程按每段**实际进入至离开**的半开区间 ``[enter, leave)`` 占用
  相应人数的容量；同一段在同一时刻的占用人数之和不得超过容量；
- 容量不足时允许在节点等待（时间依赖 FIFO 网络），等待规则与封闭、
  反向通行窗一致：按**进入时刻**判定，只推迟进入时刻；
- 容量对两个通行方向同样生效（路段容量不分方向）。

确认与并发
==========
确认在单个 ``BEGIN IMMEDIATE`` 写事务内完成“固定当前版本与世代 →
**按需读取**寻路实际触及的有容量路段占用 → 重新规划 → 原子写入
行程”。不再预加载整个世代的全部占用：只有规划过程中实际触及的
有容量路段才触发一次按路段查询（经 ``idx_trip_seg_edge`` 索引
定位），未触及路段上的行程增长不会使单次确认的读取量线性增长。
SQLite 写事务全局串行，所有占用读取与行程写入同属该事务，视图
固定，故并发确认看到的占用互不重叠，绝不可能超额；规划不可行时
事务整体回滚，不留下任何占用。

版本与世代
==========
- 客户端可在请求中携带 ``data_version``：与服务端当前版本不一致时
  **不写入**，抛出 :class:`VersionConflictError`，由调用方基于新版本
  重新规划或显式放弃；不传则总是按当前版本规划；
- 容量按**图发布世代**计量：步道图发布（``/admin/graph``）与版本恢复
  （``/admin/restore``）开启新世代；封闭记录更新沿用当前世代，其中
  已确认行程继续参与计量。重新发布/恢复后的新世代独立计量，旧行程
  仍可查询与取消（取消只释放其所属世代的容量）。
"""
from __future__ import annotations

import sqlite3
import time
from typing import Optional

from . import db
from .db import RevisionConflictError, Snapshot, TripRecord, transaction
from .routing import NoRouteError, Route, build_engine


class VersionConflictError(Exception):
    """请求基于的数据版本已变化，未写入任何行程。"""

    def __init__(self, expected: int, current: int):
        super().__init__(
            f"数据版本已变化：请求基于 v{expected}，当前为 v{current}，"
            "请基于新版本重新规划后再确认"
        )
        self.expected_version = expected
        self.current_version = current


class DemandOccupancy:
    """当前图发布世代占用的**按需视图**（容量确认规划专用）。

    不预加载整个世代的占用：寻路过程中只有实际触及的**有容量**路段
    才触发一次按路段查询，结果按路段缓存（同一路段至多读取一次）；
    未触及路段上的行程数量不影响单次确认的读取量。所有读取发生在
    确认所在的 ``BEGIN IMMEDIATE`` 写事务内，与行程写入同属一个
    串行事务——世代随版本快照在事务开始时确定，并发确认/取消/发布/
    恢复/封闭更新不会混入该视图。

    ``not_before``（行程出发时刻）之前即结束的占用
    （``leave_ts <= not_before``）按半开区间语义不可能阻挡本行程，
    读取时直接过滤，进一步缩小读取范围。

    ``edges_read``/``rows_read`` 为读取统计，供性能验证与测试断言。
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        generation_id: int,
        not_before: int = 0,
        exclude_trip_id: Optional[int] = None,
    ):
        self._conn = conn
        self._generation_id = generation_id
        self._not_before = not_before
        # 改签重规划时给出：本行程原占用先被视为已释放，不阻挡新路线
        self._exclude_trip_id = exclude_trip_id
        self._cache: dict[str, list[tuple[int, int, int]]] = {}
        self.edges_read: list[str] = []
        self.rows_read: int = 0

    def __call__(self, edge_id: str) -> list[tuple[int, int, int]]:
        cached = self._cache.get(edge_id)
        if cached is None:
            cached = db.edge_occupancy(
                self._conn,
                self._generation_id,
                edge_id,
                self._not_before,
                self._exclude_trip_id,
            )
            self._cache[edge_id] = cached
            self.edges_read.append(edge_id)
            self.rows_read += len(cached)
        return cached


def _segments_to_rows(route: Route) -> list[tuple]:
    return [
        (
            seg.edge_id,
            seg.from_node,
            seg.to_node,
            seg.enter_time,
            seg.leave_time,
            seg.travel_seconds,
            seg.waiting_seconds,
            seg.has_stairs,
        )
        for seg in route.segments
    ]


def confirm_trip(
    conn: sqlite3.Connection,
    *,
    origin: str,
    destination: str,
    departure: int,
    avoid_stairs: bool,
    people: int,
    expected_version: Optional[int] = None,
) -> tuple[TripRecord, Snapshot]:
    """原子确认一条有容量保障的行程。

    必须在外层**写事务**中调用（本函数内部不再加事务），以保证
    “读取占用 → 规划 → 写入”与并发确认/取消/发布严格串行。

    成功返回 ``(行程记录, 规划使用的版本快照)``；版本不匹配抛
    :class:`VersionConflictError`；容量/封闭/窗约束下无可行行程抛
    :class:`NoRouteError`——两种失败均不写入任何内容（调用方回滚）。
    """
    # 写事务内读取当前版本与世代：与所有其他写入严格串行
    snap = db.get_snapshot(conn)
    if snap is None:
        raise NoRouteError("尚未导入步道图，无法确认行程")
    if expected_version is not None and expected_version != snap.version_id:
        raise VersionConflictError(expected_version, snap.version_id)

    # 按需占用视图：寻路触及的有容量路段才读取（逐路段缓存），
    # 未触及路段的行程数量不影响本次确认的读取量；读取与后续
    # 写入同属当前写事务，占用视图固定（固定版本与世代）。
    occupancy = DemandOccupancy(conn, snap.generation_id, not_before=departure)
    engine = build_engine(snap, occupancy)
    route = engine.plan(
        origin=origin,
        destination=destination,
        departure=departure,
        avoid_stairs=avoid_stairs,
        party_size=people,
    )

    trip_id = db.insert_trip(
        conn,
        version_id=snap.version_id,
        generation_id=snap.generation_id,
        origin=origin,
        destination=destination,
        departure_ts=departure,
        arrival_ts=route.arrival_time,
        avoid_stairs=avoid_stairs,
        party_size=people,
        segments=_segments_to_rows(route),
    )
    record = db.get_trip(conn, trip_id)
    return record, snap


def confirm_trip_atomic(conn: sqlite3.Connection, **kwargs) -> tuple[TripRecord, Snapshot]:
    """打开立即写事务后执行 :func:`confirm_trip`；失败整体回滚。"""
    with transaction(conn):
        return confirm_trip(conn, **kwargs)


def cancel_trip_atomic(conn: sqlite3.Connection, trip_id: int) -> Optional[tuple[TripRecord, bool]]:
    """取消行程并释放容量（幂等）。

    返回 ``(行程记录, released)``：行程不存在返回 ``None``；首次取消
    ``released=True``（在单个写事务内把行程置为取消、释放其世代容量）；
    重复取消 ``released=False``，不重复释放。
    """
    with transaction(conn):
        record = db.get_trip(conn, trip_id)
        if record is None:
            return None
        released = db.cancel_trip(conn, trip_id, int(time.time()))
        if not released:
            # 此前已取消：原样返回，不做任何写入
            record = db.get_trip(conn, trip_id)
            return record, False
        return db.get_trip(conn, trip_id), True


class TripNotFoundError(Exception):
    """改签目标行程不存在。"""


class TripNotModifiableError(Exception):
    """行程已取消或已出发，不能改签（原行程与占用均不变）。"""

    def __init__(self, trip_id: int, reason: str):
        super().__init__(f"行程 {trip_id} 无法改签：{reason}")
        self.trip_id = trip_id
        self.reason = reason


class DepartureInPastError(Exception):
    """新出发时刻不晚于当前时刻。"""


def reschedule_trip(
    conn: sqlite3.Connection,
    trip_id: int,
    *,
    expected_revision: int,
    destination: str,
    departure: int,
    avoid_stairs: bool,
    people: int,
    expected_version: Optional[int] = None,
    now: Optional[int] = None,
) -> tuple[TripRecord, Snapshot]:
    """在串行写事务内为已确认且**尚未出发**的行程改签。

    必须在外层写事务中调用（本函数内部不再加事务），与行程确认、取消、
    步道图发布、封闭更新及版本恢复严格串行，容量绝不超额。

    流程：
    1. 固定当前数据版本与图发布世代（``data_version`` 不符抛
       :class:`VersionConflictError`）；
    2. 校验行程存在、仍为 ``confirmed``、尚未出发、修订号一致；
    3. 新出发时刻必须严格晚于当前时刻（``now`` 缺省取系统时钟）；
    4. **先排除本行程原占用**，在当前版本/世代下按既有封闭、反向窗、
       半开容量规则重新规划（起点不变，终点/出发时刻/人数/避台阶可改）；
    5. 成功后一次性替换逐段时刻、人数、数据版本与世代，修订号递增 1。

    任何失败都由调用方回滚：原行程及其容量占用保持不变。
    """
    current_ts = int(time.time()) if now is None else now
    if departure <= current_ts:
        raise DepartureInPastError(
            "新出发时刻必须晚于当前时刻"
            f"（当前 {current_ts}，请求出发 {departure}）"
        )

    snap = db.get_snapshot(conn)
    if snap is None:
        raise NoRouteError("尚未导入步道图，无法改签行程")
    if expected_version is not None and expected_version != snap.version_id:
        raise VersionConflictError(expected_version, snap.version_id)

    record = db.get_trip(conn, trip_id)
    if record is None:
        raise TripNotFoundError(f"行程 {trip_id} 不存在")
    if record.status == "cancelled":
        raise TripNotModifiableError(trip_id, "行程已取消")
    # 尚未出发：按行程的计划出发时刻（半开，恰在当前时刻出发即已出发）
    if record.departure_ts <= current_ts:
        raise TripNotModifiableError(trip_id, "行程已出发")
    if record.revision_no != expected_revision:
        raise RevisionConflictError(trip_id, expected_revision)
    # 起点保持不变：起点在当前版本图中必须仍然存在，否则新路线不可行
    if record.origin not in snap.node_set:
        raise NoRouteError(
            f"行程起点 {record.origin!r} 在当前步道图中已不存在，无法改签"
        )

    # 按需占用视图：排除本行程自身的旧占用（改签先视为释放原路线），
    # 其余同世代未取消行程照常计量；与写入同属当前串行写事务。
    occupancy = DemandOccupancy(
        conn,
        snap.generation_id,
        not_before=departure,
        exclude_trip_id=trip_id,
    )
    engine = build_engine(snap, occupancy)
    route = engine.plan(
        origin=record.origin,
        destination=destination,
        departure=departure,
        avoid_stairs=avoid_stairs,
        party_size=people,
    )

    db.replace_trip(
        conn,
        trip_id,
        version_id=snap.version_id,
        generation_id=snap.generation_id,
        origin=record.origin,
        destination=destination,
        departure_ts=departure,
        arrival_ts=route.arrival_time,
        avoid_stairs=avoid_stairs,
        party_size=people,
        expected_revision=expected_revision,
        segments=_segments_to_rows(route),
    )
    return db.get_trip(conn, trip_id), snap


def reschedule_trip_atomic(conn: sqlite3.Connection, trip_id: int, **kwargs) -> tuple[TripRecord, Snapshot]:
    """打开立即写事务后执行 :func:`reschedule_trip`；失败整体回滚。"""
    with transaction(conn):
        return reschedule_trip(conn, trip_id, **kwargs)
