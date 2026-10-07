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
from .db import Snapshot, TripRecord, transaction
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


class TripNotFoundError(LookupError):
    """改签目标行程不存在（行程及占用均不变）。"""


class TripCancelledError(Exception):
    """改签目标行程已取消，不能改签（占用此前已释放，本次不做任何改动）。"""


class TripDepartedError(Exception):
    """行程已经出发（当前时刻不早于其出发时刻），不能改签。"""

    def __init__(self, trip_id: int, departure_ts: int, now: int):
        super().__init__(
            f"行程 {trip_id} 已出发（出发时刻 {departure_ts}，当前时刻 {now}），"
            "不能改签"
        )
        self.departure_ts = departure_ts
        self.now = now


class RevisionConflictError(Exception):
    """请求携带的修订号与行程当前修订号不符，未写入任何内容。"""

    def __init__(self, expected_revision: int, current_revision: int):
        super().__init__(
            f"行程修订号已变化：请求携带 {expected_revision}，当前为 "
            f"{current_revision}，请按最新行程重新改签"
        )
        self.expected_revision = expected_revision
        self.current_revision = current_revision


class RebookValidationError(ValueError):
    """改签参数在写事务时刻判定非法（如新出发时刻未晚于当前时刻）。

    原行程及占用均不变；HTTP 层映射为 ``400 invalid_payload``。
    """


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

    ``exclude_trip_id``（改签时使用）在 SQL 内排除本行程的原占用：
    改签在同一事务内一次性替换逐段占用，重新规划只与**其余**行程
    竞争容量，不能与自身旧占用互相阻挡。

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
        self._exclude_trip_id = exclude_trip_id
        self._cache: dict[str, list[tuple[int, int, int]]] = {}
        self.edges_read: list[str] = []
        self.rows_read: int = 0

    def __call__(self, edge_id: str) -> list[tuple[int, int, int]]:
        cached = self._cache.get(edge_id)
        if cached is None:
            if self._exclude_trip_id is None:
                cached = db.edge_occupancy(
                    self._conn, self._generation_id, edge_id, self._not_before
                )
            else:
                cached = db.edge_occupancy(
                    self._conn,
                    self._generation_id,
                    edge_id,
                    self._not_before,
                    exclude_trip_id=self._exclude_trip_id,
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


def rebook_trip(
    conn: sqlite3.Connection,
    trip_id: int,
    *,
    expected_revision: int,
    departure: int,
    destination: str,
    avoid_stairs: bool,
    people: int,
    expected_version: Optional[int] = None,
    now: Optional[int] = None,
) -> tuple[TripRecord, Snapshot]:
    """在当前写事务内为一条已确认且**尚未出发**的行程改签。

    起点与行程编号保持不变，按调用方给出的新出发时刻、终点、人数与
    避台阶选项，在**当前数据版本与图发布世代**下重新规划：

    1. 固定当前版本与世代（与确认/取消/发布/恢复/封闭更新严格串行）；
    2. 校验新出发时刻严格晚于当前时刻，以及行程存在、仍为已确认、
       尚未出发（``now`` 早于其原出发时刻）、修订号一致，和可选
       ``data_version`` 与当前版本一致；
    3. 占用视图在 SQL 内**排除本行程原占用**后按既有封闭、反向窗与
       半开容量规则重新确认新路线；
    4. 成功后一次性替换逐段时刻、人数、数据版本与世代，修订号递增。

    任何拒绝（新出发时刻不在未来、行程不存在/已取消/已出发、修订号
    或版本不符、新路线不可行）均不写入任何内容，原行程及其容量占用
    保持不变（调用方回滚事务）。

    成功返回 ``(更新后的行程记录, 规划使用的版本快照)``。
    """
    current_time = int(time.time()) if now is None else now

    # 写事务内固定当前版本与世代：与所有其他写入严格串行
    snap = db.get_snapshot(conn)
    if snap is None:
        raise NoRouteError("尚未导入步道图，无法改签行程")

    # 新出发时刻必须严格晚于事务时刻（与“尚未出发”共用同一墙钟，
    # 避免事务前后取时不一致造成边界判定歧义）
    if departure <= current_time:
        raise RebookValidationError(
            f"新出发时刻必须晚于当前时刻（当前 {current_time}，收到 {departure}）"
        )

    record = db.get_trip(conn, trip_id)
    if record is None:
        raise TripNotFoundError(f"行程 {trip_id} 不存在")
    if record.status == "cancelled":
        raise TripCancelledError(f"行程 {trip_id} 已取消，不能改签")
    # 尚未出发：当前时刻必须严格早于行程的（请求时）出发时刻
    if current_time >= record.departure_ts:
        raise TripDepartedError(trip_id, record.departure_ts, current_time)
    if expected_revision != record.revision:
        raise RevisionConflictError(expected_revision, record.revision)
    if expected_version is not None and expected_version != snap.version_id:
        raise VersionConflictError(expected_version, snap.version_id)

    # 起点保持不变；按当前版本/世代重新规划，占用视图排除本行程原占用
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

    new_revision = db.replace_trip(
        conn,
        trip_id,
        version_id=snap.version_id,
        generation_id=snap.generation_id,
        destination=destination,
        departure_ts=departure,
        arrival_ts=route.arrival_time,
        avoid_stairs=avoid_stairs,
        party_size=people,
        segments=_segments_to_rows(route),
    )
    updated = db.get_trip(conn, trip_id)
    assert updated is not None and updated.revision == new_revision
    return updated, snap


def rebook_trip_atomic(conn: sqlite3.Connection, trip_id: int, **kwargs) -> tuple[TripRecord, Snapshot]:
    """打开立即写事务后执行 :func:`rebook_trip`；失败整体回滚。

    改签与确认、取消、图发布、版本恢复及封闭更新共用同一
    ``BEGIN IMMEDIATE`` 写事务队列，按提交顺序串行生效，容量绝不超额。
    """
    with transaction(conn):
        return rebook_trip(conn, trip_id, **kwargs)


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
