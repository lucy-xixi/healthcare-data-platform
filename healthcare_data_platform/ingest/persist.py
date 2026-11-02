"""Commit a Synthea snapshot using the phase-one readers and parsers.

Payload comparison and human quarantine release are not performed here.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import insert, select, text
from sqlalchemy.engine import Connection, Engine

from healthcare_data_platform.canonical.values import finite_decimal
from healthcare_data_platform.ingest.synthea.contract import (
    CLINICAL_TIME_FIELDS,
    FILE_HEADERS,
    REQUIRED_EVIDENCE_CLASS,
    REQUIRED_FILENAMES,
    SOURCE_SYSTEM,
    source_table_for,
)
from healthcare_data_platform.ingest.synthea.fingerprint import source_row_fingerprint
from healthcare_data_platform.ingest.synthea.hashing import sha256_file
from healthcare_data_platform.ingest.synthea.raw_reader import iterate_data_rows
from healthcare_data_platform.ingest.synthea.replay import (
    HistoricalSnapshot,
    SnapshotIdentity,
    classify_replay,
)
from healthcare_data_platform.ingest.synthea.structure import (
    evidence_class_errors,
    inspect_snapshot_files,
)
from healthcare_data_platform.ingest.synthea.time_parse import classify_time_cell, parse_clinical_time
from healthcare_data_platform.persistence.models import (
    Condition,
    Encounter,
    IngestBatch,
    IngestBatchError,
    IngestBatchFile,
    Medication,
    Observation,
    Patient,
    Procedure,
    ReplayRecordLink,
    SourceFile,
    SourceRecordLink,
    SourceRowAudit,
    SourceRowTimeCell,
)

MISSING_REQUIRED_VALUE = "MISSING_REQUIRED_VALUE"
DUPLICATE_PAYLOAD_IN_FILE = "DUPLICATE_PAYLOAD_IN_FILE"
FK_PATIENT_NOT_IN_SNAPSHOT = "FK_PATIENT_NOT_IN_SNAPSHOT"
FK_ENCOUNTER_NOT_IN_SNAPSHOT = "FK_ENCOUNTER_NOT_IN_SNAPSHOT"
TIME_FORMAT_UNREGISTERED = "TIME_FORMAT_UNREGISTERED"
SOURCE_FILE_EVIDENCE_CLASS_CONFLICT = "SOURCE_FILE_EVIDENCE_CLASS_CONFLICT"

_REQUIRED = {
    "patients.csv": ("Id", "BIRTHDATE", "GENDER", "RACE", "ETHNICITY"),
    "encounters.csv": ("Id", "PATIENT", "START", "ENCOUNTERCLASS", "CODE", "DESCRIPTION"),
    "conditions.csv": ("PATIENT", "START", "SYSTEM", "CODE", "DESCRIPTION"),
    "medications.csv": ("PATIENT", "START", "CODE", "DESCRIPTION"),
    "observations.csv": ("DATE", "PATIENT", "CODE", "DESCRIPTION", "VALUE", "TYPE"),
    "procedures.csv": ("PATIENT", "START", "SYSTEM", "CODE", "DESCRIPTION"),
}
_REQUIRED_TIME = {
    "patients.csv": ("BIRTHDATE",),
    "encounters.csv": ("START",),
    "conditions.csv": ("START",),
    "medications.csv": ("START",),
    "observations.csv": ("DATE",),
    "procedures.csv": ("START",),
}
_OPTIONAL_TIME = {
    "patients.csv": ("DEATHDATE",),
    "encounters.csv": ("STOP",),
    "conditions.csv": ("STOP",),
    "medications.csv": ("STOP",),
    "observations.csv": (),
    "procedures.csv": ("STOP",),
}
_ENCOUNTER_FIELD = {
    "conditions.csv": "ENCOUNTER",
    "medications.csv": "ENCOUNTER",
    "observations.csv": "ENCOUNTER",
    "procedures.csv": "ENCOUNTER",
}
_CHUNK = 2000


@dataclass
class ImportResult:
    ingest_batch_id: int
    status: str
    accepted_row_count: int
    quarantined_row_count: int
    message: str


def import_snapshot(engine: Engine, snapshot_dir: Path, evidence_class: str | None) -> ImportResult:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with engine.begin() as conn:
        return _import(conn, snapshot_dir, evidence_class, now)


def _import(conn: Connection, snapshot_dir: Path, evidence_class: str | None, now: datetime) -> ImportResult:
    structure_errors = [
        *evidence_class_errors(evidence_class),
        *inspect_snapshot_files(snapshot_dir),
    ]
    if structure_errors:
        batch_id = _new_batch(
            conn,
            declared=evidence_class,
            validated=None,
            status="failed",
            snapshot_key=None,
            replay_of=None,
            now=now,
            accepted=0,
            quarantined=0,
        )
        for filename in REQUIRED_FILENAMES:
            _insert_batch_file(conn, batch_id, filename, None, None, None)
        for error in structure_errors:
            _insert_error(conn, batch_id, error.code, error.filename, error.field_name, error.message, None)
        return ImportResult(batch_id, "failed", 0, 0, "结构或证据等级校验失败")

    assert evidence_class == REQUIRED_EVIDENCE_CLASS
    hashes = {name: sha256_file(snapshot_dir / name) for name in REQUIRED_FILENAMES}
    key = _snapshot_key(hashes)
    conflict = _evidence_conflict(conn, hashes, evidence_class)
    if conflict is not None:
        batch_id = _new_batch(
            conn,
            declared=evidence_class,
            validated=evidence_class,
            status="failed",
            snapshot_key=key,
            replay_of=None,
            now=now,
            accepted=0,
            quarantined=0,
        )
        for filename in REQUIRED_FILENAMES:
            existing = _source_file_id(conn, hashes[filename])
            _insert_batch_file(conn, batch_id, filename, hashes[filename], (snapshot_dir / filename).stat().st_size, existing)
        _insert_error(
            conn,
            batch_id,
            SOURCE_FILE_EVIDENCE_CLASS_CONFLICT,
            conflict[0],
            None,
            "已登记源文件的证据等级与本批次不一致",
            json.dumps({"existing_evidence_class": conflict[1], "declared_evidence_class": evidence_class}),
        )
        return ImportResult(batch_id, "failed", 0, 0, "源文件证据等级冲突")

    identity = SnapshotIdentity.from_mapping(
        source_system=SOURCE_SYSTEM,
        evidence_class=evidence_class,
        file_hashes=hashes,
    )
    history, succeeded_id = _succeeded_history(conn, identity)
    decision = classify_replay(identity, history)
    if decision.is_replay and succeeded_id is not None:
        return _write_replay(conn, snapshot_dir, hashes, key, evidence_class, succeeded_id, now)

    print("scanning clinical time formats", flush=True)
    blocked = _unregistered_time(snapshot_dir)
    if blocked:
        batch_id = _new_batch(
            conn,
            declared=evidence_class,
            validated=evidence_class,
            status="failed",
            snapshot_key=key,
            replay_of=None,
            now=now,
            accepted=0,
            quarantined=0,
        )
        for filename in REQUIRED_FILENAMES:
            _insert_batch_file(
                conn,
                batch_id,
                filename,
                hashes[filename],
                (snapshot_dir / filename).stat().st_size,
                _source_file_id(conn, hashes[filename]),
            )
        for item in blocked[:50]:
            _insert_error(conn, batch_id, TIME_FORMAT_UNREGISTERED, item["filename"], item["field"], item["message"], item["raw"])
        return ImportResult(batch_id, "failed", 0, 0, "存在未登记时间格式，整批失败")

    batch_id = _new_batch(
        conn,
        declared=evidence_class,
        validated=evidence_class,
        status="succeeded",
        snapshot_key=key,
        replay_of=None,
        now=now,
        accepted=0,
        quarantined=0,
    )
    file_ids: dict[str, int] = {}
    for filename in REQUIRED_FILENAMES:
        file_ids[filename] = _insert_batch_file(
            conn,
            batch_id,
            filename,
            hashes[filename],
            (snapshot_dir / filename).stat().st_size,
            None,
        )
    accepted, quarantined = _load_rows(conn, snapshot_dir, batch_id, file_ids, evidence_class, now)
    status = "partial" if quarantined else "succeeded"
    conn.execute(
        text(
            "update ingest_batch set status=:status, accepted_row_count=:accepted, "
            "quarantined_row_count=:quarantined, completed_at=:completed where ingest_batch_id=:batch"
        ),
        {"status": status, "accepted": accepted, "quarantined": quarantined, "completed": now, "batch": batch_id},
    )
    if status == "succeeded":
        _register_source_files(conn, snapshot_dir, hashes, evidence_class, file_ids, now)
    else:
        _link_existing_source_files(conn, hashes, file_ids)
    label = "导入完成" if status == "succeeded" else "存在隔离行，批次为 partial，已接受记录只用于审计"
    return ImportResult(batch_id, status, accepted, quarantined, label)


def _load_rows(conn, snapshot_dir, batch_id, file_ids, evidence_class, now) -> tuple[int, int]:
    duplicate_sets = {}
    for name in REQUIRED_FILENAMES:
        print(f"fingerprinting {name}", flush=True)
        duplicate_sets[name] = _duplicate_fingerprints(snapshot_dir / name, name)
    patients: dict[str, int] = {}
    encounters: dict[str, int] = {}
    accepted = 0
    quarantined = 0
    ids = _allocators(conn)
    for filename in REQUIRED_FILENAMES:
        print(f"writing {filename}", flush=True)
        header, rows = iterate_data_rows(snapshot_dir / filename)
        if header != FILE_HEADERS[filename]:
            raise RuntimeError(f"Header of {filename} changed after structure validation")
        index = {name: pos for pos, name in enumerate(header)}
        table = source_table_for(filename)
        audit_buf: list[dict] = []
        time_buf: list[dict] = []
        link_buf: list[dict] = []
        entity_buf: list[dict] = []
        entity_model = _model_for(filename)
        for data_row in rows:
            raw = data_row.raw_values
            decision = _classify_row(
                filename,
                header,
                raw,
                duplicate_sets[filename],
                patients,
                encounters,
            )
            audit_id = ids["audit"]
            ids["audit"] += 1
            audit_buf.append(
                {
                    "source_row_audit_id": audit_id,
                    "ingest_batch_id": batch_id,
                    "source_file_id": None,
                    "ingest_batch_file_id": file_ids[filename],
                    "source_system": SOURCE_SYSTEM,
                    "source_table": table,
                    "source_row_number": data_row.source_row_number,
                    "header_names": json.dumps(list(header), ensure_ascii=False),
                    "raw_field_values": json.dumps(list(raw), ensure_ascii=False),
                    "source_row_fingerprint": decision.fingerprint,
                    "parsed_source_record_id": decision.source_record_id,
                    "disposition": decision.disposition,
                    "error_code": decision.error_code,
                    "error_context": decision.error_context,
                }
            )
            for cell in decision.time_cells:
                time_buf.append({"source_row_audit_id": audit_id, "field_name": cell[0], "raw_text": cell[1], "category": cell[2], "failure_code": cell[3]})
            if decision.disposition == "quarantined":
                quarantined += 1
            else:
                record_id = ids["record"]
                ids["record"] += 1
                entity_buf.append(
                    _entity_row(
                        filename,
                        index,
                        raw,
                        record_id,
                        batch_id,
                        table,
                        decision.source_record_id,
                        data_row.source_row_number,
                        evidence_class,
                        now,
                        patients,
                        encounters,
                        decision.optional_time_failures,
                    )
                )
                link_buf.append(
                    {
                        "source_row_audit_id": audit_id,
                        "entity_name": table,
                        "canonical_record_id": record_id,
                        "inclusion_class": "primary",
                    }
                )
                if filename == "patients.csv" and decision.source_record_id:
                    patients[decision.source_record_id] = record_id
                if filename == "encounters.csv" and decision.source_record_id:
                    encounters[decision.source_record_id] = record_id
                accepted += 1
            if len(audit_buf) >= _CHUNK:
                _flush(conn, audit_buf, time_buf, link_buf, entity_buf, entity_model)
                audit_buf, time_buf, link_buf, entity_buf = [], [], [], []
        _flush(conn, audit_buf, time_buf, link_buf, entity_buf, entity_model)
    return accepted, quarantined


@dataclass
class _Decision:
    disposition: str
    error_code: str | None
    error_context: str | None
    fingerprint: str | None
    source_record_id: str | None
    time_cells: list[tuple[str, str, str, str | None]]
    optional_time_failures: set[str]


def _classify_row(filename, header, raw, duplicates, patients, encounters) -> _Decision:
    expected = len(header)
    index = {name: pos for pos, name in enumerate(header)}
    source_id = raw[index["Id"]] if "Id" in index and len(raw) == expected else None
    if len(raw) != expected:
        mismatch = "too_few" if len(raw) < expected else "too_many"
        return _Decision(
            "quarantined",
            "RAW_ROW_WIDTH_MISMATCH",
            json.dumps({"mismatch": mismatch, "actual": len(raw), "expected": expected}),
            None,
            None,
            [],
            set(),
        )
    fingerprint = source_row_fingerprint(
        source_system=SOURCE_SYSTEM,
        source_table=source_table_for(filename),
        header=header,
        raw_values=raw,
    )
    cells: list[tuple[str, str, str, str | None]] = []
    optional_failures: set[str] = set()
    if fingerprint in duplicates:
        return _Decision(
            "quarantined",
            DUPLICATE_PAYLOAD_IN_FILE,
            json.dumps({"fingerprint": fingerprint}),
            fingerprint,
            source_id or None,
            cells,
            optional_failures,
        )
    missing = [name for name in _REQUIRED[filename] if raw[index[name]] == ""]
    if missing:
        return _Decision(
            "quarantined",
            MISSING_REQUIRED_VALUE,
            json.dumps({"fields": missing}),
            fingerprint,
            source_id or None,
            cells,
            optional_failures,
        )
    for field_name in _REQUIRED_TIME[filename]:
        raw_text = raw[index[field_name]]
        category = classify_time_cell(raw_text)
        failure = None if category in ("date", "utc_datetime") else "INVALID_TIME"
        cells.append((field_name, raw_text, category, failure))
        if failure:
            return _Decision("quarantined", "INVALID_TIME", json.dumps({"field": field_name, "raw": raw_text}), fingerprint, source_id or None, cells, optional_failures)
    for field_name in _OPTIONAL_TIME[filename]:
        raw_text = raw[index[field_name]]
        category = classify_time_cell(raw_text)
        failure = "INVALID_TIME" if category == "invalid_calendar_date" else None
        cells.append((field_name, raw_text, category, failure))
        if failure:
            optional_failures.add(field_name)
    if filename != "patients.csv":
        patient_key = raw[index["PATIENT"]]
        if patient_key not in patients:
            return _Decision(
                "quarantined",
                FK_PATIENT_NOT_IN_SNAPSHOT,
                json.dumps({"patient": patient_key}),
                fingerprint,
                source_id or None,
                cells,
                optional_failures,
            )
    encounter_field = _ENCOUNTER_FIELD.get(filename)
    if encounter_field:
        encounter_key = raw[index[encounter_field]]
        if encounter_key and encounter_key not in encounters:
            return _Decision(
                "quarantined",
                FK_ENCOUNTER_NOT_IN_SNAPSHOT,
                json.dumps({"encounter": encounter_key}),
                fingerprint,
                source_id or None,
                cells,
                optional_failures,
            )
    return _Decision("accepted", None, None, fingerprint, source_id or None, cells, optional_failures)


def _entity_row(filename, index, raw, record_id, batch_id, table, source_id, row_number, evidence, now, patients, encounters, optional_failures):
    base = {
        "record_id": record_id,
        "ingest_batch_id": batch_id,
        "source_system": SOURCE_SYSTEM,
        "source_table": table,
        "source_record_id": source_id,
        "source_row_number": row_number,
        "evidence_class": evidence,
        "source_created_at": None,
        "ingested_at": now,
    }
    if filename == "patients.csv":
        birth = _stored_time(raw[index["BIRTHDATE"]], False)
        death = _stored_time(raw[index["DEATHDATE"]], "DEATHDATE" in optional_failures)
        base.update(
            {
                "given_name_source": raw[index["FIRST"]],
                "family_name_source": raw[index["LAST"]],
                "gender_source": raw[index["GENDER"]],
                "race_source": raw[index["RACE"]],
                "ethnicity_source": raw[index["ETHNICITY"]],
                **_prefixed("birth", birth),
                **_prefixed("death", death),
            }
        )
        return base
    patient_id = patients[raw[index["PATIENT"]]]
    encounter_key = raw[index["ENCOUNTER"]] if "ENCOUNTER" in index else ""
    encounter_id = encounters.get(encounter_key) if encounter_key else None
    base["patient_record_id"] = patient_id
    if filename != "patients.csv":
        base["encounter_record_id"] = encounter_id if filename != "encounters.csv" else None
    if filename == "encounters.csv":
        start = _stored_time(raw[index["START"]], False)
        end = _stored_time(raw[index["STOP"]], "STOP" in optional_failures)
        base.update(
            {
                "class_source": raw[index["ENCOUNTERCLASS"]],
                "code_source": raw[index["CODE"]],
                "text_source": raw[index["DESCRIPTION"]],
                "reason_code_source": raw[index["REASONCODE"]],
                "reason_text_source": raw[index["REASONDESCRIPTION"]],
                **_prefixed("start", start),
                **_prefixed("end", end),
            }
        )
        base.pop("encounter_record_id", None)
        return base
    start_field = "DATE" if filename == "observations.csv" else "START"
    start = _stored_time(raw[index[start_field]], False)
    if filename == "observations.csv":
        numeric = finite_decimal(raw[index["VALUE"]])
        base.update(
            {
                "category_source": raw[index["CATEGORY"]],
                "code_source": raw[index["CODE"]],
                "text_source": raw[index["DESCRIPTION"]],
                "value_text": raw[index["VALUE"]],
                "value_numeric": float(numeric) if numeric is not None else None,
                "value_type_source": raw[index["TYPE"]],
                "unit_source": raw[index["UNITS"]],
                **_prefixed("clinical", start),
            }
        )
        return base
    end = _stored_time(raw[index["STOP"]], "STOP" in optional_failures)
    base.update({**_prefixed("start", start), **_prefixed("end", end)})
    if filename == "conditions.csv":
        base.update(
            {
                "system_source": raw[index["SYSTEM"]],
                "code_source": raw[index["CODE"]],
                "text_source": raw[index["DESCRIPTION"]],
            }
        )
    elif filename == "medications.csv":
        base.update(
            {
                "code_source": raw[index["CODE"]],
                "text_source": raw[index["DESCRIPTION"]],
                "dispenses_source": raw[index["DISPENSES"]],
                "reason_code_source": raw[index["REASONCODE"]],
                "reason_text_source": raw[index["REASONDESCRIPTION"]],
            }
        )
    else:
        base.update(
            {
                "system_source": raw[index["SYSTEM"]],
                "code_source": raw[index["CODE"]],
                "text_source": raw[index["DESCRIPTION"]],
                "reason_code_source": raw[index["REASONCODE"]],
                "reason_text_source": raw[index["REASONDESCRIPTION"]],
            }
        )
    return base


def _stored_time(raw_text: str, invalid_optional: bool) -> dict:
    if invalid_optional or raw_text == "":
        return {"raw": raw_text, "precision": "absent", "calendar": None, "clock": None, "instant": None, "tz": None}
    parsed = parse_clinical_time(raw_text)
    from healthcare_data_platform.ingest.synthea.time_parse import ParsedClinicalTime

    if not isinstance(parsed, ParsedClinicalTime):
        return {"raw": raw_text, "precision": "absent", "calendar": None, "clock": None, "instant": None, "tz": None}
    clock = parsed.clock_time.isoformat() if parsed.clock_time else None
    instant = parsed.instant_utc.isoformat() if parsed.instant_utc else None
    tz = None if parsed.timezone_known is None else int(bool(parsed.timezone_known))
    return {
        "raw": parsed.raw_text,
        "precision": parsed.precision,
        "calendar": parsed.calendar_date,
        "clock": clock,
        "instant": instant,
        "tz": tz,
    }


def _prefixed(prefix: str, parts: dict) -> dict:
    return {
        f"{prefix}_raw": parts["raw"],
        f"{prefix}_precision": parts["precision"],
        f"{prefix}_calendar_date": parts["calendar"],
        f"{prefix}_clock_time": parts["clock"],
        f"{prefix}_instant_utc": parts["instant"],
        f"{prefix}_timezone_known": parts["tz"],
    }


def _model_for(filename):
    return {
        "patients.csv": Patient,
        "encounters.csv": Encounter,
        "conditions.csv": Condition,
        "medications.csv": Medication,
        "observations.csv": Observation,
        "procedures.csv": Procedure,
    }[filename]


def _flush(conn, audits, times, links, entities, model) -> None:
    if audits:
        conn.execute(insert(SourceRowAudit), audits)
    if times:
        conn.execute(insert(SourceRowTimeCell), times)
    if entities:
        conn.execute(insert(model), entities)
    if links:
        conn.execute(insert(SourceRecordLink), links)


def _duplicate_fingerprints(path: Path, filename: str) -> set[str]:
    header, rows = iterate_data_rows(path)
    counts: Counter[str] = Counter()
    expected = len(FILE_HEADERS[filename])
    table = source_table_for(filename)
    for data_row in rows:
        if len(data_row.raw_values) != expected:
            continue
        counts[
            source_row_fingerprint(
                source_system=SOURCE_SYSTEM,
                source_table=table,
                header=header,
                raw_values=data_row.raw_values,
            )
        ] += 1
    return {digest for digest, count in counts.items() if count >= 2}


def _unregistered_time(snapshot_dir: Path) -> list[dict]:
    found: list[dict] = []
    for filename in REQUIRED_FILENAMES:
        header, rows = iterate_data_rows(snapshot_dir / filename)
        index = {name: pos for pos, name in enumerate(header)}
        expected = len(header)
        for data_row in rows:
            if len(data_row.raw_values) != expected:
                continue
            for field_name in CLINICAL_TIME_FIELDS[filename]:
                raw_text = data_row.raw_values[index[field_name]]
                if classify_time_cell(raw_text) == "other":
                    found.append(
                        {
                            "filename": filename,
                            "field": field_name,
                            "raw": raw_text,
                            "message": f"{filename} row {data_row.source_row_number} {field_name} is not a registered time format",
                        }
                    )
                    return found
    return found


def _write_replay(conn, snapshot_dir, hashes, key, evidence_class, succeeded_id, now) -> ImportResult:
    batch_id = _new_batch(
        conn,
        declared=evidence_class,
        validated=evidence_class,
        status="replayed",
        snapshot_key=key,
        replay_of=succeeded_id,
        now=now,
        accepted=0,
        quarantined=0,
    )
    for filename in REQUIRED_FILENAMES:
        _insert_batch_file(
            conn,
            batch_id,
            filename,
            hashes[filename],
            (snapshot_dir / filename).stat().st_size,
            _source_file_id(conn, hashes[filename]),
        )
    originals = conn.execute(
        text(
            "select l.entity_name, l.canonical_record_id from source_record_link l "
            "join source_row_audit a on a.source_row_audit_id = l.source_row_audit_id "
            "where a.ingest_batch_id = :batch"
        ),
        {"batch": succeeded_id},
    ).all()
    if originals:
        conn.execute(
            insert(ReplayRecordLink),
            [
                {
                    "ingest_batch_id": batch_id,
                    "entity_name": row.entity_name,
                    "canonical_record_id": row.canonical_record_id,
                }
                for row in originals
            ],
        )
    return ImportResult(batch_id, "replayed", 0, 0, f"重放已有成功批次 {succeeded_id}，未新建规范记录")


def _succeeded_history(conn, identity: SnapshotIdentity):
    rows = conn.execute(
        select(
            IngestBatch.ingest_batch_id,
            IngestBatch.source_system,
            IngestBatch.validated_evidence_class,
            IngestBatch.status,
            IngestBatchFile.original_filename,
            IngestBatchFile.sha256,
        )
        .join(IngestBatchFile, IngestBatchFile.ingest_batch_id == IngestBatch.ingest_batch_id)
        .where(IngestBatch.status == "succeeded")
    ).all()
    grouped: dict[int, dict[str, str]] = {}
    meta: dict[int, tuple[str, str]] = {}
    for row in rows:
        if row.sha256 is None:
            continue
        grouped.setdefault(row.ingest_batch_id, {})[row.original_filename] = row.sha256
        meta[row.ingest_batch_id] = (row.source_system, row.validated_evidence_class or "")
    history = []
    matched = None
    current = identity.as_mapping()
    for batch_id, file_hashes in grouped.items():
        system, evidence = meta[batch_id]
        history.append(
            HistoricalSnapshot(
                source_system=system,
                evidence_class=evidence,
                status="succeeded",
                file_hashes=file_hashes,
            )
        )
        if system == identity.source_system and evidence == identity.evidence_class and file_hashes == current:
            matched = batch_id
    return history, matched


def _evidence_conflict(conn, hashes, evidence_class):
    for filename, digest in hashes.items():
        row = conn.execute(
            select(SourceFile.evidence_class).where(
                SourceFile.source_system == SOURCE_SYSTEM,
                SourceFile.sha256 == digest,
            )
        ).first()
        if row is not None and row.evidence_class != evidence_class:
            return filename, row.evidence_class
    return None


def _source_file_id(conn, digest: str) -> int | None:
    row = conn.execute(
        select(SourceFile.source_file_id).where(
            SourceFile.source_system == SOURCE_SYSTEM,
            SourceFile.sha256 == digest,
        )
    ).first()
    return None if row is None else int(row.source_file_id)


def _register_source_files(conn, snapshot_dir, hashes, evidence_class, file_ids, now) -> None:
    for filename, digest in hashes.items():
        existing = _source_file_id(conn, digest)
        if existing is None:
            result = conn.execute(
                insert(SourceFile).values(
                    source_system=SOURCE_SYSTEM,
                    sha256=digest,
                    original_filename=filename,
                    file_size_bytes=(snapshot_dir / filename).stat().st_size,
                    evidence_class=evidence_class,
                    first_successful_ingest_at=now,
                )
            )
            existing = int(result.inserted_primary_key[0])
        conn.execute(
            text("update ingest_batch_file set source_file_id=:source where ingest_batch_file_id=:file"),
            {"source": existing, "file": file_ids[filename]},
        )


def _link_existing_source_files(conn, hashes, file_ids) -> None:
    for filename, digest in hashes.items():
        existing = _source_file_id(conn, digest)
        if existing is not None:
            conn.execute(
                text("update ingest_batch_file set source_file_id=:source where ingest_batch_file_id=:file"),
                {"source": existing, "file": file_ids[filename]},
            )


def _new_batch(conn, *, declared, validated, status, snapshot_key, replay_of, now, accepted, quarantined) -> int:
    result = conn.execute(
        insert(IngestBatch).values(
            source_system=SOURCE_SYSTEM,
            declared_evidence_class=None if declared is None else str(declared),
            validated_evidence_class=validated,
            status=status,
            snapshot_key=snapshot_key,
            replay_of_batch_id=replay_of,
            started_at=now,
            completed_at=now,
            accepted_row_count=accepted,
            quarantined_row_count=quarantined,
        )
    )
    return int(result.inserted_primary_key[0])


def _insert_batch_file(conn, batch_id, filename, digest, size, source_file_id) -> int:
    result = conn.execute(
        insert(IngestBatchFile).values(
            ingest_batch_id=batch_id,
            source_file_id=source_file_id,
            original_filename=filename,
            sha256=digest,
            file_size_bytes=size,
        )
    )
    return int(result.inserted_primary_key[0])


def _insert_error(conn, batch_id, code, filename, field_name, message, context) -> None:
    conn.execute(
        insert(IngestBatchError).values(
            ingest_batch_id=batch_id,
            code=code,
            filename=filename,
            field_name=field_name,
            message=message,
            context=context,
        )
    )


def _allocators(conn) -> dict[str, int]:
    audit = conn.execute(text("select coalesce(max(source_row_audit_id), 0) + 1 from source_row_audit")).scalar_one()
    record = conn.execute(
        text(
            "select coalesce(max(record_id), 0) + 1 from ("
            "select record_id from patient union all select record_id from encounter "
            "union all select record_id from condition union all select record_id from medication "
            "union all select record_id from observation union all select record_id from procedure)"
        )
    ).scalar_one()
    return {"audit": int(audit), "record": int(record)}


def _snapshot_key(hashes: dict[str, str]) -> str:
    payload = "\n".join(f"{name}\0{hashes[name]}" for name in sorted(hashes))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
