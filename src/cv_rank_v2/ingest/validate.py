from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .models import ValidationEnvelope, ValidationMessage
from .normalize import build_header_map


def validate_csv_file(path: str | Path) -> ValidationEnvelope:
    path = Path(path)
    messages: list[ValidationMessage] = []

    if not path.exists():
        messages.append(
            ValidationMessage(level="error", code="missing_file", message=f"File not found: {path}")
        )
        return ValidationEnvelope(source=str(path), row_count=0, accepted_count=0, skipped_count=0, messages=tuple(messages))
    if not path.is_file():
        messages.append(
            ValidationMessage(level="error", code="not_file", message=f"Not a file: {path}")
        )
        return ValidationEnvelope(source=str(path), row_count=0, accepted_count=0, skipped_count=0, messages=tuple(messages))

    try:
        import csv

        with path.open(encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            headers = reader.fieldnames or []
            rows = list(reader)
    except Exception as exc:  # pragma: no cover - defensive guard
        messages.append(
            ValidationMessage(level="error", code="read_error", message=f"Cannot read CSV: {exc}")
        )
        return ValidationEnvelope(source=str(path), row_count=0, accepted_count=0, skipped_count=0, messages=tuple(messages))

    if not headers:
        messages.append(ValidationMessage(level="error", code="no_headers", message="CSV has no headers."))
        return ValidationEnvelope(source=str(path), row_count=0, accepted_count=0, skipped_count=0, messages=tuple(messages))
    if not rows:
        messages.append(
            ValidationMessage(level="warning", code="no_rows", message="CSV has headers but no data rows.")
        )
        return ValidationEnvelope(source=str(path), row_count=0, accepted_count=0, skipped_count=0, messages=tuple(messages))

    header_map = build_header_map(headers)
    has_name = "name" in header_map
    has_first_last = "first_name" in header_map and "last_name" in header_map
    has_email = "email" in header_map

    if not (has_name or has_first_last or has_email):
        messages.append(
            ValidationMessage(
                level="error",
                code="missing_identity_columns",
                message="Need a name column, first_name/last_name pair, or email column.",
            )
        )

    if not has_email:
        messages.append(
            ValidationMessage(
                level="warning",
                code="no_email_column",
                message="No email column detected; applicant_id will fall back to other stable fields.",
            )
        )

    return ValidationEnvelope(
        source=str(path),
        row_count=len(rows),
        accepted_count=0,
        skipped_count=0,
        messages=tuple(messages),
    )


def validate_headers(headers: Iterable[str]) -> ValidationEnvelope:
    header_map = build_header_map(list(headers))
    messages: list[ValidationMessage] = []
    if not header_map:
        messages.append(
            ValidationMessage(level="error", code="no_known_columns", message="No known applicant columns were detected.")
        )
    return ValidationEnvelope(source="headers", row_count=0, accepted_count=0, skipped_count=0, messages=tuple(messages))

