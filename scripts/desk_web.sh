#!/bin/zsh
# desk-web launcher — run by launchd (com.thelab.desk-web, KeepAlive).
# Serves the production build on :3000; rebuild with `npm run build` in
# desk/web then `launchctl kickstart -k gui/$UID/com.thelab.desk-web`.

REPO=/Users/huncho/TheLab
mkdir -p $REPO/var/desk

# A stale/missing build would crash-loop under KeepAlive; wait instead.
while [[ ! -f $REPO/desk/web/.next/BUILD_ID ]]; do
  echo "$(date '+%F %T') desk_web.sh: no production build in desk/web/.next — waiting 60s"
  sleep 60
done

cd $REPO/desk/web || exit 1
exec /Users/huncho/.local/bin/npm start
