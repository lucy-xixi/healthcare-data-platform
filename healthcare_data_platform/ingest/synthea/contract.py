"""Frozen Synthea six-file snapshot contract.

Header names and order come from the reviewed CSV files. This module does not
read those files and does not embed a path to the old project.
"""

from __future__ import annotations

SOURCE_SYSTEM = "synthea_csv"
REQUIRED_EVIDENCE_CLASS = "synthetic_synthea"

REQUIRED_FILENAMES: tuple[str, ...] = (
    "patients.csv",
    "encounters.csv",
    "conditions.csv",
    "medications.csv",
    "observations.csv",
    "procedures.csv",
)

FILE_HEADERS: dict[str, tuple[str, ...]] = {
    "patients.csv": (
        "Id",
        "BIRTHDATE",
        "DEATHDATE",
        "SSN",
        "DRIVERS",
        "PASSPORT",
        "PREFIX",
        "FIRST",
        "MIDDLE",
        "LAST",
        "SUFFIX",
        "MAIDEN",
        "MARITAL",
        "RACE",
        "ETHNICITY",
        "GENDER",
        "BIRTHPLACE",
        "ADDRESS",
        "CITY",
        "STATE",
        "COUNTY",
        "FIPS",
        "ZIP",
        "LAT",
        "LON",
        "HEALTHCARE_EXPENSES",
        "HEALTHCARE_COVERAGE",
        "INCOME",
    ),
    "encounters.csv": (
        "Id",
        "START",
        "STOP",
        "PATIENT",
        "ORGANIZATION",
        "PROVIDER",
        "PAYER",
        "ENCOUNTERCLASS",
        "CODE",
        "DESCRIPTION",
        "BASE_ENCOUNTER_COST",
        "TOTAL_CLAIM_COST",
        "PAYER_COVERAGE",
        "REASONCODE",
        "REASONDESCRIPTION",
    ),
    "conditions.csv": (
        "START",
        "STOP",
        "PATIENT",
        "ENCOUNTER",
        "SYSTEM",
        "CODE",
        "DESCRIPTION",
    ),
    "medications.csv": (
        "START",
        "STOP",
        "PATIENT",
        "PAYER",
        "ENCOUNTER",
        "CODE",
        "DESCRIPTION",
        "BASE_COST",
        "PAYER_COVERAGE",
        "DISPENSES",
        "TOTALCOST",
        "REASONCODE",
        "REASONDESCRIPTION",
    ),
    "observations.csv": (
        "DATE",
        "PATIENT",
        "ENCOUNTER",
        "CATEGORY",
        "CODE",
        "DESCRIPTION",
        "VALUE",
        "UNITS",
        "TYPE",
    ),
    "procedures.csv": (
        "START",
        "STOP",
        "PATIENT",
        "ENCOUNTER",
        "SYSTEM",
        "CODE",
        "DESCRIPTION",
        "BASE_COST",
        "REASONCODE",
        "REASONDESCRIPTION",
    ),
}

CLINICAL_TIME_FIELDS: dict[str, tuple[str, ...]] = {
    "patients.csv": ("BIRTHDATE", "DEATHDATE"),
    "encounters.csv": ("START", "STOP"),
    "conditions.csv": ("START", "STOP"),
    "medications.csv": ("START", "STOP"),
    "observations.csv": ("DATE",),
    "procedures.csv": ("START", "STOP"),
}

SOURCE_RECORD_ID_FIELD = {
    "patients.csv": "Id",
    "encounters.csv": "Id",
}

UTF8_BOM = b"\xef\xbb\xbf"


def source_table_for(filename: str) -> str:
    if not filename.endswith(".csv"):
        raise ValueError(f"Not a Synthea snapshot CSV name: {filename}")
    return filename[: -len(".csv")]
