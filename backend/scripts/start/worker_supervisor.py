"""Keep both Celery workers alive, or exit so the container can restart."""

import os
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from contextlib import suppress

SHUTDOWN_TIMEOUT_SECONDS = 30.0
POLL_INTERVAL_SECONDS = 0.2


def stop_workers(workers: Sequence[subprocess.Popen[bytes]]) -> None:
    """Stop each worker process group and bound graceful shutdown time."""
    for worker in workers:
        with suppress(ProcessLookupError):
            os.killpg(worker.pid, signal.SIGTERM)
    deadline = time.monotonic() + SHUTDOWN_TIMEOUT_SECONDS
    for worker in workers:
        try:
            worker.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(worker.pid, signal.SIGKILL)
            worker.wait()


def supervise(commands: Sequence[Sequence[str]]) -> int:
    """Forward shutdown and fail the container if either worker exits early."""
    workers: list[subprocess.Popen[bytes]] = []
    shutdown_signal = 0

    def request_shutdown(signum: int, _frame: object) -> None:
        nonlocal shutdown_signal
        shutdown_signal = signum

    previous_handlers = {sig: signal.signal(sig, request_shutdown) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        for command in commands:
            if shutdown_signal:
                return 128 + shutdown_signal
            workers.append(subprocess.Popen(command, start_new_session=True))
        while not shutdown_signal:
            for worker in workers:
                return_code = worker.poll()
                if return_code is not None:
                    print(f"Celery worker {worker.pid} exited ({return_code}); stopping sibling worker", flush=True)
                    # Even an unexpected clean exit must trigger an on-failure restart.
                    return return_code if return_code > 0 else 1
            time.sleep(POLL_INTERVAL_SECONDS)
        return 128 + shutdown_signal
    finally:
        stop_workers(workers)
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)


def main() -> int:
    common = [sys.executable, "-m", "celery", "-A", "app.main:celery_app", "worker", "--loglevel=info"]
    return supervise(
        [
            [*common, "--pool=threads", "-Q", "default,sdk_sync,garmin_sync,webhook_sync", "-n", "io@%h"],
            [*common, "--pool=prefork", "--concurrency=2", "-Q", "xml_sync", "-n", "cpu@%h"],
        ]
    )


if __name__ == "__main__":
    sys.exit(main())
