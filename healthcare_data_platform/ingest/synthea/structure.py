"""Snapshot file and header checks.

These checks read file bytes and the header record only. They do not iterate
data rows.
"""

from __future__ import annotations

import csv
from pathlib import Path

from healthcare_data_platform.ingest.synthea.contract import (
    FILE_HEADERS,
    REQUIRED_EVIDENCE_CLASS,
    REQUIRED_FILENAMES,
    UTF8_BOM,
)
from healthcare_data_platform.ingest.synthea.errors import (
    SNAPSHOT_EVIDENCE_CLASS_CONFLICT,
    SNAPSHOT_EVIDENCE_CLASS_MISSING,
    SNAPSHOT_FILE_MISSING,
    SNAPSHOT_FILE_UNREADABLE,
    SNAPSHOT_HEADER_MISMATCH,
    StructureError,
)


def evidence_class_errors(evidence_class: str | None) -> list[StructureError]:
    if evidence_class is None or evidence_class == "":
        return [
            StructureError(
                code=SNAPSHOT_EVIDENCE_CLASS_MISSING,
                message="evidence_class must be declared explicitly as synthetic_synthea",
            )
        ]
    if evidence_class != REQUIRED_EVIDENCE_CLASS:
        return [
            StructureError(
                code=SNAPSHOT_EVIDENCE_CLASS_CONFLICT,
                message=(
                    "evidence_class "
                    f"{evidence_class!r} does not match required {REQUIRED_EVIDENCE_CLASS!r}"
                ),
            )
        ]
    return []


def inspect_snapshot_files(snapshot_dir: Path) -> list[StructureError]:
    """Return every structure error visible without reading data rows."""
    errors: list[StructureError] = []
    for filename in REQUIRED_FILENAMES:
        path = snapshot_dir / filename
        if not path.is_file():
            errors.append(
                StructureError(
                    code=SNAPSHOT_FILE_MISSING,
                    message=f"Required snapshot file is missing: {filename}",
                    filename=filename,
                )
            )
            continue
        errors.extend(_inspect_existing_file(path, filename))
    return errors


def _inspect_existing_file(path: Path, filename: str) -> list[StructureError]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return [
            StructureError(
                code=SNAPSHOT_FILE_UNREADABLE,
                message=f"Cannot read {filename}: {exc}",
                filename=filename,
            )
        ]
    errors: list[StructureError] = []
    if raw.startswith(UTF8_BOM):
        errors.append(
            StructureError(
                code=SNAPSHOT_HEADER_MISMATCH,
                message="UTF-8 BOM is not accepted for a Synthea snapshot CSV",
                filename=filename,
            )
        )
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        errors.append(
            StructureError(
                code=SNAPSHOT_FILE_UNREADABLE,
                message=f"Cannot decode {filename} as UTF-8: {exc}",
                filename=filename,
            )
        )
        return errors
    header = _first_record(text)
    expected = list(FILE_HEADERS[filename])
    if header != expected:
        errors.append(
            StructureError(
                code=SNAPSHOT_HEADER_MISMATCH,
                message=f"Header of {filename} does not match the frozen field contract",
                filename=filename,
            )
        )
    return errors


def _first_record(text: str) -> list[str]:
    if text == "":
        return []
    first_line = text.splitlines()[0] if text.splitlines() else ""
    try:
        return next(csv.reader([first_line]))
    except StopIteration:
        return []
