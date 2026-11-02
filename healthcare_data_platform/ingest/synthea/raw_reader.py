"""Read Synthea CSV data rows as original cell text."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RawDataRow:
    source_row_number: int
    raw_values: tuple[str, ...]


def iterate_data_rows(path: Path):
    """Stream one CSV.

    The returned generator yields ``RawDataRow`` values and closes the file
    when exhausted or closed. Cell text is not trimmed, padded, or truncated.
    """

    handle = path.open("r", encoding="utf-8", newline="")
    reader = csv.reader(handle)
    try:
        header = tuple(next(reader))
    except StopIteration:
        handle.close()
        return (), iter(())

    def generate():
        try:
            for number, record in enumerate(reader, start=1):
                yield RawDataRow(source_row_number=number, raw_values=tuple(record))
        finally:
            handle.close()

    return header, generate()


def read_header_and_rows(path: Path) -> tuple[tuple[str, ...], tuple[RawDataRow, ...]]:
    """Return the header and 1-based data rows.

    ``source_row_number`` counts data records only. The header row is not a
    data row. Cell text is the CSV field value, without trimming.

    ``csv.reader`` keeps the field count it parsed. A short record stays short
    and extra fields stay present. This reader does not pad or drop cells.
    """
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        try:
            header = tuple(next(reader))
        except StopIteration:
            return (), ()
        rows: list[RawDataRow] = []
        for number, record in enumerate(reader, start=1):
            rows.append(RawDataRow(source_row_number=number, raw_values=tuple(record)))
    return header, tuple(rows)
