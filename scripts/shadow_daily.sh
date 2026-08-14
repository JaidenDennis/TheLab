#!/bin/zsh
# TFR shadow daily launcher -- fired by launchd at 09:00 ET on weekdays.
# Guards against double-starts, keeps the Mac awake for the session, and
# logs everything. The runner stops itself at 16:35 ET, persists the flow
# record, and refreshes the next session's calibration.
cd "$(dirname "$0")/.." || exit 1
mkdir -p var/shadow
# GFM Track B: tag today's gamma regime before the session (the OI batch
# publishes 06:30 ET; this runs at 09:00). Idempotent -- already-tagged
# dates are skipped -- and a failure must not block the shadow.
uv run python scripts/snapshot_gamma.py >> var/shadow/gamma.log 2>&1 ||   echo "$(date): gamma snapshot FAILED (see gamma.log)" >> var/shadow/launcher.log
# Match the python invocation specifically: a bare `pgrep -f` on the script
# path also matches an editor or pager holding the file open, and a false
# positive here silently skips the whole day's session.
if pgrep -f "python scripts/run_shadow.py" >/dev/null; then
  echo "$(date): shadow already running, not starting a second" >> var/shadow/launcher.log
  exit 0
fi
exec caffeinate -is uv run python scripts/run_shadow.py \
  >> "var/shadow/session-$(date +%Y-%m-%d).log" 2>&1
