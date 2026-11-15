import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from rental_inventory.availability import (
    AVAILABLE,
    CANCELLED,
    CONFLICT,
    AmbiguousLocalTime,
    DeclarationError,
    NonexistentLocalTime,
    inspect_declaration,
    merge_windows,
    resolve,
)


def declaration(**overrides):
    raw = {
        "supplier": "hire-a", "equipment": "EQ-9", "revision": 1,
        "zone": "Europe/Berlin", "granularity": "30m",
        "start": "2026-10-25T01:00:00+02:00", "end": "2026-10-25T01:30:00+02:00",
        "status": "available",
    }
    raw.update(overrides)
    return inspect_declaration(raw, source=overrides.pop("source", "feed"))


class TimezoneTests(unittest.TestCase):
    def test_day_granularity_spans_the_short_dst_day(self):
        item = declaration(granularity="day", start="2026-03-29", end="2026-03-30")
        self.assertEqual(item.end - item.start, timedelta(hours=23))

    def test_day_granularity_spans_the_long_dst_day(self):
        item = declaration(granularity="day", start="2026-10-25", end="2026-10-26")
        self.assertEqual(item.end - item.start, timedelta(hours=25))
        self.assertEqual(item.start.isoformat(), "2026-10-24T22:00:00+00:00")
        self.assertEqual(item.end.isoformat(), "2026-10-25T23:00:00+00:00")

    def test_offsets_are_kept_per_declaration_across_zones(self):
        berlin = declaration(start="2026-10-25T01:00:00+02:00", end="2026-10-25T01:30:00+02:00")
        utc = declaration(zone="UTC", start="2026-10-24T23:00:00Z", end="2026-10-24T23:30:00Z")
        auckland = declaration(zone="Pacific/Auckland", start="2026-10-26T00:00:00+13:00",
                               end="2026-10-26T00:30:00+13:00")
        self.assertEqual(berlin.start, utc.start)
        self.assertNotEqual(berlin.start, auckland.start)
        self.assertEqual(auckland.start.isoformat(), "2026-10-25T11:00:00+00:00")
        # Each declaration is rendered in UTC but keeps its declared zone.
        self.assertEqual(
            [item.zone for item in (berlin, utc, auckland)],
            ["Europe/Berlin", "UTC", "Pacific/Auckland"],
        )

    def test_closed_boundary_includes_the_end_date(self):
        closed = declaration(granularity="day", start="2026-10-20", end="2026-10-20", boundary="closed")
        self.assertEqual(closed.end - closed.start, timedelta(hours=24))
        with self.assertRaises(DeclarationError):
            declaration(granularity="day", start="2026-10-20", end="2026-10-20")

    def test_closed_end_matches_the_next_day_under_the_same_convention(self):
        closed_one = declaration(granularity="day", start="2026-10-26", end="2026-10-26", boundary="[]")
        half_open_next = declaration(granularity="day", start="2026-10-26", end="2026-10-27")
        self.assertEqual(closed_one.end, half_open_next.end)
        self.assertEqual(closed_one.start, half_open_next.start)

    def test_closed_boundary_on_fine_granularity_advances_one_slot(self):
        closed = declaration(boundary="[]", start="2026-10-25T01:00:00+02:00", end="2026-10-25T01:30:00+02:00")
        open_slot = declaration(start="2026-10-25T01:00:00+02:00", end="2026-10-25T02:00:00+02:00")
        self.assertEqual(closed.end, open_slot.end)

    def test_ambiguous_wall_clock_requires_an_explicit_choice(self):
        with self.assertRaises(AmbiguousLocalTime):
            declaration(start="2026-10-25T02:30:00", end="2026-10-25T03:00:00")
        first = declaration(start="2026-10-25T02:30:00", end="2026-10-25T03:00:00", start_fold=0)
        second = declaration(start="2026-10-25T02:30:00", end="2026-10-25T03:00:00", start_fold=1)
        self.assertEqual(second.start - first.start, timedelta(hours=1))

    def test_nonexistent_wall_clock_is_rejected(self):
        with self.assertRaises(NonexistentLocalTime):
            declaration(start="2026-03-29T02:30:00", end="2026-03-29T03:00:00")

    def test_unknown_zone_and_granularity_are_rejected(self):
        for overrides in ({"zone": "Mars/Olympus"}, {"granularity": "15m"}, {"status": "maybe"}):
            with self.subTest(overrides=overrides), self.assertRaises(DeclarationError):
                declaration(**overrides)


class ResolutionTests(unittest.TestCase):
    def test_adjacent_slots_merge_into_one_readable_window(self):
        first = declaration(revision=1, end="2026-10-25T01:30:00+02:00")
        second = declaration(revision=2, start="2026-10-25T01:30:00+02:00", end="2026-10-25T02:00:00+02:00")
        _effective, _superseded, _retractions, segments = resolve([first, second])
        windows = merge_windows(segments)
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].end - windows[0].start, timedelta(hours=1))
        self.assertEqual(len(windows[0].segments), 2)
        self.assertEqual(set(windows[0].declaration_ids), {first.declaration_id, second.declaration_id})

    def test_conflicting_overlaps_keep_both_sources(self):
        # Different stated intervals that contradict each other in the overlap:
        # neither statement may erase the other.
        available = declaration(revision=1, start="2026-10-25T01:00:00+02:00", end="2026-10-25T02:00:00+02:00")
        blocked = declaration(revision=2, start="2026-10-25T01:30:00+02:00", end="2026-10-25T02:30:00+02:00",
                              status="unavailable")
        _effective, _superseded, _retractions, segments = resolve([available, blocked])
        overlap = [segment for segment in segments if segment.start == blocked.start]
        self.assertEqual(len(overlap), 1)
        self.assertEqual(overlap[0].status, CONFLICT)
        self.assertEqual(set(overlap[0].declaration_ids),
                         {available.declaration_id, blocked.declaration_id})
        self.assertEqual(overlap[0].reason, "overlapping_declarations_conflict")
        self.assertEqual(len(segments), 3)

    def test_cancelled_interval_remains_traceable(self):
        available = declaration(revision=1)
        cancelled = declaration(revision=2, status="cancelled")
        effective, superseded, retractions, segments = resolve([available, cancelled])
        self.assertEqual(len(effective), 1)
        self.assertEqual(len(superseded), 1)
        self.assertEqual(segments[0].status, CANCELLED)
        self.assertEqual(segments[0].reason, "cancelled_interval_retained")
        self.assertEqual(segments[0].deciding[0].declaration_id, cancelled.declaration_id)
        roles = {claim.role: claim.declaration.declaration_id for claim in segments[0].claims}
        self.assertEqual(roles["retracted_prior"], available.declaration_id)
        self.assertEqual(len(retractions), 1)
        self.assertTrue(retractions[0].to_dict()["previously_available"])
        self.assertEqual(retractions[0].supersedes.declaration_id, available.declaration_id)

    def test_cancelled_interval_is_not_a_gap(self):
        cancelled = declaration(revision=1, status="cancelled")
        _effective, _superseded, _retractions, segments = resolve([cancelled])
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0].status, CANCELLED)
        self.assertEqual(cancelled.start, segments[0].start)

    def test_fine_and_day_coverage_are_layered_not_added(self):
        day = declaration(revision=1, granularity="day", start="2026-10-25", end="2026-10-26")
        slot = declaration(revision=2, start="2026-10-25T01:30:00+02:00", end="2026-10-25T02:00:00+02:00",
                           status="unavailable")
        _effective, _superseded, _retractions, segments = resolve([day, slot])
        span = [segment for segment in segments if segment.start == slot.start]
        self.assertEqual(len(span), 1)
        self.assertEqual(span[0].status, "unavailable")
        self.assertEqual(span[0].reason, "finer_granularity_refined_coarse_declaration")
        self.assertEqual(span[0].deciding[0].declaration_id, slot.declaration_id)
        self.assertEqual(span[0].refined[0].declaration_id, day.declaration_id)
        covered = sum((segment.end - segment.start for segment in segments), timedelta())
        self.assertEqual(covered, day.end - day.start)

    def test_granularity_switch_keeps_both_coverages_distinct(self):
        day = declaration(revision=1, granularity="day", start="2026-10-25", end="2026-10-26")
        fine = declaration(revision=2, start="2026-10-25T01:30:00+02:00", end="2026-10-25T02:00:00+02:00")
        _effective, _superseded, _retractions, segments = resolve([day, fine])
        fine_segments = [segment for segment in segments if segment.deciding[0].declaration_id == fine.declaration_id]
        self.assertEqual(len(fine_segments), 1)
        self.assertIsNotNone(fine_segments[0].refined)
        self.assertEqual(fine_segments[0].refined[0].granularity, "day")

    def test_revision_supersedes_the_same_stated_interval(self):
        old = declaration(revision=1)
        new = declaration(revision=2, status="unavailable")
        effective, superseded, _retractions, segments = resolve([old, new])
        self.assertEqual({item.declaration_id for item in effective}, {new.declaration_id})
        self.assertEqual({item.declaration_id for item in superseded}, {old.declaration_id})
        self.assertEqual([segment.status for segment in segments], ["unavailable"])

    def test_revised_coverage_overlapping_another_claim_stays_a_conflict(self):
        old = declaration(revision=1, start="2026-10-25T01:00:00+02:00", end="2026-10-25T02:00:00+02:00")
        new = declaration(revision=2, start="2026-10-25T01:00:00+02:00", end="2026-10-25T03:00:00+02:00",
                          status="unavailable")
        effective, superseded, _retractions, segments = resolve([old, new])
        self.assertEqual(len(effective), 2)
        self.assertEqual(superseded, ())
        overlap = [segment for segment in segments if segment.start == old.start]
        self.assertEqual(overlap[0].status, CONFLICT)

    def test_different_suppliers_never_collapse(self):
        first = declaration(supplier="hire-a")
        second = declaration(supplier="hire-b", source="other")
        _effective, _superseded, _retractions, segments = resolve(first and [first, second])
        self.assertEqual({segment.supplier for segment in segments}, {"hire-a", "hire-b"})
        self.assertEqual(len(segments), 2)


if __name__ == "__main__":
    unittest.main()


class MidnightTransitionTests(unittest.TestCase):
    def test_skipped_midnight_starts_at_the_first_existing_wall_clock_time(self):
        # America/Santiago skips 2026-09-06 00:00 on a forward transition.
        item = declaration(zone="America/Santiago", granularity="day",
                           start="2026-09-06", end="2026-09-07")
        self.assertEqual(item.start.isoformat(), "2026-09-06T04:00:00+00:00")
        self.assertEqual(item.end - item.start, timedelta(hours=23))

    def test_repeated_midnight_uses_its_earlier_occurrence(self):
        # Some zones repeat 00:00 on a backward transition; the day starts at
        # the first instant even if it makes the day 24 hours long.
        item = declaration(zone="America/Santiago", granularity="day",
                           start="2026-04-05", end="2026-04-06")
        self.assertEqual(item.start.isoformat(), "2026-04-05T04:00:00+00:00")
        self.assertEqual(item.end - item.start, timedelta(hours=24))
