from __future__ import annotations

from typing import List, Mapping

from .models import Settings


def assign_points(new_count: int, current: Mapping[str, int], settings: Settings) -> List[str]:
    points = list(settings.point_values)
    counts = {p: int(current.get(p, 0)) for p in points}
    total_after = sum(counts.values()) + new_count
    if settings.point_mode == "percent":
        whole = sum(settings.point_values.values())
        target = {p: settings.point_values[p] / whole * total_after for p in points}
    else:
        fixed = sum(settings.point_values.values())
        target = {p: float(settings.point_values[p]) for p in points}
        if settings.point_rest is not None:
            target[settings.point_rest] += max(0.0, total_after - fixed)
    result: List[str] = []
    for _ in range(new_count):
        best = max(points, key=lambda p: (target[p] - counts[p], -points.index(p)))
        counts[best] += 1
        result.append(best)
    return result
