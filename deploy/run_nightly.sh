#!/bin/bash
# Wrapper the launchd job runs each night: activate the venv and run the pipeline.
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"
mkdir -p data
echo "=== run $(date) ==="
.venv/bin/tshirt-nightly
.venv/bin/tshirt-recommend
