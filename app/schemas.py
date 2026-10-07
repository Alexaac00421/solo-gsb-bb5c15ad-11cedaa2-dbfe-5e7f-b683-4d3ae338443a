"""导入与查询负载的校验、规范化。

所有校验在写库事务开启之前完成，错误以 :class:`ValidationError` 抛出，
调用方负责返回 400 并保留数据库旧版本。
"""
from typing import Any

from .timeutil import TimeError, parse_time


class ValidationError(ValueError):
    """负载非法（未知节点、非正秒数、无效时间区间等）。"""


def _require_object(payload: Any, kind: str) -> dict:
    if not isinstance(payload, dict):
        raise ValidationError(f"{kind}必须是 JSON 对象")
    return payload


def _clean_identifier(value: Any, field: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValidationError(f"{field}必须是非空字符串")
    text = str(value).strip()
    if not text:
        raise ValidationError(f"{field}不能为空")
    return text


def _validate_seconds(value: Any) -> int:
    # 严格类型：拒绝 bool、float、数字字符串等隐式转换
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError("通行秒数(travel_seconds)必须是正整数")
    if value <= 0:
        raise ValidationError(f"通行秒数(travel_seconds)必须为正数，收到 {value}")
    return value


def _validate_stairs(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValidationError("台阶标记(has_stairs)必须是布尔值 true/false")
    return value


def _validate_capacity(value: Any, where: str):
    """校验路段通行容量：缺省/为 null 表示不限；否则必须是正整数。"""
    if value is None:
        return None
    # 严格类型：拒绝 bool、float、数字字符串等隐式转换
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{where}.capacity 必须是正整数或 null（缺省表示不限容量）")
    if value <= 0:
        raise ValidationError(f"{where}.capacity 必须为正整数，收到 {value}")
    return value


def _normalize_reverse_windows(raw: Any, where: str) -> list[tuple[int, int]]:
    """校验一条路段上的反向通行时间窗数组。

    每项 ``{"start": ..., "end": ...}`` 使用既有时间格式，区间为半开
    ``[start, end)`` 且必须 ``start < end``。窗内仅允许从原终点走向原起点，
    窗外仅允许原方向。同一数组内的时间窗不得重叠（端点相接允许）。
    返回按开始时刻排序的 ``(start, end)`` 纪元秒元组列表。
    """
    if not isinstance(raw, list):
        raise ValidationError(f"{where}.reverse_windows 必须是数组")
    windows: list[tuple[int, int]] = []
    for j, item in enumerate(raw):
        wwhere = f"{where}.reverse_windows[{j}]"
        if not isinstance(item, dict):
            raise ValidationError(f"{wwhere} 必须是对象")
        if "start" not in item:
            raise ValidationError(f"{wwhere} 缺少 start")
        if "end" not in item:
            raise ValidationError(f"{wwhere} 缺少 end")
        try:
            start = parse_time(item["start"])
            end = parse_time(item["end"])
        except TimeError as exc:
            raise ValidationError(f"{wwhere}: {exc}") from None
        if start >= end:
            raise ValidationError(
                f"{wwhere} 无效时间窗: start({item['start']}) 必须早于 end({item['end']})"
            )
        windows.append((start, end))

    windows.sort()
    for (s1, e1), (s2, e2) in zip(windows, windows[1:]):
        if e1 > s2:
            raise ValidationError(
                f"{where} 的反向时间窗存在重叠（半开区间仅允许端点相接）: "
                f"[{s1}, {e1}) 与 [{s2}, {e2}) 重叠"
            )
    return windows


def normalize_graph(payload: Any) -> dict:
    """校验步道图导入负载，返回规范化结构。

    期望格式::

        {
          "nodes": ["gate", "plaza", ...],
          "edges": [
            {"id": "E1", "from": "gate", "to": "plaza",
             "travel_seconds": 120, "has_stairs": false,
             "capacity": 100,
             "reverse_windows": [
               {"start": "2026-10-05T08:00:00Z",
                "end": "2026-10-05T09:00:00Z"}]},
            ...
          ]
        }

    每条边可选 ``capacity``：正整数通行容量（按半开区间占用计），
    缺省或为 ``null`` 表示不限容量；可选 ``reverse_windows``：半开
    ``[start, end)`` 时间窗数组，窗内仅允许从 ``to`` 反向走向 ``from``；
    缺省或为空时行为不变。
    """
    data = _require_object(payload, "步道图")
    raw_nodes = data.get("nodes")
    raw_edges = data.get("edges")
    if not isinstance(raw_nodes, list):
        raise ValidationError("nodes 必须是数组")
    if not isinstance(raw_edges, list):
        raise ValidationError("edges 必须是数组")

    nodes: list[str] = []
    node_set: set[str] = set()
    for i, raw in enumerate(raw_nodes):
        node = _clean_identifier(raw, f"nodes[{i}]")
        if node in node_set:
            raise ValidationError(f"节点重复: {node!r}")
        node_set.add(node)
        nodes.append(node)

    edges: list[dict] = []
    edge_ids: set[str] = set()
    for i, raw in enumerate(raw_edges):
        if not isinstance(raw, dict):
            raise ValidationError(f"edges[{i}] 必须是对象")
        where = f"edges[{i}]"
        edge_id = _clean_identifier(raw.get("id"), f"{where}.id")
        if edge_id in edge_ids:
            raise ValidationError(f"路段编号重复: {edge_id!r}")
        edge_ids.add(edge_id)

        src = _clean_identifier(raw.get("from"), f"{where}.from")
        dst = _clean_identifier(raw.get("to"), f"{where}.to")
        unknown = [n for n in (src, dst) if n not in node_set]
        if unknown:
            raise ValidationError(f"{where} 引用了未定义的节点: {', '.join(unknown)}")

        if "travel_seconds" not in raw:
            raise ValidationError(f"{where} 缺少 travel_seconds")
        seconds = _validate_seconds(raw["travel_seconds"])
        has_stairs = _validate_stairs(raw.get("has_stairs", False))
        # 通行容量：缺省或 null 表示不限，正整数表示可同时占用的人数
        capacity = _validate_capacity(raw.get("capacity"), where) if "capacity" in raw else None

        # 反向通行时间窗：缺省（None）与空数组都表示全程仅允许原方向
        reverse_windows: list[tuple[int, int]] = []
        if "reverse_windows" in raw and raw["reverse_windows"] is not None:
            reverse_windows = _normalize_reverse_windows(
                raw["reverse_windows"], where
            )

        edges.append(
            {
                "id": edge_id,
                "src": src,
                "dst": dst,
                "seconds": seconds,
                "has_stairs": has_stairs,
                "capacity": capacity,
                "reverse_windows": reverse_windows,
            }
        )

    # 允许空图；允许自环（正秒数下不会被最短路采用）
    return {"nodes": nodes, "edges": edges}


def normalize_closures(payload: Any, known_edges: set[str]) -> list[dict]:
    """校验封闭记录负载。

    期望格式::

        {"closures": [
          {"edge_id": "E1", "start": "2026-10-05T08:00:00Z",
           "end": "2026-10-05T10:00:00Z"}, ...]}

    时间区间为半开 ``[start, end)``，必须满足 ``start < end``。
    """
    data = _require_object(payload, "封闭记录")
    raw_closures = data.get("closures")
    if not isinstance(raw_closures, list):
        raise ValidationError("closures 必须是数组")

    closures: list[dict] = []
    for i, raw in enumerate(raw_closures):
        if not isinstance(raw, dict):
            raise ValidationError(f"closures[{i}] 必须是对象")
        where = f"closures[{i}]"
        edge_id = _clean_identifier(raw.get("edge_id"), f"{where}.edge_id")
        if edge_id not in known_edges:
            raise ValidationError(f"{where} 引用了不存在的路段: {edge_id!r}")
        if "start" not in raw:
            raise ValidationError(f"{where} 缺少 start")
        if "end" not in raw:
            raise ValidationError(f"{where} 缺少 end")
        try:
            start = parse_time(raw["start"])
            end = parse_time(raw["end"])
        except TimeError as exc:
            raise ValidationError(f"{where}: {exc}") from None
        if start >= end:
            raise ValidationError(
                f"{where} 无效时间区间: start({raw['start']}) 必须早于 end({raw['end']})"
            )
        closures.append({"edge_id": edge_id, "start": start, "end": end})
    return closures


def normalize_restore(payload: Any) -> int:
    """校验版本恢复请求负载，返回目标版本号。

    期望格式::

        {"version": 2}

    ``version`` 必须是正整数版本号（严格类型：拒绝布尔、浮点、数字字符串）。
    版本 0 表示“尚未发布任何数据”，不是可恢复的历史版本。
    """
    data = _require_object(payload, "恢复请求")
    if "version" not in data:
        raise ValidationError("恢复请求缺少 version 字段")
    value = data["version"]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError("version 必须是正整数版本号")
    if value <= 0:
        raise ValidationError(f"version 必须是正整数版本号，收到 {value}")
    return value


def _normalize_stop(raw: Any, index: int, node_set: set[str]) -> dict:
    """校验一个按顺序停靠点。

    期望格式::

        {"node": "PLAZA",
         "earliest_start": "2026-10-05T10:00:00Z",
         "latest_start":   "2026-10-05T10:30:00Z",
         "stay_seconds": 600}

    时间窗为闭区间 ``[earliest_start, latest_start]``：开始停留不得早于
    ``earliest_start``、不得晚于 ``latest_start``；``stay_seconds`` 为
    非负整数秒。
    """
    where = f"stops[{index}]"
    if not isinstance(raw, dict):
        raise ValidationError(f"{where} 必须是对象")
    for field in ("node", "earliest_start", "latest_start", "stay_seconds"):
        if field not in raw:
            raise ValidationError(f"{where} 缺少必填字段: {field}")

    node = _clean_identifier(raw.get("node"), f"{where}.node")
    if node not in node_set:
        raise ValidationError(f"{where} 引用了未知节点: {node!r}")

    try:
        earliest = parse_time(raw["earliest_start"])
        latest = parse_time(raw["latest_start"])
    except TimeError as exc:
        raise ValidationError(f"{where}: {exc}") from None
    if earliest > latest:
        raise ValidationError(
            f"{where} 无效时间窗: earliest_start({raw['earliest_start']}) "
            f"必须不晚于 latest_start({raw['latest_start']})"
        )

    stay = raw["stay_seconds"]
    # 严格类型：拒绝 bool、float、数字字符串等隐式转换
    if isinstance(stay, bool) or not isinstance(stay, int):
        raise ValidationError(f"{where}.stay_seconds 必须是非负整数")
    if stay < 0:
        raise ValidationError(f"{where}.stay_seconds 必须是非负整数，收到 {stay}")

    return {"node": node, "earliest": earliest, "latest": latest, "stay": stay}


def _normalize_meeting_visitor(raw: Any, index: int, node_set: set[str]) -> dict:
    """校验一名汇合查询访客。

    期望格式::

        {"id": "alice",              # 可选，缺省按序号生成
         "origin": "GATE",
         "departure_time": "2026-10-05T09:00:00Z",   # 最早出发时刻
         "avoid_stairs": false}
    """
    where = f"visitors[{index}]"
    if not isinstance(raw, dict):
        raise ValidationError(f"{where} 必须是对象")
    for field in ("origin", "departure_time"):
        if field not in raw:
            raise ValidationError(f"{where} 缺少必填字段: {field}")

    origin = _clean_identifier(raw.get("origin"), f"{where}.origin")
    if origin not in node_set:
        raise ValidationError(f"{where}.origin 引用了未知节点: {origin!r}")

    try:
        departure = parse_time(raw["departure_time"])
    except TimeError as exc:
        raise ValidationError(f"{where}.departure_time: {exc}") from None

    avoid_stairs = raw.get("avoid_stairs", False)
    if not isinstance(avoid_stairs, bool):
        raise ValidationError(f"{where}.avoid_stairs 必须是布尔值 true/false")

    visitor = {
        "id": index + 1,  # 默认按 1 起始序号
        "origin": origin,
        "departure": departure,
        "avoid_stairs": avoid_stairs,
    }
    if "id" in raw:
        visitor["id"] = _clean_identifier(raw.get("id"), f"{where}.id")
    return visitor


def normalize_meeting(payload: Any, node_set: set[str]) -> dict:
    """校验一次多人汇合点查询负载。

    期望格式::

        {"visitors": [{"origin": ..., "departure_time": ...,
                       "avoid_stairs": ...}, ...],
         "candidates": ["PLAZA", "TOWER"],
         "latest_meeting_time": "2026-10-05T12:00:00Z"}
    """
    data = _require_object(payload, "汇合查询")
    for field in ("visitors", "candidates", "latest_meeting_time"):
        if field not in data:
            raise ValidationError(f"缺少必填字段: {field}")

    raw_visitors = data["visitors"]
    if not isinstance(raw_visitors, list):
        raise ValidationError("visitors 必须是数组")
    if len(raw_visitors) < 2:
        raise ValidationError("visitors 至少需要包含两名访客")
    visitors = [
        _normalize_meeting_visitor(raw, i, node_set)
        for i, raw in enumerate(raw_visitors)
    ]
    seen_ids: set = set()
    for v in visitors:
        if v["id"] in seen_ids:
            raise ValidationError(f"访客标识重复: {v['id']!r}")
        seen_ids.add(v["id"])

    raw_candidates = data["candidates"]
    if not isinstance(raw_candidates, list):
        raise ValidationError("candidates 必须是数组")
    if not raw_candidates:
        raise ValidationError("candidates 不能为空")
    candidates: list[str] = []
    candidate_set: set[str] = set()
    for i, raw in enumerate(raw_candidates):
        node = _clean_identifier(raw, f"candidates[{i}]")
        if node not in node_set:
            raise ValidationError(f"candidates[{i}] 引用了未知节点: {node!r}")
        if node in candidate_set:
            raise ValidationError(f"candidates 存在重复节点: {node!r}")
        candidate_set.add(node)
        candidates.append(node)

    try:
        latest = parse_time(data["latest_meeting_time"])
    except TimeError as exc:
        raise ValidationError(f"latest_meeting_time: {exc}") from None

    return {
        "visitors": visitors,
        "candidates": candidates,
        "latest_meeting_time": latest,
    }


def normalize_query(payload: Any, node_set: set[str]) -> dict:
    """校验一次路线查询负载。"""
    data = _require_object(payload, "查询")
    for field in ("origin", "destination", "departure_time"):
        if field not in data:
            raise ValidationError(f"缺少必填字段: {field}")

    origin = _clean_identifier(data.get("origin"), "origin")
    destination = _clean_identifier(data.get("destination"), "destination")
    for name, node in (("origin", origin), ("destination", destination)):
        if node not in node_set:
            raise ValidationError(f"{name} 引用了未知节点: {node!r}")

    try:
        departure = parse_time(data["departure_time"])
    except TimeError as exc:
        raise ValidationError(f"departure_time: {exc}") from None

    avoid_stairs = data.get("avoid_stairs", False)
    if not isinstance(avoid_stairs, bool):
        raise ValidationError("avoid_stairs 必须是布尔值 true/false")

    raw_stops = data.get("stops", [])
    if not isinstance(raw_stops, list):
        raise ValidationError("stops 必须是数组")
    stops = [_normalize_stop(raw, i, node_set) for i, raw in enumerate(raw_stops)]

    return {
        "origin": origin,
        "destination": destination,
        "departure": departure,
        "avoid_stairs": avoid_stairs,
        "stops": stops,
    }


def normalize_booking(payload: Any, node_set: set[str]) -> dict:
    """校验一次容量确认请求负载。

    期望格式::

        {"origin": "GATE", "destination": "TOWER",
         "departure_time": "2026-10-05T09:55:00Z",
         "avoid_stairs": false,
         "people": 3,
         "data_version": 2}            # 可选：乐观并发版本

    规则与 :func:`normalize_query` 相同（起终点、出发时刻、避台阶；
    不支持停靠点，停靠点字段出现在确认请求中视为非法），另要求：

    - ``people`` 必填，为**正整数**人数（严格类型，拒绝布尔/浮点/字符串）；
    - ``data_version`` 可选，给出时必须为正整数版本号；服务端当前版本与之
      不同时不写入，返回版本冲突，由调用方基于新版本重试。
    """
    data = _require_object(payload, "行程确认")
    for field in ("origin", "destination", "departure_time", "people"):
        if field not in data:
            raise ValidationError(f"缺少必填字段: {field}")
    if "stops" in data:
        raise ValidationError("行程确认不支持 stops 停靠点字段")

    origin = _clean_identifier(data.get("origin"), "origin")
    destination = _clean_identifier(data.get("destination"), "destination")
    for name, node in (("origin", origin), ("destination", destination)):
        if node not in node_set:
            raise ValidationError(f"{name} 引用了未知节点: {node!r}")

    try:
        departure = parse_time(data["departure_time"])
    except TimeError as exc:
        raise ValidationError(f"departure_time: {exc}") from None

    avoid_stairs = data.get("avoid_stairs", False)
    if not isinstance(avoid_stairs, bool):
        raise ValidationError("avoid_stairs 必须是布尔值 true/false")

    people = data["people"]
    if isinstance(people, bool) or not isinstance(people, int):
        raise ValidationError("people 必须是正整数")
    if people <= 0:
        raise ValidationError(f"people 必须为正整数，收到 {people}")

    expected_version = None
    if "data_version" in data and data["data_version"] is not None:
        expected_version = data["data_version"]
        if isinstance(expected_version, bool) or not isinstance(expected_version, int):
            raise ValidationError("data_version 必须是正整数版本号")
        if expected_version <= 0:
            raise ValidationError(
                f"data_version 必须是正整数版本号，收到 {expected_version}"
            )

    return {
        "origin": origin,
        "destination": destination,
        "departure": departure,
        "avoid_stairs": avoid_stairs,
        "people": people,
        "expected_version": expected_version,
    }
