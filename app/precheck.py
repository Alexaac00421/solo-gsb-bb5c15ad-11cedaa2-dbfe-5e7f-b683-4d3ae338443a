"""联合预检：候选步道图 + 封闭记录的只读批量校验。

与导入接口（``POST /admin/graph``、``POST /admin/closures``）使用**相同**
的校验规则（字段、时间区间、节点与路段引用、反向窗重叠），但有两点不同：

- 收集**全部**可判定错误并给出原数组位置，而不是遇到第一个错误就中止；
- 封闭记录的路段引用针对**本次提交的候选图**校验，与库中当前版本无关；
  候选图自身无效时，路段引用不可判定——跳过该项校验（不误报），封闭
  记录自身的字段/时间区间错误仍照常报告。

预检为纯函数，不触碰数据库，任何结果都不会改变当前数据版本。
"""
from typing import Any, Optional

# 复用 schemas 的基础校验函数，保证预检与导入的错误口径一致
from .schemas import (
    ValidationError,
    _clean_identifier,
    _validate_capacity,
    _validate_seconds,
    _validate_stairs,
)
from .timeutil import TimeError, parse_time


def _add(errors: list[dict], source: str, path: str, index: Optional[int], message: str) -> None:
    """追加一条错误。``index`` 为 path 中最内层数组元素的下标（无则 None）。"""
    errors.append({"source": source, "path": path, "index": index, "message": message})


def _collect_reverse_windows(errors: list[dict], raw_edge: dict, where: str, edge_index: int) -> list[tuple[int, int]]:
    """校验一条路段的反向通行时间窗数组（收集全部错误），返回有效窗列表。"""
    raw = raw_edge.get("reverse_windows")
    if raw is None:
        return []  # 缺省或 null：全程仅允许原方向
    if not isinstance(raw, list):
        _add(errors, "graph", f"{where}.reverse_windows", edge_index,
             f"{where}.reverse_windows 必须是数组")
        return []

    windows: list[tuple[int, int]] = []
    for j, item in enumerate(raw):
        wwhere = f"{where}.reverse_windows[{j}]"
        if not isinstance(item, dict):
            _add(errors, "graph", wwhere, j, f"{wwhere} 必须是对象")
            continue
        missing = False
        for field in ("start", "end"):
            if field not in item:
                _add(errors, "graph", f"{wwhere}.{field}", j, f"{wwhere} 缺少 {field}")
                missing = True
        if missing:
            continue
        start = end = None
        try:
            start = parse_time(item["start"])
        except TimeError as exc:
            _add(errors, "graph", f"{wwhere}.start", j, f"{wwhere}: {exc}")
        try:
            end = parse_time(item["end"])
        except TimeError as exc:
            _add(errors, "graph", f"{wwhere}.end", j, f"{wwhere}: {exc}")
        if start is None or end is None:
            continue
        if start >= end:
            _add(errors, "graph", wwhere, j,
                 f"{wwhere} 无效时间窗: start({item['start']}) 必须早于 end({item['end']})")
            continue
        windows.append((start, end))

    # 重叠判定只对各自有效的窗进行（半开区间端点相接允许）
    ordered = sorted(windows)
    for (s1, e1), (s2, e2) in zip(ordered, ordered[1:]):
        if e1 > s2:
            _add(errors, "graph", f"{where}.reverse_windows", edge_index,
                 f"{where} 的反向时间窗存在重叠（半开区间仅允许端点相接）: "
                 f"[{s1}, {e1}) 与 [{s2}, {e2}) 重叠")
    return windows


def collect_graph_errors(payload: Any) -> tuple[list[dict], Optional[dict]]:
    """按导入规则校验候选步道图，收集全部可判定错误。

    返回 ``(errors, graph)``：无错误时 ``graph`` 为规范化结果（结构同
    :func:`app.schemas.normalize_graph`），否则为 ``None``。
    """
    errors: list[dict] = []
    source = "graph"
    if not isinstance(payload, dict):
        _add(errors, source, "", None, "步道图必须是 JSON 对象")
        return errors, None

    # 节点：节点数组本身非法时，路段的节点引用不可判定，跳过引用校验
    nodes: list[str] = []
    node_set: Optional[set[str]] = set()
    raw_nodes = payload.get("nodes")
    if not isinstance(raw_nodes, list):
        _add(errors, source, "nodes", None, "nodes 必须是数组")
        node_set = None
    else:
        for i, raw in enumerate(raw_nodes):
            try:
                node = _clean_identifier(raw, f"nodes[{i}]")
            except ValidationError as exc:
                _add(errors, source, f"nodes[{i}]", i, str(exc))
                continue
            if node in node_set:
                _add(errors, source, f"nodes[{i}]", i, f"节点重复: {node!r}")
            node_set.add(node)  # 重复名仍视为已定义，供路段引用校验
            nodes.append(node)

    edges: list[dict] = []
    edge_ids: set[str] = set()
    raw_edges = payload.get("edges")
    if not isinstance(raw_edges, list):
        _add(errors, source, "edges", None, "edges 必须是数组")
    else:
        for i, raw in enumerate(raw_edges):
            where = f"edges[{i}]"
            if not isinstance(raw, dict):
                _add(errors, source, where, i, f"{where} 必须是对象")
                continue

            edge_id: Optional[str] = None
            try:
                edge_id = _clean_identifier(raw.get("id"), f"{where}.id")
            except ValidationError as exc:
                _add(errors, source, f"{where}.id", i, str(exc))
            else:
                if edge_id in edge_ids:
                    _add(errors, source, f"{where}.id", i, f"路段编号重复: {edge_id!r}")
                edge_ids.add(edge_id)

            endpoints: dict[str, Optional[str]] = {}
            for field in ("from", "to"):
                try:
                    value = _clean_identifier(raw.get(field), f"{where}.{field}")
                except ValidationError as exc:
                    _add(errors, source, f"{where}.{field}", i, str(exc))
                    value = None
                else:
                    if node_set is not None and value not in node_set:
                        _add(errors, source, f"{where}.{field}", i,
                             f"{where} 引用了未定义的节点: {value}")
                endpoints[field] = value

            seconds: Optional[int] = None
            if "travel_seconds" not in raw:
                _add(errors, source, f"{where}.travel_seconds", i,
                     f"{where} 缺少 travel_seconds")
            else:
                try:
                    seconds = _validate_seconds(raw["travel_seconds"])
                except ValidationError as exc:
                    _add(errors, source, f"{where}.travel_seconds", i, str(exc))

            has_stairs = False
            try:
                has_stairs = _validate_stairs(raw.get("has_stairs", False))
            except ValidationError as exc:
                _add(errors, source, f"{where}.has_stairs", i, str(exc))

            # 通行容量：缺省/null 不限；出现时必须为正整数
            capacity = None
            if "capacity" in raw:
                try:
                    capacity = _validate_capacity(raw["capacity"], where)
                except ValidationError as exc:
                    _add(errors, source, f"{where}.capacity", i, str(exc))

            windows = _collect_reverse_windows(errors, raw, where, i)

            edges.append(
                {
                    "id": edge_id,
                    "src": endpoints["from"],
                    "dst": endpoints["to"],
                    "seconds": seconds,
                    "has_stairs": has_stairs,
                    "capacity": capacity,
                    "reverse_windows": windows,
                }
            )

    if errors:
        return errors, None
    return errors, {"nodes": nodes, "edges": edges}


def collect_closure_errors(payload: Any, known_edges: Optional[set[str]]) -> tuple[list[dict], Optional[list[dict]]]:
    """按导入规则校验候选封闭记录，收集全部可判定错误。

    ``known_edges`` 为候选图的路段编号集合；为 ``None``（候选图无效）时
    路段引用不可判定，跳过引用校验（不误报），其余检查照常。
    返回 ``(errors, closures)``：无错误时 ``closures`` 为规范化列表
    （结构同 :func:`app.schemas.normalize_closures`），否则为 ``None``。
    """
    errors: list[dict] = []
    source = "closures"
    if not isinstance(payload, dict):
        _add(errors, source, "", None, "封闭记录必须是 JSON 对象")
        return errors, None
    raw_closures = payload.get("closures")
    if not isinstance(raw_closures, list):
        _add(errors, source, "closures", None, "closures 必须是数组")
        return errors, None

    closures: list[dict] = []
    for i, raw in enumerate(raw_closures):
        where = f"closures[{i}]"
        if not isinstance(raw, dict):
            _add(errors, source, where, i, f"{where} 必须是对象")
            continue

        edge_id: Optional[str] = None
        try:
            edge_id = _clean_identifier(raw.get("edge_id"), f"{where}.edge_id")
        except ValidationError as exc:
            _add(errors, source, f"{where}.edge_id", i, str(exc))
        else:
            if known_edges is not None and edge_id not in known_edges:
                _add(errors, source, f"{where}.edge_id", i,
                     f"{where} 引用了不存在的路段: {edge_id!r}")

        missing = False
        for field in ("start", "end"):
            if field not in raw:
                _add(errors, source, f"{where}.{field}", i, f"{where} 缺少 {field}")
                missing = True
        if missing:
            continue
        start = end = None
        try:
            start = parse_time(raw["start"])
        except TimeError as exc:
            _add(errors, source, f"{where}.start", i, f"{where}: {exc}")
        try:
            end = parse_time(raw["end"])
        except TimeError as exc:
            _add(errors, source, f"{where}.end", i, f"{where}: {exc}")
        if start is None or end is None:
            continue
        if start >= end:
            _add(errors, source, where, i,
                 f"{where} 无效时间区间: start({raw['start']}) 必须早于 end({raw['end']})")
            continue
        closures.append({"edge_id": edge_id, "start": start, "end": end})

    if errors:
        return errors, None
    return errors, closures


def precheck(payload: Any) -> dict:
    """联合预检入口：校验 ``{"graph": ..., "closures": ...}`` 负载。

    返回报告字典::

        {"ok": bool,
         "errors": [{"source", "path", "index", "message"}, ...],
         "counts": {"nodes", "edges", "closures", "reverse_windows"}}  # 仅 ok 时携带

    包络非法（不是 JSON 对象、缺少 ``graph``/``closures``）抛
    :class:`ValidationError`，由路由层映射为 400。
    """
    if not isinstance(payload, dict):
        raise ValidationError("预检请求必须是 JSON 对象")
    for field in ("graph", "closures"):
        if field not in payload:
            raise ValidationError(f"缺少必填字段: {field}")

    graph_errors, graph = collect_graph_errors(payload["graph"])
    # 候选图无效时，封闭记录的路段引用不可判定：跳过引用校验，其余照常
    known_edges = {e["id"] for e in graph["edges"]} if graph is not None else None
    closure_errors, closures = collect_closure_errors(payload["closures"], known_edges)

    errors = graph_errors + closure_errors
    report: dict[str, Any] = {"ok": not errors, "errors": errors}
    if not errors:
        # 无错误时 graph/closures 必为规范化结果
        assert graph is not None and closures is not None
        report["counts"] = {
            "nodes": len(graph["nodes"]),
            "edges": len(graph["edges"]),
            "closures": len(closures),
            "reverse_windows": sum(len(e["reverse_windows"]) for e in graph["edges"]),
        }
    return report
