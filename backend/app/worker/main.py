"""Neryva background worker entrypoint.

Processes the shared job queue (Redis-backed, in-memory fallback) with
the registered job handlers, and optionally enqueues scheduled jobs
(periodic cleanup, retention sweeps) defined in a JSON schedule file.

Usage:
    python -m backend.app.worker.main [--redis-url URL] [--schedule PATH]
                                      [--once] [--verbose]

``--once`` processes currently queued jobs and exits (cron / k8s Job style).
"""

import argparse
import asyncio
import json
import signal
import sys
import time

import structlog

from backend.app.infrastructure.queue.manager import init_queue
from backend.app.settings.env import get_settings

logger = structlog.get_logger(__name__)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    settings = get_settings()
    parser = argparse.ArgumentParser(prog="neryva-worker", description=__doc__)
    parser.add_argument("--redis-url", default=settings.REDIS_URL, help="Redis URL for the job queue")
    parser.add_argument("--schedule", metavar="PATH", default=None, help="JSON file of periodic jobs")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process currently queued jobs, then exit",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    return parser.parse_args(argv)


def load_schedule(path: str) -> list[dict]:
    """Load the schedule file: [{"name", "job_type", "interval_seconds", "payload"}]."""
    with open(path, "r", encoding="utf-8") as fh:
        schedule = json.load(fh)
    if not isinstance(schedule, list):
        raise ValueError("schedule file must contain a JSON array of job definitions")
    for entry in schedule:
        if not entry.get("name") or not entry.get("job_type"):
            raise ValueError("each schedule entry requires 'name' and 'job_type'")
        if not entry.get("interval_seconds") or entry["interval_seconds"] <= 0:
            raise ValueError(f"schedule entry {entry.get('name')!r} requires positive interval_seconds")
    return schedule


async def scheduler_loop(
    schedule: list[dict],
    interval: float = 5.0,
    exit_event: asyncio.Event | None = None,
) -> None:
    """Enqueue each scheduled job once per its interval window.

    Idempotency keys are derived from the window index, so restarts and
    overlapping runs never double-enqueue a scheduled job.
    """
    from backend.app.infrastructure.queue.manager import Job, get_queue_manager

    manager = get_queue_manager()
    last_run: dict[str, int] = {}
    while True:
        if exit_event and exit_event.is_set():
            break
        now = time.time()
        for entry in schedule:
            name = entry["name"]
            window = int(now // entry["interval_seconds"])
            if last_run.get(name) == window:
                continue
            last_run[name] = window
            job_type = entry["job_type"]
            idem_key = f"schedule:{name}:{window}"
            job = Job(type=job_type, payload=entry.get("payload", {}) or {})
            accepted = await manager.enqueue(job, idempotency_key=idem_key)
            logger.info(
                "scheduled_job_enqueued",
                name=name,
                job_type=job_type,
                window=window,
                accepted=accepted,
            )
        await asyncio.sleep(interval)


async def run(args: argparse.Namespace) -> int:
    import logging

    settings = get_settings()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    from backend.app.infrastructure.cache import init_cache
    from backend.app.infrastructure.db import init_database
    from backend.app.infrastructure.storage import init_storage

    manager = init_queue(redis_url=args.redis_url)
    cache = init_cache(redis_url=args.redis_url)
    storage = init_storage()
    db = init_database(settings.DATABASE_URL, echo=settings.DB_ECHO)
    await asyncio.gather(manager.initialize(), cache.initialize(), storage.initialize(), db.initialize())
    if settings.RUN_MIGRATIONS_ON_STARTUP:
        await db.run_migrations()

    from backend.app.worker.handlers import build_handlers

    build_handlers()

    exit_event = asyncio.Event()

    def _request_exit(signum, frame):
        logger.info("shutdown_requested", signal=signum)
        exit_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_exit, sig, None)
        except NotImplementedError:
            pass  # non-POSIX platforms

    tasks: list[asyncio.Task] = []
    if args.schedule:
        schedule = load_schedule(args.schedule)
        tasks.append(asyncio.create_task(scheduler_loop(schedule, exit_event=exit_event)))
        logger.info("scheduler_started", entries=len(schedule))

    try:
        if args.once:
            logger.info("worker_once_mode")
            await _drain_once(manager, exit_event)
        else:
            await manager.process_loop(exit_event=exit_event)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(
            manager.close(), cache.close(), storage.close(), db.close()
        )

    logger.info("worker_stopped")
    return 0


async def _drain_once(manager, exit_event: asyncio.Event) -> None:
    """Process queued jobs until the queue is drained or a shutdown arrives."""
    while not exit_event.is_set():
        job = await manager.dequeue(timeout=0.1)
        if job is None:
            break
        await manager.process_job(job)
    manager.stop_processing()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        logger.info("worker_interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
