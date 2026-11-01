"""Judge Worker daemon for CPJudge.

Continuously polls the database for queued submissions, evaluates them
using the engine sandbox in isolation, writes Result and Score records,
and recovers any orphaned or stuck submissions on startup.

Can be run:
- Continuously as a daemon: python -m cpjudge.worker
- Single-batch mode (processes all queued items and exits): python -m cpjudge.worker --once
"""
import argparse
import logging
import os
import signal
import sys
import time
from typing import Optional

from flask import Flask

from cpjudge.app import create_app
from cpjudge.engine.judge import judge
from cpjudge.models import Submission, db

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [Worker] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("cpjudge.worker")


class JudgeWorker:
    """Judge worker process handling submission queue polling, judging, and recovery."""

    def __init__(self, app: Optional[Flask] = None, poll_interval: float = 1.0):
        self.app = app or create_app()
        self.poll_interval = poll_interval
        self._stop_requested = False

    def recover_stuck_submissions(self) -> int:
        """Recovers any submissions left in 'judging' state (e.g., from prior crash or power loss).

        Resets them to 'queued' so they are evaluated automatically.
        """
        with self.app.app_context():
            stuck_subs = Submission.query.filter_by(status="judging").all()
            if not stuck_subs:
                return 0

            count = len(stuck_subs)
            for sub in stuck_subs:
                sub.status = "queued"
                logger.warning(
                    f"Recovering stuck submission #{sub.id} (user {sub.user_id}, problem {sub.problem_id}) -> 'queued'"
                )
            db.session.commit()
            logger.info(f"Successfully recovered {count} stuck submission(s) to 'queued' status.")
            return count

    def claim_next_submission(self) -> Optional[int]:
        """Atomically finds and claims the oldest queued submission.

        Returns:
            The claimed submission ID, or None if the queue is empty.
        """
        with self.app.app_context():
            # Oldest first FIFO queue
            sub = (
                Submission.query.filter_by(status="queued")
                .order_by(Submission.created_at.asc())
                .first()
            )
            if not sub:
                return None

            sub_id = sub.id
            sub.status = "judging"
            db.session.commit()
            return sub_id

    def process_submission(self, submission_id: int) -> bool:
        """Evaluates a single claimed submission with full error recovery."""
        with self.app.app_context():
            logger.info(f"Starting evaluation of submission #{submission_id}...")
            start_time = time.time()
            try:
                result = judge(submission_id)
                elapsed = time.time() - start_time
                logger.info(
                    f"Finished submission #{submission_id} in {elapsed:.2f}s -> "
                    f"Verdict: {result.verdict.value}, Score: {result.total_score}/{result.max_score}"
                )
                return True
            except Exception as e:
                logger.exception(f"Unexpected error judging submission #{submission_id}: {e}")
                try:
                    db.session.rollback()
                    sub = db.session.get(Submission, submission_id)
                    if sub:
                        sub.status = "error"
                        sub.compile_error = f"Judge Worker Runtime Failure: {str(e)}"
                        db.session.commit()
                except Exception as rollback_err:
                    logger.error(f"Failed to record error status for submission #{submission_id}: {rollback_err}")
                return False

    def process_one(self) -> bool:
        """Claims and processes at most one submission.

        Returns:
            True if a submission was claimed and processed, False if queue was empty.
        """
        sub_id = self.claim_next_submission()
        if sub_id is None:
            return False

        self.process_submission(sub_id)
        return True

    def run_once(self) -> int:
        """Processes all currently queued submissions until empty, then returns the count."""
        processed_count = 0
        while not self._stop_requested:
            had_work = self.process_one()
            if not had_work:
                break
            processed_count += 1
        return processed_count

    def run(self) -> None:
        """Main continuous polling loop. Handles graceful shutdown on SIGINT/SIGTERM."""
        logger.info(f"CPJudge Worker starting (poll interval: {self.poll_interval}s)...")

        # Register signal handlers for graceful stop
        def _handle_signal(signum, frame):
            logger.info(f"Received signal {signum}, initiating graceful shutdown...")
            self._stop_requested = True

        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)

        # Recover stuck jobs from previous runs
        self.recover_stuck_submissions()

        logger.info("Worker is ready and listening for queued submissions.")
        while not self._stop_requested:
            had_work = self.process_one()
            if had_work:
                # If there was work, immediately check for next without waiting
                continue

            # Sleep during idle time in small slices so shutdown requests are responsive
            sleep_step = 0.2
            elapsed_sleep = 0.0
            while elapsed_sleep < self.poll_interval and not self._stop_requested:
                time.sleep(sleep_step)
                elapsed_sleep += sleep_step

        logger.info("Worker has shut down cleanly.")


def main():
    parser = argparse.ArgumentParser(description="CPJudge Evaluation Worker Daemon")
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=1.0,
        help="Polling interval in seconds when idle (default: 1.0)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Drain all queued submissions currently in the database and exit",
    )
    args = parser.parse_args()

    worker = JudgeWorker(poll_interval=args.poll_interval)
    if args.once:
        worker.recover_stuck_submissions()
        count = worker.run_once()
        logger.info(f"Drained {count} queued submission(s). Exiting.")
    else:
        worker.run()


if __name__ == "__main__":
    main()
