"""Recoverable paginated collection with a stale-cache fence.

A run is a sequence of pages.  Raw responses always go to the append-only
archive; interpretation state (which page was accepted, at which calendar
version) is persisted after every page, so a crashed or interrupted run
resumes at the first page that was not accepted.

Two guards matter for the calendar:

* a page carrying an older ``X-Calendar-Version`` than a page already accepted
  in this run is a stale cache reflow.  It is archived but not merged, and it
  may not advance the cursor.
* a page from a different granularity is accepted, but declarations keep the
  granularity they were stated under.  Whole-day and 30-minute coverage is
  layered by :mod:`rental_inventory.availability`, never added together.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .availability import Declaration, HALF_OPEN, inspect_declaration


class IngestError(RuntimeError):
    pass


class FetchFailed(IngestError):
    """A page could not be fetched; the run remains resumable."""

    def __init__(self, path: str, status: int, page_index: int):
        super().__init__(f"fetch failed for page {page_index} ({path}): HTTP {status}")
        self.path = path
        self.status = status
        self.page_index = page_index


class StaleCacheExhausted(IngestError):
    """Every retry of a page still returned an older calendar version."""

    def __init__(self, path: str, page_index: int, received: int, confirmed: int):
        super().__init__(
            f"page {page_index} ({path}) kept returning calendar version {received}, "
            f"older than confirmed version {confirmed}"
        )
        self.path = path
        self.page_index = page_index
        self.received = received
        self.confirmed = confirmed


@dataclass(frozen=True)
class Page:
    index: int
    path: str
    capture_id: int
    status: int
    calendar_version: int
    granularity: str
    items: tuple[dict[str, Any], ...]
    next_path: str | None
    accepted: bool
    reason: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)

    def to_state(self) -> dict[str, Any]:
        return {
            "index": self.index, "path": self.path, "capture_id": self.capture_id,
            "status": self.status, "calendar_version": self.calendar_version,
            "granularity": self.granularity, "items": list(self.items),
            "next_path": self.next_path, "accepted": self.accepted,
            "reason": self.reason,
            "headers": {str(name): str(value) for name, value in (self.headers or {}).items()},
        }

    @classmethod
    def from_state(cls, state: Mapping[str, Any]) -> "Page":
        return cls(
            index=int(state["index"]), path=state["path"],
            capture_id=int(state["capture_id"]), status=int(state["status"]),
            calendar_version=int(state["calendar_version"]),
            granularity=state["granularity"],
            items=tuple(state["items"]), next_path=state.get("next_path"),
            accepted=bool(state["accepted"]), reason=state.get("reason"),
            headers=dict(state.get("headers") or {}),
        )


def _int_header(headers: Mapping[str, str], name: str, payload: Mapping[str, Any]) -> int:
    value = headers.get(name.lower())
    if value is None:
        value = payload.get("calendar_version")
    if value is None:
        return 0
    try:
        parsed = int(str(value).strip())
    except ValueError as error:
        raise IngestError(f"invalid {name}: {value!r}") from error
    if parsed < 0:
        raise IngestError(f"invalid {name}: {value!r}")
    return parsed


def _granularity(headers: Mapping[str, str], payload: Mapping[str, Any], default: str) -> str:
    value = headers.get("x-granularity") or payload.get("granularity") or default
    return str(value).strip().lower()


class IngestRun:
    """One paginated collection run, persisted between pages."""

    def __init__(
        self,
        client,
        archive,
        catalog,
        source: str,
        start_path: str = "/availability/1",
        *,
        run_id: str | None = None,
        default_zone: str | None = None,
        default_boundary: str = HALF_OPEN,
        default_granularity: str = "day",
    ):
        if not source or not str(source).strip():
            raise ValueError("source is required")
        self.client = client
        self.archive = archive
        self.catalog = catalog
        self.source = str(source).strip()
        self.start_path = start_path
        self.run_id = run_id or self._derive_run_id()
        self.default_zone = default_zone
        self.default_boundary = default_boundary
        self.default_granularity = default_granularity
        self.pages: list[Page] = []
        self.next_path: str | None = start_path
        self.confirmed_version = 0
        self.finished = False
        self.granularities: set[str] = set()
        self.pending_reason: str | None = None
        self._load()

    def _derive_run_id(self) -> str:
        # A run is identified by source and entry path; resuming means opening
        # the same run id again with the same catalog.
        return f"{self.source}|{self.start_path}"

    # -- persistence -----------------------------------------------------
    def _load(self) -> None:
        saved = self.catalog.load_run(self.run_id)
        if saved is None:
            return
        self.pages = [Page.from_state(state) for state in saved["pages"]]
        self.next_path = saved["next_path"]
        self.confirmed_version = int(saved["confirmed_version"])
        self.finished = bool(saved["finished"])
        self.granularities = set(saved["granularities"])
        self.pending_reason = saved.get("pending_reason")

    def _save(self) -> None:
        self.catalog.save_run(self.run_id, {
            "source": self.source,
            "start_path": self.start_path,
            "pages": [page.to_state() for page in self.pages],
            "next_path": self.next_path,
            "confirmed_version": self.confirmed_version,
            "finished": self.finished,
            "granularities": sorted(self.granularities),
            "pending_reason": self.pending_reason,
        })

    # -- fetching --------------------------------------------------------
    def _decode(self, response) -> tuple[list[dict[str, Any]], str | None, Mapping[str, Any]]:
        from .decoding import records

        payload: Mapping[str, Any] = {}
        items = records(response.body, response.headers.get("content-type", ""))
        # Re-read the raw payload for pagination metadata; the decoder returns
        # only records so both JSON and embedded blocks stay supported.
        if response.headers.get("content-type", "").partition(";")[0].strip().lower() == "application/json":
            import json
            loaded = json.loads(response.body)
            if isinstance(loaded, dict):
                payload = loaded
        return items, payload.get("next"), payload

    def fetch_page(self, path: str | None = None) -> tuple[Page, bool, bool]:
        """Fetch and archive one page.

        Returns ``(page, accepted, retryable)``.  A stale cached response is
        archived, rejected, and retryable; an HTTP error is archived, rejected,
        and retryable.  Nothing is merged until the page is accepted.
        """
        target = path or self.next_path
        if target is None:
            raise IngestError("run is finished")
        # Retries of the same cursor keep one page number; only accepted pages
        # advance it, so a rejected attempt never looks like progress.
        index = sum(1 for page in self.pages if page.accepted) + 1
        response = self.client.fetch(target)
        capture_id = self.archive.append(
            self.source, response.url, response.status, response.headers, response.body
        )
        if response.status != 200:
            # Error bodies are archived but never decoded as source records;
            # the cursor stays put so the page can be retried on resume.
            version = _int_header(response.headers, "x-calendar-version", {})
            granularity = _granularity(response.headers, {}, self.default_granularity)
            page = Page(index, target, capture_id, response.status, version, granularity,
                        (), None, False, f"http_{response.status}", response.headers)
            self.pages.append(page)
            self.pending_reason = page.reason
            self._save()
            return page, False, True
        items, next_path, payload = self._decode(response)
        version = _int_header(response.headers, "x-calendar-version", payload)
        granularity = _granularity(response.headers, payload, self.default_granularity)
        if version < self.confirmed_version:
            page = Page(index, target, capture_id, response.status, version, granularity,
                        tuple(items), next_path, False, "stale_cached_response", response.headers)
            self.pages.append(page)
            self.pending_reason = page.reason
            self._save()
            return page, False, True
        page = Page(index, target, capture_id, response.status, version, granularity,
                    tuple(items), next_path, True, None, response.headers)
        self.pages.append(page)
        self.confirmed_version = max(self.confirmed_version, version)
        self.granularities.add(granularity)
        self.pending_reason = None
        self.next_path = next_path
        if next_path is None:
            self.finished = True
        self._save()
        return page, True, False

    def step(self) -> Page | None:
        if self.finished:
            return None
        page, accepted, _retryable = self.fetch_page()
        return page

    def drain(
        self,
        attempts: int = 3,
        sleeper: Callable[[float], None] = time.sleep,
        backoff: float = 0.05,
    ) -> list[Page]:
        """Collect until finished, retrying transient and stale pages.

        ``attempts`` counts tries per page.  Retry-After is honoured when
        present, capped so a local demonstration stays responsive; callers can
        inject ``sleeper`` for tests.
        """
        produced: list[Page] = []
        while not self.finished:
            last: Page | None = None
            for attempt in range(max(1, attempts)):
                page, accepted, retryable = self.fetch_page()
                produced.append(page)
                last = page
                if accepted:
                    break
                if not retryable:
                    raise IngestError(f"page {page.index} rejected: {page.reason}")
                delay = backoff * (attempt + 1)
                retry_after = page.headers.get("retry-after") if page.headers else None
                if retry_after:
                    try:
                        delay = min(float(retry_after), 5.0)
                    except ValueError:
                        pass
                if attempt + 1 < attempts:
                    sleeper(delay)
            if last is not None and not last.accepted:
                if last.reason == "stale_cached_response":
                    raise StaleCacheExhausted(last.path, last.index, last.calendar_version, self.confirmed_version)
                raise FetchFailed(last.path, last.status, last.index)
        return produced

    # -- interpretation --------------------------------------------------
    def declarations(self) -> tuple[Declaration, ...]:
        """Declarations from accepted pages only, with page context attached."""
        collected: dict[str, Declaration] = {}
        for page in self.pages:
            if not page.accepted:
                continue
            upper = {name.lower(): value for name, value in page.headers.items()} if page.headers else {}
            page_zone = upper.get("x-zone")
            for raw in page.items:
                record = dict(raw)
                record.setdefault("granularity", page.granularity)
                if page_zone:
                    record.setdefault("zone", page_zone)
                declaration = inspect_declaration(
                    record,
                    source=self.source,
                    capture_id=page.capture_id,
                    received_at=self.catalog.capture_received_at(self.archive, page.capture_id),
                    default_zone=self.default_zone,
                    default_boundary=self.default_boundary,
                    default_granularity=page.granularity,
                )
                collected.setdefault(declaration.declaration_id, declaration)
        return tuple(collected[key] for key in sorted(collected))

    def summary(self) -> dict[str, Any]:
        rejected = [page for page in self.pages if not page.accepted]
        return {
            "run_id": self.run_id,
            "source": self.source,
            "finished": self.finished,
            "next_path": self.next_path,
            "confirmed_calendar_version": self.confirmed_version,
            "accepted_pages": sum(1 for page in self.pages if page.accepted),
            "rejected_pages": [
                {"page": page.index, "path": page.path, "status": page.status,
                 "calendar_version": page.calendar_version, "reason": page.reason}
                for page in rejected
            ],
            "granularities": sorted(self.granularities),
            "pending_reason": self.pending_reason,
            "capture_ids": [page.capture_id for page in self.pages if page.accepted],
        }


def resume(client, archive, catalog, source: str, start_path: str = "/availability/1", **options) -> IngestRun:
    """Open (or reopen) a run; accepted pages are not fetched again."""
    return IngestRun(client, archive, catalog, source, start_path, **options)
