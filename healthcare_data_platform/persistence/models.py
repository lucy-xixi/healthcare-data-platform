"""Audit tables and the six frozen clinical entities.

Deferred audit tables are created so the schema can be extended later.
This version does not fill payload comparisons or quarantine decisions.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class SourceFile(Base):
    __tablename__ = "source_file"
    __table_args__ = (UniqueConstraint("source_system", "sha256"),)

    source_file_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_system: Mapped[str] = mapped_column(String(64))
    sha256: Mapped[str] = mapped_column(String(64))
    original_filename: Mapped[str] = mapped_column(String(255))
    file_size_bytes: Mapped[int] = mapped_column(Integer)
    evidence_class: Mapped[str] = mapped_column(String(64))
    first_successful_ingest_at: Mapped[datetime] = mapped_column(DateTime)


class IngestBatch(Base):
    __tablename__ = "ingest_batch"

    ingest_batch_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_system: Mapped[str] = mapped_column(String(64))
    declared_evidence_class: Mapped[str | None] = mapped_column(String(128), nullable=True)
    validated_evidence_class: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32))
    snapshot_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    replay_of_batch_id: Mapped[int | None] = mapped_column(ForeignKey("ingest_batch.ingest_batch_id"), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime)
    completed_at: Mapped[datetime] = mapped_column(DateTime)
    accepted_row_count: Mapped[int] = mapped_column(Integer, default=0)
    quarantined_row_count: Mapped[int] = mapped_column(Integer, default=0)


class IngestBatchFile(Base):
    __tablename__ = "ingest_batch_file"
    __table_args__ = (UniqueConstraint("ingest_batch_id", "original_filename"),)

    ingest_batch_file_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ingest_batch_id: Mapped[int] = mapped_column(ForeignKey("ingest_batch.ingest_batch_id"))
    source_file_id: Mapped[int | None] = mapped_column(ForeignKey("source_file.source_file_id"), nullable=True)
    original_filename: Mapped[str] = mapped_column(String(255))
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    file_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)


class IngestBatchError(Base):
    __tablename__ = "ingest_batch_error"

    ingest_batch_error_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ingest_batch_id: Mapped[int] = mapped_column(ForeignKey("ingest_batch.ingest_batch_id"))
    code: Mapped[str] = mapped_column(String(64))
    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    field_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    message: Mapped[str] = mapped_column(Text)
    context: Mapped[str | None] = mapped_column(Text, nullable=True)


class SourceRowAudit(Base):
    __tablename__ = "source_row_audit"
    __table_args__ = (UniqueConstraint("ingest_batch_id", "ingest_batch_file_id", "source_row_number"),)

    source_row_audit_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ingest_batch_id: Mapped[int] = mapped_column(ForeignKey("ingest_batch.ingest_batch_id"))
    source_file_id: Mapped[int | None] = mapped_column(ForeignKey("source_file.source_file_id"), nullable=True)
    ingest_batch_file_id: Mapped[int] = mapped_column(ForeignKey("ingest_batch_file.ingest_batch_file_id"))
    source_system: Mapped[str] = mapped_column(String(64))
    source_table: Mapped[str] = mapped_column(String(64))
    source_row_number: Mapped[int] = mapped_column(Integer)
    header_names: Mapped[str] = mapped_column(Text)
    raw_field_values: Mapped[str] = mapped_column(Text)
    source_row_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parsed_source_record_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    disposition: Mapped[str] = mapped_column(String(32))
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_context: Mapped[str | None] = mapped_column(Text, nullable=True)


class SourceRowTimeCell(Base):
    __tablename__ = "source_row_time_cell"
    __table_args__ = (UniqueConstraint("source_row_audit_id", "field_name"),)

    source_row_time_cell_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_row_audit_id: Mapped[int] = mapped_column(ForeignKey("source_row_audit.source_row_audit_id"))
    field_name: Mapped[str] = mapped_column(String(64))
    raw_text: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(32))
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)


class SourceRecordLink(Base):
    __tablename__ = "source_record_link"
    __table_args__ = (
        UniqueConstraint("source_row_audit_id"),
        UniqueConstraint("entity_name", "canonical_record_id"),
    )

    source_record_link_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_row_audit_id: Mapped[int] = mapped_column(ForeignKey("source_row_audit.source_row_audit_id"))
    entity_name: Mapped[str] = mapped_column(String(32))
    canonical_record_id: Mapped[int] = mapped_column(Integer)
    inclusion_class: Mapped[str] = mapped_column(String(32))


class ReplayRecordLink(Base):
    __tablename__ = "replay_record_link"
    __table_args__ = (UniqueConstraint("ingest_batch_id", "entity_name", "canonical_record_id"),)

    replay_record_link_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ingest_batch_id: Mapped[int] = mapped_column(ForeignKey("ingest_batch.ingest_batch_id"))
    entity_name: Mapped[str] = mapped_column(String(32))
    canonical_record_id: Mapped[int] = mapped_column(Integer)


class PayloadComparison(Base):
    __tablename__ = "payload_comparison"
    __table_args__ = (
        UniqueConstraint(
            "source_system",
            "source_table",
            "source_row_fingerprint",
            "left_batch_id",
            "right_batch_id",
        ),
    )

    payload_comparison_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_system: Mapped[str] = mapped_column(String(64))
    source_table: Mapped[str] = mapped_column(String(64))
    source_row_fingerprint: Mapped[str] = mapped_column(String(64))
    left_batch_id: Mapped[int] = mapped_column(ForeignKey("ingest_batch.ingest_batch_id"))
    right_batch_id: Mapped[int] = mapped_column(ForeignKey("ingest_batch.ingest_batch_id"))
    status: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime)


class PayloadComparisonMember(Base):
    __tablename__ = "payload_comparison_member"
    __table_args__ = (UniqueConstraint("payload_comparison_id", "source_row_audit_id"),)

    payload_comparison_member_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    payload_comparison_id: Mapped[int] = mapped_column(ForeignKey("payload_comparison.payload_comparison_id"))
    side: Mapped[str] = mapped_column(String(8))
    source_row_audit_id: Mapped[int] = mapped_column(ForeignKey("source_row_audit.source_row_audit_id"))


class QuarantineDecision(Base):
    __tablename__ = "quarantine_decision"
    __table_args__ = (UniqueConstraint("idempotency_key"),)

    decision_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    action: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(128))
    reason: Mapped[str] = mapped_column(Text)
    decided_at: Mapped[datetime] = mapped_column(DateTime)
    idempotency_key: Mapped[str] = mapped_column(String(128))


class QuarantineDecisionMember(Base):
    __tablename__ = "quarantine_decision_member"
    __table_args__ = (UniqueConstraint("source_row_audit_id"),)

    quarantine_decision_member_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    decision_id: Mapped[int] = mapped_column(ForeignKey("quarantine_decision.decision_id"))
    source_row_audit_id: Mapped[int] = mapped_column(ForeignKey("source_row_audit.source_row_audit_id"))
    selected: Mapped[int] = mapped_column(Integer, default=0)


class Patient(Base):
    __tablename__ = "patient"

    record_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ingest_batch_id: Mapped[int] = mapped_column(ForeignKey("ingest_batch.ingest_batch_id"))
    source_system: Mapped[str] = mapped_column(String(64))
    source_table: Mapped[str] = mapped_column(String(64))
    source_record_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_row_number: Mapped[int] = mapped_column(Integer)
    evidence_class: Mapped[str] = mapped_column(String(64))
    source_created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime)
    given_name_source: Mapped[str] = mapped_column(Text, default="")
    family_name_source: Mapped[str] = mapped_column(Text, default="")
    gender_source: Mapped[str] = mapped_column(Text)
    race_source: Mapped[str] = mapped_column(Text)
    ethnicity_source: Mapped[str] = mapped_column(Text)
    birth_raw: Mapped[str] = mapped_column(Text)
    birth_precision: Mapped[str] = mapped_column(String(16))
    birth_calendar_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    birth_clock_time: Mapped[str | None] = mapped_column(String(16), nullable=True)
    birth_instant_utc: Mapped[str | None] = mapped_column(String(40), nullable=True)
    birth_timezone_known: Mapped[int | None] = mapped_column(Integer, nullable=True)
    death_raw: Mapped[str] = mapped_column(Text, default="")
    death_precision: Mapped[str] = mapped_column(String(16))
    death_calendar_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    death_clock_time: Mapped[str | None] = mapped_column(String(16), nullable=True)
    death_instant_utc: Mapped[str | None] = mapped_column(String(40), nullable=True)
    death_timezone_known: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Encounter(Base):
    __tablename__ = "encounter"

    record_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ingest_batch_id: Mapped[int] = mapped_column(ForeignKey("ingest_batch.ingest_batch_id"))
    patient_record_id: Mapped[int] = mapped_column(ForeignKey("patient.record_id"))
    source_system: Mapped[str] = mapped_column(String(64))
    source_table: Mapped[str] = mapped_column(String(64))
    source_record_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_row_number: Mapped[int] = mapped_column(Integer)
    evidence_class: Mapped[str] = mapped_column(String(64))
    source_created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime)
    class_source: Mapped[str] = mapped_column(Text)
    code_source: Mapped[str] = mapped_column(Text)
    text_source: Mapped[str] = mapped_column(Text)
    reason_code_source: Mapped[str] = mapped_column(Text, default="")
    reason_text_source: Mapped[str] = mapped_column(Text, default="")
    start_raw: Mapped[str] = mapped_column(Text)
    start_precision: Mapped[str] = mapped_column(String(16))
    start_calendar_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    start_clock_time: Mapped[str | None] = mapped_column(String(16), nullable=True)
    start_instant_utc: Mapped[str | None] = mapped_column(String(40), nullable=True)
    start_timezone_known: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_raw: Mapped[str] = mapped_column(Text, default="")
    end_precision: Mapped[str] = mapped_column(String(16))
    end_calendar_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_clock_time: Mapped[str | None] = mapped_column(String(16), nullable=True)
    end_instant_utc: Mapped[str | None] = mapped_column(String(40), nullable=True)
    end_timezone_known: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Condition(Base):
    __tablename__ = "condition"

    record_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ingest_batch_id: Mapped[int] = mapped_column(ForeignKey("ingest_batch.ingest_batch_id"))
    patient_record_id: Mapped[int] = mapped_column(ForeignKey("patient.record_id"))
    encounter_record_id: Mapped[int | None] = mapped_column(ForeignKey("encounter.record_id"), nullable=True)
    source_system: Mapped[str] = mapped_column(String(64))
    source_table: Mapped[str] = mapped_column(String(64))
    source_record_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_row_number: Mapped[int] = mapped_column(Integer)
    evidence_class: Mapped[str] = mapped_column(String(64))
    source_created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime)
    system_source: Mapped[str] = mapped_column(Text)
    code_source: Mapped[str] = mapped_column(Text)
    text_source: Mapped[str] = mapped_column(Text)
    start_raw: Mapped[str] = mapped_column(Text)
    start_precision: Mapped[str] = mapped_column(String(16))
    start_calendar_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    start_clock_time: Mapped[str | None] = mapped_column(String(16), nullable=True)
    start_instant_utc: Mapped[str | None] = mapped_column(String(40), nullable=True)
    start_timezone_known: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_raw: Mapped[str] = mapped_column(Text, default="")
    end_precision: Mapped[str] = mapped_column(String(16))
    end_calendar_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_clock_time: Mapped[str | None] = mapped_column(String(16), nullable=True)
    end_instant_utc: Mapped[str | None] = mapped_column(String(40), nullable=True)
    end_timezone_known: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Medication(Base):
    __tablename__ = "medication"

    record_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ingest_batch_id: Mapped[int] = mapped_column(ForeignKey("ingest_batch.ingest_batch_id"))
    patient_record_id: Mapped[int] = mapped_column(ForeignKey("patient.record_id"))
    encounter_record_id: Mapped[int | None] = mapped_column(ForeignKey("encounter.record_id"), nullable=True)
    source_system: Mapped[str] = mapped_column(String(64))
    source_table: Mapped[str] = mapped_column(String(64))
    source_record_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_row_number: Mapped[int] = mapped_column(Integer)
    evidence_class: Mapped[str] = mapped_column(String(64))
    source_created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime)
    code_source: Mapped[str] = mapped_column(Text)
    text_source: Mapped[str] = mapped_column(Text)
    dispenses_source: Mapped[str] = mapped_column(Text, default="")
    reason_code_source: Mapped[str] = mapped_column(Text, default="")
    reason_text_source: Mapped[str] = mapped_column(Text, default="")
    start_raw: Mapped[str] = mapped_column(Text)
    start_precision: Mapped[str] = mapped_column(String(16))
    start_calendar_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    start_clock_time: Mapped[str | None] = mapped_column(String(16), nullable=True)
    start_instant_utc: Mapped[str | None] = mapped_column(String(40), nullable=True)
    start_timezone_known: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_raw: Mapped[str] = mapped_column(Text, default="")
    end_precision: Mapped[str] = mapped_column(String(16))
    end_calendar_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_clock_time: Mapped[str | None] = mapped_column(String(16), nullable=True)
    end_instant_utc: Mapped[str | None] = mapped_column(String(40), nullable=True)
    end_timezone_known: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Observation(Base):
    __tablename__ = "observation"

    record_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ingest_batch_id: Mapped[int] = mapped_column(ForeignKey("ingest_batch.ingest_batch_id"))
    patient_record_id: Mapped[int] = mapped_column(ForeignKey("patient.record_id"))
    encounter_record_id: Mapped[int | None] = mapped_column(ForeignKey("encounter.record_id"), nullable=True)
    source_system: Mapped[str] = mapped_column(String(64))
    source_table: Mapped[str] = mapped_column(String(64))
    source_record_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_row_number: Mapped[int] = mapped_column(Integer)
    evidence_class: Mapped[str] = mapped_column(String(64))
    source_created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime)
    category_source: Mapped[str] = mapped_column(Text, default="")
    code_source: Mapped[str] = mapped_column(Text)
    text_source: Mapped[str] = mapped_column(Text)
    value_text: Mapped[str] = mapped_column(Text)
    value_numeric: Mapped[float | None] = mapped_column(Float, nullable=True)
    value_type_source: Mapped[str] = mapped_column(Text, default="")
    unit_source: Mapped[str] = mapped_column(Text, default="")
    clinical_raw: Mapped[str] = mapped_column(Text)
    clinical_precision: Mapped[str] = mapped_column(String(16))
    clinical_calendar_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    clinical_clock_time: Mapped[str | None] = mapped_column(String(16), nullable=True)
    clinical_instant_utc: Mapped[str | None] = mapped_column(String(40), nullable=True)
    clinical_timezone_known: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Procedure(Base):
    __tablename__ = "procedure"

    record_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ingest_batch_id: Mapped[int] = mapped_column(ForeignKey("ingest_batch.ingest_batch_id"))
    patient_record_id: Mapped[int] = mapped_column(ForeignKey("patient.record_id"))
    encounter_record_id: Mapped[int | None] = mapped_column(ForeignKey("encounter.record_id"), nullable=True)
    source_system: Mapped[str] = mapped_column(String(64))
    source_table: Mapped[str] = mapped_column(String(64))
    source_record_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_row_number: Mapped[int] = mapped_column(Integer)
    evidence_class: Mapped[str] = mapped_column(String(64))
    source_created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime)
    system_source: Mapped[str] = mapped_column(Text)
    code_source: Mapped[str] = mapped_column(Text)
    text_source: Mapped[str] = mapped_column(Text)
    reason_code_source: Mapped[str] = mapped_column(Text, default="")
    reason_text_source: Mapped[str] = mapped_column(Text, default="")
    start_raw: Mapped[str] = mapped_column(Text)
    start_precision: Mapped[str] = mapped_column(String(16))
    start_calendar_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    start_clock_time: Mapped[str | None] = mapped_column(String(16), nullable=True)
    start_instant_utc: Mapped[str | None] = mapped_column(String(40), nullable=True)
    start_timezone_known: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_raw: Mapped[str] = mapped_column(Text, default="")
    end_precision: Mapped[str] = mapped_column(String(16))
    end_calendar_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_clock_time: Mapped[str | None] = mapped_column(String(16), nullable=True)
    end_instant_utc: Mapped[str | None] = mapped_column(String(40), nullable=True)
    end_timezone_known: Mapped[int | None] = mapped_column(Integer, nullable=True)
