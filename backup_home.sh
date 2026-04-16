#!/bin/bash
# backup_home.sh - Create a compressed local backup of the home directory
# Runs daily at 4:10 AM ET (3:10 AM CDT on this machine)

set -euo pipefail

LOGFILE="/home/evelyn/backup.log"
BACKUP_DIR="/home/evelyn/backups"
HOME_DIR="/home/evelyn"
RETENTION_DAYS=7  # keep backups for a week before cleaning up
PIGZ_PROCESSES=3

mkdir -p "$BACKUP_DIR" "$(dirname "$LOGFILE")"

if ! command -v pigz >/dev/null 2>&1; then
    echo "pigz is required for parallel gzip compression" >&2
    exit 1
fi

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOGFILE"
}

log "=== Backup started ==="

# Create a timestamped tar.gz archive.
TIMESTAMP=$(date '+%Y%m%d_%H%M%S')
ARCHIVE_FILE="$BACKUP_DIR/home_backup_${TIMESTAMP}.tar.gz"

log "Creating tar.gz archive: $ARCHIVE_FILE"
if ! tar -C "$HOME_DIR" \
    --exclude="./.cache" \
    --exclude="./.local" \
    --exclude="./.local/share/Trash" \
    --exclude="./backups" \
    --exclude="./logs" \
    --exclude="./.config/rclone" \
    --exclude="./.gunicorn" \
    --exclude="./bootstrap" \
    --exclude="./.rustup" \
    --exclude="./.julia" \
    -cf - . \
    2> >(tee -a "$LOGFILE" >&2) \
    | pigz -6 -p "$PIGZ_PROCESSES" \
    > "$ARCHIVE_FILE" \
    2> >(tee -a "$LOGFILE" >&2); then
    log "Archive failed; removing partial file"
    rm -f "$ARCHIVE_FILE"
    exit 1
fi

ARCHIVE_SIZE=$(du -h "$ARCHIVE_FILE" | cut -f1)
log "Archive created: $ARCHIVE_SIZE"

# Clean up old local backups beyond retention
log "Cleaning up local backups older than ${RETENTION_DAYS} days"
find "$BACKUP_DIR" \( -name "home_backup_*.tar.gz" -o -name "home_backup_*.zip" \) -mtime +${RETENTION_DAYS} -delete 2>&1 | tee -a "$LOGFILE"

log "=== Backup finished ==="
echo "" >> "$LOGFILE"
