#!/bin/bash
set -e

scripts/start/install_extensions.sh

exec uv run python scripts/start/worker_supervisor.py
