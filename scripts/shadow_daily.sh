#!/bin/zsh
# TFR shadow daily launcher -- fired by launchd at 09:00 ET on weekdays.
# Guards against double-starts, keeps the Mac awake for the session, and
# logs everything. The runner stops itself at 16:35 ET, persists the flow
# record, and refreshes the next session's calibration.
#
# The runner is supervised: any exit before the 16:35 close is treated as
# a crash and relaunched after a 30s pause (run_shadow.py preloads the
# same day's flow record, so a restart resumes rather than double-counts).
# The clean end-of-session exit lands at ~16:34:58, which fails the
# before-16:34 re-entry check, so it is never restarted.
cd "$(dirname "$0")/.." || exit 1
mkdir -p var/shadow
# Match the python invocation specifically: a bare `pgrep -f` on the script
# path also matches an editor or pager holding the file open, and a false
# positive here silently skips the whole day's session.
if pgrep -f "python scripts/run_shadow.py" >/dev/null; then
  echo "$(date): shadow already running, not starting a second" >> var/shadow/launcher.log
  exit 0
fi
attempt=0
while (( 10#$(date +%H%M) < 1634 )); do
  (( attempt++ ))
  if (( attempt > 1 )); then
    echo "$(date): runner exited before close, restart #$((attempt-1)) in 30s" >> var/shadow/launcher.log
    if (( attempt == 2 )); then
      osascript -e 'display notification "runner died mid-session, supervising restarts until close" with title "TFR shadow"' 2>/dev/null
    fi
    sleep 30
  fi
  caffeinate -is uv run python scripts/run_shadow.py \
    >> "var/shadow/session-$(date +%Y-%m-%d).log" 2>&1
done
echo "$(date): launcher done after $attempt run(s)" >> var/shadow/launcher.log
