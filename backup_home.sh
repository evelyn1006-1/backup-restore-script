#!/bin/bash
# backup_home.sh - Create a local full or differential backup of the home directory.

set -euo pipefail

LOGFILE="/home/evelyn/backups/backup.log"
BACKUP_DIR="/home/evelyn/backups"
ARTIFACT_DIR="${BACKUP_DIR}/artifacts"
STATE_DIR="${BACKUP_DIR}/state"
RESULT_FILE="${STATE_DIR}/last_result.json"
HELPER="${BACKUP_DIR}/create_backup.py"
RETENTION_DAYS=7
PIGZ_PROCESSES=3
MODE="${1:-full}"

mkdir -p "$BACKUP_DIR" "$ARTIFACT_DIR" "$STATE_DIR" "$(dirname "$LOGFILE")"

if ! command -v pigz >/dev/null 2>&1; then
    echo "pigz is required for parallel gzip compression" >&2
    exit 1
fi

if [[ ! -x "$HELPER" ]]; then
    echo "Backup helper is missing or not executable: $HELPER" >&2
    exit 1
fi

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOGFILE"
}

log "=== Backup started ==="
log "Requested mode: ${MODE}"

RESULT_JSON="$(python3 "$HELPER" --mode "$MODE" --retention-days "$RETENTION_DAYS" --pigz-processes "$PIGZ_PROCESSES")"
printf '%s\n' "$RESULT_JSON" > "$RESULT_FILE"

ARCHIVE_PATH="$(python3 - "$RESULT_FILE" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text())
print(payload["archive_path"])
PY
)"

MANIFEST_PATH="$(python3 - "$RESULT_FILE" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text())
print(payload["manifest_path"])
PY
)"

MODE_USED="$(python3 - "$RESULT_FILE" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text())
print(payload["mode_used"])
PY
)"

DELETED_PATHS_PATH="$(python3 - "$RESULT_FILE" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text())
print(payload.get("deleted_paths_path") or "")
PY
)"

ARCHIVE_SIZE="$(du -h "$ARCHIVE_PATH" | cut -f1)"
log "Created ${MODE_USED} archive: ${ARCHIVE_PATH}"
log "Manifest: ${MANIFEST_PATH}"
if [[ -n "$DELETED_PATHS_PATH" ]]; then
    log "Deleted paths manifest: ${DELETED_PATHS_PATH}"
fi
log "Archive created: ${ARCHIVE_SIZE}"

log "Cleaning up local backups older than ${RETENTION_DAYS} days"
find "$ARTIFACT_DIR" -maxdepth 1 \
    \( -name "home_backup_*.tar.gz" -o -name "home_backup_*.manifest.json" -o -name "home_backup_*.deleted.txt" \) \
    -mtime +${RETENTION_DAYS} -delete 2>&1 | tee -a "$LOGFILE"
find "$STATE_DIR" \( -name "*.json" -o -name "*.txt" \) -mtime +${RETENTION_DAYS} -delete 2>&1 | tee -a "$LOGFILE"

log "=== Backup finished ==="
echo "" >> "$LOGFILE"
