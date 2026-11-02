"""Phase-one Synthea adapter tests.

Fixtures are temporary directories. The old project's Synthea CSV files are not
modified. Historical snapshots passed to the replay classifier are test inputs,
not a persistence implementation.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import tempfile
import unittest
from pathlib import Path

from healthcare_data_platform.ingest.synthea.contract import (
    FILE_HEADERS,
    REQUIRED_EVIDENCE_CLASS,
    REQUIRED_FILENAMES,
)
from healthcare_data_platform.ingest.synthea.errors import (
    INVALID_TIME,
    SNAPSHOT_EVIDENCE_CLASS_CONFLICT,
    SNAPSHOT_EVIDENCE_CLASS_MISSING,
    SNAPSHOT_FILE_MISSING,
    SNAPSHOT_HEADER_MISMATCH,
)
from healthcare_data_platform.ingest.synthea.fingerprint import (
    payload_contrast_eligible,
    source_row_fingerprint,
)
from healthcare_data_platform.ingest.synthea.hashing import sha256_bytes, sha256_file
from healthcare_data_platform.ingest.synthea.pipeline import assess_synthea_snapshot
from healthcare_data_platform.ingest.synthea.replay import (
    FAILED,
    PARTIAL,
    SUCCEEDED,
    HistoricalSnapshot,
    SnapshotIdentity,
    classify_replay,
    same_snapshot_identity,
)
from healthcare_data_platform.ingest.synthea.time_parse import (
    ParsedClinicalTime,
    TimeParseFailure,
    parse_clinical_time,
)


def _write_snapshot(root: Path, *, mutate=None, bom_file: str | None = None) -> None:
    values = {
        "patients.csv": [
            "7cfebcfc-e1cc-2993-d512-9c450d4457b8",
            "2023-12-18",
            "",
            "999-85-5286",
            "",
            "",
            "",
            "Harlan808",
            "Kory651",
            "Upton904",
            "",
            "",
            "",
            "white",
            "nonhispanic",
            "M",
            "Wenham",
            "902 Block Camp",
            "Millis",
            "Massachusetts",
            "Norfolk County",
            "",
            "00000",
            "42.21887836645007",
            "-71.40720288626291",
            "7577.28",
            "0.00",
            "128032",
        ],
        "encounters.csv": [
            "7cfebcfc-e1cc-2993-0235-31263f549c60",
            "2023-12-18T13:31:25Z",
            "2023-12-18T13:46:25Z",
            "7cfebcfc-e1cc-2993-d512-9c450d4457b8",
            "org",
            "provider",
            "payer",
            "wellness",
            "410620009",
            "Well child visit (procedure)",
            "136.80",
            "347.38",
            "0.00",
            "",
            "",
        ],
        "conditions.csv": [
            "2023-12-18",
            "2024-01-22",
            "7cfebcfc-e1cc-2993-d512-9c450d4457b8",
            "7cfebcfc-e1cc-2993-0235-31263f549c60",
            "http://snomed.info/sct",
            "314529007",
            "Medication review due (situation)",
        ],
        "medications.csv": [
            "2025-04-27T15:04:26Z",
            "",
            "7cfebcfc-e1cc-2993-d512-9c450d4457b8",
            "payer",
            "7cfebcfc-e1cc-2993-ba51-6882b240e26e",
            "1014676",
            "cetirizine hydrochloride 5 MG Oral Tablet",
            "229.32",
            "0.00",
            "14",
            "3210.48",
            "",
            "",
        ],
        "observations.csv": [
            "2023-12-18T13:31:25Z",
            "7cfebcfc-e1cc-2993-d512-9c450d4457b8",
            "7cfebcfc-e1cc-2993-0235-31263f549c60",
            "vital-signs",
            "8302-2",
            "Body Height",
            "53.8",
            "cm",
            "numeric",
        ],
        "procedures.csv": [
            "2024-01-22T13:31:25Z",
            "2024-01-22T13:46:25Z",
            "7cfebcfc-e1cc-2993-d512-9c450d4457b8",
            "7cfebcfc-e1cc-2993-f4ac-81beb3739994",
            "http://snomed.info/sct",
            "430193006",
            "Medication reconciliation (procedure)",
            "862.80",
            "",
            "",
        ],
    }
    if mutate:
        mutate(values)
    for filename in REQUIRED_FILENAMES:
        header = ",".join(FILE_HEADERS[filename])
        body = ",".join(values[filename])
        payload = f"{header}\n{body}\n".encode("utf-8")
        if bom_file == filename:
            payload = b"\xef\xbb\xbf" + payload
        (root / filename).write_bytes(payload)


class ListHistory:
    """Test double. This object is not the platform persistence store."""

    def __init__(self, records: list[HistoricalSnapshot]) -> None:
        self.records = records
        self.calls = 0

    def find_snapshots(self, *, source_system, evidence_class, file_hashes):
        self.calls += 1
        found = []
        for record in self.records:
            if (
                record.source_system == source_system
                and record.evidence_class == evidence_class
                and dict(record.file_hashes) == dict(file_hashes)
            ):
                found.append(record)
        return found


class StructureTests(unittest.TestCase):
    def test_complete_snapshot_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_snapshot(root)
            result = assess_synthea_snapshot(root, REQUIRED_EVIDENCE_CLASS)
        self.assertEqual(result.outcome, "prepared")
        self.assertEqual(result.structure_errors, ())
        self.assertEqual(tuple(item.filename for item in result.file_identities), REQUIRED_FILENAMES)
        self.assertTrue(result.row_processing_performed)
        self.assertTrue(result.time_parse_performed)
        self.assertIsNone(result.replay.matched_status)
        self.assertFalse(result.replay.evaluated)

    def test_each_missing_file_fails_before_row_processing(self) -> None:
        for missing in REQUIRED_FILENAMES:
            with self.subTest(missing=missing):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    _write_snapshot(root)
                    (root / missing).unlink()
                    result = assess_synthea_snapshot(root, REQUIRED_EVIDENCE_CLASS)
                self.assertEqual(result.outcome, "structure_failed")
                self.assertFalse(result.row_processing_performed)
                self.assertEqual(result.rows, ())
                codes = [error.code for error in result.structure_errors]
                self.assertIn(SNAPSHOT_FILE_MISSING, codes)
                self.assertIn(missing, [error.filename for error in result.structure_errors])

    def test_missing_header_field_fails(self) -> None:
        def drop_deathdate(values: dict) -> None:
            header = list(FILE_HEADERS["patients.csv"])
            index = header.index("DEATHDATE")
            del values["patients.csv"][index]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_snapshot(root, mutate=drop_deathdate)
            original = (root / "patients.csv").read_bytes()
            text = original.decode("utf-8")
            text = text.replace("DEATHDATE,", "", 1)
            (root / "patients.csv").write_bytes(text.encode("utf-8"))
            result = assess_synthea_snapshot(root, REQUIRED_EVIDENCE_CLASS)
        self.assertEqual(result.outcome, "structure_failed")
        self.assertFalse(result.row_processing_performed)
        self.assertTrue(
            any(
                error.code == SNAPSHOT_HEADER_MISMATCH and error.filename == "patients.csv"
                for error in result.structure_errors
            )
        )

    def test_header_order_change_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_snapshot(root)
            text = (root / "encounters.csv").read_text(encoding="utf-8")
            text = text.replace("START,STOP", "STOP,START", 1)
            (root / "encounters.csv").write_text(text, encoding="utf-8", newline="")
            result = assess_synthea_snapshot(root, REQUIRED_EVIDENCE_CLASS)
        self.assertEqual(result.outcome, "structure_failed")
        self.assertFalse(result.row_processing_performed)
        self.assertTrue(
            any(error.code == SNAPSHOT_HEADER_MISMATCH for error in result.structure_errors)
        )

    def test_bom_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_snapshot(root, bom_file="observations.csv")
            result = assess_synthea_snapshot(root, REQUIRED_EVIDENCE_CLASS)
        self.assertEqual(result.outcome, "structure_failed")
        self.assertFalse(result.row_processing_performed)
        self.assertTrue(
            any("BOM" in error.message for error in result.structure_errors)
        )

    def test_evidence_class_must_be_explicit_and_exact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_snapshot(root)
            missing = assess_synthea_snapshot(root, None)
            blank = assess_synthea_snapshot(root, "")
            other = assess_synthea_snapshot(root, "synthetic_faker")
            spaced = assess_synthea_snapshot(root, " synthetic_synthea")
        for result in (missing, blank):
            self.assertEqual(result.outcome, "structure_failed")
            self.assertEqual(result.rows, ())
            self.assertTrue(
                any(error.code == SNAPSHOT_EVIDENCE_CLASS_MISSING for error in result.structure_errors)
            )
        for result in (other, spaced):
            self.assertEqual(result.outcome, "structure_failed")
            self.assertTrue(
                any(error.code == SNAPSHOT_EVIDENCE_CLASS_CONFLICT for error in result.structure_errors)
            )


class HashAndReplayTests(unittest.TestCase):
    def test_sha256_uses_original_bytes(self) -> None:
        payload = b"Id,BIRTHDATE\r\n00000\r\n"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.csv"
            path.write_bytes(payload)
            self.assertEqual(sha256_file(path), hashlib.sha256(payload).hexdigest())
            self.assertEqual(sha256_file(path), sha256_bytes(payload))
        other = payload.replace(b"\r\n", b"\n")
        self.assertNotEqual(sha256_bytes(payload), sha256_bytes(other))

    def test_snapshot_match_requires_all_six_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_snapshot(root)
            first = assess_synthea_snapshot(root, REQUIRED_EVIDENCE_CLASS)
            text = (root / "procedures.csv").read_text(encoding="utf-8")
            text = text.replace("862.80", "862.81", 1)
            (root / "procedures.csv").write_text(text, encoding="utf-8", newline="")
            second = assess_synthea_snapshot(root, REQUIRED_EVIDENCE_CLASS)
        left = SnapshotIdentity.from_mapping(
            source_system="synthea_csv",
            evidence_class=REQUIRED_EVIDENCE_CLASS,
            file_hashes={item.filename: item.sha256 for item in first.file_identities},
        )
        right = SnapshotIdentity.from_mapping(
            source_system="synthea_csv",
            evidence_class=REQUIRED_EVIDENCE_CLASS,
            file_hashes={item.filename: item.sha256 for item in second.file_identities},
        )
        self.assertTrue(same_snapshot_identity(left, left))
        self.assertFalse(same_snapshot_identity(left, right))
        self.assertNotEqual(
            dict(left.file_hashes)["procedures.csv"],
            dict(right.file_hashes)["procedures.csv"],
        )

    def test_succeeded_history_is_replay_and_skips_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_snapshot(root)
            prepared = assess_synthea_snapshot(root, REQUIRED_EVIDENCE_CLASS)
            history = ListHistory(
                [
                    HistoricalSnapshot(
                        source_system="synthea_csv",
                        evidence_class=REQUIRED_EVIDENCE_CLASS,
                        status=SUCCEEDED,
                        file_hashes={item.filename: item.sha256 for item in prepared.file_identities},
                    )
                ]
            )
            replayed = assess_synthea_snapshot(root, REQUIRED_EVIDENCE_CLASS, history)
        self.assertEqual(replayed.outcome, "replayed")
        self.assertTrue(replayed.replay.is_replay)
        self.assertEqual(replayed.replay.matched_status, SUCCEEDED)
        self.assertFalse(replayed.row_processing_performed)
        self.assertEqual(replayed.rows, ())

    def test_partial_or_failed_history_is_not_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_snapshot(root)
            prepared = assess_synthea_snapshot(root, REQUIRED_EVIDENCE_CLASS)
            hashes = {item.filename: item.sha256 for item in prepared.file_identities}
        identity = SnapshotIdentity.from_mapping(
            source_system="synthea_csv",
            evidence_class=REQUIRED_EVIDENCE_CLASS,
            file_hashes=hashes,
        )
        for status in (PARTIAL, FAILED, "replayed"):
            decision = classify_replay(
                identity,
                [
                    HistoricalSnapshot(
                        source_system="synthea_csv",
                        evidence_class=REQUIRED_EVIDENCE_CLASS,
                        status=status,
                        file_hashes=hashes,
                    )
                ],
            )
            self.assertFalse(decision.is_replay, status)
            self.assertTrue(decision.evaluated, status)
        mismatched = classify_replay(
            identity,
            [
                HistoricalSnapshot(
                    source_system="synthea_csv",
                    evidence_class="synthetic_faker",
                    status=SUCCEEDED,
                    file_hashes=hashes,
                )
            ],
        )
        self.assertFalse(mismatched.is_replay)
        unconfigured = classify_replay(identity, None)
        self.assertFalse(unconfigured.evaluated)
        self.assertFalse(unconfigured.is_replay)


class FingerprintTests(unittest.TestCase):
    def test_raw_text_is_not_normalized_before_fingerprint(self) -> None:
        header = FILE_HEADERS["patients.csv"]
        raw = [""] * len(header)
        raw[header.index("ZIP")] = "00000"
        raw[header.index("Id")] = "7cfebcfc-e1cc-2993-d512-9c450d4457b8"
        padded = list(raw)
        padded[header.index("ZIP")] = "0"
        spaced = list(raw)
        spaced[header.index("GENDER")] = " M"
        original = source_row_fingerprint(
            source_system="synthea_csv",
            source_table="patients",
            header=header,
            raw_values=raw,
        )
        self.assertNotEqual(
            original,
            source_row_fingerprint(
                source_system="synthea_csv",
                source_table="patients",
                header=header,
                raw_values=padded,
            ),
        )
        self.assertNotEqual(
            original,
            source_row_fingerprint(
                source_system="synthea_csv",
                source_table="patients",
                header=header,
                raw_values=spaced,
            ),
        )
        self.assertEqual(raw[header.index("Id")], "7cfebcfc-e1cc-2993-d512-9c450d4457b8")

    def test_duplicate_rows_in_one_file_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_snapshot(root)
            text = (root / "conditions.csv").read_text(encoding="utf-8")
            header, body = text.splitlines()
            (root / "conditions.csv").write_text(
                header + "\n" + body + "\n" + body + "\n",
                encoding="utf-8",
                newline="",
            )
            result = assess_synthea_snapshot(root, REQUIRED_EVIDENCE_CLASS)
        groups = [group for group in result.duplicate_groups if group.filename == "conditions.csv"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].source_row_numbers, (1, 2))
        flagged = [
            row.source_row_number
            for row in result.rows
            if row.filename == "conditions.csv" and row.duplicate_in_file
        ]
        self.assertEqual(flagged, [1, 2])
        condition_ids = [
            row.source_record_id
            for row in result.rows
            if row.filename == "conditions.csv"
        ]
        self.assertEqual(condition_ids, [None, None])
        patient = next(row for row in result.rows if row.filename == "patients.csv")
        self.assertEqual(patient.source_record_id, "7cfebcfc-e1cc-2993-d512-9c450d4457b8")
        self.assertEqual(patient.raw_values[patient.header.index("ZIP")], "00000")

    def test_different_source_tables_are_not_the_same_payload(self) -> None:
        self.assertFalse(
            payload_contrast_eligible(
                snapshot_id_a="snapshot-a",
                snapshot_id_b="snapshot-b",
                source_system_a="synthea_csv",
                source_system_b="synthea_csv",
                source_table_a="conditions",
                source_table_b="procedures",
                fingerprint_a="same-digest",
                fingerprint_b="same-digest",
            )
        )
        header = ("CODE",)
        shared = source_row_fingerprint(
            source_system="synthea_csv",
            source_table="conditions",
            header=header,
            raw_values=("314529007",),
        )
        other = source_row_fingerprint(
            source_system="synthea_csv",
            source_table="procedures",
            header=header,
            raw_values=("314529007",),
        )
        self.assertNotEqual(shared, other)


class TimeParseTests(unittest.TestCase):
    def test_date_has_no_midnight_instant(self) -> None:
        parsed = parse_clinical_time("2023-12-18")
        self.assertIsInstance(parsed, ParsedClinicalTime)
        self.assertEqual(parsed.precision, "date")
        self.assertEqual(parsed.calendar_date, dt.date(2023, 12, 18))
        self.assertIsNone(parsed.clock_time)
        self.assertIsNone(parsed.instant_utc)
        self.assertIs(parsed.timezone_known, False)
        self.assertEqual(parsed.raw_text, "2023-12-18")

    def test_utc_datetime_keeps_utc_fields(self) -> None:
        parsed = parse_clinical_time("2023-12-18T13:31:25Z")
        self.assertIsInstance(parsed, ParsedClinicalTime)
        self.assertEqual(parsed.precision, "datetime")
        self.assertEqual(parsed.calendar_date, dt.date(2023, 12, 18))
        self.assertEqual(parsed.clock_time, dt.time(13, 31, 25))
        self.assertEqual(
            parsed.instant_utc,
            dt.datetime(2023, 12, 18, 13, 31, 25, tzinfo=dt.timezone.utc),
        )
        self.assertIs(parsed.timezone_known, True)
        self.assertEqual(parsed.instant_utc.utcoffset(), dt.timedelta(0))

    def test_empty_is_absent(self) -> None:
        parsed = parse_clinical_time("")
        self.assertIsInstance(parsed, ParsedClinicalTime)
        self.assertEqual(parsed.precision, "absent")
        self.assertIsNone(parsed.calendar_date)
        self.assertIsNone(parsed.clock_time)
        self.assertIsNone(parsed.instant_utc)
        self.assertIsNone(parsed.timezone_known)

    def test_invalid_calendar_and_clock_and_unregistered_formats(self) -> None:
        cases = {
            "2023-02-29": "invalid date",
            "2024-13-01": "invalid month",
            "2023-12-18T24:00:00Z": "invalid hour",
            "2023-12-18T13:60:00Z": "invalid minute",
            "2023-12-18T13:31:60Z": "invalid second",
            "2023-12-18T13:31:25": "missing Z",
            "2023-12-18 13:31:25Z": "space separator",
            "2023-12-18T13:31:25.000Z": "fractional seconds",
            "2023-12-18T13:31:25+00:00": "numeric offset",
        }
        for raw, label in cases.items():
            with self.subTest(label=label):
                failed = parse_clinical_time(
                    raw,
                    filename="encounters.csv",
                    source_table="encounters",
                    field_name="START",
                    source_row_number=4,
                )
                self.assertIsInstance(failed, TimeParseFailure)
                self.assertEqual(failed.code, INVALID_TIME)
                self.assertEqual(failed.raw_text, raw)
                self.assertEqual(failed.filename, "encounters.csv")
                self.assertEqual(failed.field_name, "START")
                self.assertEqual(failed.source_row_number, 4)
                self.assertIn(raw, failed.message)

    def test_snapshot_stops_time_parsing_when_unregistered_text_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_snapshot(root)
            text = (root / "patients.csv").read_text(encoding="utf-8")
            text = text.replace("2023-12-18", "18/12/2023", 1)
            (root / "patients.csv").write_text(text, encoding="utf-8", newline="")
            result = assess_synthea_snapshot(root, REQUIRED_EVIDENCE_CLASS)
        self.assertEqual(result.outcome, "time_parse_blocked")
        self.assertFalse(result.time_parse_performed)
        self.assertTrue(result.row_processing_performed)
        self.assertTrue(result.rows)
        self.assertTrue(any(item.other == 1 and item.field_name == "BIRTHDATE" for item in result.time_scan))
        self.assertTrue(all(item.parsed is None for item in result.time_outcomes))
        self.assertEqual(result.time_outcomes[0].failure.code, INVALID_TIME)
        self.assertEqual(result.time_outcomes[0].failure.raw_text, "18/12/2023")

    def test_invalid_calendar_in_snapshot_rejects_only_that_cell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_snapshot(root)
            text = (root / "conditions.csv").read_text(encoding="utf-8")
            text = text.replace("2024-01-22", "2023-02-29", 1)
            (root / "conditions.csv").write_text(text, encoding="utf-8", newline="")
            result = assess_synthea_snapshot(root, REQUIRED_EVIDENCE_CLASS)
        self.assertEqual(result.outcome, "prepared")
        self.assertTrue(result.time_parse_performed)
        stop = next(
            item
            for item in result.time_outcomes
            if item.filename == "conditions.csv" and item.field_name == "STOP"
        )
        start = next(
            item
            for item in result.time_outcomes
            if item.filename == "conditions.csv" and item.field_name == "START"
        )
        self.assertEqual(stop.failure.code, INVALID_TIME)
        self.assertEqual(stop.failure.raw_text, "2023-02-29")
        self.assertEqual(stop.failure.source_row_number, 1)
        self.assertEqual(start.parsed.precision, "date")
        self.assertIsNone(start.parsed.instant_utc)

    def test_prepared_snapshot_parses_registered_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_snapshot(root)
            result = assess_synthea_snapshot(root, REQUIRED_EVIDENCE_CLASS)
        birth = next(
            item
            for item in result.time_outcomes
            if item.filename == "patients.csv" and item.field_name == "BIRTHDATE"
        )
        death = next(
            item
            for item in result.time_outcomes
            if item.filename == "patients.csv" and item.field_name == "DEATHDATE"
        )
        start = next(
            item
            for item in result.time_outcomes
            if item.filename == "encounters.csv" and item.field_name == "START"
        )
        stop = next(
            item
            for item in result.time_outcomes
            if item.filename == "medications.csv" and item.field_name == "STOP"
        )
        self.assertEqual(birth.parsed.precision, "date")
        self.assertIsNone(birth.parsed.instant_utc)
        self.assertEqual(death.parsed.precision, "absent")
        self.assertEqual(start.parsed.instant_utc.tzinfo, dt.timezone.utc)
        self.assertEqual(stop.parsed.precision, "absent")
        self.assertTrue(all(item.other == 0 for item in result.time_scan))
        self.assertTrue(all(item.invalid_calendar_date == 0 for item in result.time_scan))


class RowWidthTests(unittest.TestCase):
    def test_matching_width_rows_remain_auditable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_snapshot(root)
            result = assess_synthea_snapshot(root, REQUIRED_EVIDENCE_CLASS)
        self.assertEqual(result.row_width_errors, ())
        conditions = [row for row in result.rows if row.filename == "conditions.csv"]
        self.assertEqual(len(conditions), 1)
        self.assertEqual(len(conditions[0].raw_values), len(FILE_HEADERS["conditions.csv"]))
        self.assertTrue(conditions[0].fingerprint)

    def test_short_and_long_rows_are_row_errors_without_stopping_the_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_snapshot(root)
            text = (root / "conditions.csv").read_text(encoding="utf-8")
            header, valid = text.splitlines()
            short = ",".join(valid.split(",")[:-1])
            long = valid + ",extra-cell"
            (root / "conditions.csv").write_text(
                "\n".join((header, valid, short, long)) + "\n",
                encoding="utf-8",
                newline="",
            )
            result = assess_synthea_snapshot(root, REQUIRED_EVIDENCE_CLASS)
        self.assertEqual(result.outcome, "prepared")
        self.assertEqual(result.structure_errors, ())
        self.assertTrue(result.row_processing_performed)
        self.assertTrue(result.time_parse_performed)
        condition_rows = [row for row in result.rows if row.filename == "conditions.csv"]
        self.assertEqual([row.source_row_number for row in condition_rows], [1])
        self.assertEqual(len(result.row_width_errors), 2)
        short_error, long_error = result.row_width_errors
        expected = len(FILE_HEADERS["conditions.csv"])
        self.assertEqual(short_error.code, "RAW_ROW_WIDTH_MISMATCH")
        self.assertEqual(short_error.mismatch, "too_few")
        self.assertEqual(short_error.source_system, "synthea_csv")
        self.assertEqual(short_error.source_table, "conditions")
        self.assertEqual(short_error.filename, "conditions.csv")
        self.assertTrue(short_error.source_file_id.startswith("conditions.csv:"))
        self.assertEqual(short_error.source_row_number, 2)
        self.assertEqual(short_error.actual_column_count, expected - 1)
        self.assertEqual(short_error.expected_column_count, expected)
        self.assertEqual(len(short_error.raw_values), expected - 1)
        self.assertNotIn("", short_error.raw_values[-1:])
        self.assertEqual(long_error.mismatch, "too_many")
        self.assertEqual(long_error.source_row_number, 3)
        self.assertEqual(long_error.actual_column_count, expected + 1)
        self.assertEqual(long_error.raw_values[-1], "extra-cell")
        parsed_rows = {
            item.source_row_number
            for item in result.time_outcomes
            if item.filename == "conditions.csv"
        }
        self.assertEqual(parsed_rows, {1})
        self.assertTrue(all(row.filename != "conditions.csv" or row.source_row_number == 1 for row in result.rows))


if __name__ == "__main__":
    unittest.main()
