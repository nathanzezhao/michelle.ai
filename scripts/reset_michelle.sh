#!/usr/bin/env bash
# Wipe personal Michelle state on this machine so the next person starts clean.
# Does NOT delete sample docs/ files (those are shared demo content).

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "Stopping tip: quit the backend (Ctrl+C) before resetting if it is running."
echo

rm -f michelle.db michelle.db-wal michelle.db-shm
echo "Deleted michelle.db (+ wal/shm if present)."

echo
echo "Also clear Electron localStorage so the old user/conversation IDs go away:"
echo "  1. Open Michelle (Electron)"
echo "  2. DevTools console (View → Toggle Developer Tools, or Cmd+Option+I)"
echo "  3. Run:"
echo "       localStorage.removeItem('michelle_user_id')"
echo "       localStorage.removeItem('michelle_conversation_id')"
echo "       location.reload()"
echo
echo "Then restart the backend with: b"
echo "Michelle will ask for a name again and re-index docs/ on startup."
