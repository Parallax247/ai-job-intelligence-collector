from __future__ import annotations

import argparse
import os
import signal
import sys
from datetime import datetime
from pathlib import Path

from exporters.excel_exporter import ExcelExporter
from scrapers.boss import BossPageUnavailableError, BossScraper
from scrapers.liepin import LiepinPageUnavailableError, LiepinScraper
from utils.logger import setup_logger
from utils.storage import JsonlStore
from utils.config import load_config
from utils.runtime_options import apply_runtime_overrides, save_last_run_config
from utils.run_paths import (
    create_run_directory,
    discard_running_directory,
    finalize_run_directory,
    flatten_task_artifacts,
    internalize_run_directory,
    list_run_directories,
    rewrite_artifact_paths,
    run_file_path,
    update_run_config,
    write_run_config,
)


ROOT = Path(__file__).resolve().parent


def create_scraper(platform: str, *, root: Path, config: dict, store: JsonlStore,
                   logger, debug: bool, data_dir: Path,
                   historical_urls: set[str]):
    adapters = {"boss": BossScraper, "liepin": LiepinScraper}
    try:
        adapter = adapters[platform]
    except KeyError as exc:
        raise ValueError(f"Platform not implemented: {platform}") from exc
    return adapter(
        root, config, store, logger, debug=debug, data_dir=data_dir,
        historical_urls=historical_urls,
    )


def load_historical_urls(output_root: Path) -> set[str]:
    """Read URLs from previous runs when save-new-jobs-only mode is enabled."""
    urls: set[str] = set()
    for previous_run in list_run_directories(output_root):
        store = JsonlStore(run_file_path(previous_run, "jobs.jsonl"))
        previous_urls, _ = store.load_keys()
        urls.update(previous_urls)
    return urls


def _raise_keyboard_interrupt(_signum, _frame) -> None:
    """Handle UI SIGTERM through normal finalization and preserve partial results."""
    raise KeyboardInterrupt


def _close_file_log_handlers(logger) -> None:
    for handler in list(logger.handlers):
        if not hasattr(handler, "baseFilename"):
            continue
        try:
            handler.flush()
            handler.close()
        finally:
            logger.removeHandler(handler)


def should_discard_empty_page_failure(processed_job_count: int, boss_page_failure: bool,
                                      page_recovery_waiting: bool) -> bool:
    return processed_job_count == 0 and (boss_page_failure or page_recovery_waiting)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AI Job Intelligence Collector")
    parser.add_argument("--config", default="config.json", help="Configuration file path")
    parser.add_argument("--platform", choices=["boss", "liepin"], help="Override the configured platform")
    keyword_group = parser.add_mutually_exclusive_group()
    keyword_group.add_argument("--keyword", help="Run one keyword and override search_keywords")
    keyword_group.add_argument("--keywords", help="Multiple keywords separated by commas, semicolons, or line breaks")
    parser.add_argument("--limit", type=int, help="Override jobs_per_keyword")
    parser.add_argument("--city", help="Override city; pass an empty string to disable city filtering")
    parser.add_argument("--wait-min", type=float, help="Override the minimum per-job wait in seconds")
    parser.add_argument("--wait-max", type=float, help="Override the maximum per-job wait in seconds")
    parser.add_argument("--debug", action="store_true")
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = Path.cwd() / config_path
    try:
        config = load_config(config_path)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.platform:
        config["platform"] = args.platform
    try:
        config = apply_runtime_overrides(
            config, keywords=args.keywords, keyword=args.keyword, limit=args.limit,
            city=args.city, wait_min=args.wait_min, wait_max=args.wait_max,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    save_last_run_config(ROOT / "data" / "last_run_config.json", config)
    started = datetime.now().astimezone()
    started_at = started.isoformat(timespec="seconds")
    try:
        desktop_output = os.environ.get("JOB_SCANNER_OUTPUT_ROOT", "").strip()
        output_root = Path(desktop_output).expanduser() if desktop_output else ROOT / "data"
        run_dir = create_run_directory(
            output_root, direct=bool(desktop_output), update_latest=True
        )
        run_id = run_dir.name.removeprefix(".running_")
        write_run_config(run_dir / "run_config.json", {
            **config,
            "run_id": run_id,
            "started_at": started_at,
            "completed_at": "",
            "final_run_dir": "",
            "status": "running",
        })
    except (OSError, RuntimeError) as exc:
        print(f"Failed to create the run directory: {exc}", file=sys.stderr)
        return 2
    mirror_log = Path(os.environ.get("JOB_SCANNER_APP_LOG", ROOT / "logs" / "app.log")).expanduser()
    logger = setup_logger(run_dir / "app.log", args.debug, mirror_log)
    logger.info("Run directory: %s", run_dir)
    logger.info("Keywords in this run: %d", len(config["search_keywords"]))
    for index, keyword in enumerate(config["search_keywords"], 1):
        logger.info("%d. %s", index, keyword)
    logger.info("Target jobs per keyword: %d", config["jobs_per_keyword"])
    store = JsonlStore(run_dir / "jobs.jsonl", logger)
    store.path.touch(exist_ok=True)
    run_log = {"started_at": started_at, "platform": config["platform"],
               "keywords": ", ".join(config["search_keywords"]),
               "jobs_per_keyword": config["jobs_per_keyword"],
               "requested": len(config["search_keywords"]) * config["jobs_per_keyword"],
               "captured": 0, "skipped": 0, "status": "running", "message": ""}

    historical_urls = (
        load_historical_urls(output_root)
        if config.get("save_mode") == "new_only" else set()
    )
    logger.info(
        "Save mode: %s%s",
        "snapshot" if config.get("save_mode") == "snapshot" else "new jobs only",
        "" if not historical_urls else f" ({len(historical_urls)} historical jobs)",
    )
    scraper = create_scraper(
        config["platform"], root=ROOT, config=config, store=store, logger=logger,
        debug=args.debug, data_dir=run_dir, historical_urls=historical_urls,
    )
    boss_page_failure = False
    previous_sigterm_handler = None
    if hasattr(signal, "SIGTERM"):
        previous_sigterm_handler = signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
    try:
        stats = scraper.run(config["search_keywords"], config["jobs_per_keyword"])
        run_log.update(stats)
        run_log["status"] = str(stats.get("status", "failed"))
        return_code = 0 if run_log["status"] == "completed" else (
            4 if run_log["status"] == "partial_failed" else 1
        )
    except KeyboardInterrupt:
        logger.warning("Interrupt received; saving Excel output…")
        if scraper.task_status == "paused_browser_lost":
            run_log.update({
                "status": "paused_browser_lost",
                "message": "Browser connection lost; stopped and retained current results",
            })
            return_code = 5
        else:
            run_log.update({"status": "stopped", "message": "Stopped by user"})
            return_code = 130
    except (BossPageUnavailableError, LiepinPageUnavailableError) as exc:
        boss_page_failure = True
        logger.error("%s page unavailable; run not started: %s", config["platform"], exc)
        run_log.update({"status": "page_unavailable", "message": str(exc)})
        return_code = 3
    except Exception as exc:
        logger.exception("Collection terminated with an error: %s", exc)
        run_log.update({"status": "failed", "message": str(exc)})
        return_code = 1
    finally:
        completed_at = datetime.now().astimezone()
        completed_at_text = completed_at.isoformat(timespec="seconds")
        run_log["finished_at"] = completed_at_text
        run_log["captured"] = scraper.captured_count
        run_log["skipped"] = scraper.skipped_count
        run_log["infrastructure_failed_count"] = scraper.infrastructure_failed_count
        run_log["browser_disconnect_count"] = scraper.browser_disconnect_count
        run_log["pending_count"] = len(scraper.pending_urls)
        completed = run_log["status"] == "completed" and return_code == 0
        old_run_dir = run_dir
        records = store.read_all()
        invalid_records = list(scraper.invalid_records)
        keyword_summaries = list(scraper.keyword_summaries)
        scraper.close()
        discard_empty_page_failure = should_discard_empty_page_failure(
            scraper.processed_job_count, boss_page_failure, scraper.page_recovery_waiting
        )
        if discard_empty_page_failure:
            logger.warning("BOSS page missing before any job was processed; removing the empty .running directory")
            _close_file_log_handlers(logger)
            try:
                discard_running_directory(old_run_dir, output_root)
            except Exception:
                logger.exception("Failed to remove the empty .running directory")
        else:
            try:
                flatten_task_artifacts(old_run_dir, [*records, *invalid_records])
                run_dir = finalize_run_directory(
                    old_run_dir,
                    output_root,
                    config["search_keywords"],
                    completed_at,
                    completed=completed,
                    status=run_log["status"],
                    processed_count=scraper.processed_job_count,
                )
                records = rewrite_artifact_paths(records, old_run_dir, run_dir, ROOT)
                invalid_records = rewrite_artifact_paths(
                    invalid_records, old_run_dir, run_dir, ROOT
                )
                internal_dir = internalize_run_directory(run_dir)
                store.path = internal_dir / "jobs.jsonl"
                store.write_all(records)
                invalid_path = internal_dir / "invalid_records.jsonl"
                JsonlStore(invalid_path, logger).write_all(invalid_records)
                run_log["final_run_dir"] = str(run_dir)
                update_run_config(
                    internal_dir / "run_config.json",
                    completed_at=completed_at_text,
                    final_run_dir=str(run_dir),
                    status=run_log["status"],
                )
                exporter = ExcelExporter(run_dir / "jobs.xlsx", logger, project_root=run_dir)
                exporter.export(
                    records, keyword_summaries, run_log, invalid_records
                )
                logger.info("Final run directory: %s", run_dir)
            except Exception:
                logger.exception("Run finalization or Excel export failed; JSONL data remains safely stored")
        if previous_sigterm_handler is not None:
            signal.signal(signal.SIGTERM, previous_sigterm_handler)
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
