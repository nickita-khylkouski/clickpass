from __future__ import annotations

import csv
from pathlib import Path

from .models import IngestResult, ValidationEnvelope, ValidationMessage
from .normalize import build_header_map, normalize_row
from .validate import validate_csv_file


def load_csv(path: str | Path) -> IngestResult:
    path = Path(path)
    base_validation = validate_csv_file(path)
    if any(message.level == "error" for message in base_validation.messages):
        return IngestResult(applicants=(), validation=base_validation)

    messages = list(base_validation.messages)
    applicants = []
    seen_ids: set[str] = set()

    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        headers = reader.fieldnames or []
        header_map = build_header_map(headers)

        for row_number, row in enumerate(reader, start=2):
            applicant, row_messages = normalize_row(row, header_map, source_name=str(path), row_number=row_number)
            messages.extend(row_messages)
            if applicant is None:
                continue
            if applicant.applicant_id in seen_ids:
                messages.append(
                    ValidationMessage(
                        level="warning",
                        code="duplicate_applicant",
                        message="Duplicate applicant_id encountered; later row skipped.",
                        row_number=row_number,
                    )
                )
                continue
            seen_ids.add(applicant.applicant_id)
            applicants.append(applicant)

    validation = ValidationEnvelope(
        source=str(path),
        row_count=base_validation.row_count,
        accepted_count=len(applicants),
        skipped_count=max(base_validation.row_count - len(applicants), 0),
        messages=tuple(messages),
    )
    return IngestResult(applicants=tuple(applicants), validation=validation)

