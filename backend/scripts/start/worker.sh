#!/bin/bash
set -e
trap 'exit 143' TERM INT

scripts/start/install_extensions.sh

uv run python scripts/start/wait_for_migrations.py

# OWExtension.celery_queues of the active extensions, for the extension worker the supervisor starts.
if [[ ",${WORKER_ROLES:-io,cpu,ext}," == *,ext,* ]]; then
    EXTENSION_QUEUES=$(uv run python scripts/start/extension_queues.py | tee /dev/stderr | sed -n 's/^EXTENSION_QUEUES=//p')
    export EXTENSION_QUEUES
fi

exec uv run python scripts/start/worker_supervisor.py
