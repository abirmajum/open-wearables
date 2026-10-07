#!/bin/bash
set -e

exec uv run python scripts/start/worker_supervisor.py
