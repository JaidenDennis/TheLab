#!/bin/zsh
# desk-brain launcher — run by launchd (com.thelab.desk-brain, KeepAlive).
# The brain's own supervisor restarts internal tasks; launchd restarts the
# process. This wrapper only sets up env and refuses to spin hot when the
# required secrets are missing (KeepAlive would otherwise crash-loop).

REPO=/Users/huncho/TheLab
BRAIN=$REPO/desk/brain
LOGDIR=$REPO/var/desk
mkdir -p "$LOGDIR"

# Databento fallback: brain config reads NQ_DATABENTO_API_KEY from the process env.
if [[ -f $REPO/.env ]]; then
  export NQ_DATABENTO_API_KEY=$(grep '^NQ_DATABENTO_API_KEY=' $REPO/.env | cut -d= -f2-)
fi

# Hard requirements (make_db crashes without them). Wait instead of crash-looping.
while ! grep -q '^SUPABASE_SERVICE_ROLE_KEY=..' $BRAIN/.env 2>/dev/null; do
  echo "$(date '+%F %T') desk_brain.sh: SUPABASE_SERVICE_ROLE_KEY not set in desk/brain/.env — waiting 60s"
  sleep 60
done

cd $BRAIN || exit 1
exec /opt/homebrew/bin/uv run python -m desk_brain.main
