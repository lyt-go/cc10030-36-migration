"""终端 × 日期区间工具：区间合并、覆盖判定与缺口计算。

区间统一用 ISO 日期字符串（YYYY-MM-DD）表示闭区间，按终端维度分别计算，
字符串字典序与日期先后顺序一致，可直接比较。
"""
from __future__ import annotations

from typing import Any


def merge_intervals(intervals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """合并同终端下互相重叠或首尾相接的区间。"""
    by_terminal: dict[str, list[tuple[str, str]]] = {}
    for item in intervals:
        terminal = item.get("terminal") or "_ANY"
        by_terminal.setdefault(terminal, []).append(
            (item["start_date"], item["end_date"]))

    merged: list[dict[str, Any]] = []
    for terminal, ranges in by_terminal.items():
        ranges.sort()
        cur_start, cur_end = ranges[0]
        for start, end in ranges[1:]:
            if start <= cur_end:  # 重叠或相邻（按天连续）
                cur_end = max(cur_end, end)
            else:
                merged.append({"terminal": terminal, "start_date": cur_start, "end_date": cur_end})
                cur_start, cur_end = start, end
        merged.append({"terminal": terminal, "start_date": cur_start, "end_date": cur_end})
    return merged


def is_covered(covered: list[dict[str, Any]], terminal: str | None,
               start_date: str, end_date: str) -> bool:
    """判断某终端的目标闭区间是否已被已核验区间完全覆盖。"""
    for item in covered:
        if item.get("terminal") not in (terminal, "_ANY", None):
            continue
        if item["start_date"] <= start_date and item["end_date"] >= end_date:
            return True
    return False


def uncovered_pieces(covered: list[dict[str, Any]], terminal: str | None,
                     start_date: str, end_date: str) -> list[dict[str, Any]]:
    """计算目标区间中未被已核验区间覆盖的剩余片段（按天闭区间）。"""
    same_terminal = [
        (item["start_date"], item["end_date"])
        for item in covered
        if item.get("terminal") in (terminal, "_ANY", None)
    ]
    same_terminal.sort()

    pieces: list[tuple[str, str]] = []
    cursor = start_date
    for cov_start, cov_end in same_terminal:
        if cov_end < cursor:
            continue
        if cov_start > cursor:
            pieces.append((cursor, _day_before(cov_start)))
        cursor = max(cursor, _day_after(cov_end))
        if cursor > end_date:
            break
    if cursor <= end_date:
        pieces.append((cursor, end_date))

    return [
        {"terminal": terminal, "start_date": s, "end_date": e}
        for s, e in pieces
    ]


def _day_before(day: str) -> str:
    import datetime
    return (datetime.date.fromisoformat(day) - datetime.timedelta(days=1)).isoformat()


def _day_after(day: str) -> str:
    import datetime
    return (datetime.date.fromisoformat(day) + datetime.timedelta(days=1)).isoformat()
