import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from rental_inventory.archive import Archive
from rental_inventory.catalog import Catalog
from rental_inventory.client import Client
from rental_inventory.ingest import IngestRun
from rental_inventory.origin import load_scenario, serve


def cases():
    path = Path(__file__).resolve().parents[1] / "fixtures" / "business_cases.json"
    return {case["name"]: case for case in json.loads(path.read_text(encoding="utf-8"))}


class BusinessCaseTests(unittest.TestCase):
    """The fixture's stated expectations are executable, not decorative."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.archive = Archive(root / "raw.sqlite3")
        self.catalog = Catalog(root / "catalog.sqlite3")

    def tearDown(self):
        self.catalog.close()
        self.archive.close()
        self.temporary.cleanup()

    def sync(self, origin):
        run = IngestRun(Client(origin.base_url), self.archive, self.catalog, "local-source")
        pages = run.drain(attempts=3, sleeper=lambda _: None)
        return run, pages

    def test_cases_name_the_four_required_situations(self):
        self.assertEqual(
            set(cases()),
            {"cross-timezone-boundary", "stale-cache-reflow", "granularity-switch",
             "booking-review-without-movement"},
        )

    def test_cross_timezone_boundary_case(self):
        case = cases()["cross-timezone-boundary"]
        with serve(load_scenario()) as origin:
            run, _pages = self.sync(origin)
        expected = case["expect"]
        for supplier, want in expected.items():
            item = next(
                declaration for declaration in run.declarations()
                if declaration.supplier == supplier and declaration.granularity == "day"
            )
            with self.subTest(supplier=supplier):
                self.assertEqual(item.zone, want["zone"])
                self.assertEqual(item.boundary, want["boundary"])
                self.assertEqual(item.granularity, want["granularity"])
                self.assertEqual(item.start.isoformat(), want["start_utc"])
                self.assertEqual(item.end.isoformat(), want["end_utc"])
                self.assertEqual(item.end - item.start, timedelta(hours=want["hours"]))

    def test_stale_cache_reflow_case(self):
        case = cases()["stale-cache-reflow"]
        with serve(load_scenario()) as origin:
            run, pages = self.sync(origin)
        expected = case["expect"]
        rejected = [page for page in pages if not page.accepted]
        self.assertEqual([page.reason for page in rejected], [expected["rejected_reason"]])
        self.assertEqual(rejected[0].calendar_version, expected["rejected_calendar_version"])
        self.assertEqual(run.confirmed_version, expected["confirmed_calendar_version"])
        rejected_body = self.archive.body(rejected[0].capture_id)
        accepted_ids = {page.capture_id for page in pages if page.accepted}
        self.assertNotIn(rejected[0].capture_id, accepted_ids)
        self.assertTrue(rejected_body)

    def test_granularity_switch_case(self):
        case = cases()["granularity-switch"]
        with serve(load_scenario()) as origin:
            run, _pages = self.sync(origin)
        published = self.catalog.publish(run.declarations(), calendar_version=run.confirmed_version)
        expected = case["expect"]
        self.assertEqual(published["summary"]["coverage_semantics"], expected["coverage_semantics"])
        self.assertEqual(published["summary"]["granularities"], sorted(expected["granularities"]))
        conflicts = [
            segment
            for window in published["windows"] for segment in window["segments"]
            if segment["status"] == "conflict"
        ]
        self.assertTrue(conflicts)
        self.assertTrue(
            all(len(segment["deciding_declaration_ids"]) == expected["conflict_sources_retained"]
                for segment in conflicts)
        )

    def test_booking_review_case(self):
        case = cases()["booking-review-without-movement"]
        expected = case["expect"]
        self.catalog.add_booking(
            "B1", "hire-a", "EQ-9", "2026-10-25T01:30:00+02:00", "2026-10-25T02:00:00+02:00",
        )
        self.catalog.add_booking(
            "B3", "hire-a", "EQ-9", "2026-10-25T02:15:00+02:00", "2026-10-25T02:45:00+02:00",
        )
        before = [dict(booking) for booking in self.catalog.bookings()]
        with serve(load_scenario()) as origin:
            run, _pages = self.sync(origin)
        published = self.catalog.publish(run.declarations(), calendar_version=run.confirmed_version)
        reviews = {review["booking_id"]: review for review in published["reviews"]}
        self.assertEqual({review["action"] for review in reviews.values()}, set(expected["actions"]))
        self.assertEqual(
            set(expected["review_reasons"]),
            {review["reason"] for review in reviews.values()},
        )
        self.assertTrue(all(review["requires_review"] for review in reviews.values()))
        for review in reviews.values():
            self.assertTrue(review["differences"])
            for difference in review["differences"]:
                self.assertTrue(difference["declarations"])
                for declaration in difference["declarations"]:
                    self.assertIn("declaration_id", declaration)
                    self.assertIn("zone", declaration)
                    self.assertIn("boundary", declaration)
                    self.assertIn("granularity", declaration)
        self.assertEqual([dict(booking) for booking in self.catalog.bookings()], before)

    def test_booking_case_keeps_quiet_when_open_ends_do_not_bite(self):
        # A different clean calendar creates no review noise for the same booking.
        self.catalog.add_booking(
            "B-clean", "hire-c", "EQ-1", "2026-11-01T09:00:00+01:00", "2026-11-01T10:00:00+01:00",
        )
        from rental_inventory.availability import inspect_declaration

        declaration = inspect_declaration({
            "supplier": "hire-c", "equipment": "EQ-1", "revision": 1, "zone": "Europe/Berlin",
            "granularity": "day", "start": "2026-11-01", "end": "2026-11-02", "status": "available",
        }, source="other")
        published = self.catalog.publish([declaration], calendar_version=1)
        self.assertFalse(published["reviews"][0]["requires_review"])


if __name__ == "__main__":
    unittest.main()
