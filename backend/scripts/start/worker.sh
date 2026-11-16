#!/bin/bash
set -e

scripts/start/install_extensions.sh

uv run python scripts/start/wait_for_migrations.py

exec uv run python scripts/start/worker_supervisor.py
