"""Quality figures computed from imported rows."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine


def summary(engine: Engine) -> dict:
    with engine.connect() as conn:
        counts = {
            "patient": _count(conn, "patient"),
            "encounter": _count(conn, "encounter"),
            "condition": _count(conn, "condition"),
            "medication": _count(conn, "medication"),
            "observation": _count(conn, "observation"),
            "procedure": _count(conn, "procedure"),
        }
        accepted = conn.execute(text("select count(*) from source_row_audit where disposition='accepted'")).scalar_one()
        quarantined = conn.execute(text("select count(*) from source_row_audit where disposition='quarantined'")).scalar_one()
        by_code = conn.execute(
            text(
                "select error_code, count(*) from source_row_audit "
                "where disposition='quarantined' group by error_code order by count(*) desc"
            )
        ).all()
        dangling_patient = conn.execute(
            text(
                "select "
                "(select count(*) from encounter e left join patient p on p.record_id=e.patient_record_id where p.record_id is null) + "
                "(select count(*) from condition c left join patient p on p.record_id=c.patient_record_id where p.record_id is null) + "
                "(select count(*) from medication m left join patient p on p.record_id=m.patient_record_id where p.record_id is null) + "
                "(select count(*) from observation o left join patient p on p.record_id=o.patient_record_id where p.record_id is null) + "
                "(select count(*) from procedure r left join patient p on p.record_id=r.patient_record_id where p.record_id is null)"
            )
        ).scalar_one()
        dangling_encounter = conn.execute(
            text(
                "select "
                "(select count(*) from condition c left join encounter e on e.record_id=c.encounter_record_id "
                " where c.encounter_record_id is not null and e.record_id is null) + "
                "(select count(*) from medication m left join encounter e on e.record_id=m.encounter_record_id "
                " where m.encounter_record_id is not null and e.record_id is null) + "
                "(select count(*) from observation o left join encounter e on e.record_id=o.encounter_record_id "
                " where o.encounter_record_id is not null and e.record_id is null) + "
                "(select count(*) from procedure r left join encounter e on e.record_id=r.encounter_record_id "
                " where r.encounter_record_id is not null and e.record_id is null)"
            )
        ).scalar_one()
        empty_encounter_links = conn.execute(
            text(
                "select "
                "(select count(*) from condition where encounter_record_id is null) + "
                "(select count(*) from medication where encounter_record_id is null) + "
                "(select count(*) from observation where encounter_record_id is null) + "
                "(select count(*) from procedure where encounter_record_id is null)"
            )
        ).scalar_one()
        issues = conn.execute(
            text(
                "select a.source_table, a.source_row_number, a.error_code, a.error_context, "
                "f.original_filename, a.disposition "
                "from source_row_audit a join ingest_batch_file f on f.ingest_batch_file_id=a.ingest_batch_file_id "
                "where a.disposition='quarantined' "
                "order by a.source_row_audit_id desc limit 200"
            )
        ).mappings().all()
        batches = conn.execute(
            text(
                "select ingest_batch_id, status, accepted_row_count, quarantined_row_count, "
                "validated_evidence_class, declared_evidence_class, replay_of_batch_id, completed_at "
                "from ingest_batch order by ingest_batch_id desc"
            )
        ).mappings().all()
        counts_by_batch = []
        for batch in batches:
            batch_id = batch["ingest_batch_id"]
            item = {"ingest_batch_id": batch_id, "status": batch["status"]}
            for table in ("patient", "encounter", "condition", "medication", "observation", "procedure"):
                item[table] = int(
                    conn.execute(
                        text(f"select count(*) from {table} where ingest_batch_id=:batch"),
                        {"batch": batch_id},
                    ).scalar_one()
                )
            counts_by_batch.append(item)
    return {
        "counts": counts,
        "accepted_rows": int(accepted),
        "quarantined_rows": int(quarantined),
        "issues_by_code": [(row[0], int(row[1])) for row in by_code],
        "dangling_patient_links": int(dangling_patient),
        "dangling_encounter_links": int(dangling_encounter),
        "records_without_encounter": int(empty_encounter_links),
        "quarantine_samples": [dict(row) for row in issues],
        "batches": [dict(row) for row in batches],
        "counts_by_batch": counts_by_batch,
    }


def _count(conn, table: str) -> int:
    return int(conn.execute(text(f"select count(*) from {table}")).scalar_one())
