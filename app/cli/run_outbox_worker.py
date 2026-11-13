"""CLI runner for the outbox webhook worker daemon — ADR-013 / Week 13 Slice C.

Continuously polls un-receipted outbox rows for webhook fanout and dispatches
due deliveries with lease locking.

Usage:
    uv run python -m app.cli.run_outbox_worker [--once] [--poll-interval 1.0] [--max-batches N]
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
import uuid

from app import main as _app_main  # noqa: F401
from app.core.authorization import ActorContext, actor_context
from app.core.db import dispose_engine, get_session_factory
from app.platform.integration.worker import WorkerRunSummary, process_outbox_batch

logger = logging.getLogger("app.cli.run_outbox_worker")

INTEGRATION_WORKER_ACTOR = ActorContext(
    user_id=uuid.UUID("00000000-0000-0000-0000-000000000014"),
    permission_codes=frozenset({"platform.integration.manage"}),
    is_system_actor=True,
)


async def run_worker_loop(
    *,
    once: bool = False,
    poll_interval: float = 1.0,
    max_batches: int | None = None,
) -> WorkerRunSummary:
    """Run the worker loop until cancelled or max_batches/once condition met."""
    session_factory = get_session_factory()
    aggregate = WorkerRunSummary()
    batch_count = 0
    stop_event = asyncio.Event()

    def _handle_signal(*_args: object) -> None:
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _handle_signal)

    with actor_context(INTEGRATION_WORKER_ACTOR):
        while not stop_event.is_set():
            try:
                batch_summary = await process_outbox_batch(session_factory)
                aggregate.fanned_out_events += batch_summary.fanned_out_events
                aggregate.created_deliveries += batch_summary.created_deliveries
                aggregate.delivered += batch_summary.delivered
                aggregate.retried += batch_summary.retried
                aggregate.dead += batch_summary.dead
                batch_count += 1

                if once:
                    break

                if max_batches is not None and batch_count >= max_batches:
                    break

                # If no work was done, wait poll_interval; otherwise poll immediately
                work_done = (
                    batch_summary.fanned_out_events > 0
                    or batch_summary.delivered > 0
                    or batch_summary.retried > 0
                    or batch_summary.dead > 0
                )
                sleep_time = 0.05 if work_done else poll_interval

                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop_event.wait(), timeout=sleep_time)

            except Exception:
                logger.exception("Error in outbox worker iteration")
                if once:
                    raise
                await asyncio.sleep(poll_interval)

    return aggregate


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the platform.integration outbox delivery worker"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process a single batch pass and exit",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=1.0,
        help="Interval in seconds between polling passes (default: 1.0)",
    )
    parser.add_argument(
        "--max-batches",
        type=int,
        default=None,
        help="Maximum batch iterations before exiting",
    )
    return parser


async def _amain(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    try:
        summary = await run_worker_loop(
            once=args.once,
            poll_interval=args.poll_interval,
            max_batches=args.max_batches,
        )
        print(
            f"Worker completed: fanned_out={summary.fanned_out_events}, "
            f"created={summary.created_deliveries}, delivered={summary.delivered}, "
            f"retried={summary.retried}, dead={summary.dead}"
        )
        return 0
    finally:
        await dispose_engine()


def main() -> None:
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
