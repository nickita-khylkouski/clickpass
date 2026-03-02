from __future__ import annotations

from typing import Any


def compare_analyses(baseline: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    baseline_map = _index(baseline)
    current_map = _index(current)

    baseline_keys = set(baseline_map)
    current_keys = set(current_map)

    removed = sorted(baseline_keys - current_keys)
    added = sorted(current_keys - baseline_keys)
    changed_statuses: list[dict[str, Any]] = []
    for key in sorted(baseline_keys & current_keys):
        b = set((baseline_map[key].get("statuses") or []))
        c = set((current_map[key].get("statuses") or []))
        if b != c:
            changed_statuses.append(
                {
                    "endpoint": _render_key(key),
                    "baseline_statuses": sorted(b),
                    "current_statuses": sorted(c),
                }
            )

    return {
        "summary": {
            "removed_count": len(removed),
            "added_count": len(added),
            "changed_status_count": len(changed_statuses),
            "breaking_count": len(removed) + len(changed_statuses),
        },
        "removed": [_render_key(k) for k in removed],
        "added": [_render_key(k) for k in added],
        "changed_statuses": changed_statuses,
    }


def _index(analysis: dict[str, Any]) -> dict[tuple[str, str, str], dict[str, Any]]:
    out: dict[tuple[str, str, str], dict[str, Any]] = {}
    for ep in analysis.get("endpoints") or []:
        key = (ep["method"], ep["host"], ep["path_template"])
        out[key] = ep
    return out


def _render_key(key: tuple[str, str, str]) -> str:
    method, host, path = key
    return f"{method} {host}{path}"

