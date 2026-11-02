"""Persistence, quality, and matching adapter tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from healthcare_data_platform.ingest.persist import import_snapshot
from healthcare_data_platform.ingest.synthea.contract import FILE_HEADERS, REQUIRED_EVIDENCE_CLASS
from healthcare_data_platform.matching.adapter import evaluate_platform_patient, load_protocols, to_patient_chart
from healthcare_data_platform.patients.explorer import search_patients
from healthcare_data_platform.persistence.database import get_engine, init_db
from healthcare_data_platform.quality.report import summary


def _write_snapshot(directory: Path, *, condition_rows: list[str], patient_birth: str = "1980-01-01") -> None:
    patient = (
        "p1,1980-01-01,,ssn,d,p,Mr,Ada,Q,Lovelace,,,S,white,nonhispanic,F,city,1,City,ST,County,1,00000,0,0,0,0,1"
    ).replace("1980-01-01", patient_birth, 1)
    files = {
        "patients.csv": [patient],
        "encounters.csv": [
            "e1,2020-01-01T00:00:00Z,2020-01-01T01:00:00Z,p1,org,prov,payer,ambulatory,185345009,Encounter,0,0,0,,"
        ],
        "conditions.csv": condition_rows,
        "medications.csv": [
            "2020-01-02T00:00:00Z,,p1,payer,e1,123,Aspirin,0,0,1,0,,"
        ],
        "observations.csv": [
            "2020-01-03T00:00:00Z,p1,e1,laboratory,4548-4,Hemoglobin A1c,7.2,%,numeric"
        ],
        "procedures.csv": [
            "2020-01-04T00:00:00Z,2020-01-04T00:30:00Z,p1,e1,http://snomed.info/sct,123,Procedure,0,,"
        ],
    }
    for name, rows in files.items():
        header = ",".join(FILE_HEADERS[name])
        (directory / name).write_text(header + "\n" + "\n".join(rows) + "\n", encoding="utf-8")


class PlatformImportTests(unittest.TestCase):
    def test_import_keeps_source_values_and_quarantines_width_and_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = root / "snap"
            snapshot.mkdir()
            _write_snapshot(
                snapshot,
                condition_rows=[
                    "2020-01-01,2020-02-01,p1,e1,http://snomed.info/sct,44054006,Diabetes",
                    "2020-01-01,2020-02-01,p1,e1,http://snomed.info/sct,44054006,Diabetes",
                    "2020-01-01,2020-02-01,p1,e1,http://snomed.info/sct,44054006",
                ],
            )
            engine = init_db(get_engine(root / "db.sqlite"))
            result = import_snapshot(engine, snapshot, REQUIRED_EVIDENCE_CLASS)
            self.assertEqual(result.status, "partial")
            report = summary(engine)
            self.assertEqual(report["counts"]["patient"], 1)
            self.assertEqual(report["counts"]["condition"], 0)
            self.assertGreaterEqual(report["quarantined_rows"], 3)
            self.assertEqual(report["dangling_patient_links"], 0)
            found = search_patients(engine, "Ada")
            self.assertEqual(found[0]["gender_source"], "F")
            self.assertEqual(found[0]["birth_raw"], "1980-01-01")
            chart = {
                "patient": {
                    "record_id": found[0]["record_id"],
                    "gender_source": "F",
                    "birth_calendar_date": found[0]["birth_raw"][:10] if False else __import__("datetime").date(1980, 1, 1),
                    "race_source": "white",
                },
                "conditions": [],
                "medications": [],
                "observations": [],
            }
            built = to_patient_chart(
                {
                    "patient": chart["patient"],
                    "conditions": [
                        {
                            "record_id": 9,
                            "code_source": "44054006",
                            "system_source": "http://snomed.info/sct",
                            "text_source": "Diabetes",
                            "start_calendar_date": __import__("datetime").date(2020, 1, 1),
                        },
                        {
                            "record_id": 10,
                            "code_source": "E11.9",
                            "system_source": "http://hl7.org/fhir/sid/icd-10",
                            "text_source": "Type 2 diabetes mellitus",
                            "start_calendar_date": __import__("datetime").date(2020, 1, 1),
                        },
                    ],
                    "medications": [],
                    "observations": [],
                }
            )
            self.assertEqual([item.icd_code for item in built.conditions], ["E11.9"])
            engine.dispose()

    def test_missing_evidence_fails_without_patients(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = root / "snap"
            snapshot.mkdir()
            _write_snapshot(snapshot, condition_rows=["2020-01-01,,p1,e1,s,1,Text"])
            engine = init_db(get_engine(root / "db.sqlite"))
            result = import_snapshot(engine, snapshot, None)
            self.assertEqual(result.status, "failed")
            self.assertEqual(summary(engine)["counts"]["patient"], 0)
            engine.dispose()

    def test_replay_does_not_duplicate_patients(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = root / "snap"
            snapshot.mkdir()
            _write_snapshot(snapshot, condition_rows=["2020-01-01,,p1,e1,s,1,Text"])
            engine = init_db(get_engine(root / "db.sqlite"))
            first = import_snapshot(engine, snapshot, REQUIRED_EVIDENCE_CLASS)
            self.assertEqual(first.status, "succeeded")
            second = import_snapshot(engine, snapshot, REQUIRED_EVIDENCE_CLASS)
            self.assertEqual(second.status, "replayed")
            self.assertEqual(summary(engine)["counts"]["patient"], 1)
            engine.dispose()

    def test_age_protocol_uses_existing_engine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = root / "snap"
            snapshot.mkdir()
            _write_snapshot(snapshot, condition_rows=["2020-01-01,,p1,e1,s,1,Text"])
            engine = init_db(get_engine(root / "db.sqlite"))
            import_snapshot(engine, snapshot, REQUIRED_EVIDENCE_CLASS)
            patient_id = search_patients(engine, "Ada")[0]["record_id"]
            protocol = next(item for item in load_protocols() if item["protocol_id"] == "PROT-AGE-ONLY")
            result = evaluate_platform_patient(engine, patient_id, protocol)
            self.assertEqual(result.patient_status.value, "eligible")
            self.assertIn("age", result.rule_evaluations)
            basic = next(item for item in load_protocols() if item["protocol_id"] == "PROT-BASIC-T2DM")
            basic_result = evaluate_platform_patient(engine, patient_id, basic)
            t2dm = basic_result.rule_evaluations["t2dm"]
            self.assertEqual(t2dm.status.value, "insufficient_evidence")
            self.assertIn("s|1", str(t2dm.evidence))
            self.assertNotEqual(basic_result.patient_status.value, "not_eligible")
            medication = next(item for item in load_protocols() if item["protocol_id"] == "PROT-MED-METFORMIN")
            medication_result = evaluate_platform_patient(engine, patient_id, medication)
            self.assertEqual(medication_result.rule_evaluations["met"].status.value, "not_eligible")
            self.assertIn("Aspirin", str(medication_result.rule_evaluations["met"].evidence))
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
