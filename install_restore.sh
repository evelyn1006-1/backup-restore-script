#!/bin/sh
# Restore the verified Discord backup with one curl-piped shell command.
#
# Usage on a fresh machine:
#   export DISCORD_TOKEN='your-bot-token'
#   curl -fsSL https://raw.githubusercontent.com/evelyn1006-1/backup-restore-script/main/install_restore.sh | sh

set -eu

ARCHIVE_NAME="${RESTORE_ARCHIVE_NAME:-home_backup_20260415_203026.tar.gz}"
CHANNEL_ID="${RESTORE_CHANNEL_ID:-1494148852951814254}"
EXPECTED_CHUNKS="${RESTORE_EXPECTED_CHUNKS:-51}"
EXPECTED_SIZE="${RESTORE_EXPECTED_SIZE:-509126153}"
EXPECTED_SHA256="${RESTORE_EXPECTED_SHA256:-bad264641f396e58924c0f12ac7e717593316885d9268b837db9f576c44c70f8}"
API_BASE="${RESTORE_API_BASE:-https://discord.com/api/v10}"
OUTPUT_DIR="${RESTORE_OUTPUT_DIR:-$HOME/discord-backup-restore}"
EXTRACT_DIR="${RESTORE_EXTRACT_DIR:-$HOME/restored-home-backup-20260415_203026}"
PYTHON="${PYTHON:-python3}"

if [ -z "${DISCORD_TOKEN:-}" ]; then
    echo "DISCORD_TOKEN is not set. Run: export DISCORD_TOKEN='your-bot-token'" >&2
    exit 1
fi

if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "python3 is required." >&2
    exit 1
fi

if ! command -v tar >/dev/null 2>&1; then
    echo "tar is required." >&2
    exit 1
fi

if [ -e "$EXTRACT_DIR" ]; then
    if [ "${RESTORE_OVERWRITE:-0}" = "1" ]; then
        rm -rf "$EXTRACT_DIR"
    else
        echo "Extraction directory already exists: $EXTRACT_DIR" >&2
        echo "Set RESTORE_OVERWRITE=1 to replace it, or RESTORE_EXTRACT_DIR=/some/new/path." >&2
        exit 1
    fi
fi

mkdir -p "$OUTPUT_DIR"

export ARCHIVE_NAME
export CHANNEL_ID
export EXPECTED_CHUNKS
export EXPECTED_SIZE
export EXPECTED_SHA256
export API_BASE
export OUTPUT_DIR

"$PYTHON" <<'PY'
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


archive_name = os.environ["ARCHIVE_NAME"]
channel_id = os.environ["CHANNEL_ID"]
expected_chunks = int(os.environ["EXPECTED_CHUNKS"])
expected_size = int(os.environ["EXPECTED_SIZE"])
expected_sha256 = os.environ["EXPECTED_SHA256"]
api_base = os.environ["API_BASE"].rstrip("/")
output_dir = Path(os.environ["OUTPUT_DIR"]).expanduser()
chunk_dir = output_dir / f"{archive_name}.chunks"
restored_path = output_dir / archive_name
token = os.environ["DISCORD_TOKEN"]

headers = {
    "Authorization": f"Bot {token}",
    "User-Agent": "backup-restore-script/1.0",
}


def request_json(url, retries=6):
    delay = 10.0
    for attempt in range(1, retries + 1):
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            retry_after = None
            body = exc.read()
            if exc.code == 429:
                try:
                    retry_after = float(json.loads(body.decode("utf-8")).get("retry_after", delay))
                except Exception:
                    retry_after = delay
            if exc.code == 429 or exc.code >= 500:
                if attempt == retries:
                    raise
                sleep_for = retry_after if retry_after is not None else delay
                print(f"Discord returned HTTP {exc.code}; retrying in {sleep_for:.1f}s", flush=True)
                time.sleep(sleep_for)
                delay = min(delay * 2, 120.0)
                continue
            raise
        except (TimeoutError, OSError) as exc:
            if attempt == retries:
                raise
            print(f"Request failed: {exc}; retrying in {delay:.1f}s", flush=True)
            time.sleep(delay)
            delay = min(delay * 2, 120.0)


def download(url, destination, expected_bytes, retries=6):
    delay = 10.0
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    for attempt in range(1, retries + 1):
        request = urllib.request.Request(url, headers={"User-Agent": "backup-restore-script/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                temporary.unlink(missing_ok=True)
                with temporary.open("wb") as output:
                    while True:
                        block = response.read(1024 * 1024)
                        if not block:
                            break
                        output.write(block)

            actual_bytes = temporary.stat().st_size
            if actual_bytes != expected_bytes:
                temporary.unlink(missing_ok=True)
                raise RuntimeError(
                    f"{destination.name} size mismatch: got {actual_bytes}, expected {expected_bytes}"
                )

            temporary.replace(destination)
            return
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                try:
                    retry_after = float(json.loads(exc.read().decode("utf-8")).get("retry_after", delay))
                except Exception:
                    retry_after = delay
                if attempt < retries:
                    print(f"Download rate limited; retrying in {retry_after:.1f}s", flush=True)
                    time.sleep(retry_after)
                    delay = min(delay * 2, 120.0)
                    continue
            if exc.code >= 500 and attempt < retries:
                print(f"Download returned HTTP {exc.code}; retrying in {delay:.1f}s", flush=True)
                time.sleep(delay)
                delay = min(delay * 2, 120.0)
                continue
            raise
        except (TimeoutError, OSError, RuntimeError) as exc:
            if attempt == retries:
                raise
            print(f"{destination.name} failed: {exc}; retrying in {delay:.1f}s", flush=True)
            time.sleep(delay)
            delay = min(delay * 2, 120.0)


print(f"Fetching messages from Discord channel {channel_id}...", flush=True)
messages = []
before = None
while True:
    url = f"{api_base}/channels/{channel_id}/messages?limit=100"
    if before:
        url = f"{url}&before={before}"
    page = request_json(url)
    if not page:
        break
    messages.extend(page)
    before = page[-1]["id"]
    if len(page) < 100:
        break

print(f"Fetched {len(messages)} messages.", flush=True)

pattern = re.compile(rf"^{re.escape(archive_name)}\.part(\d+)of(\d+)$")
chunks_by_index = {}
for message in messages:
    for attachment in message.get("attachments", []):
        filename = attachment.get("filename", "")
        match = pattern.match(filename)
        if not match:
            continue
        index = int(match.group(1))
        total = int(match.group(2))
        chunks_by_index.setdefault(
            index,
            {
                "index": index,
                "total": total,
                "filename": filename,
                "url": attachment["url"],
                "size": int(attachment["size"]),
            },
        )

chunks = [chunks_by_index[index] for index in sorted(chunks_by_index)]
if len(chunks) != expected_chunks:
    raise SystemExit(f"Found {len(chunks)} chunks, expected {expected_chunks}.")

expected_indices = list(range(1, expected_chunks + 1))
actual_indices = [chunk["index"] for chunk in chunks]
if actual_indices != expected_indices:
    raise SystemExit(f"Chunk index mismatch: got {actual_indices}, expected {expected_indices}.")

if any(chunk["total"] != expected_chunks for chunk in chunks):
    raise SystemExit("At least one chunk filename has the wrong total chunk count.")

chunk_dir.mkdir(parents=True, exist_ok=True)
print(f"Downloading chunks into {chunk_dir}...", flush=True)
for chunk in chunks:
    destination = chunk_dir / chunk["filename"]
    if destination.exists() and destination.stat().st_size == chunk["size"]:
        print(f"skip {chunk['index']:02d}/{expected_chunks}: {chunk['filename']}", flush=True)
        continue
    print(
        f"download {chunk['index']:02d}/{expected_chunks}: "
        f"{chunk['filename']} ({chunk['size']:,} bytes)",
        flush=True,
    )
    download(chunk["url"], destination, chunk["size"])

print(f"Combining chunks into {restored_path}...", flush=True)
digest = hashlib.sha256()
bytes_written = 0
with restored_path.open("wb") as output:
    for chunk in chunks:
        with (chunk_dir / chunk["filename"]).open("rb") as source:
            while True:
                block = source.read(1024 * 1024)
                if not block:
                    break
                output.write(block)
                digest.update(block)
                bytes_written += len(block)

actual_sha256 = digest.hexdigest()
print(f"restored_size={bytes_written}", flush=True)
print(f"restored_sha256={actual_sha256}", flush=True)

if bytes_written != expected_size:
    raise SystemExit(f"Restored size mismatch: got {bytes_written}, expected {expected_size}.")
if actual_sha256 != expected_sha256:
    raise SystemExit(f"SHA256 mismatch: got {actual_sha256}, expected {expected_sha256}.")

print("archive_verified=True", flush=True)
PY

mkdir -p "$EXTRACT_DIR"
echo "Extracting $OUTPUT_DIR/$ARCHIVE_NAME into $EXTRACT_DIR..."
tar -xzf "$OUTPUT_DIR/$ARCHIVE_NAME" -C "$EXTRACT_DIR"
echo "Restored backup directory: $EXTRACT_DIR"
