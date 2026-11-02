"""Snapshot identity and replay classification.

Replay decisions are computed from historical snapshot records supplied by a
persistence port. This phase does not store those records and does not treat an
in-memory collection as the platform's import database.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence

from healthcare_data_platform.ingest.synthea.contract import REQUIRED_FILENAMES, SOURCE_SYSTEM

SUCCEEDED = "succeeded"
PARTIAL = "partial"
FAILED = "failed"


@dataclass(frozen=True)
class HistoricalSnapshot:
    """Record a future persistence adapter would return.

    ``file_hashes`` maps each required filename to the SHA-256 of its original
    bytes. ``status`` uses the frozen batch vocabulary.
    """

    source_system: str
    evidence_class: str
    status: str
    file_hashes: Mapping[str, str]


@dataclass(frozen=True)
class SnapshotIdentity:
    source_system: str
    evidence_class: str
    file_hashes: tuple[tuple[str, str], ...]

    @classmethod
    def from_mapping(
        cls,
        *,
        source_system: str,
        evidence_class: str,
        file_hashes: Mapping[str, str],
    ) -> "SnapshotIdentity":
        ordered = tuple(sorted(file_hashes.items()))
        return cls(
            source_system=source_system,
            evidence_class=evidence_class,
            file_hashes=ordered,
        )

    def as_mapping(self) -> dict[str, str]:
        return dict(self.file_hashes)


class SnapshotHistoryQuery(Protocol):
    """Persistence lookup. No production implementation is provided in this phase."""

    def find_snapshots(
        self,
        *,
        source_system: str,
        evidence_class: str,
        file_hashes: Mapping[str, str],
    ) -> Sequence[HistoricalSnapshot]:
        """Return stored snapshots associated with this identity.

        Implementations must query durable history. Returning an empty sequence
        means the lookup ran and found nothing. Omitting the port entirely means
        the lookup was not configured.
        """


@dataclass(frozen=True)
class ReplayDecision:
    """Result of comparing one identity with historical records.

    ``evaluated`` is false when no persistence port was configured. In that case
    ``is_replay`` is false and must not be read as proof that the snapshot is new.
    """

    evaluated: bool
    is_replay: bool
    reason: str
    matched_status: str | None = None


def complete_hash_set(file_hashes: Mapping[str, str]) -> bool:
    return set(file_hashes) == set(REQUIRED_FILENAMES)


def same_snapshot_identity(left: SnapshotIdentity, right: SnapshotIdentity) -> bool:
    return (
        left.source_system == right.source_system
        and left.evidence_class == right.evidence_class
        and left.file_hashes == right.file_hashes
        and complete_hash_set(left.as_mapping())
    )


def classify_replay(
    current: SnapshotIdentity,
    history: Sequence[HistoricalSnapshot] | None,
) -> ReplayDecision:
    """Classify replay only from a succeeded historical snapshot.

    A historical ``partial`` or ``failed`` record with the same bytes does not
    qualify. A historical ``replayed`` record also does not qualify by itself.
    """
    if history is None:
        return ReplayDecision(
            evaluated=False,
            is_replay=False,
            reason="persistence_not_configured",
        )
    if not complete_hash_set(current.as_mapping()):
        return ReplayDecision(
            evaluated=True,
            is_replay=False,
            reason="incomplete_hash_set",
        )
    current_hashes = current.as_mapping()
    saw_non_succeeded = False
    for record in history:
        if record.source_system != current.source_system:
            continue
        if record.source_system != SOURCE_SYSTEM:
            continue
        if record.evidence_class != current.evidence_class:
            continue
        if dict(record.file_hashes) != current_hashes:
            continue
        if record.status == SUCCEEDED:
            return ReplayDecision(
                evaluated=True,
                is_replay=True,
                reason="matched_succeeded_snapshot",
                matched_status=SUCCEEDED,
            )
        saw_non_succeeded = True
    if saw_non_succeeded:
        return ReplayDecision(
            evaluated=True,
            is_replay=False,
            reason="no_succeeded_snapshot",
        )
    return ReplayDecision(
        evaluated=True,
        is_replay=False,
        reason="no_historical_snapshot",
    )
