"""时间工具。

对外统一使用 ISO 8601 字符串（例如 ``2026-10-05T09:00:00Z``）；
不带时区偏移的时间一律按 UTC 处理。内部全部换算成 UTC 纪元整秒。

封闭记录为半开区间 ``[start, end)``：``end`` 时刻该路段立即恢复通行，
允许恰好在 ``end`` 时刻进入；``start`` 时刻该路段已经封闭。
"""
from datetime import datetime, timezone


class TimeError(ValueError):
    """时间格式或时间区间非法。"""


def parse_time(value) -> int:
    """把输入解析为 UTC 纪元整秒。

    接受：
    - 带 ``Z`` 或偏移量的 ISO 8601 字符串；
    - 不带时区的 ISO 8601 字符串（按 UTC 解释）；
    - 非负整数（视为纪元秒）。

    拒绝：布尔、浮点、带小数秒的字符串以及无法解析的内容。
    """
    if isinstance(value, bool):
        raise TimeError("时间不能是布尔值")
    if isinstance(value, int):
        if value < 0:
            raise TimeError("纪元秒不能为负数")
        return value
    if not isinstance(value, str):
        raise TimeError(f"无法解析的时间: {value!r}")

    text = value.strip()
    if not text:
        raise TimeError("时间字符串为空")
    # 只接受整秒，避免出现半开边界上的歧义
    if "." in text or "," in text:
        raise TimeError(f"时间必须为整秒，不支持小数秒: {value!r}")

    iso = text
    if iso.endswith("Z") or iso.endswith("z"):
        iso = iso[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        raise TimeError(f"无法解析的 ISO 8601 时间: {value!r}") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def format_time(epoch: int) -> str:
    """纪元秒格式化为 UTC ISO 8601（``Z`` 结尾）。"""
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
