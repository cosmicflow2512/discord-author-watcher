#!/bin/sh
# Startet als root nur, um /data zu übernehmen, und wechselt dann auf PUID:PGID.
set -eu

PUID="${PUID:-99}"
PGID="${PGID:-100}"
DATA_DIR="${DATA_DIR:-/data}"

case "$PUID:$PGID" in
  *[!0-9:]*|:*|*:|*:*:*) echo "PUID/PGID müssen numerisch sein" >&2; exit 1 ;;
esac

# .NET und Python schreiben nichts nach /root.
export HOME=/tmp

if [ "$(id -u)" = "0" ] && [ "$PUID" != "0" ]; then
  mkdir -p "$DATA_DIR"
  # Nur Abweichungen korrigieren, nicht bei jedem Start alles neu schreiben.
  find "$DATA_DIR" \( ! -user "$PUID" -o ! -group "$PGID" \) -exec chown "$PUID:$PGID" {} +
  chmod 0700 "$DATA_DIR"
  exec su-exec "$PUID:$PGID" python3 /watcher/watcher.py "$@"
fi

exec python3 /watcher/watcher.py "$@"
