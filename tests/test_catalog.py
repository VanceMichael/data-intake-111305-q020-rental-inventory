import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from rental_inventory.availability import inspect_declaration
from rental_inventory.catalog import Catalog, PublishError, StalePublication, compute_reviews


def declaration(**overrides):
    raw = {
        "supplier": "hire-a", "equipment": "EQ-9", "revision": 1,
        "zone": "Europe/Berlin", "granularity": "day",
        "start": "2026-10-25", "end": "2026-10-26", "status": "available",
    }
    raw.update(overrides)
    source = raw.pop("source", "feed")
    return inspect_declaration(raw, source=source)


def moment(text):
    return datetime.fromisoformat(text)


class CatalogTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "catalog.sqlite3"
        self.catalog = Catalog(self.path)

    def tearDown(self):
        self.catalog.close()
        self.temporary.cleanup()

    def add_booking(self, booking_id="B1", start="2026-10-25T01:30:00+02:00", end="2026-10-25T02:00:00+02:00", **extra):
        self.catalog.add_booking(booking_id, "hire-a", "EQ-9", start, end, **extra)


class PublicationTests(CatalogTestCase):
    def test_publish_is_immutable_and_versions_are_numbered(self):
        first = self.catalog.publish([declaration()], calendar_version=1)
        second = self.catalog.publish([declaration(revision=2, status="cancelled")], calendar_version=2)
        self.assertEqual((first["version"], second["version"]), (1, 2))
        old = self.catalog.query(1)
        self.assertEqual(old["calendar_version"], 1)
        self.assertEqual(old["windows"][0]["status"], "available")
        self.assertEqual(self.catalog.query(2)["windows"][0]["status"], "cancelled")
        self.assertNotEqual(old["digest"], self.catalog.query(2)["digest"])

    def test_late_publication_of_older_calendar_is_refused(self):
        self.catalog.publish([declaration()], calendar_version=5)
        with self.assertRaises(StalePublication):
            self.catalog.publish([declaration(revision=9, status="cancelled")], calendar_version=4)
        self.assertEqual(self.catalog.latest_version(), 1)
        self.assertEqual(self.catalog.published_calendar_version(), 5)

    def test_same_calendar_version_may_be_republished_with_new_supplier_data(self):
        self.catalog.publish([declaration()], calendar_version=5)
        result = self.catalog.publish(
            [declaration(), declaration(supplier="hire-b", equipment="EQ-7", source="other")],
            calendar_version=5,
        )
        self.assertEqual(result["version"], 2)
        self.assertEqual({window["supplier"] for window in result["windows"]}, {"hire-a", "hire-b"})

    def test_query_returns_windows_and_reviews_from_one_explanation(self):
        self.add_booking()
        self.catalog.publish([declaration()], calendar_version=1)
        self.catalog.publish([declaration(revision=2, status="cancelled")], calendar_version=2)
        latest = self.catalog.query()
        self.assertEqual(latest["version"], 2)
        self.assertEqual(latest["windows"][0]["status"], "cancelled")
        self.assertTrue(latest["reviews"][0]["requires_review"])
        earlier = self.catalog.query(1)
        self.assertEqual(earlier["windows"][0]["status"], "available")
        self.assertFalse(earlier["reviews"][0]["requires_review"])

    def test_tampering_with_a_snapshot_is_detected(self):
        self.catalog.publish([declaration()], calendar_version=1)
        row = self.catalog.connection.execute(
            "SELECT payload FROM windows WHERE version=1"
        ).fetchone()
        payload = json.loads(row["payload"])
        payload["status"] = "conflict"
        with self.catalog.connection:
            self.catalog.connection.execute(
                "UPDATE windows SET payload=? WHERE version=1", (json.dumps(payload),)
            )
        with self.assertRaises(PublishError):
            self.catalog.query(1)

    def test_declaration_payload_keeps_zone_boundary_and_revision(self):
        self.catalog.publish(
            [declaration(boundary="closed", zone="Pacific/Auckland", start="2026-10-26", end="2026-10-26")],
            calendar_version=1,
        )
        sources = self.catalog.query()["windows"][0]["segments"][0]["sources"]
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["zone"], "Pacific/Auckland")
        self.assertEqual(sources[0]["boundary"], "closed")
        self.assertEqual(sources[0]["revision"], 1)
        self.assertEqual(sources[0]["start"], "2026-10-26")
        self.assertEqual(sources[0]["start_utc"], "2026-10-25T11:00:00+00:00")


class BookingReviewTests(CatalogTestCase):
    def test_unaffected_booking_is_not_flagged(self):
        self.add_booking()
        result = self.catalog.publish([declaration()], calendar_version=1)
        review = result["reviews"][0]
        self.assertFalse(review["requires_review"])
        self.assertIsNone(review["reason"])

    def test_booking_is_flagged_when_the_supplier_cancels(self):
        self.add_booking()
        result = self.catalog.publish(
            [
                declaration(),
                declaration(revision=2, granularity="30m",
                            start="2026-10-25T01:30:00+02:00", end="2026-10-25T02:00:00+02:00",
                            status="cancelled"),
            ],
            calendar_version=1,
        )
        review = result["reviews"][0]
        self.assertTrue(review["requires_review"])
        self.assertEqual(review["reason"], "supplier_availability_changed")
        self.assertEqual(review["action"], "review_only_do_not_move_booking")
        difference = review["differences"][0]
        self.assertEqual(difference["status"], "cancelled")
        self.assertEqual(
            {source["granularity"] for source in difference["declarations"]},
            {"day", "30m"},
        )

    def test_booking_is_flagged_on_a_conflict_and_both_sources_are_named(self):
        self.add_booking()
        # Two different sources declare the same instant with different
        # availability: neither may erase the other.
        result = self.catalog.publish(
            [
                declaration(revision=1, granularity="30m", source="feed",
                            start="2026-10-25T01:30:00+02:00", end="2026-10-25T02:00:00+02:00",
                            status="available"),
                declaration(revision=7, granularity="30m", source="legacy-feed", zone="UTC",
                            start="2026-10-24T23:30:00Z", end="2026-10-25T00:00:00Z",
                            status="unavailable"),
            ],
            calendar_version=1,
        )
        review = result["reviews"][0]
        self.assertTrue(review["requires_review"])
        self.assertEqual(review["reason"], "supplier_declaration_conflict")
        self.assertEqual(len(review["differences"][0]["declarations"]), 2)
        self.assertEqual(
            {source["declared_status"] for source in review["differences"][0]["declarations"]},
            {"available", "unavailable"},
        )

    def test_booking_outside_declared_coverage_is_flagged_without_moving_it(self):
        self.add_booking(start="2026-10-27T01:30:00+01:00", end="2026-10-27T02:00:00+01:00")
        result = self.catalog.publish([declaration()], calendar_version=1)
        review = result["reviews"][0]
        self.assertTrue(review["requires_review"])
        self.assertEqual(review["reason"], "booking_outside_declared_coverage")
        stored = self.catalog.bookings()[0]
        self.assertEqual(
            (stored["start_utc"], stored["end_utc"]),
            ("2026-10-27T00:30:00+00:00", "2026-10-27T01:00:00+00:00"),
        )

    def test_reviews_across_a_granularity_switch_use_the_fine_declaration(self):
        self.add_booking()
        declarations = [
            declaration(revision=1),
            declaration(revision=2, granularity="30m",
                        start="2026-10-25T01:30:00+02:00", end="2026-10-25T02:00:00+02:00",
                        status="available"),
        ]
        result = self.catalog.publish(declarations, calendar_version=1)
        self.assertFalse(result["reviews"][0]["requires_review"])

    def test_booking_spanning_a_dst_fallback_is_compared_in_utc(self):
        self.add_booking(start="2026-10-25T01:30:00+02:00", end="2026-10-25T02:30:00+01:00")
        result = self.catalog.publish([declaration()], calendar_version=1)
        self.assertFalse(result["reviews"][0]["requires_review"])
        self.assertEqual(
            result["reviews"][0]["end_utc"],
            "2026-10-25T01:30:00+00:00",
        )

    def test_reviews_without_segments_are_still_reported(self):
        self.add_booking()
        result = self.catalog.publish([], calendar_version=1)
        review = result["reviews"][0]
        self.assertTrue(review["requires_review"])
        self.assertEqual(review["reason"], "booking_outside_declared_coverage")
        self.assertEqual(result["windows"], [])


class ReviewHelperTests(unittest.TestCase):
    def test_compute_reviews_never_edits_bookings(self):
        booking = {
            "booking_id": "B1", "supplier": "hire-a", "equipment": "EQ-9",
            "start_utc": "2026-10-25T00:00:00+00:00", "end_utc": "2026-10-25T01:00:00+00:00",
            "status": "confirmed",
        }
        snapshot = json.loads(json.dumps(booking))
        compute_reviews([booking], [])
        self.assertEqual(booking, snapshot)


if __name__ == "__main__":
    unittest.main()


class ConsistencyTests(CatalogTestCase):
    def test_concurrent_publication_never_splits_a_query(self):
        import threading

        self.add_booking()
        self.catalog.publish([declaration()], calendar_version=1)
        stop = threading.Event()
        failures = []

        def publisher(index):
            writer = Catalog(self.path)
            try:
                while not stop.is_set():
                    revision = 2 + index
                    writer.publish(
                        [
                            declaration(),
                            declaration(revision=revision, granularity="30m",
                                        start="2026-10-25T01:30:00+02:00",
                                        end="2026-10-25T02:00:00+02:00",
                                        status="cancelled" if revision % 2 == 0 else "available"),
                        ],
                        calendar_version=revision,
                    )
            except Exception as error:  # pragma: no cover - surfaced below
                failures.append(error)
            finally:
                writer.close()

        worker = threading.Thread(target=publisher, args=(0,), daemon=True)
        worker.start()
        try:
            for _ in range(25):
                view = self.catalog.query()
                review = view["reviews"][0]
                cancelled = any(window["status"] == "cancelled" for window in view["windows"])
                # Windows and the review list always describe the same version.
                self.assertEqual(cancelled, review["requires_review"])
                self.assertEqual(view["summary"]["review_count"], int(review["requires_review"]))
        finally:
            stop.set()
            worker.join(timeout=5)
        self.assertEqual(failures, [])


class SummaryIntegrityTests(CatalogTestCase):
    def test_tampered_summary_is_detected(self):
        self.catalog.publish([declaration()], calendar_version=1)
        summary = json.loads(
            self.catalog.connection.execute(
                "SELECT summary FROM versions WHERE version=1"
            ).fetchone()["summary"]
        )
        summary["conflict_count"] = 99
        with self.catalog.connection:
            self.catalog.connection.execute(
                "UPDATE versions SET summary=? WHERE version=1", (json.dumps(summary),)
            )
        with self.assertRaises(PublishError):
            self.catalog.query(1)
