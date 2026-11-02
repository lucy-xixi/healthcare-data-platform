"""Structure and time error values for the Synthea phase-one adapter."""

from __future__ import annotations

from dataclasses import dataclass

SNAPSHOT_FILE_MISSING = "SNAPSHOT_FILE_MISSING"
SNAPSHOT_HEADER_MISMATCH = "SNAPSHOT_HEADER_MISMATCH"
SNAPSHOT_EVIDENCE_CLASS_MISSING = "SNAPSHOT_EVIDENCE_CLASS_MISSING"
SNAPSHOT_EVIDENCE_CLASS_CONFLICT = "SNAPSHOT_EVIDENCE_CLASS_CONFLICT"
SNAPSHOT_FILE_UNREADABLE = "SNAPSHOT_FILE_UNREADABLE"
INVALID_TIME = "INVALID_TIME"
RAW_ROW_WIDTH_MISMATCH = "RAW_ROW_WIDTH_MISMATCH"


@dataclass(frozen=True)
class StructureError:
    code: str
    message: str
    filename: str | None = None
    field_name: str | None = None


@dataclass(frozen=True)
class RowWidthError:
    """A data row whose field count differs from the frozen header.

    The raw cells are the CSV record as read. They are not padded or truncated.
    """

    code: str
    mismatch: str
    message: str
    source_system: str
    source_table: str
    filename: str
    source_file_id: str
    source_row_number: int
    actual_column_count: int
    expected_column_count: int
    raw_values: tuple[str, ...]


@dataclass(frozen=True)
class TimeParseFailure:
    code: str
    raw_text: str
    message: str
    filename: str | None = None
    source_table: str | None = None
    field_name: str | None = None
    source_row_number: int | None = None
