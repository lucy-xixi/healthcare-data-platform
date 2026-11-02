"""Source-row fingerprint of unconverted CSV text.

The digest identifies identical source payloads. It is not a clinical-event key
and it is not a canonical record primary key.
"""

from __future__ import annotations

import hashlib
from typing import Sequence


def source_row_fingerprint(
    *,
    source_system: str,
    source_table: str,
    header: Sequence[str],
    raw_values: Sequence[str],
) -> str:
    """Hash system, table, header order, and each cell's original text.

    Values are taken in the header's order. Text is not trimmed, case-folded,
    reordered, or parsed as a date or number.
    """
    if len(header) != len(raw_values):
        raise ValueError("raw_values must align with header order and width")
    digest = hashlib.sha256()
    _add_token(digest, source_system)
    _add_token(digest, source_table)
    for name, value in zip(header, raw_values):
        _add_token(digest, name)
        _add_token(digest, value)
    return digest.hexdigest()


def payload_contrast_eligible(
    *,
    snapshot_id_a: str,
    snapshot_id_b: str,
    source_system_a: str,
    source_system_b: str,
    source_table_a: str,
    source_table_b: str,
    fingerprint_a: str,
    fingerprint_b: str,
) -> bool:
    """Pure predicate for a future cross-snapshot payload contrast.

    This function does not persist a contrast record. Different source tables
    are never the same payload, even when the fingerprint strings are equal.
    """
    return (
        snapshot_id_a != snapshot_id_b
        and source_system_a == source_system_b
        and source_table_a == source_table_b
        and fingerprint_a == fingerprint_b
    )


def _add_token(digest: "hashlib._Hash", token: str) -> None:
    encoded = token.encode("utf-8")
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)
