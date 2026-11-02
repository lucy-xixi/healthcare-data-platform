"""Classify and parse Synthea clinical time text.

Accepted shapes are the empty string, ``YYYY-MM-DD``, and
``YYYY-MM-DDTHH:MM:SSZ``. A previous full-file scan is not an allow-list for a
later snapshot.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Literal

from healthcare_data_platform.ingest.synthea.errors import INVALID_TIME, TimeParseFailure

TimeCategory = Literal["empty", "date", "utc_datetime", "invalid_calendar_date", "other"]

_DATE_SHAPE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_UTC_SHAPE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_UTC = dt.timezone.utc


@dataclass(frozen=True)
class ParsedClinicalTime:
    raw_text: str
    precision: str
    calendar_date: dt.date | None
    clock_time: dt.time | None
    instant_utc: dt.datetime | None
    timezone_known: bool | None


def classify_time_cell(raw_text: str) -> TimeCategory:
    """Classify one original cell. The cell is not rewritten."""
    if raw_text == "":
        return "empty"
    if _DATE_SHAPE.fullmatch(raw_text):
        if _valid_date(raw_text):
            return "date"
        return "invalid_calendar_date"
    if _UTC_SHAPE.fullmatch(raw_text):
        if _valid_utc(raw_text):
            return "utc_datetime"
        return "invalid_calendar_date"
    return "other"


def parse_clinical_time(
    raw_text: str,
    *,
    filename: str | None = None,
    source_table: str | None = None,
    field_name: str | None = None,
    source_row_number: int | None = None,
) -> ParsedClinicalTime | TimeParseFailure:
    category = classify_time_cell(raw_text)
    if category == "empty":
        return ParsedClinicalTime(
            raw_text=raw_text,
            precision="absent",
            calendar_date=None,
            clock_time=None,
            instant_utc=None,
            timezone_known=None,
        )
    if category == "date":
        year, month, day = (int(part) for part in raw_text.split("-"))
        return ParsedClinicalTime(
            raw_text=raw_text,
            precision="date",
            calendar_date=dt.date(year, month, day),
            clock_time=None,
            instant_utc=None,
            timezone_known=False,
        )
    if category == "utc_datetime":
        year = int(raw_text[0:4])
        month = int(raw_text[5:7])
        day = int(raw_text[8:10])
        hour = int(raw_text[11:13])
        minute = int(raw_text[14:16])
        second = int(raw_text[17:19])
        return ParsedClinicalTime(
            raw_text=raw_text,
            precision="datetime",
            calendar_date=dt.date(year, month, day),
            clock_time=dt.time(hour, minute, second),
            instant_utc=dt.datetime(year, month, day, hour, minute, second, tzinfo=_UTC),
            timezone_known=True,
        )
    return TimeParseFailure(
        code=INVALID_TIME,
        raw_text=raw_text,
        message=_invalid_message(category, raw_text, filename, field_name, source_row_number),
        filename=filename,
        source_table=source_table,
        field_name=field_name,
        source_row_number=source_row_number,
    )


def _valid_date(raw_text: str) -> bool:
    try:
        parsed = dt.datetime.strptime(raw_text, "%Y-%m-%d")
    except ValueError:
        return False
    return parsed.strftime("%Y-%m-%d") == raw_text


def _valid_utc(raw_text: str) -> bool:
    try:
        parsed = dt.datetime.strptime(raw_text, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ") == raw_text


def _invalid_message(
    category: str,
    raw_text: str,
    filename: str | None,
    field_name: str | None,
    source_row_number: int | None,
) -> str:
    reason = (
        "calendar or clock value is not valid"
        if category == "invalid_calendar_date"
        else "time text is not a registered Synthea format"
    )
    return (
        f"{INVALID_TIME}: {filename}:{field_name}:row {source_row_number}: "
        f"{raw_text!r} ({reason})"
    )
