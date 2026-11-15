"""Interval availability derived from supplier declarations.

This module does not resolve business identity across suppliers.  It normalises
one supplier's declared intervals while keeping the original timezone, boundary
convention and revision, then resolves declarations into non-overlapping
segments.  Contradictory overlapping declarations become an explicit
``conflict`` segment rather than silently picking one source.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = dt_timezone.utc

AVAILABLE = "available"
CANCELLED = "cancelled"
UNAVAILABLE = "unavailable"
CONFLICT = "conflict"

HALF_OPEN = "half_open"
CLOSED = "closed"

_GRANULARITY_ALIASES = {
    "day": "day", "daily": "day", "date": "day", "whole_day": "day",
    "whole-day": "day", "all_day": "day", "allday": "day", "24h": "day",
    "30m": "30m", "30min": "30m", "30mins": "30m", "30minutes": "30m",
    "30_minutes": "30m", "30-minute": "30m", "half_hour": "30m",
    "half-hour": "30m", "half_hourly": "30m", "slot": "30m", "slots": "30m",
    "pt30m": "30m",
}

_STATUS_ALIASES = {
    "available": AVAILABLE, "avail": AVAILABLE, "free": AVAILABLE,
    "open": AVAILABLE, "yes": AVAILABLE, "true": AVAILABLE,
    "cancelled": CANCELLED, "canceled": CANCELLED, "cancel": CANCELLED,
    "withdrawn": CANCELLED, "retracted": CANCELLED,
    "unavailable": UNAVAILABLE, "blocked": UNAVAILABLE, "busy": UNAVAILABLE,
    "no": UNAVAILABLE, "false": UNAVAILABLE, "released": UNAVAILABLE,
}

_BOUNDARY_ALIASES = {
    "half_open": HALF_OPEN, "[)": HALF_OPEN, "start_inclusive": HALF_OPEN,
    "包括开始不包括结束": HALF_OPEN,
    "closed": CLOSED, "[]": CLOSED, "closed_closed": CLOSED,
    "inclusive": CLOSED, "包括两端": CLOSED,
}

# Only these statuses actually occupy the timeline.  A cancelled declaration
# removes a prior declaration from its interval but never makes the timeline
# look as if the interval had never been declared.
_ACTIVE_STATUSES = (AVAILABLE, UNAVAILABLE)

# Interval identity is content based: no wall-clock timestamps or random ids.
# Re-capturing the same declaration yields the same id, so snapshots and
# booking reviews remain reproducible.


class DeclarationError(ValueError):
    """A supplier declaration cannot be interpreted without guessing."""


class AmbiguousLocalTime(DeclarationError):
    """A naive local time occurs twice around a backward DST transition."""


class NonexistentLocalTime(DeclarationError):
    """A naive local time does not occur around a forward DST transition."""


def normalise_granularity(value: Any) -> str:
    text = str(value).strip().lower()
    return _GRANULARITY_ALIASES.get(text, text)


def normalise_status(value: Any) -> str:
    text = str(value).strip().lower()
    status = _STATUS_ALIASES.get(text, text)
    if status not in (AVAILABLE, CANCELLED, UNAVAILABLE):
        raise DeclarationError("unknown availability status: " + text)
    return status


def normalise_boundary(value: Any) -> str:
    text = str(value).strip().lower()
    boundary = _BOUNDARY_ALIASES.get(text, text)
    if boundary not in (HALF_OPEN, CLOSED):
        raise DeclarationError("unknown boundary convention: " + text)
    return boundary


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise DeclarationError("unknown timezone: " + str(name)) from error


def _local_instants(zone: ZoneInfo, naive: datetime) -> list[datetime]:
    """Return the UTC instants denoted by a naive wall-clock time.

    Zero candidates means the wall clock was skipped by a forward transition;
    two candidates means it occurred twice around a backward transition.
    """
    candidates = []
    for fold in (0, 1):
        aware = naive.replace(tzinfo=zone, fold=fold)
        instant = aware.astimezone(UTC)
        # Round-trip: a candidate is only valid when it maps back to the same
        # wall clock under its own fold.
        if instant.astimezone(zone).replace(tzinfo=None) == naive:
            if instant not in candidates:
                candidates.append(instant)
    return candidates


def _parse_moment(raw: Any, zone: ZoneInfo, fold: Any = None) -> datetime:
    text = str(raw).strip()
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise DeclarationError("invalid timestamp: " + text) from error
    if moment.tzinfo is not None:
        return moment.astimezone(UTC)
    if fold is not None:
        if fold not in (0, 1):
            raise DeclarationError("fold must be 0 or 1")
        aware = moment.replace(tzinfo=zone, fold=int(fold))
        if aware.astimezone(zone).replace(tzinfo=None) != moment:
            raise NonexistentLocalTime("local time does not exist in " + zone.key + ": " + text)
        return aware.astimezone(UTC)
    candidates = _local_instants(zone, moment)
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise NonexistentLocalTime("local time does not exist in " + zone.key + ": " + text)
    raise AmbiguousLocalTime("local time is ambiguous in " + zone.key + "; supply an offset or fold: " + text)


def _parse_date(raw: Any) -> date:
    text = str(raw).strip()
    try:
        return date.fromisoformat(text)
    except ValueError as error:
        raise DeclarationError("invalid date: " + text) from error


def _interval(raw: Mapping[str, Any], zone: ZoneInfo, boundary: str, granularity: str) -> tuple[datetime, datetime]:
    """Map a declared start/end pair to a half-open UTC interval.

    Day granularity reads plain dates in the supplier's zone; the transition
    day can therefore be 23 or 25 hours long.  Fine granularity reads
    timestamps.  Under the ``closed`` convention the end boundary itself is
    included, so it advances one granularity step to keep the stored interval
    half-open.
    """
    if granularity == "day":
        start_date = _parse_date(raw["start"])
        end_date = _parse_date(raw["end"])
        if boundary == CLOSED:
            end_date = end_date + timedelta(days=1)
        start_local = datetime.combine(start_date, time(0))
        end_local = datetime.combine(end_date, time(0))
    else:
        start = _parse_moment(raw["start"], zone, raw.get("start_fold"))
        end = _parse_moment(raw["end"], zone, raw.get("end_fold"))
        if boundary == CLOSED:
            end = end + timedelta(minutes=30)
        if end <= start:
            raise DeclarationError("declaration end must be after start")
        return start, end
    if end_local <= start_local:
        raise DeclarationError("declaration end must be after start")
    # Day boundaries are wall-clock midnights in the supplier's zone, so a DST
    # transition day is 23 or 25 hours long.  A midnight that occurs twice
    # (a backward transition at 00:00) starts at its first instant; a midnight
    # skipped entirely starts at the first valid wall-clock time.
    start = _first_instant(zone, start_local)
    end = _first_instant(zone, end_local)
    if end <= start:
        raise DeclarationError("declaration end must be after start")
    return start, end


def _first_instant(zone: ZoneInfo, naive: datetime) -> datetime:
    """First real instant at or after a wall-clock boundary.

    A boundary that occurs twice (backward transition at midnight) resolves to
    its earlier occurrence.  A boundary skipped by a forward transition
    resolves to the first wall-clock time that exists, which is how a supplier
    reading "the day starts at midnight" should be interpreted.
    """
    candidates = _local_instants(zone, naive)
    if candidates:
        return candidates[0]
    for minutes in range(1, 4 * 60 + 1):
        candidates = _local_instants(zone, naive + timedelta(minutes=minutes))
        if candidates:
            return candidates[0]
    raise DeclarationError("cannot resolve day boundary in " + zone.key)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _declaration_id(fields: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(fields).encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True)
class Declaration:
    declaration_id: str
    supplier: str
    equipment: str
    revision: int
    zone: str
    boundary: str
    granularity: str
    status: str
    start_raw: str
    end_raw: str
    start: datetime
    end: datetime
    source: str
    capture_id: int | None = None
    received_at: str | None = None

    @property
    def granularity_rank(self) -> int:
        """Fine-grained declarations outrank whole-day declarations."""
        return 1 if self.granularity != "day" else 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "declaration_id": self.declaration_id,
            "supplier": self.supplier,
            "equipment": self.equipment,
            "revision": self.revision,
            "zone": self.zone,
            "boundary": self.boundary,
            "granularity": self.granularity,
            "status": self.status,
            "start": self.start_raw,
            "end": self.end_raw,
            "start_utc": self.start.isoformat(),
            "end_utc": self.end.isoformat(),
            "source": self.source,
            "capture_id": self.capture_id,
            "received_at": self.received_at,
        }


def inspect_declaration(
    raw: Mapping[str, Any],
    *,
    source: str = "",
    capture_id: int | None = None,
    received_at: str | None = None,
    default_zone: str | None = None,
    default_boundary: str = HALF_OPEN,
    default_granularity: str | None = None,
) -> Declaration:
    """Validate one supplier interval declaration without resolving identity."""
    required = ("supplier", "equipment", "revision", "start", "end", "status")
    missing = [name for name in required if name not in raw]
    if missing:
        raise DeclarationError("missing fields: " + ", ".join(missing))
    supplier = str(raw["supplier"]).strip()
    equipment = str(raw["equipment"]).strip()
    if not supplier or not equipment:
        raise DeclarationError("supplier and equipment must be non-empty")
    revision = raw["revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise DeclarationError("revision must be a non-negative integer")
    zone_name = str(raw.get("zone") or default_zone or "").strip()
    if not zone_name:
        raise DeclarationError("zone is required")
    zone = _zone(zone_name)
    boundary = normalise_boundary(raw.get("boundary", default_boundary))
    granularity = normalise_granularity(raw.get("granularity") or default_granularity or "day")
    if granularity not in ("day", "30m"):
        raise DeclarationError("unsupported granularity: " + granularity)
    status = normalise_status(raw["status"])
    start_raw = str(raw["start"]).strip()
    end_raw = str(raw["end"]).strip()
    start, end = _interval({**raw, "start": start_raw, "end": end_raw}, zone, boundary, granularity)
    fields = {
        "supplier": supplier, "equipment": equipment, "revision": revision,
        "zone": zone.key, "boundary": boundary, "granularity": granularity,
        "status": status, "start": start_raw, "end": end_raw, "source": source,
    }
    return Declaration(
        declaration_id=_declaration_id(fields),
        supplier=supplier, equipment=equipment, revision=revision,
        zone=zone.key, boundary=boundary, granularity=granularity, status=status,
        start_raw=start_raw, end_raw=end_raw, start=start, end=end,
        source=source, capture_id=capture_id, received_at=received_at,
    )


@dataclass(frozen=True)
class Observation:
    source: str
    key: str
    revision: int
    value: Any


def inspect_record(raw: dict[str, Any]) -> dict[str, Any]:
    """校验并规范化一条来源观测，同时保留来源信息。"""
    required = ("source", "key", "revision", "value")
    missing = [name for name in required if name not in raw]
    if missing:
        raise ValueError("missing fields: " + ", ".join(missing))
    source, key = str(raw["source"]).strip(), str(raw["key"]).strip()
    if not source or not key:
        raise ValueError("source and key must be non-empty")
    revision = raw["revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ValueError("revision must be a non-negative integer")
    item = Observation(source, key, revision, raw["value"])
    return {"source": item.source, "key": item.key, "revision": item.revision, "value": item.value, "event": "stale_response_guard"}


def _declaration_key(declaration: Declaration) -> tuple:
    """Identity of one repeatedly-revised statement.

    A revision supersedes earlier revisions of the same *claim*, keyed by the
    interval it covers rather than by the literal ``start``/``end`` text spread
    across pages.  Overlapping declarations with a different coverage (a
    narrow slot and a wide slot) stay side by side, so a contradiction cannot
    be hidden behind revision order.
    """
    return (
        declaration.supplier, declaration.equipment, declaration.source,
        declaration.zone, declaration.boundary, declaration.granularity,
        declaration.start, declaration.end,
    )


@dataclass(frozen=True)
class Retraction:
    declaration: Declaration
    supersedes: Declaration | None

    @property
    def cause(self) -> str:
        return "withdrawn_after_available" if self.supersedes else "withdrew_unarchived_claim"

    def to_dict(self) -> dict[str, Any]:
        return {
            "declaration_id": self.declaration.declaration_id,
            "supersedes": self.supersedes.declaration_id if self.supersedes else None,
            "previously_available": bool(
                self.supersedes and self.supersedes.status == AVAILABLE
            ),
            "start_utc": self.declaration.start.isoformat(),
            "end_utc": self.declaration.end.isoformat(),
            "revision": self.declaration.revision,
            "cause": self.cause,
        }


@dataclass(frozen=True)
class Claim:
    declaration: Declaration
    role: str  # "decides", "refined" or "retracted_prior"


@dataclass(frozen=True)
class Segment:
    supplier: str
    equipment: str
    start: datetime
    end: datetime
    status: str
    reason: str
    claims: tuple[Claim, ...]

    @property
    def deciding(self) -> tuple[Declaration, ...]:
        return tuple(claim.declaration for claim in self.claims if claim.role == "decides")

    @property
    def refined(self) -> tuple[Declaration, ...]:
        return tuple(claim.declaration for claim in self.claims if claim.role == "refined")

    @property
    def declaration_ids(self) -> tuple[str, ...]:
        return tuple(d.declaration_id for d in self.deciding)

    @property
    def all_declaration_ids(self) -> tuple[str, ...]:
        return tuple(claim.declaration.declaration_id for claim in self.claims)

    def to_dict(self) -> dict[str, Any]:
        return {
            "supplier": self.supplier,
            "equipment": self.equipment,
            "start_utc": self.start.isoformat(),
            "end_utc": self.end.isoformat(),
            "status": self.status,
            "reason": self.reason,
            "deciding_declaration_ids": list(self.declaration_ids),
            "sources": [
                {
                    "declaration_id": claim.declaration.declaration_id,
                    "role": claim.role,
                    "source": claim.declaration.source,
                    "supplier": claim.declaration.supplier,
                    "equipment": claim.declaration.equipment,
                    "revision": claim.declaration.revision,
                    "zone": claim.declaration.zone,
                    "boundary": claim.declaration.boundary,
                    "granularity": claim.declaration.granularity,
                    "declared_status": claim.declaration.status,
                    "start": claim.declaration.start_raw,
                    "end": claim.declaration.end_raw,
                    "start_utc": claim.declaration.start.isoformat(),
                    "end_utc": claim.declaration.end.isoformat(),
                    "capture_id": claim.declaration.capture_id,
                    "received_at": claim.declaration.received_at,
                }
                for claim in self.claims
            ],
        }


@dataclass(frozen=True)
class Window:
    supplier: str
    equipment: str
    start: datetime
    end: datetime
    status: str
    segments: tuple[Segment, ...]

    @property
    def declaration_ids(self) -> tuple[str, ...]:
        ordered = []
        for segment in self.segments:
            for declaration_id in segment.all_declaration_ids:
                if declaration_id not in ordered:
                    ordered.append(declaration_id)
        return tuple(ordered)

    @property
    def granularities(self) -> tuple[str, ...]:
        return tuple(sorted({item.granularity for item in self._deciding}))

    @property
    def _deciding(self) -> tuple[Declaration, ...]:
        seen = []
        for segment in self.segments:
            for declaration in segment.deciding:
                if declaration not in seen:
                    seen.append(declaration)
        return tuple(seen)

    def to_dict(self) -> dict[str, Any]:
        return {
            "supplier": self.supplier,
            "equipment": self.equipment,
            "start_utc": self.start.isoformat(),
            "end_utc": self.end.isoformat(),
            "status": self.status,
            "granularities": list(self.granularities),
            "declaration_ids": list(self.declaration_ids),
            "segments": [segment.to_dict() for segment in self.segments],
        }


def _lineages(declarations: Iterable[Declaration]) -> dict[tuple, list[Declaration]]:
    grouped: dict[tuple, list[Declaration]] = {}
    for declaration in declarations:
        grouped.setdefault(_declaration_key(declaration), []).append(declaration)
    return grouped


def select_effective(
    declarations: Iterable[Declaration],
) -> tuple[tuple[Declaration, ...], tuple[Declaration, ...], tuple[Retraction, ...]]:
    """Keep the highest revision of each statement and record retractions.

    Returns ``(effective, superseded, retractions)``.  A retracted statement
    remains effective as a cancellation and points at the revision it removed,
    so a cancellation never reads as "was never offered".
    """
    effective: list[Declaration] = []
    superseded: list[Declaration] = []
    retractions: list[Retraction] = []
    for lineage in _lineages(declarations).values():
        top = max(item.revision for item in lineage)
        for item in lineage:
            if item.revision < top:
                superseded.append(item)
        for item in lineage:
            if item.revision != top:
                continue
            effective.append(item)
            if item.status == CANCELLED:
                earlier = [
                    other for other in lineage
                    if other.revision < item.revision and other.status != CANCELLED
                ]
                previous = max(earlier, key=lambda other: other.revision, default=None)
                retractions.append(Retraction(item, previous))
    effective.sort(key=lambda item: (item.supplier, item.equipment, item.start, item.declaration_id))
    superseded.sort(key=lambda item: (item.start, item.revision))
    retractions.sort(key=lambda item: (item.declaration.start, item.declaration.declaration_id))
    return tuple(effective), tuple(superseded), tuple(retractions)


def _rank(declaration: Declaration) -> int:
    return declaration.granularity_rank


def _boundaries(items: Iterable[Declaration]) -> list[datetime]:
    points: set[datetime] = set()
    for item in items:
        points.add(item.start)
        points.add(item.end)
    return sorted(points)


def _span_status(items: Sequence[Declaration]) -> tuple[str, str]:
    """Classify a span from its deciding declarations.

    ``cancelled`` declarations count as a stance here: a span that is only
    covered by retractions is cancelled, and a retraction that contradicts a
    live availability claim is a conflict rather than a silent withdrawal.
    """
    statuses = {item.status for item in items}
    if len(statuses) > 1:
        return CONFLICT, "overlapping_declarations_conflict"
    status = next(iter(statuses))
    if status == CANCELLED:
        return CANCELLED, "cancelled_interval_retained"
    return status, ""


def _resolve_span(
    supplier: str,
    equipment: str,
    start: datetime,
    end: datetime,
    claims: Sequence[Declaration],
    cancellations: Sequence[Declaration],
) -> Segment:
    fine_claims = [item for item in claims if _rank(item) == 1]
    coarse_claims = [item for item in claims if _rank(item) == 0]
    fine_cancellations = [item for item in cancellations if _rank(item) == 1]
    coarse_cancellations = [item for item in cancellations if _rank(item) == 0]
    if fine_claims or fine_cancellations:
        deciding = fine_claims + fine_cancellations
        refined = coarse_claims + coarse_cancellations
        specific = "fine"
    else:
        deciding = coarse_claims + coarse_cancellations
        refined = []
        specific = "coarse"
    status, reason = _span_status(deciding)
    if not reason:
        if specific == "fine" and refined:
            reason = "finer_granularity_refined_coarse_declaration"
        elif specific == "fine":
            reason = "fine_granularity"
        else:
            reason = "day_granularity"
    provenance = tuple(Claim(item, "decides") for item in deciding) + tuple(Claim(item, "refined") for item in refined)
    return Segment(supplier, equipment, start, end, status, reason, provenance)


def resolve(
    declarations: Iterable[Declaration],
) -> tuple[tuple[Declaration, ...], tuple[Declaration, ...], tuple[Retraction, ...], tuple[Segment, ...]]:
    """Resolve declarations into non-overlapping segments with provenance.

    Whole-day and fine declarations are layered, never added: a fine
    declaration decides its own span and a covering day declaration is kept as
    ``refined`` context.  Declarations of the same granularity that disagree
    over a span become an explicit ``conflict`` segment listing every source.
    A cancellation removes availability from its span but stays visible as the
    deciding provenance and as a :class:`Retraction`, so a cancelled interval
    never looks like it was never declared.
    """
    effective, superseded, retractions = select_effective(declarations)
    claims = tuple(item for item in effective if item.status != CANCELLED)
    cancellations = tuple(item for item in effective if item.status == CANCELLED)
    segments: list[Segment] = []
    for supplier, equipment in sorted({(item.supplier, item.equipment) for item in effective}):
        scoped_claims = [item for item in claims if item.supplier == supplier and item.equipment == equipment]
        scoped_cancellations = [
            item for item in cancellations
            if item.supplier == supplier and item.equipment == equipment
        ]
        for start, end in zip(
            (points := _boundaries(scoped_claims + scoped_cancellations)),
            points[1:],
        ):
            if end <= start:
                continue
            covering_claims = [
                item for item in scoped_claims if item.start <= start and item.end >= end
            ]
            covering_cancellations = [
                item for item in scoped_cancellations if item.start <= start and item.end >= end
            ]
            if not covering_claims and not covering_cancellations:
                continue
            segment = _resolve_span(
                supplier, equipment, start, end,
                covering_claims, covering_cancellations,
            )
            if segment.status == CANCELLED:
                # Attach the earlier availability the retraction removed, so a
                # cancellation still shows that the interval was once offered.
                prior = [
                    retraction.supersedes for retraction in retractions
                    if retraction.supersedes is not None
                    and retraction.declaration.declaration_id in segment.all_declaration_ids
                ]
                known = {claim.declaration.declaration_id for claim in segment.claims}
                extras = tuple(
                    Claim(item, "retracted_prior") for item in prior
                    if item.declaration_id not in known
                )
                if extras:
                    segment = Segment(
                        segment.supplier, segment.equipment, segment.start, segment.end,
                        segment.status, segment.reason, segment.claims + extras,
                    )
            segments.append(segment)
    return effective, superseded, retractions, tuple(segments)


def merge_windows(segments: Iterable[Segment]) -> tuple[Window, ...]:
    """Join adjacent same-status segments into readable windows.

    Merging is presentation only: every contributing segment (with its
    declarations) stays inside the window, so a scheduler can still trace each
    interval back to the declaring record.
    """
    windows: list[Window] = []
    for segment in segments:
        previous = windows[-1] if windows else None
        if (
            previous is not None
            and previous.supplier == segment.supplier
            and previous.equipment == segment.equipment
            and previous.status == segment.status
            and previous.end == segment.start
            and segment.status != CONFLICT
        ):
            windows[-1] = Window(
                previous.supplier, previous.equipment, previous.start,
                segment.end, previous.status, previous.segments + (segment,),
            )
        else:
            windows.append(Window(
                segment.supplier, segment.equipment, segment.start, segment.end,
                segment.status, (segment,),
            ))
    return tuple(windows)
