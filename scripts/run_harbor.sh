#!/usr/bin/env bash
# The scripted part of the judging protocol on the given scenarios:
#   oracle (should score near full marks), early-stop baseline, do-nothing (should score near zero).
# The assistant under test (Gemini) is added here once it exists.
#
#   SCENARIOS="synthetic-stirfry" scripts/run_harbor.sh
#
# Needs: docker and harbor (pip install harbor).
set -euo pipefail
cd "$(dirname "$0")/.."
SCENARIOS=${SCENARIOS:-"synthetic-stirfry"}
JOBS=${JOBS:-jobs}
STAMP=$(date +%Y%m%d-%H%M%S)
PY=${PYTHON:-python3}

$PY -m scripts.build_tasks --scenarios $SCENARIOS --out tasks/oracle
$PY -m scripts.build_tasks --scenarios $SCENARIOS --out tasks/early-stop --oracle-policy early_stop

for s in $SCENARIOS; do
  harbor run -p tasks/oracle/$s     -a oracle -o "$JOBS" --job-name "$STAMP-$s-oracle"
  harbor run -p tasks/early-stop/$s -a oracle -o "$JOBS" --job-name "$STAMP-$s-early-stop"
  harbor run -p tasks/oracle/$s     -a nop    -o "$JOBS" --job-name "$STAMP-$s-do-nothing"
done
