"""Read committed clinical rows for the patient explorer."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine


def search_patients(engine: Engine, query: str, limit: int = 50) -> list[dict]:
    pattern = f"%{query.strip()}%"
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "select record_id, source_record_id, given_name_source, family_name_source, "
                "gender_source, birth_raw, birth_precision, evidence_class, ingest_batch_id "
                "from patient "
                "where source_record_id like :q or given_name_source like :q or family_name_source like :q "
                "or (given_name_source || ' ' || family_name_source) like :q "
                "order by record_id limit :limit"
            ),
            {"q": pattern, "limit": limit},
        ).mappings().all()
    return [dict(row) for row in rows]


_FILE_JOIN = (
    "left join ingest_batch_file f on f.ingest_batch_id = t.ingest_batch_id "
    "and f.original_filename = t.source_table || '.csv'"
)


def patient_chart(
    engine: Engine,
    record_id: int,
    *,
    observation_limit: int | None = 500,
    observation_offset: int = 0,
) -> dict | None:
    with engine.connect() as conn:
        patient = conn.execute(
            text(
                "select t.*, f.original_filename as source_filename from patient t "
                + _FILE_JOIN
                + " where t.record_id=:id"
            ),
            {"id": record_id},
        ).mappings().first()
        if patient is None:
            return None
        params = {"id": record_id}
        encounters = _rows(conn, "encounter", params)
        conditions = _rows(conn, "condition", params)
        medications = _rows(conn, "medication", params)
        procedures = _rows(conn, "procedure", params)
        observation_total = int(
            conn.execute(
                text("select count(*) from observation where patient_record_id=:id"),
                params,
            ).scalar_one()
        )
        observation_sql = (
            "select t.*, f.original_filename as source_filename from observation t "
            + _FILE_JOIN
            + " where t.patient_record_id=:id order by t.clinical_calendar_date, t.source_row_number"
        )
        observation_params = dict(params)
        if observation_limit is not None:
            observation_sql += " limit :limit offset :offset"
            observation_params["limit"] = observation_limit
            observation_params["offset"] = observation_offset
        observations = conn.execute(text(observation_sql), observation_params).mappings().all()
    return {
        "patient": dict(patient),
        "encounters": encounters,
        "conditions": conditions,
        "medications": medications,
        "observations": [dict(row) for row in observations],
        "observation_total": observation_total,
        "observation_offset": observation_offset,
        "procedures": procedures,
    }


def _rows(conn, table: str, params: dict) -> list[dict]:
    order_column = "start_calendar_date"
    rows = conn.execute(
        text(
            f"select t.*, f.original_filename as source_filename from {table} t "
            + _FILE_JOIN
            + f" where t.patient_record_id=:id order by t.{order_column}, t.source_row_number"
        ),
        params,
    ).mappings().all()
    return [dict(row) for row in rows]
