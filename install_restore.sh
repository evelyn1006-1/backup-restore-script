#!/bin/sh
# Restore a Discord backup with one curl-piped shell command.
#
# Required:
#   DISCORD_TOKEN
#   and either RESTORE_CHANNEL_ID
#   or RESTORE_GUILD_ID/GUILD_ID plus RESTORE_CHANNEL_NAME
#
# Optional:
#   RESTORE_ARCHIVE_NAME
#   RESTORE_EXPECTED_CHUNKS
#   RESTORE_EXPECTED_SIZE
#   RESTORE_EXPECTED_SHA256
#   RESTORE_OUTPUT_DIR
#   RESTORE_EXTRACT_DIR
#   RESTORE_OVERWRITE=1

set -eu

API_BASE="${RESTORE_API_BASE:-https://discord.com/api/v10}"
OUTPUT_DIR="${RESTORE_OUTPUT_DIR:-$HOME/discord-backup-restore}"
PYTHON="${PYTHON:-python3}"

if [ -z "${DISCORD_TOKEN:-}" ]; then
    echo "DISCORD_TOKEN is not set. Run: export DISCORD_TOKEN='your-bot-token'" >&2
    exit 1
fi

if [ -z "${RESTORE_CHANNEL_ID:-}" ]; then
    if [ -z "${RESTORE_CHANNEL_NAME:-}" ]; then
        echo "Set RESTORE_CHANNEL_ID, or set RESTORE_CHANNEL_NAME with RESTORE_GUILD_ID/GUILD_ID." >&2
        exit 1
    fi
    if [ -z "${RESTORE_GUILD_ID:-${GUILD_ID:-}}" ]; then
        echo "Set RESTORE_GUILD_ID or GUILD_ID when using RESTORE_CHANNEL_NAME." >&2
        exit 1
    fi
fi

if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "python3 is required." >&2
    exit 1
fi

if ! command -v tar >/dev/null 2>&1; then
    echo "tar is required." >&2
    exit 1
fi

mkdir -p "$OUTPUT_DIR"
export API_BASE OUTPUT_DIR

"$PYTHON" <<'PY'
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


token = os.environ["DISCORD_TOKEN"]
api_base = os.environ["API_BASE"].rstrip("/")
output_dir = Path(os.environ["OUTPUT_DIR"]).expanduser()
channel_id = os.environ.get("RESTORE_CHANNEL_ID", "").strip()
channel_name = os.environ.get("RESTORE_CHANNEL_NAME", "").strip().lstrip("#")
guild_id = (
    os.environ.get("RESTORE_GUILD_ID")
    or os.environ.get("GUILD_ID")
    or ""
).strip()
archive_name = os.environ.get("RESTORE_ARCHIVE_NAME", "").strip()
expected_chunks_raw = os.environ.get("RESTORE_EXPECTED_CHUNKS", "").strip()
expected_size_raw = os.environ.get("RESTORE_EXPECTED_SIZE", "").strip()
expected_sha256 = os.environ.get("RESTORE_EXPECTED_SHA256", "").strip().lower()
extract_dir_raw = os.environ.get("RESTORE_EXTRACT_DIR", "").strip()
overwrite = os.environ.get("RESTORE_OVERWRITE", "") == "1"

headers = {
    "Authorization": f"Bot {token}",
    "User-Agent": "backup-restore-script/1.0",
}


def parse_int_env(name, raw):
    if not raw:
        return None
    try:
        return int(raw.replace(",", ""))
    except ValueError:
        raise SystemExit(f"{name} must be an integer, got {raw!r}.")


expected_chunks = parse_int_env("RESTORE_EXPECTED_CHUNKS", expected_chunks_raw)
expected_size = parse_int_env("RESTORE_EXPECTED_SIZE", expected_size_raw)


def request_json(url, retries=6):
    delay = 10.0
    for attempt in range(1, retries + 1):
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read()
            retry_after = None
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


def infer_archive_name(name):
    match = re.fullmatch(r"backup-(\d{8})-(\d{6})(?:-\d+)?", name)
    if not match:
        raise SystemExit(
            "RESTORE_ARCHIVE_NAME is not set and the channel name does not look like "
            "backup-YYYYMMDD-HHMMSS."
        )
    return f"home_backup_{match.group(1)}_{match.group(2)}.tar.gz"


def resolve_channel():
    global channel_id, channel_name

    if channel_id:
        print(f"Resolving Discord channel {channel_id}...", flush=True)
        channel = request_json(f"{api_base}/channels/{urllib.parse.quote(channel_id)}")
        channel_name = channel.get("name", channel_name)
        return

    print(f"Resolving Discord channel #{channel_name} in guild {guild_id}...", flush=True)
    channels = request_json(f"{api_base}/guilds/{urllib.parse.quote(guild_id)}/channels")
    matches = [
        channel
        for channel in channels
        if channel.get("type") == 0 and channel.get("name") == channel_name
    ]
    if not matches:
        raise SystemExit(f"Could not find text channel named {channel_name!r}.")
    if len(matches) > 1:
        print(f"Found {len(matches)} channels named {channel_name!r}; using the first one.", flush=True)

    channel_id = str(matches[0]["id"])
    channel_name = matches[0].get("name", channel_name)


resolve_channel()
if not archive_name:
    archive_name = infer_archive_name(channel_name)
    print(f"Inferred archive name from channel name: {archive_name}", flush=True)

chunk_dir = output_dir / f"{archive_name}.chunks"
restored_path = output_dir / archive_name
if extract_dir_raw:
    extract_dir = Path(extract_dir_raw).expanduser()
else:
    archive_stem = archive_name
    for suffix in (".tar.gz", ".tgz", ".tar"):
        if archive_stem.endswith(suffix):
            archive_stem = archive_stem[: -len(suffix)]
            break
    extract_dir = Path.home() / f"restored-{archive_stem}"

if extract_dir.exists():
    if overwrite:
        shutil.rmtree(extract_dir)
    else:
        raise SystemExit(
            f"Extraction directory already exists: {extract_dir}\n"
            "Set RESTORE_OVERWRITE=1 to replace it, or RESTORE_EXTRACT_DIR to choose a new path."
        )

print(f"Fetching messages from Discord channel {channel_id}...", flush=True)
messages = []
before = None
while True:
    url = f"{api_base}/channels/{urllib.parse.quote(channel_id)}/messages?limit=100"
    if before:
        url = f"{url}&before={urllib.parse.quote(before)}"
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
if not chunks:
    raise SystemExit(
        f"No chunk attachments found for archive {archive_name!r} in #{channel_name or channel_id}."
    )

totals = {chunk["total"] for chunk in chunks}
if len(totals) != 1:
    raise SystemExit(f"Chunk filenames disagree on total chunk count: {sorted(totals)}.")

inferred_chunks = totals.pop()
if expected_chunks is None:
    expected_chunks = inferred_chunks
elif expected_chunks != inferred_chunks:
    raise SystemExit(
        f"Chunk filenames say {inferred_chunks} chunks, RESTORE_EXPECTED_CHUNKS={expected_chunks}."
    )

if len(chunks) != expected_chunks:
    raise SystemExit(f"Found {len(chunks)} chunks, expected {expected_chunks}.")

expected_indices = list(range(1, expected_chunks + 1))
actual_indices = [chunk["index"] for chunk in chunks]
if actual_indices != expected_indices:
    raise SystemExit(f"Chunk index mismatch: got {actual_indices}, expected {expected_indices}.")

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

if expected_size is not None and bytes_written != expected_size:
    raise SystemExit(f"Restored size mismatch: got {bytes_written}, expected {expected_size}.")
if expected_sha256 and actual_sha256 != expected_sha256:
    raise SystemExit(f"SHA256 mismatch: got {actual_sha256}, expected {expected_sha256}.")

if expected_size is not None or expected_sha256:
    print("archive_verified=True", flush=True)
else:
    print("archive_verified=False; set RESTORE_EXPECTED_SIZE and/or RESTORE_EXPECTED_SHA256 to verify.", flush=True)

extract_dir.mkdir(parents=True)
print(f"Extracting {restored_path} into {extract_dir}...", flush=True)
subprocess.run(["tar", "-xzf", str(restored_path), "-C", str(extract_dir)], check=True)
print(f"Restored backup directory: {extract_dir}", flush=True)
PY
