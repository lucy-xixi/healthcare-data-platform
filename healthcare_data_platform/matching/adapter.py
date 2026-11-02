"""Adapt canonical rows to the existing deterministic trial-matching engine.

The engine remains the implementation in medical-ai-platform. This module only
builds a PatientChart and loads that project's protocol files.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from healthcare_data_platform.patients.explorer import patient_chart


def medical_ai_root() -> Path:
    configured = os.environ.get("MEDICAL_AI_PLATFORM_ROOT")
    if configured:
        return Path(configured)
    candidate = Path(r"e:\medical-ai-platform")
    if (candidate / "trial_matching" / "evaluator.py").is_file():
        return candidate
    raise FileNotFoundError("medical-ai-platform trial_matching package was not found")


def _ensure_path() -> Path:
    root = medical_ai_root()
    root_text = str(root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    return root


@lru_cache(maxsize=1)
def load_protocols() -> list[dict]:
    root = _ensure_path()
    protocols = []
    for path in sorted((root / "evaluation" / "gold" / "protocols").glob("*.json")):
        protocols.append(json.loads(path.read_text(encoding="utf-8")))
    return protocols


def evaluate_platform_patient(engine, record_id: int, protocol_payload: dict):
    _ensure_path()
    from trial_matching.evaluator import evaluate_patient
    from trial_matching.schemas import TrialProtocol

    chart_rows = patient_chart(engine, record_id, observation_limit=None)
    if chart_rows is None:
        raise LookupError(f"patient {record_id} was not found")
    chart = to_patient_chart(chart_rows)
    if chart is None:
        raise ValueError("患者没有可用的出生日期，年龄规则无法在不编造日期的情况下执行")
    protocol = TrialProtocol.model_validate(protocol_payload)
    result = evaluate_patient(chart, protocol)
    return _annotate_source_limits(result, chart_rows, protocol_payload)


def source_code_system_is_icd(system_source: str) -> bool:
    """True only when the source names an ICD system.

    Other systems, including SNOMED, stay outside ICD comparison. Their codes
    are not rewritten and are not treated as ICD misses.
    """

    text = system_source.strip().lower()
    return any(marker in text for marker in ("icd-10", "icd10", "icd-9", "icd9"))


def _source_evidence(rule_id: str, reason: str, row: dict, field_name: str, field_value: str, record_date) -> dict:
    return {
        "rule_id": rule_id,
        "reason": reason,
        "table_name": row.get("source_table"),
        "record_id": row.get("record_id"),
        "field_name": field_name,
        "field_value": field_value,
        "record_date": str(record_date) if record_date else None,
        "source_filename": row.get("source_filename"),
        "source_table": row.get("source_table"),
        "source_row_number": row.get("source_row_number"),
    }


def _annotate_source_limits(result, chart_rows: dict, protocol_payload: dict):
    """Attach source rows the engine cannot interpret, without changing its status."""

    from trial_matching.schemas import CriterionStatus

    non_icd = [
        row
        for row in chart_rows["conditions"]
        if not source_code_system_is_icd(str(row.get("system_source") or ""))
    ]
    for rule in protocol_payload.get("rules") or []:
        evaluation = result.rule_evaluations.get(rule.get("rule_id"))
        if evaluation is None:
            continue
        evidence = list(evaluation.evidence)
        summary = evaluation.summary
        if rule.get("type") == "icd" and evaluation.status == CriterionStatus.INSUFFICIENT_EVIDENCE and non_icd:
            for row in non_icd[:8]:
                evidence.append(
                    _source_evidence(
                        rule["rule_id"],
                        "源诊断使用非 ICD 编码体系，原编码已保留且未做术语映射，因此本条不能判为不符合",
                        row,
                        "code_source",
                        f"{row.get('system_source')}|{row.get('code_source')}",
                        row.get("start_calendar_date"),
                    )
                )
            summary = (
                f"有 {len(non_icd)} 条源诊断，编码体系不是 ICD，不能按 {rule.get('icd_code')} 判断；"
                "证据不足，不是不符合"
            )
        elif rule.get("type") == "medication_in_window" and chart_rows["medications"]:
            for row in chart_rows["medications"][:3]:
                evidence.append(
                    _source_evidence(
                        rule["rule_id"],
                        "参与比较的源药品名称，未翻译成规则中的药名",
                        row,
                        "text_source",
                        str(row.get("text_source") or ""),
                        row.get("start_calendar_date"),
                    )
                )
        elif rule.get("type") in ("lab_threshold", "lab_exists_in_window") and evaluation.status == CriterionStatus.INSUFFICIENT_EVIDENCE:
            examples = [row for row in chart_rows["observations"] if row.get("value_numeric") is not None][:3]
            for row in examples:
                evidence.append(
                    _source_evidence(
                        rule["rule_id"],
                        "源检验名称未改写成规则中的 measurement_type",
                        row,
                        "text_source",
                        str(row.get("text_source") or ""),
                        row.get("clinical_calendar_date"),
                    )
                )
            if examples and rule.get("measurement_type") not in {row.get("text_source") for row in examples}:
                summary = (
                    f"{evaluation.summary}。平台保留源检验名称，没有把它改写成 {rule.get('measurement_type')}"
                )
        if evidence != list(evaluation.evidence) or summary != evaluation.summary:
            result.rule_evaluations[rule["rule_id"]] = evaluation.model_copy(
                update={"summary": summary, "evidence": evidence}
            )
    return result


def to_patient_chart(chart_rows: dict):
    """Map source-preserving canonical rows onto the engine's chart.

    Condition codes stay source text. They are not rewritten into ICD.
    Date-only clinical values stay dates. They are not stored as midnight.
    """

    _ensure_path()
    from trial_matching.patient_repository import (
        ConditionRecord,
        MedicationRecord,
        ObservationRecord,
        PatientChart,
        PatientDemographics,
    )

    patient = chart_rows["patient"]
    birth = patient.get("birth_calendar_date")
    if birth is None:
        return None
    if isinstance(birth, str):
        birth = date.fromisoformat(birth[:10])
    demo = PatientDemographics(
        patient_id=int(patient["record_id"]),
        gender=str(patient.get("gender_source") or ""),
        birth_date=birth,
        race=str(patient.get("race_source") or ""),
    )
    conditions = []
    for row in chart_rows["conditions"]:
        onset = _as_date(row.get("start_calendar_date"))
        if onset is None or not source_code_system_is_icd(str(row.get("system_source") or "")):
            continue
        conditions.append(
            ConditionRecord(
                condition_id=int(row["record_id"]),
                patient_id=int(patient["record_id"]),
                icd_code=str(row.get("code_source") or ""),
                condition_name=str(row.get("text_source") or ""),
                onset_date=onset,
            )
        )
    medications = []
    for row in chart_rows["medications"]:
        start = _as_date(row.get("start_calendar_date"))
        if start is None:
            continue
        medications.append(
            MedicationRecord(
                medication_id=int(row["record_id"]),
                patient_id=int(patient["record_id"]),
                drug_name=str(row.get("text_source") or ""),
                start_date=start,
                end_date=_as_date(row.get("end_calendar_date")),
            )
        )
    observations = []
    for row in chart_rows["observations"]:
        if row.get("value_numeric") is None:
            continue
        observed = _as_observed(row)
        if observed is None:
            continue
        observations.append(
            ObservationRecord(
                observation_id=int(row["record_id"]),
                patient_id=int(patient["record_id"]),
                measurement_type=str(row.get("text_source") or ""),
                value=Decimal(str(row["value_text"])),
                unit=str(row.get("unit_source") or ""),
                observed_at=observed,
            )
        )
    return PatientChart(
        demographics=demo,
        conditions=conditions,
        medications=medications,
        observations=observations,
    )


def run_gold_evaluation() -> dict:
    root = _ensure_path()
    from evaluation.gold_classification_metrics import compute_classification_metrics
    from evaluation.gold_consistency_scan import run_consistency_scan

    scan = run_consistency_scan(root / "evaluation" / "gold")
    metrics = compute_classification_metrics(scan.rows)
    mismatches = [row for row in scan.rows if not row.execution_error and not row.match]
    reasons: dict[str, int] = {}
    for row in mismatches:
        key = row.preliminary_mismatch_reason or "unclassified"
        reasons[key] = reasons.get(key, 0) + 1
    evaluated = [row for row in scan.rows if not row.execution_error]
    examples = [
        {
            "case_id": row.case_id,
            "patient_id": row.patient_id,
            "protocol_id": row.protocol_id,
            "gold": row.gold_patient_status,
            "actual": row.actual_patient_status,
            "reason": row.preliminary_mismatch_reason,
            "summary": row.engine_patient_summary,
        }
        for row in mismatches[:12]
    ]
    overall = metrics["overall"]
    return {
        "sample_count": len(scan.rows),
        "evaluated_count": overall["evaluated_cases"],
        "match_count": overall["classification_matches"],
        "mismatch_count": overall["classification_mismatches"],
        "agreement_rate": overall["accuracy"],
        "disclaimer": metrics.get("disclaimer"),
        "reasons": sorted(reasons.items(), key=lambda item: item[1], reverse=True),
        "examples": examples,
        "per_class": metrics.get("per_class_metrics"),
    }


def _as_date(value):
    if value is None or value == "":
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    return date.fromisoformat(str(value)[:10])


def _as_observed(row):
    if row.get("clinical_precision") == "datetime" and row.get("clinical_instant_utc"):
        return datetime.fromisoformat(str(row["clinical_instant_utc"]))
    if row.get("clinical_precision") == "date" and row.get("clinical_calendar_date"):
        return _as_date(row["clinical_calendar_date"])
    return None
