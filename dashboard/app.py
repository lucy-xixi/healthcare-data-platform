"""Healthcare Data Platform demonstration UI."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd
import streamlit as st

from healthcare_data_platform.ingest.persist import import_snapshot
from healthcare_data_platform.ingest.synthea.contract import REQUIRED_EVIDENCE_CLASS
from healthcare_data_platform.matching.adapter import evaluate_platform_patient, load_protocols, run_gold_evaluation
from healthcare_data_platform.patients.explorer import patient_chart, search_patients
from healthcare_data_platform.persistence.database import default_db_path, get_engine, init_db
from healthcare_data_platform.quality.report import summary

DEFAULT_SNAPSHOT = Path(r"e:\medproject\rwe-project\data\synthea_csv")

st.set_page_config(page_title="Healthcare Data Platform", layout="wide")
st.markdown(
    """
    <style>
    .stApp { background: #f3f6f6; }
    h1, h2, h3 { color: #16302e; letter-spacing: -0.02em; }
    .banner { background: #16302e; color: #f4fbfa; padding: 0.9rem 1.1rem; border-radius: 8px; }
    .note { color: #3d4f4c; font-size: 0.92rem; }
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_resource
def engine():
    bound = get_engine(default_db_path())
    init_db(bound)
    return bound


def main() -> None:
    st.sidebar.markdown("### Healthcare Data Platform")
    st.sidebar.caption("合成数据演示。不是临床验证系统。")
    page = st.sidebar.radio(
        "导航",
        ["总览", "数据接入", "数据质量", "患者档案", "临床试验匹配", "评测"],
    )
    pages = {
        "总览": page_overview,
        "数据接入": page_ingest,
        "数据质量": page_quality,
        "患者档案": page_patients,
        "临床试验匹配": page_match,
        "评测": page_eval,
    }
    pages[page]()


def page_overview() -> None:
    st.markdown('<div class="banner"><strong>平台总览</strong><br>接入、质量、纵向档案和规则匹配使用同一份已导入数据。</div>', unsafe_allow_html=True)
    report = summary(engine())
    counts = report["counts"]
    columns = st.columns(6)
    labels = [
        ("患者", counts["patient"]),
        ("就诊", counts["encounter"]),
        ("诊断", counts["condition"]),
        ("用药", counts["medication"]),
        ("观察", counts["observation"]),
        ("操作", counts["procedure"]),
    ]
    for column, (label, value) in zip(columns, labels):
        column.metric(label, f"{value:,}")
    left, right = st.columns(2)
    left.metric("已接受源行", f"{report['accepted_rows']:,}")
    right.metric("隔离源行", f"{report['quarantined_rows']:,}")
    st.subheader("导入批次")
    if report["batches"]:
        st.dataframe(pd.DataFrame(report["batches"]), width="stretch", hide_index=True)
        st.subheader("各批次规范记录数")
        st.dataframe(pd.DataFrame(report["counts_by_batch"]), width="stretch", hide_index=True)
        st.caption("上方六个总数包含数据库中的全部批次。某一快照的数量以该批次的行计数为准。")
    else:
        st.info("还没有导入批次。请到“数据接入”导入六文件快照。")
    st.caption("partial 批次的已接受记录可以查看，但不能作为分析数据集。failed 批次没有规范记录。")


def page_ingest() -> None:
    st.header("数据接入")
    st.markdown(
        '<p class="note">六个 CSV 作为一个快照导入。证据等级必须是 synthetic_synthea。'
        "文件哈希、源行和隔离结果来自实际读取，不使用预置计数。</p>",
        unsafe_allow_html=True,
    )
    snapshot = st.text_input("快照目录", value=str(DEFAULT_SNAPSHOT))
    evidence = st.text_input("evidence_class", value=REQUIRED_EVIDENCE_CLASS)
    if st.button("导入快照", type="primary"):
        path = Path(snapshot)
        with st.spinner("正在检查文件并写入数据库。观察表较大时需要几分钟。"):
            result = import_snapshot(engine(), path, evidence)
        st.success(f"批次 {result.ingest_batch_id}：{result.status}。接受 {result.accepted_row_count}，隔离 {result.quarantined_row_count}。{result.message}")
    report = summary(engine())
    st.subheader("批次与文件结果")
    st.dataframe(pd.DataFrame(report["batches"]), width="stretch", hide_index=True)
    st.caption(f"数据库：{default_db_path()}")


def page_quality() -> None:
    st.header("数据质量")
    report = summary(engine())
    columns = st.columns(4)
    columns[0].metric("隔离源行", f"{report['quarantined_rows']:,}")
    columns[1].metric("悬空患者引用", report["dangling_patient_links"])
    columns[2].metric("悬空就诊引用", report["dangling_encounter_links"])
    columns[3].metric("无就诊关联的事件", f"{report['records_without_encounter']:,}")
    st.caption("无就诊关联表示源单元格为空，或该行被隔离后没有生成规范记录。系统不会按日期补就诊。")
    st.subheader("问题分类")
    st.dataframe(pd.DataFrame(report["issues_by_code"], columns=["错误码", "行数"]), width="stretch", hide_index=True)
    st.subheader("隔离明细（最近 200 行）")
    samples = report["quarantine_samples"]
    if samples:
        st.dataframe(pd.DataFrame(samples), width="stretch", hide_index=True)
    else:
        st.info("当前没有隔离行。若尚未导入，明细为空。")


def page_patients() -> None:
    st.header("患者档案")
    query = st.text_input("按姓名或源患者标识搜索", value="")
    rows = search_patients(engine(), query or "")
    if not rows:
        st.info("没有匹配的患者。")
        return
    frame = pd.DataFrame(rows)
    st.dataframe(frame, width="stretch", hide_index=True)
    selected = st.selectbox("打开患者", frame["record_id"].tolist())
    page = st.number_input("观察记录页码（每页 500 条）", min_value=1, value=1, step=1)
    chart = patient_chart(engine(), int(selected), observation_limit=500, observation_offset=(int(page) - 1) * 500)
    if chart is None:
        return
    patient = chart["patient"]
    st.subheader(f"{patient['given_name_source']} {patient['family_name_source']}".strip() or patient["source_record_id"])
    st.write(
        {
            "record_id": patient["record_id"],
            "source_record_id": patient["source_record_id"],
            "gender_source": patient["gender_source"],
            "race_source": patient["race_source"],
            "ethnicity_source": patient["ethnicity_source"],
            "birth": f"{patient['birth_raw']} ({patient['birth_precision']})",
            "death": f"{patient['death_raw'] or '—'} ({patient['death_precision']})",
            "evidence_class": patient["evidence_class"],
            "ingest_batch_id": patient["ingest_batch_id"],
        }
    )
    st.caption("同一天内，日期精度和时刻精度不比较先后。下面按日历日期和源行号排列，不表示临床先后已经确定。")
    trace = ["source_filename", "source_table", "source_row_number"]
    _show_table("就诊", chart["encounters"], trace + ["start_raw", "start_precision", "start_instant_utc", "end_raw", "class_source", "code_source", "text_source", "source_record_id"])
    _show_table("诊断", chart["conditions"], trace + ["start_raw", "start_precision", "start_instant_utc", "code_source", "system_source", "text_source", "encounter_record_id"])
    _show_table("用药", chart["medications"], trace + ["start_raw", "start_precision", "end_raw", "end_precision", "code_source", "text_source", "dispenses_source", "encounter_record_id"])
    shown = len(chart["observations"])
    total = chart["observation_total"]
    start = chart["observation_offset"] + 1 if shown else 0
    stop = chart["observation_offset"] + shown
    st.markdown(f"**观察** · 本页 {shown} 条（第 {start}–{stop} 条），数据库中共 {total} 条")
    st.caption("页面只分页显示。未出现在本页的观察记录仍保存在数据库中，匹配会读取该患者的全部观察记录。")
    if chart["observations"]:
        frame = pd.DataFrame(chart["observations"])
        keep = [column for column in trace + ["clinical_raw", "clinical_precision", "clinical_instant_utc", "code_source", "text_source", "value_text", "unit_source", "encounter_record_id"] if column in frame.columns]
        st.dataframe(frame[keep], width="stretch", hide_index=True)
    _show_table("操作", chart["procedures"], trace + ["start_raw", "start_precision", "code_source", "system_source", "text_source", "encounter_record_id"])


def _show_table(title: str, rows: list[dict], columns: list[str]) -> None:
    st.markdown(f"**{title}** · {len(rows)} 条")
    if not rows:
        st.caption("无记录")
        return
    frame = pd.DataFrame(rows)
    keep = [column for column in columns if column in frame.columns]
    st.dataframe(frame[keep], width="stretch", hide_index=True)


def page_match() -> None:
    st.header("临床试验匹配")
    st.markdown(
        '<p class="note">判定由 medical-ai-platform 的确定性规则引擎执行。'
        "Synthea 诊断保留源编码，不会改写成 ICD。编码体系不是 ICD 时，结果是证据不足并附上源编码，不会把源编码当作 ICD 比较后判为不符合。</p>",
        unsafe_allow_html=True,
    )
    try:
        protocols = load_protocols()
    except FileNotFoundError as exc:
        st.error(str(exc))
        return
    protocol_ids = [item["protocol_id"] for item in protocols]
    chosen = st.selectbox("试验方案", protocol_ids)
    protocol = next(item for item in protocols if item["protocol_id"] == chosen)
    st.json({"reference_date": protocol["reference_date"], "inclusion": protocol["inclusion"], "exclusion": protocol["exclusion"], "rules": protocol["rules"]})
    query = st.text_input("先搜索患者", value="", key="match-search")
    found = search_patients(engine(), query or "")
    if not found:
        st.info("没有可选患者。")
        return
    options = {f"{row['record_id']} · {row['given_name_source']} {row['family_name_source']} · {row['source_record_id']}": row["record_id"] for row in found}
    label = st.selectbox("患者", list(options))
    if st.button("执行匹配", type="primary"):
        try:
            result = evaluate_platform_patient(engine(), int(options[label]), protocol)
        except ValueError as exc:
            st.warning(str(exc))
            return
        st.subheader(_status_label(result.patient_status.value))
        st.write(result.summary)
        rows = []
        for rule_id, evaluation in result.rule_evaluations.items():
            rows.append(
                {
                    "rule_id": rule_id,
                    "status": _status_label(evaluation.status.value),
                    "summary": evaluation.summary,
                    "evidence": evaluation.evidence,
                }
            )
        for row in rows:
            with st.expander(f"{row['rule_id']} · {row['status']}"):
                st.write(row["summary"])
                if row["evidence"]:
                    st.dataframe(pd.DataFrame(row["evidence"]), width="stretch", hide_index=True)
                else:
                    st.caption("这条规则没有附带记录级证据。")
                if "不足" in row["summary"] or "无" in row["summary"]:
                    st.info(f"补充信息以规则说明为准：{row['summary']}")


def page_eval() -> None:
    st.header("合成 Gold 评测")
    st.markdown(
        '<p class="note">这是现有 Gold 病例与规则引擎的一致性结果，使用合成患者和合成方案。'
        "它不是真实临床准确率，也不是医院应用效果。</p>",
        unsafe_allow_html=True,
    )
    if st.button("运行评测", type="primary"):
        with st.spinner("正在用规则引擎重跑 Gold 病例。"):
            st.session_state["gold"] = run_gold_evaluation()
    report = st.session_state.get("gold")
    if not report:
        st.info("尚未运行。结果会在点击后由引擎实际计算。")
        return
    columns = st.columns(4)
    columns[0].metric("样本数", report["sample_count"])
    columns[1].metric("一致", report["match_count"])
    columns[2].metric("不一致", report["mismatch_count"])
    columns[3].metric("一致率", f"{report['agreement_rate']:.1%}" if report["agreement_rate"] is not None else "—")
    st.json(report["disclaimer"])
    st.subheader("不一致类型")
    if report["reasons"]:
        st.dataframe(pd.DataFrame(report["reasons"], columns=["类型", "数量"]), hide_index=True)
    else:
        st.caption("本次运行没有不一致病例。")
    st.subheader("不一致案例")
    if report["examples"]:
        st.dataframe(pd.DataFrame(report["examples"]), width="stretch", hide_index=True)
    if report["per_class"]:
        st.subheader("分类指标")
        st.dataframe(pd.DataFrame(report["per_class"]).T, width="stretch")


def _status_label(value: str) -> str:
    return {
        "eligible": "符合 Eligible",
        "not_eligible": "不符合 Not Eligible",
        "insufficient_evidence": "证据不足 Insufficient Evidence",
        "unsupported": "规则无法执行 Unsupported",
    }.get(value, value)


if __name__ == "__main__":
    main()
else:
    main()
