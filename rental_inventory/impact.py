"""Booking impact: flag reviews, never move confirmed bookings.

Availability resolution lives in :mod:`rental_inventory.availability`; the
catalog stores one review list per published explanation.  This module keeps a
thin, explicit entry point so callers can preview the review list for a set of
declarations without publishing, and it documents the invariant: a booking is
only ever *reported*, never edited.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from .availability import resolve
from .catalog import compute_reviews


def booking_impact(
    declarations: Iterable,
    bookings: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return segments, windows and review flags for one interpretation."""
    from .availability import merge_windows

    effective, superseded, retractions, segments = resolve(declarations)
    reviews = compute_reviews(bookings, segments)
    return {
        "segments": [segment.to_dict() for segment in segments],
        "windows": [window.to_dict() for window in merge_windows(segments)],
        "retractions": [retraction.to_dict() for retraction in retractions],
        "reviews": reviews,
        "changed_bookings": [
            review["booking_id"] for review in reviews if review["requires_review"]
        ],
        "booking_action": "review_only_do_not_move_booking",
    }
