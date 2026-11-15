"""Append-only calendar publication and consistent query views.

A published interpretation is an immutable, numbered snapshot.  Windows, the
per-interval provenance, the retractions and the booking review list are all
written in one transaction and read back under the same version, so a query can
never mix windows from one explanation with a review list from another.

Publication is fenced against late responses: the snapshot records the calendar
version it was built from, and a publish built from an older confirmed version
is refused instead of overwriting a newer calendar.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from .availability import (
    AVAILABLE,
    CONFLICT,
    Declaration,
    Segment,
    merge_windows,
    resolve,
)


class PublishError(RuntimeError):
    pass


class StalePublication(PublishError):
    """A publication built from an older calendar version than the newest one."""

    def __init__(self, offered: int, published: int):
        super().__init__(
            f"refusing to publish calendar version {offered}; version {published} is already published"
        )
        self.offered = offered
        self.published = published


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_dumps(value).encode("utf-8")).hexdigest()


class Catalog:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE TABLE IF NOT EXISTS versions (
                version INTEGER PRIMARY KEY,
                calendar_version INTEGER NOT NULL,
                digest TEXT NOT NULL,
                published_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                summary TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS declarations (
                version INTEGER NOT NULL REFERENCES versions(version),
                declaration_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (version, declaration_id)
            );
            CREATE TABLE IF NOT EXISTS retractions (
                version INTEGER NOT NULL REFERENCES versions(version),
                declaration_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (version, declaration_id)
            );
            CREATE TABLE IF NOT EXISTS windows (
                version INTEGER NOT NULL REFERENCES versions(version),
                ordinal INTEGER NOT NULL,
                supplier TEXT NOT NULL,
                equipment TEXT NOT NULL,
                status TEXT NOT NULL,
                start_utc TEXT NOT NULL,
                end_utc TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (version, ordinal)
            );
            CREATE TABLE IF NOT EXISTS bookings (
                booking_id TEXT PRIMARY KEY,
                supplier TEXT NOT NULL,
                equipment TEXT NOT NULL,
                start_utc TEXT NOT NULL,
                end_utc TEXT NOT NULL,
                status TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reviews (
                version INTEGER NOT NULL REFERENCES versions(version),
                booking_id TEXT NOT NULL,
                requires_review INTEGER NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (version, booking_id)
            );
        """)

    def close(self):
        self.connection.close()

    @contextmanager
    def transaction(self):
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield self.connection
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        else:
            self.connection.execute("COMMIT")

    # -- ingest run state -------------------------------------------------
    def load_run(self, run_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT state FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return None if row is None else json.loads(row["state"])

    def save_run(self, run_id: str, state: Mapping[str, Any]) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO runs(run_id,state,updated_at) VALUES (?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
                "ON CONFLICT(run_id) DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at",
                (run_id, _dumps(state)),
            )

    def capture_received_at(self, archive, capture_id: int) -> str | None:
        row = archive.connection.execute(
            "SELECT received_at FROM captures WHERE id=?", (capture_id,)
        ).fetchone()
        return None if row is None else row["received_at"]

    # -- bookings ---------------------------------------------------------
    def add_booking(
        self,
        booking_id: str,
        supplier: str,
        equipment: str,
        start: str | datetime,
        end: str | datetime,
        *,
        status: str = "confirmed",
        reference: str | None = None,
    ) -> None:
        start_utc = _instant(start)
        end_utc = _instant(end)
        if end_utc <= start_utc:
            raise ValueError("booking end must be after start")
        payload = {
            "booking_id": booking_id, "supplier": supplier, "equipment": equipment,
            "start_utc": start_utc.isoformat(), "end_utc": end_utc.isoformat(),
            "status": status, "reference": reference,
        }
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO bookings(booking_id,supplier,equipment,start_utc,end_utc,status,payload) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(booking_id) DO UPDATE SET "
                "supplier=excluded.supplier, equipment=excluded.equipment, "
                "start_utc=excluded.start_utc, end_utc=excluded.end_utc, "
                "status=excluded.status, payload=excluded.payload",
                (booking_id, supplier, equipment, start_utc.isoformat(), end_utc.isoformat(), status, _dumps(payload)),
            )

    def bookings(self) -> tuple[dict[str, Any], ...]:
        rows = self.connection.execute("SELECT payload FROM bookings ORDER BY booking_id").fetchall()
        return tuple(json.loads(row["payload"]) for row in rows)

    # -- publication ------------------------------------------------------
    def latest_version(self) -> int | None:
        row = self.connection.execute("SELECT max(version) AS version FROM versions").fetchone()
        return row["version"]

    def published_calendar_version(self) -> int | None:
        row = self.connection.execute(
            "SELECT calendar_version FROM versions ORDER BY version DESC LIMIT 1"
        ).fetchone()
        return None if row is None else int(row["calendar_version"])

    def publish(
        self,
        declarations: Iterable[Declaration],
        *,
        calendar_version: int,
        note: str = "",
        allow_stale: bool = False,
    ) -> dict[str, Any]:
        """Resolve and store one immutable interpretation.

        The whole snapshot — declarations, retractions, windows and booking
        reviews — is written in a single transaction under one version number.
        """
        effective, superseded, retractions, segments = resolve(declarations)
        windows = merge_windows(segments)
        reviews = compute_reviews(self.bookings(), segments)
        window_payloads = [window.to_dict() for window in windows]
        segment_payloads = [segment.to_dict() for segment in segments]
        summary = {
            "calendar_version": calendar_version,
            "note": note,
            "granularities": sorted({item.granularity for item in effective}),
            "declaration_count": len(effective),
            "superseded_count": len(superseded),
            "segment_count": len(segments),
            "window_count": len(windows),
            "review_count": sum(1 for review in reviews if review["requires_review"]),
            "conflict_count": sum(1 for segment in segments if segment.status == CONFLICT),
            "coverage_semantics": "layered_by_granularity_never_summed",
        }
        # The digest is computed over exactly the ordering used by ``query``:
        # declarations by declaration_id, windows by ordinal, reviews by
        # booking_id.  A mismatched digest means the stored explanation was
        # altered or mixed across versions.
        content_digest = _digest({
            "calendar_version": calendar_version,
            "declarations": sorted(
                (item.to_dict() for item in effective),
                key=lambda item: item["declaration_id"],
            ),
            "windows": window_payloads,
            "segments": segment_payloads,
            "reviews": sorted(reviews, key=lambda item: item["booking_id"]),
            "summary": summary,
        })
        with self.transaction() as connection:
            # Fence inside the write transaction: a late response cannot
            # overwrite a newer calendar even when publications race.
            row = connection.execute(
                "SELECT max(version) AS version, max(calendar_version) AS calendar_version FROM versions"
            ).fetchone()
            published_calendar = row["calendar_version"]
            if published_calendar is not None and calendar_version < published_calendar and not allow_stale:
                raise StalePublication(calendar_version, published_calendar)
            version = (row["version"] or 0) + 1
            connection.execute(
                "INSERT INTO versions(version,calendar_version,digest,summary) VALUES (?,?,?,?)",
                (version, calendar_version, content_digest, _dumps(summary)),
            )
            for declaration in effective:
                connection.execute(
                    "INSERT INTO declarations(version,declaration_id,payload) VALUES (?,?,?)",
                    (version, declaration.declaration_id, _dumps(declaration.to_dict())),
                )
            for retraction in retractions:
                connection.execute(
                    "INSERT INTO retractions(version,declaration_id,payload) VALUES (?,?,?)",
                    (version, retraction.declaration.declaration_id, _dumps(retraction.to_dict())),
                )
            for ordinal, window in enumerate(window_payloads):
                connection.execute(
                    "INSERT INTO windows(version,ordinal,supplier,equipment,status,start_utc,end_utc,payload) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (version, ordinal, window["supplier"], window["equipment"], window["status"],
                     window["start_utc"], window["end_utc"], _dumps(window)),
                )
            for review in reviews:
                connection.execute(
                    "INSERT INTO reviews(version,booking_id,requires_review,payload) VALUES (?,?,?,?)",
                    (version, review["booking_id"], int(review["requires_review"]), _dumps(review)),
                )
        return {
            "version": version,
            "calendar_version": calendar_version,
            "digest": content_digest,
            "summary": summary,
            "windows": window_payloads,
            "reviews": reviews,
        }

    # -- query ------------------------------------------------------------
    def query(self, version: int | None = None, *, supplier: str | None = None) -> dict[str, Any]:
        """Read windows and reviews from one and the same explanation.

        The read runs inside a single SQLite read transaction and checks the
        stored digest, so windows and reviews cannot come from different
        publications even while another writer is publishing.
        """
        if version is None:
            version = self.latest_version()
        if version is None:
            raise KeyError("no published version")
        self.connection.execute("BEGIN")
        try:
            header = self.connection.execute(
                "SELECT version,calendar_version,digest,summary,published_at FROM versions WHERE version=?",
                (version,),
            ).fetchone()
            if header is None:
                raise KeyError(version)
            windows = [json.loads(row["payload"]) for row in self.connection.execute(
                "SELECT payload FROM windows WHERE version=? ORDER BY ordinal", (version,)
            ).fetchall()]
            reviews = [json.loads(row["payload"]) for row in self.connection.execute(
                "SELECT payload FROM reviews WHERE version=? ORDER BY booking_id", (version,)
            ).fetchall()]
            declarations = [json.loads(row["payload"]) for row in self.connection.execute(
                "SELECT payload FROM declarations WHERE version=? ORDER BY declaration_id", (version,)
            ).fetchall()]
            summary = json.loads(header["summary"])
            recomputed = _digest({
                "calendar_version": header["calendar_version"],
                "declarations": declarations,
                "windows": windows,
                "segments": _segments_from_windows(windows),
                "reviews": reviews,
                "summary": summary,
            })
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        else:
            self.connection.execute("COMMIT")
        if recomputed != header["digest"]:
            raise PublishError(f"stored version {version} does not match its digest")
        if supplier is not None:
            windows = [window for window in windows if window["supplier"] == supplier]
            reviews = [review for review in reviews if review["supplier"] == supplier]
        return {
            "version": header["version"],
            "calendar_version": header["calendar_version"],
            "published_at": header["published_at"],
            "digest": header["digest"],
            "summary": summary,
            "windows": windows,
            "reviews": reviews,
        }

    def versions(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT version,calendar_version,digest,published_at,summary FROM versions ORDER BY version"
        ).fetchall()
        return [
            {"version": row["version"], "calendar_version": row["calendar_version"],
             "digest": row["digest"], "published_at": row["published_at"],
             "summary": json.loads(row["summary"])}
            for row in rows
        ]


def _segments_from_windows(windows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [segment for window in windows for segment in window["segments"]]


def _instant(value: str | datetime) -> datetime:
    from datetime import timezone

    if isinstance(value, datetime):
        moment = value
    else:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError("booking times must carry an offset")
    return moment.astimezone(timezone.utc)


def compute_reviews(
    bookings: Iterable[Mapping[str, Any]],
    segments: Iterable[Segment],
) -> list[dict[str, Any]]:
    """Flag confirmed bookings affected by supplier changes.

    Bookings are never moved.  A booking is flagged when any part of its
    interval is no longer covered by an uninterrupted ``available`` segment, or
    when the coverage is a conflict.  Every flagged review names the source
    differences that triggered it.
    """
    grouped: dict[tuple[str, str], list[Segment]] = {}
    for segment in segments:
        grouped.setdefault((segment.supplier, segment.equipment), []).append(segment)
    reviews = []
    for booking in bookings:
        start = _instant(booking["start_utc"])
        end = _instant(booking["end_utc"])
        scoped = grouped.get((booking["supplier"], booking["equipment"]), [])
        relevant = [
            segment for segment in scoped
            if segment.start < end and segment.end > start and segment.status != AVAILABLE
        ]
        covered = _covered_by_available(scoped, start, end)
        requires_review = bool(relevant) or not covered
        differences = [_difference(segment, start, end) for segment in relevant]
        reason = None
        if differences:
            statuses = {difference["status"] for difference in differences}
            reason = "supplier_declaration_conflict" if CONFLICT in statuses else "supplier_availability_changed"
        elif not covered:
            reason = "booking_outside_declared_coverage"
        reviews.append({
            "booking_id": booking["booking_id"],
            "supplier": booking["supplier"],
            "equipment": booking["equipment"],
            "start_utc": start.isoformat(),
            "end_utc": end.isoformat(),
            "booking_status": booking.get("status"),
            "reference": booking.get("reference"),
            "requires_review": requires_review,
            "reason": reason,
            "action": "review_only_do_not_move_booking",
            "differences": differences,
        })
    return reviews


def _difference(segment: Segment, start: datetime, end: datetime) -> dict[str, Any]:
    """The part of a supplier segment that overlaps one booking."""
    return {
        "start_utc": max(segment.start, start).isoformat(),
        "end_utc": min(segment.end, end).isoformat(),
        "status": segment.status,
        "reason": segment.reason,
        "declarations": [
            {
                "declaration_id": source["declaration_id"],
                "role": source["role"],
                "source": source["source"],
                "capture_id": source.get("capture_id"),
                "received_at": source.get("received_at"),
                "revision": source["revision"],
                "granularity": source["granularity"],
                "zone": source["zone"],
                "boundary": source["boundary"],
                "declared_status": source["declared_status"],
                "start": source["start"],
                "end": source["end"],
            }
            for source in segment.to_dict()["sources"]
        ],
    }


def _covered_by_available(segments: Iterable[Segment], start: datetime, end: datetime) -> bool:
    """True when available segments tile ``[start, end]`` without a gap."""
    cursor = start
    for segment in sorted(segments, key=lambda item: (item.start, item.end)):
        if segment.status != AVAILABLE:
            continue
        if segment.end <= cursor:
            continue
        if segment.start > cursor:
            return False
        cursor = max(cursor, segment.end)
        if cursor >= end:
            return True
    return cursor >= end
