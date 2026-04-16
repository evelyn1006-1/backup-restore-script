#!/bin/sh
# Restore a Discord backup with one curl-piped shell command.

set -eu

RESTORE_SCRIPT_URL="${RESTORE_SCRIPT_URL:-https://raw.githubusercontent.com/evelyn1006-1/backup-restore-script/main/restore_from_discord.py}"
OUTPUT_DIR="${RESTORE_OUTPUT_DIR:-$HOME/discord-backup-restore}"
PYTHON="${PYTHON:-python3}"
TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT HUP INT TERM

if [ -z "${DISCORD_TOKEN:-}" ]; then
    echo "DISCORD_TOKEN is not set. Run: export DISCORD_TOKEN='your-bot-token'" >&2
    exit 1
fi

if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "python3 is required." >&2
    exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
    echo "curl is required." >&2
    exit 1
fi

if ! command -v tar >/dev/null 2>&1; then
    echo "tar is required." >&2
    exit 1
fi

mkdir -p "$OUTPUT_DIR"
curl -fsSL "$RESTORE_SCRIPT_URL" -o "$TMPDIR/restore_from_discord.py"
"$PYTHON" -m venv "$TMPDIR/venv"
"$TMPDIR/venv/bin/pip" install --quiet requests python-dotenv

set -- "$TMPDIR/venv/bin/python" "$TMPDIR/restore_from_discord.py" --output-dir "$OUTPUT_DIR" --extract

if [ -n "${RESTORE_CHANNEL_ID:-}" ]; then
    set -- "$@" --channel-id "$RESTORE_CHANNEL_ID"
fi

if [ -n "${RESTORE_CHANNEL_NAME:-}" ]; then
    set -- "$@" --channel-name "$RESTORE_CHANNEL_NAME"
fi

if [ -n "${RESTORE_GUILD_ID:-${GUILD_ID:-}}" ]; then
    set -- "$@" --guild-id "${RESTORE_GUILD_ID:-${GUILD_ID:-}}"
fi

if [ -n "${RESTORE_ARCHIVE_NAME:-}" ]; then
    set -- "$@" --archive-name "$RESTORE_ARCHIVE_NAME"
fi

if [ -n "${RESTORE_EXPECTED_CHUNKS:-}" ]; then
    set -- "$@" --expected-chunks "$RESTORE_EXPECTED_CHUNKS"
fi

if [ -n "${RESTORE_EXPECTED_SIZE:-}" ]; then
    set -- "$@" --expected-size "$RESTORE_EXPECTED_SIZE"
fi

if [ -n "${RESTORE_EXPECTED_SHA256:-}" ]; then
    set -- "$@" --expected-sha256 "$RESTORE_EXPECTED_SHA256"
fi

if [ -n "${RESTORE_EXTRACT_DIR:-}" ]; then
    set -- "$@" --extract-dir "$RESTORE_EXTRACT_DIR"
fi

if [ "${RESTORE_OVERWRITE:-0}" = "1" ]; then
    set -- "$@" --overwrite
fi

"$@"
