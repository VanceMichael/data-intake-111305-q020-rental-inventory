import http.client
import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError

from rental_inventory.archive import Archive
from rental_inventory.catalog import Catalog
from rental_inventory.client import Client
from rental_inventory.ingest import FetchFailed, IngestRun, StaleCacheExhausted
from rental_inventory.origin import load_scenario, serve


class Harness(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.archive = Archive(root / "raw.sqlite3")
        self.catalog = Catalog(root / "catalog.sqlite3")
        self.scenario = load_scenario()

    def tearDown(self):
        self.catalog.close()
        self.archive.close()
        self.temporary.cleanup()

    def run_ingest(self, origin, attempts=3, start_path="/availability/1"):
        run = IngestRun(Client(origin.base_url), self.archive, self.catalog, "local-source", start_path)
        pages = run.drain(attempts=attempts, sleeper=lambda _: None)
        return run, pages


class PaginationTests(Harness):
    def test_stale_cached_page_is_archived_but_not_merged(self):
        with serve(self.scenario) as origin:
            run, pages = self.run_ingest(origin)
        rejected = [page for page in pages if not page.accepted]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0].reason, "stale_cached_response")
        self.assertEqual(rejected[0].calendar_version, 1)
        self.assertEqual(run.confirmed_version, 3)
        # The stale body is still in the raw archive for later forensics.
        self.assertEqual(self.archive.body(rejected[0].capture_id).startswith(b'{"items"'), True)
        self.assertIn(rejected[0].capture_id, [capture["id"] for capture in self.archive.captures("local-source")])

    def test_late_response_cannot_overwrite_newer_calendar(self):
        with serve(self.scenario) as origin:
            run, _pages = self.run_ingest(origin)
        declarations = run.declarations()
        first = self.catalog.publish(declarations, calendar_version=run.confirmed_version)
        # A late arriving page from the older calendar version must be refused
        # at publication time as well as at ingestion time.
        from rental_inventory.catalog import StalePublication

        with self.assertRaises(StalePublication):
            self.catalog.publish(declarations, calendar_version=1)
        self.assertEqual(self.catalog.latest_version(), first["version"])
        self.assertEqual(self.catalog.published_calendar_version(), 3)

    def test_cursor_does_not_advance_past_a_stale_page(self):
        scenario = json.loads(json.dumps(self.scenario))
        # Page 1 confirms calendar version 2; every attempt at page 2 keeps
        # returning version 1, so the run must stop resumable at page 2 instead
        # of skipping it.
        scenario["routes"]["/availability/1"] = {
            "json": {"items": [], "calendar_version": 2, "granularity": "day", "next": "/availability/2"},
            "headers": {"X-Calendar-Version": "2", "X-Granularity": "day"},
        }
        scenario["routes"]["/availability/2"] = {
            "json": {"items": [], "calendar_version": 1, "granularity": "day", "next": "/availability/3"},
            "headers": {"X-Calendar-Version": "1", "X-Granularity": "day"},
        }
        with serve(scenario) as origin:
            run = IngestRun(Client(origin.base_url), self.archive, self.catalog, "local-source")
            with self.assertRaises(StaleCacheExhausted) as caught:
                run.drain(attempts=2, sleeper=lambda _: None)
        self.assertEqual(caught.exception.page_index, 2)
        self.assertEqual(run.next_path, "/availability/2")
        self.assertFalse(run.finished)

    def test_run_resumes_at_first_unaccepted_page(self):
        with serve(self.scenario) as origin:
            client = Client(origin.base_url)
            partial = IngestRun(client, self.archive, self.catalog, "local-source")
            partial.step()
            partial.step()
            self.assertEqual(partial.next_path, "/availability/3")
            captures_before = len(self.archive.captures())
            resumed = IngestRun(client, self.archive, self.catalog, "local-source")
            self.assertEqual(resumed.pages[0].capture_id, partial.pages[0].capture_id)
            resumed.drain(attempts=3, sleeper=lambda _: None)
        accepted_after_resume = [page for page in resumed.pages if page.accepted][2:]
        self.assertTrue(accepted_after_resume)
        self.assertEqual(resumed.confirmed_version, 3)
        self.assertEqual(len(self.archive.captures()), captures_before + 3)

    def test_transient_failure_is_retried_and_archived(self):
        scenario = json.loads(json.dumps(self.scenario))
        first = scenario["routes"]["/availability/1"]
        scenario["routes"]["/availability/1"] = [
            {"status": 503, "json": {"error": "unstable"}},
            first,
        ]
        with serve(scenario) as origin:
            run, pages = self.run_ingest(origin)
        self.assertEqual(run.confirmed_version, 3)
        self.assertEqual([page.status for page in pages if page.status != 200], [503])
        self.assertIn(503, [capture["status"] for capture in self.archive.captures()])

    def test_exhausted_fetch_failure_stays_resumable(self):
        scenario = json.loads(json.dumps(self.scenario))
        scenario["routes"]["/availability/1"] = {"status": 503, "json": {"error": "down"}}
        with serve(scenario) as origin:
            run = IngestRun(Client(origin.base_url), self.archive, self.catalog, "local-source")
            with self.assertRaises(FetchFailed):
                run.drain(attempts=2, sleeper=lambda _: None)
        self.assertEqual(run.next_path, "/availability/1")
        self.assertFalse(run.finished)
        self.assertTrue(self.archive.captures())
        self.assertEqual(run.pages[0].status, 503)


class GranularitySwitchTests(Harness):
    def test_day_and_fine_interfaces_are_layered_not_summed(self):
        with serve(self.scenario) as origin:
            run, _pages = self.run_ingest(origin)
        self.assertEqual(run.granularities, {"day", "30m"})
        published = self.catalog.publish(run.declarations(), calendar_version=run.confirmed_version)
        hire_a = [
            window for window in published["windows"]
            if window["supplier"] == "hire-a" and window["equipment"] == "EQ-9"
        ]
        coverage = sum(
            (_duration(window) for window in hire_a), 0.0
        )
        # The fine declarations sit inside the whole-day coverage; adding both
        # interfaces would have produced >25 h for this calendar day.
        self.assertEqual(coverage, 25 * 3600)
        self.assertIn("day", {g for window in hire_a for g in window["granularities"]})
        self.assertIn("30m", {g for window in hire_a for g in window["granularities"]})

    def test_legacy_day_endpoint_still_parses(self):
        with serve(self.scenario) as origin:
            from rental_inventory.decoding import records

            response = Client(origin.base_url).fetch("/availability/day/1")
        items = records(response.body, response.headers["content-type"])
        self.assertEqual({item["granularity"] for item in items}, {"day"})

    def test_mixed_endpoints_keep_their_own_granularity(self):
        with serve(self.scenario) as origin:
            client = Client(origin.base_url)
            day = IngestRun(client, self.archive, self.catalog, "day-source", "/availability/day/1")
            day.drain(attempts=2, sleeper=lambda _: None)
            fine = IngestRun(client, self.archive, self.catalog, "fine-source", "/availability/fine/1")
            fine.drain(attempts=2, sleeper=lambda _: None)
        self.assertEqual(day.granularities, {"day"})
        self.assertEqual(fine.granularities, {"30m"})
        self.assertTrue({item.granularity for item in day.declarations()} == {"day"})
        self.assertTrue({item.granularity for item in fine.declarations()} == {"30m"})


class CrossTimezoneTests(Harness):
    def test_every_interval_traces_back_to_a_capture(self):
        with serve(self.scenario) as origin:
            run, _pages = self.run_ingest(origin)
        published = self.catalog.publish(run.declarations(), calendar_version=run.confirmed_version)
        captures = {capture["id"]: capture for capture in self.archive.captures()}
        for window in published["windows"]:
            for segment in window["segments"]:
                self.assertTrue(segment["sources"])
                for source in segment["sources"]:
                    self.assertIn(source["capture_id"], captures)
                    self.assertEqual(captures[source["capture_id"]]["url"].split("/")[-1].isdigit(), True)
                    self.assertTrue(source["zone"])
                    self.assertTrue(source["revision"] >= 1)
        zones = {source["zone"] for window in published["windows"] for segment in window["segments"]
                 for source in segment["sources"]}
        self.assertEqual(zones, {"Europe/Berlin", "Pacific/Auckland", "UTC"})

    def test_closed_boundary_supplier_is_interpreted_in_its_own_zone(self):
        with serve(self.scenario) as origin:
            run, _pages = self.run_ingest(origin)
        auckland = [item for item in run.declarations() if item.supplier == "hire-b" and item.granularity == "day"]
        self.assertEqual(len(auckland), 1)
        article = auckland[0]
        self.assertEqual(article.boundary, "closed")
        # 2026-10-26 00:00 in Auckland (+13) == 2026-10-25 11:00 UTC.
        self.assertEqual(article.start.isoformat(), "2026-10-25T11:00:00+00:00")
        self.assertEqual(article.end - article.start, __import__("datetime").timedelta(hours=24))

    def test_berlin_day_covers_25_hours_across_the_dst_fallback(self):
        with serve(self.scenario) as origin:
            run, _pages = self.run_ingest(origin)
        berlin = [item for item in run.declarations() if item.zone == "Europe/Berlin" and item.granularity == "day"]
        self.assertEqual(len(berlin), 1)
        self.assertEqual(berlin[0].end - berlin[0].start, __import__("datetime").timedelta(hours=25))


def _duration(window):
    from datetime import datetime

    start = datetime.fromisoformat(window["start_utc"])
    end = datetime.fromisoformat(window["end_utc"])
    return (end - start).total_seconds()


if __name__ == "__main__":
    unittest.main()
