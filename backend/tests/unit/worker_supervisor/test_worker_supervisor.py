"""Real subprocess checks; no broker, database, or Celery workers are started."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

SUPERVISOR = Path(__file__).resolve().parents[3] / "scripts/start/worker_supervisor.py"


def run_supervisor(commands: list[list[str]]) -> subprocess.Popen[bytes]:
    bootstrap = (
        "import importlib.util,sys; "
        f"spec=importlib.util.spec_from_file_location('worker_supervisor',{str(SUPERVISOR)!r}); "
        "module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); "
        "module.SHUTDOWN_TIMEOUT_SECONDS=1; "
        f"sys.exit(module.supervise({commands!r}))"
    )
    return subprocess.Popen([sys.executable, "-c", bootstrap], stdout=subprocess.DEVNULL)


def waiting_worker(pid_file: Path, stopped_file: Path) -> list[str]:
    return [
        sys.executable,
        "-c",
        "import os,signal,time; from pathlib import Path; "
        f"signal.signal(signal.SIGTERM,lambda *args: (Path({str(stopped_file)!r}).write_text('stopped'),exit(0))); "
        f"Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(60)",
    ]


def wait_for_file(path: Path, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 5
    while not path.exists() and time.monotonic() < deadline:
        assert process.poll() is None
        time.sleep(0.02)
    assert path.exists()


@pytest.mark.parametrize("failed_worker", [0, 1])
@pytest.mark.parametrize("exit_code", [0, 7])
def test_either_worker_exit_stops_sibling(tmp_path: Path, failed_worker: int, exit_code: int) -> None:
    pid_file, stopped_file = tmp_path / "pid", tmp_path / "stopped"
    commands = [waiting_worker(pid_file, stopped_file)]
    commands.insert(
        failed_worker, [sys.executable, "-c", f"import time; time.sleep(.5); raise SystemExit({exit_code})"]
    )
    process = run_supervisor(commands)
    try:
        wait_for_file(pid_file, process)
        assert process.wait(timeout=5) == (exit_code or 1)
        assert stopped_file.exists()
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


@pytest.mark.parametrize("shutdown_signal", [signal.SIGTERM, signal.SIGINT])
def test_container_termination_stops_both_workers(tmp_path: Path, shutdown_signal: int) -> None:
    pid_files = [tmp_path / f"pid-{i}" for i in range(2)]
    stopped_files = [tmp_path / f"stopped-{i}" for i in range(2)]
    process = run_supervisor(
        [waiting_worker(pid, stopped) for pid, stopped in zip(pid_files, stopped_files, strict=True)]
    )
    try:
        for pid_file in pid_files:
            wait_for_file(pid_file, process)
        process.send_signal(shutdown_signal)
        assert process.wait(timeout=5) == 128 + shutdown_signal
        assert all(path.exists() for path in stopped_files)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_stubborn_worker_is_killed_after_grace_period(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    command = [
        sys.executable,
        "-c",
        "import os,signal,time; from pathlib import Path; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        f"Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(60)",
    ]
    process = run_supervisor([command, [sys.executable, "-c", "import time;time.sleep(.5);raise SystemExit(7)"]])
    try:
        wait_for_file(pid_file, process)
        assert process.wait(timeout=5) == 7
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
