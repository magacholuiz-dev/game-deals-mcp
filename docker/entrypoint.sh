#!/bin/sh
# Runs the scheduler and the dashboard side by side. Both share one SQLite file
# on the same volume, which is why they live in ONE container: SQLite's WAL mode
# needs shared memory, and that is not reliable across containers on a Mac.
# If either process dies the container exits, so the restart policy revives it.
set -eu
game-deals-scheduler --serve &
SCHED=$!
game-deals-web &
WEB=$!
trap 'kill $SCHED $WEB 2>/dev/null || true' TERM INT
# wait for whichever ends first
while kill -0 $SCHED 2>/dev/null && kill -0 $WEB 2>/dev/null; do sleep 2; done
kill $SCHED $WEB 2>/dev/null || true
exit 1
