from __future__ import annotations

from collections import defaultdict

from ghostapi.models import Endpoint


def merge_endpoints(endpoints: list[Endpoint]) -> list[Endpoint]:
    grouped: dict[str, list[Endpoint]] = defaultdict(list)
    for ep in endpoints:
        grouped[ep.key()].append(ep)

    merged: list[Endpoint] = []
    for key, items in grouped.items():
        base = items[0]
        sources = sorted({i.source for i in items})
        notes = []
        for i in items:
            notes.extend(i.notes)
        source_note = ",".join(sources)
        merged.append(
            Endpoint(
                method=base.method,
                url=base.url,
                path=base.path,
                source=source_note,
                status=base.status,
                content_type=base.content_type,
                notes=sorted(set(notes)),
            )
        )
    merged.sort(key=lambda e: (e.path, e.method))
    return merged

