import argparse
import json
import tempfile
from pathlib import Path

from .archive import Archive
from .catalog import Catalog
from .client import Client, collect
from .decoding import records
from .origin import serve


def capture(base_url, archive_path, source, path):
    archive = Archive(archive_path)
    try:
        capture_id, response = collect(Client(base_url), archive, source, path)
        result = {"capture_id": capture_id, "status": response.status, "bytes": len(response.body), "url": response.url}
        if response.status == 200:
            result["source_records"] = records(response.body, response.headers.get("content-type", ""))
        result["archived_observations"] = len(archive.captures())
        return result
    finally:
        archive.close()


def sync(base_url, archive_path, catalog_path, source, start_path, attempts, publish):
    """Collect every page, then publish one interpretation and query it."""
    from .ingest import FetchFailed, IngestRun, StaleCacheExhausted

    archive = Archive(archive_path)
    catalog = Catalog(catalog_path)
    try:
        run = IngestRun(Client(base_url), archive, catalog, source, start_path)
        try:
            run.drain(attempts=attempts)
        except (FetchFailed, StaleCacheExhausted) as error:
            result = {
                "operation": "availability_sync",
                "status": "incomplete",
                "error": str(error),
                "run": run.summary(),
                "resumable": True,
            }
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return result
        result = {
            "operation": "availability_sync",
            "status": "complete",
            "run": run.summary(),
            "capture_ids": run.summary()["capture_ids"],
        }
        if publish:
            published = catalog.publish(run.declarations(), calendar_version=run.confirmed_version)
            result["published"] = {
                "version": published["version"],
                "calendar_version": published["calendar_version"],
                "digest": published["digest"],
                "summary": published["summary"],
            }
            view = catalog.query(published["version"])
            result["query"] = {
                "version": view["version"],
                "calendar_version": view["calendar_version"],
                "digest": view["digest"],
                "windows": view["windows"],
                "reviews": view["reviews"],
            }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result
    finally:
        catalog.close()
        archive.close()


def main():
    parser = argparse.ArgumentParser(description="来源响应接收与归档")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("demo")
    collector = subcommands.add_parser("capture")
    collector.add_argument("--base-url", required=True)
    collector.add_argument("--archive", type=Path, required=True)
    collector.add_argument("--source", required=True)
    collector.add_argument("--path", default="/feed")
    syncer = subcommands.add_parser("sync", help="分页抓取、发布并查询同一次解释")
    syncer.add_argument("--base-url", required=True)
    syncer.add_argument("--archive", type=Path, required=True)
    syncer.add_argument("--catalog", type=Path, required=True)
    syncer.add_argument("--source", required=True)
    syncer.add_argument("--path", default="/availability/1")
    syncer.add_argument("--attempts", type=int, default=3)
    syncer.add_argument("--no-publish", action="store_true")
    booker = subcommands.add_parser("book", help="录入已确认预约（仅用于复核标记）")
    booker.add_argument("--catalog", type=Path, required=True)
    booker.add_argument("--booking-id", required=True)
    booker.add_argument("--supplier", required=True)
    booker.add_argument("--equipment", required=True)
    booker.add_argument("--start", required=True)
    booker.add_argument("--end", required=True)
    booker.add_argument("--reference")
    querier = subcommands.add_parser("query", help="读取同一版本的窗口与复核清单")
    querier.add_argument("--catalog", type=Path, required=True)
    querier.add_argument("--version", type=int)
    querier.add_argument("--supplier")
    server = subcommands.add_parser("serve")
    server.add_argument("--scenario", type=Path)
    args = parser.parse_args()
    if args.command == "demo":
        with tempfile.TemporaryDirectory() as temporary, serve() as origin:
            result = capture(origin.base_url, Path(temporary) / "archive.sqlite3", "local-source", "/feed")
            print(json.dumps({"operation": "raw_capture", **result}, ensure_ascii=False, indent=2))
    elif args.command == "capture":
        print(json.dumps(capture(args.base_url, args.archive, args.source, args.path), ensure_ascii=False, indent=2))
    elif args.command == "sync":
        sync(args.base_url, args.archive, args.catalog, args.source, args.path, args.attempts, not args.no_publish)
    elif args.command == "book":
        catalog = Catalog(args.catalog)
        try:
            catalog.add_booking(args.booking_id, args.supplier, args.equipment, args.start, args.end, reference=args.reference)
            print(json.dumps({"operation": "booking_recorded", "booking_id": args.booking_id, "action": "review_only_do_not_move_booking"}, ensure_ascii=False, indent=2))
        finally:
            catalog.close()
    elif args.command == "query":
        catalog = Catalog(args.catalog)
        try:
            try:
                result = catalog.query(args.version, supplier=args.supplier)
            except KeyError:
                result = {"status": "empty", "error": "no published version"}
            print(json.dumps(result, ensure_ascii=False, indent=2))
        finally:
            catalog.close()
    else:
        from .origin import load_scenario
        with serve(load_scenario(args.scenario)) as origin:
            print(json.dumps({"base_url": origin.base_url}, ensure_ascii=False), flush=True)
            try:
                import threading
                threading.Event().wait()
            except KeyboardInterrupt:
                pass


if __name__ == "__main__":
    main()
