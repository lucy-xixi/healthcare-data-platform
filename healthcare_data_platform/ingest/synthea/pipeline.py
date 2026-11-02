"""Phase-one Synthea snapshot assessment.

The assessment prepares audit data. It does not write canonical entities,
origin links, quarantine decisions, or an import-batch transaction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from healthcare_data_platform.ingest.synthea.contract import (
    CLINICAL_TIME_FIELDS,
    FILE_HEADERS,
    REQUIRED_FILENAMES,
    SOURCE_RECORD_ID_FIELD,
    SOURCE_SYSTEM,
    source_table_for,
)
from healthcare_data_platform.ingest.synthea.errors import (
    INVALID_TIME,
    RAW_ROW_WIDTH_MISMATCH,
    RowWidthError,
    StructureError,
    TimeParseFailure,
)
from healthcare_data_platform.ingest.synthea.fingerprint import source_row_fingerprint
from healthcare_data_platform.ingest.synthea.hashing import sha256_file
from healthcare_data_platform.ingest.synthea.raw_reader import read_header_and_rows
from healthcare_data_platform.ingest.synthea.replay import (
    HistoricalSnapshot,
    ReplayDecision,
    SnapshotHistoryQuery,
    SnapshotIdentity,
    classify_replay,
)
from healthcare_data_platform.ingest.synthea.structure import (
    evidence_class_errors,
    inspect_snapshot_files,
)
from healthcare_data_platform.ingest.synthea.time_parse import (
    ParsedClinicalTime,
    classify_time_cell,
    parse_clinical_time,
)


@dataclass(frozen=True)
class SourceFileIdentity:
    filename: str
    source_table: str
    sha256: str
    source_file_id: str


@dataclass(frozen=True)
class SourceRowAudit:
    source_file_id: str
    filename: str
    source_system: str
    source_table: str
    source_row_number: int
    header: tuple[str, ...]
    raw_values: tuple[str, ...]
    fingerprint: str
    source_record_id: str | None
    duplicate_in_file: bool


@dataclass(frozen=True)
class DuplicatePayloadGroup:
    filename: str
    source_table: str
    fingerprint: str
    source_row_numbers: tuple[int, ...]


@dataclass(frozen=True)
class TimeScanCount:
    filename: str
    field_name: str
    empty: int = 0
    date: int = 0
    utc_datetime: int = 0
    invalid_calendar_date: int = 0
    other: int = 0


@dataclass(frozen=True)
class TimeCellOutcome:
    filename: str
    source_table: str
    field_name: str
    source_row_number: int
    category: str
    parsed: ParsedClinicalTime | None
    failure: TimeParseFailure | None


@dataclass(frozen=True)
class PhaseOneAssessment:
    """In-memory phase-one result. Nothing here has been committed to storage."""

    outcome: str
    evidence_class: str | None
    structure_errors: tuple[StructureError, ...] = ()
    file_identities: tuple[SourceFileIdentity, ...] = ()
    replay: ReplayDecision | None = None
    rows: tuple[SourceRowAudit, ...] = ()
    row_width_errors: tuple[RowWidthError, ...] = ()
    duplicate_groups: tuple[DuplicatePayloadGroup, ...] = ()
    time_scan: tuple[TimeScanCount, ...] = ()
    time_outcomes: tuple[TimeCellOutcome, ...] = ()
    time_parse_performed: bool = False
    row_processing_performed: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)


def assess_synthea_snapshot(
    snapshot_dir: Path,
    evidence_class: str | None,
    history: SnapshotHistoryQuery | None = None,
) -> PhaseOneAssessment:
    """Validate one configured snapshot directory and prepare raw-row audit data.

    ``evidence_class`` has no default. ``history`` is optional: when it is
    omitted, replay is reported as not configured rather than as a stored lookup.
    """
    structure_errors = [
        *evidence_class_errors(evidence_class),
        *inspect_snapshot_files(snapshot_dir),
    ]
    if structure_errors:
        return PhaseOneAssessment(
            outcome="structure_failed",
            evidence_class=evidence_class,
            structure_errors=tuple(structure_errors),
            notes=("No data rows were read because structure validation failed.",),
        )

    assert evidence_class is not None
    identities = _file_identities(snapshot_dir)
    identity = SnapshotIdentity.from_mapping(
        source_system=SOURCE_SYSTEM,
        evidence_class=evidence_class,
        file_hashes={item.filename: item.sha256 for item in identities},
    )
    replay = _replay_from_port(identity, history)
    if replay.is_replay:
        return PhaseOneAssessment(
            outcome="replayed",
            evidence_class=evidence_class,
            file_identities=identities,
            replay=replay,
            notes=(
                "Matched a succeeded historical snapshot. "
                "Row preparation was not repeated.",
            ),
        )

    rows, row_width_errors = _audit_rows(snapshot_dir, identities)
    duplicates = _duplicate_groups(rows)
    rows = _mark_duplicates(rows, duplicates)
    scan, blocked = _scan_times(rows)
    if blocked:
        failures = _unregistered_failures(rows)
        return PhaseOneAssessment(
            outcome="time_parse_blocked",
            evidence_class=evidence_class,
            file_identities=identities,
            replay=replay,
            rows=rows,
            row_width_errors=row_width_errors,
            duplicate_groups=duplicates,
            time_scan=scan,
            time_outcomes=failures,
            time_parse_performed=False,
            row_processing_performed=True,
            notes=(
                "Unregistered time text was found. "
                "Clinical time parsing for this snapshot was stopped.",
            ),
        )

    return PhaseOneAssessment(
        outcome="prepared",
        evidence_class=evidence_class,
        file_identities=identities,
        replay=replay,
        rows=rows,
        row_width_errors=row_width_errors,
        duplicate_groups=duplicates,
        time_scan=scan,
        time_outcomes=_parse_registered_times(rows),
        time_parse_performed=True,
        row_processing_performed=True,
    )


def _replay_from_port(
    identity: SnapshotIdentity,
    history: SnapshotHistoryQuery | None,
) -> ReplayDecision:
    if history is None:
        return classify_replay(identity, None)
    found = history.find_snapshots(
        source_system=identity.source_system,
        evidence_class=identity.evidence_class,
        file_hashes=identity.as_mapping(),
    )
    return classify_replay(identity, found)


def _file_identities(snapshot_dir: Path) -> tuple[SourceFileIdentity, ...]:
    identities: list[SourceFileIdentity] = []
    for filename in REQUIRED_FILENAMES:
        digest = sha256_file(snapshot_dir / filename)
        identities.append(
            SourceFileIdentity(
                filename=filename,
                source_table=source_table_for(filename),
                sha256=digest,
                source_file_id=f"{filename}:{digest}",
            )
        )
    return tuple(identities)


def _audit_rows(
    snapshot_dir: Path,
    identities: tuple[SourceFileIdentity, ...],
) -> tuple[tuple[SourceRowAudit, ...], tuple[RowWidthError, ...]]:
    prepared: list[SourceRowAudit] = []
    width_errors: list[RowWidthError] = []
    for identity in identities:
        header, data_rows = read_header_and_rows(snapshot_dir / identity.filename)
        expected = FILE_HEADERS[identity.filename]
        if header != expected:
            raise RuntimeError(
                f"Header of {identity.filename} changed after structure validation"
            )
        id_field = SOURCE_RECORD_ID_FIELD.get(identity.filename)
        id_index = header.index(id_field) if id_field else None
        expected_width = len(header)
        for data_row in data_rows:
            actual_width = len(data_row.raw_values)
            if actual_width != expected_width:
                mismatch = "too_few" if actual_width < expected_width else "too_many"
                width_errors.append(
                    RowWidthError(
                        code=RAW_ROW_WIDTH_MISMATCH,
                        mismatch=mismatch,
                        message=(
                            f"{identity.filename} row {data_row.source_row_number} "
                            f"has {actual_width} columns; expected {expected_width}"
                        ),
                        source_system=SOURCE_SYSTEM,
                        source_table=identity.source_table,
                        filename=identity.filename,
                        source_file_id=identity.source_file_id,
                        source_row_number=data_row.source_row_number,
                        actual_column_count=actual_width,
                        expected_column_count=expected_width,
                        raw_values=data_row.raw_values,
                    )
                )
                continue
            fingerprint = source_row_fingerprint(
                source_system=SOURCE_SYSTEM,
                source_table=identity.source_table,
                header=header,
                raw_values=data_row.raw_values,
            )
            source_record_id = None
            if id_index is not None:
                source_record_id = data_row.raw_values[id_index]
            prepared.append(
                SourceRowAudit(
                    source_file_id=identity.source_file_id,
                    filename=identity.filename,
                    source_system=SOURCE_SYSTEM,
                    source_table=identity.source_table,
                    source_row_number=data_row.source_row_number,
                    header=header,
                    raw_values=data_row.raw_values,
                    fingerprint=fingerprint,
                    source_record_id=source_record_id,
                    duplicate_in_file=False,
                )
            )
    return tuple(prepared), tuple(width_errors)


def _duplicate_groups(rows: tuple[SourceRowAudit, ...]) -> tuple[DuplicatePayloadGroup, ...]:
    grouped: dict[tuple[str, str], list[int]] = {}
    for row in rows:
        grouped.setdefault((row.filename, row.fingerprint), []).append(row.source_row_number)
    groups: list[DuplicatePayloadGroup] = []
    for (filename, fingerprint), numbers in grouped.items():
        if len(numbers) < 2:
            continue
        groups.append(
            DuplicatePayloadGroup(
                filename=filename,
                source_table=source_table_for(filename),
                fingerprint=fingerprint,
                source_row_numbers=tuple(numbers),
            )
        )
    return tuple(groups)


def _mark_duplicates(
    rows: tuple[SourceRowAudit, ...],
    groups: tuple[DuplicatePayloadGroup, ...],
) -> tuple[SourceRowAudit, ...]:
    marked: set[tuple[str, int]] = set()
    for group in groups:
        for number in group.source_row_numbers:
            marked.add((group.filename, number))
    if not marked:
        return rows
    return tuple(
        SourceRowAudit(
            source_file_id=row.source_file_id,
            filename=row.filename,
            source_system=row.source_system,
            source_table=row.source_table,
            source_row_number=row.source_row_number,
            header=row.header,
            raw_values=row.raw_values,
            fingerprint=row.fingerprint,
            source_record_id=row.source_record_id,
            duplicate_in_file=(row.filename, row.source_row_number) in marked,
        )
        for row in rows
    )


def _scan_times(
    rows: tuple[SourceRowAudit, ...],
) -> tuple[tuple[TimeScanCount, ...], bool]:
    counts: dict[tuple[str, str], dict[str, int]] = {}
    for filename, fields in CLINICAL_TIME_FIELDS.items():
        for field_name in fields:
            counts[(filename, field_name)] = {
                "empty": 0,
                "date": 0,
                "utc_datetime": 0,
                "invalid_calendar_date": 0,
                "other": 0,
            }
    blocked = False
    for row in rows:
        for field_name in CLINICAL_TIME_FIELDS[row.filename]:
            index = row.header.index(field_name)
            category = classify_time_cell(row.raw_values[index])
            counts[(row.filename, field_name)][category] += 1
            if category == "other":
                blocked = True
    scan = tuple(
        TimeScanCount(filename=filename, field_name=field_name, **bucket)
        for (filename, field_name), bucket in counts.items()
    )
    return scan, blocked


def _unregistered_failures(rows: tuple[SourceRowAudit, ...]) -> tuple[TimeCellOutcome, ...]:
    outcomes: list[TimeCellOutcome] = []
    for row in rows:
        for field_name in CLINICAL_TIME_FIELDS[row.filename]:
            raw_text = row.raw_values[row.header.index(field_name)]
            if classify_time_cell(raw_text) != "other":
                continue
            failure = TimeParseFailure(
                code=INVALID_TIME,
                raw_text=raw_text,
                message=(
                    f"{INVALID_TIME}: {row.filename}:{field_name}:row {row.source_row_number}: "
                    f"{raw_text!r} (time text is not a registered Synthea format)"
                ),
                filename=row.filename,
                source_table=row.source_table,
                field_name=field_name,
                source_row_number=row.source_row_number,
            )
            outcomes.append(
                TimeCellOutcome(
                    filename=row.filename,
                    source_table=row.source_table,
                    field_name=field_name,
                    source_row_number=row.source_row_number,
                    category="other",
                    parsed=None,
                    failure=failure,
                )
            )
    return tuple(outcomes)


def _parse_registered_times(rows: tuple[SourceRowAudit, ...]) -> tuple[TimeCellOutcome, ...]:
    outcomes: list[TimeCellOutcome] = []
    for row in rows:
        for field_name in CLINICAL_TIME_FIELDS[row.filename]:
            raw_text = row.raw_values[row.header.index(field_name)]
            category = classify_time_cell(raw_text)
            result = parse_clinical_time(
                raw_text,
                filename=row.filename,
                source_table=row.source_table,
                field_name=field_name,
                source_row_number=row.source_row_number,
            )
            parsed = result if isinstance(result, ParsedClinicalTime) else None
            failure = result if isinstance(result, TimeParseFailure) else None
            outcomes.append(
                TimeCellOutcome(
                    filename=row.filename,
                    source_table=row.source_table,
                    field_name=field_name,
                    source_row_number=row.source_row_number,
                    category=category,
                    parsed=parsed,
                    failure=failure,
                )
            )
    return tuple(outcomes)


def hashes_by_filename(identities: tuple[SourceFileIdentity, ...]) -> Mapping[str, str]:
    return {item.filename: item.sha256 for item in identities}


def historical_from_identity(
    identity: SnapshotIdentity,
    *,
    status: str,
) -> HistoricalSnapshot:
    return HistoricalSnapshot(
        source_system=identity.source_system,
        evidence_class=identity.evidence_class,
        status=status,
        file_hashes=identity.as_mapping(),
    )
