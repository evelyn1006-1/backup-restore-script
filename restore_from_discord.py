#!/usr/bin/env python3
"""Restore a Discord-uploaded backup archive from chunk attachments.

This uses Discord's REST API directly with the bot token from .env. It does not
use the running backup bot process or open a Discord gateway connection.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_ENV_FILE = BASE_DIR / ".env"
DEFAULT_BACKUP_LOG = BASE_DIR / "backup.log"
DEFAULT_OUTPUT_DIR = Path("/tmp/discord-backup-restore")
DEFAULT_API_BASE = "https://discord.com/api/v10"


@dataclass
class BackupDefaults:
    channel_name: str | None = None
    archive_name: str | None = None
    expected_size: int | None = None
    expected_chunks: int | None = None
    expected_sha256: str | None = None


@dataclass
class ChunkAttachment:
    index: int
    total: int
    filename: str
    url: str
    size: int
    message_id: str


def parse_size_int(value: str | None) -> int | None:
    if value is None:
        return None
    return int(value.replace(",", "").strip())


def normalize_channel_name(name: str | None) -> str | None:
    if not name:
        return None
    return name[1:] if name.startswith("#") else name


def parse_latest_backup_log(path: Path) -> BackupDefaults:
    defaults = BackupDefaults()
    if not path.exists():
        return defaults

    blocks = [block.strip() for block in path.read_text(encoding="utf-8").split("\n\n")]
    for block in reversed(blocks):
        if "Backup uploaded to Discord" not in block:
            continue

        for line in block.splitlines():
            if line.startswith("Channel: #"):
                defaults.channel_name = normalize_channel_name(line.split("#", 1)[1].strip())
            elif line.startswith("Archive: "):
                defaults.archive_name = line.split(": ", 1)[1].strip()
            elif line.startswith("Size: "):
                match = re.search(r"\(([\d,]+) bytes\)", line)
                if match:
                    defaults.expected_size = parse_size_int(match.group(1))
            elif line.startswith("Chunks: "):
                match = re.search(r"Chunks:\s*([\d,]+)\s*x", line)
                if match:
                    defaults.expected_chunks = parse_size_int(match.group(1))
            elif line.startswith("SHA256: "):
                defaults.expected_sha256 = line.split(": ", 1)[1].strip()

        break

    return defaults


def parser() -> argparse.ArgumentParser:
    arg_parser = argparse.ArgumentParser(
        description="Download Discord backup chunks, concatenate them, and optionally extract the archive.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    arg_parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    arg_parser.add_argument("--backup-log", type=Path, default=DEFAULT_BACKUP_LOG)
    arg_parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    arg_parser.add_argument("--guild-id", help="Discord guild/server ID. Defaults to GUILD_ID from .env.")
    arg_parser.add_argument("--channel-id", help="Discord text channel ID containing the chunks.")
    arg_parser.add_argument("--channel-name", help="Discord text channel name. Defaults to latest backup.log channel.")
    arg_parser.add_argument("--archive-name", help="Original archive filename. Defaults to latest backup.log archive.")
    arg_parser.add_argument("--expected-size", type=parse_size_int, help="Expected restored byte size.")
    arg_parser.add_argument("--expected-chunks", type=parse_size_int, help="Expected number of chunk attachments.")
    arg_parser.add_argument("--expected-sha256", help="Expected SHA256 of the restored archive.")
    arg_parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    arg_parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Redownload existing chunk files and overwrite an explicit extract directory.",
    )
    arg_parser.add_argument(
        "--extract",
        action="store_true",
        help="Extract the restored .tar.gz after SHA256 verification.",
    )
    arg_parser.add_argument(
        "--extract-dir",
        type=Path,
        help="Extraction target. If omitted with --extract, a fresh directory is created under output-dir.",
    )
    arg_parser.add_argument("--timeout", type=int, default=180, help="HTTP timeout in seconds.")
    arg_parser.add_argument("--max-retries", type=int, default=6)
    arg_parser.add_argument("--retry-initial-seconds", type=float, default=10)
    arg_parser.add_argument("--retry-max-seconds", type=float, default=120)
    return arg_parser


def request_json(
    session: requests.Session,
    method: str,
    url: str,
    *,
    timeout: int,
    max_retries: int,
    retry_initial_seconds: float,
    retry_max_seconds: float,
    **kwargs: Any,
) -> Any:
    delay = retry_initial_seconds
    last_response: requests.Response | None = None

    for attempt in range(1, max_retries + 1):
        response = session.request(method, url, timeout=timeout, **kwargs)
        last_response = response

        if response.status_code == 429:
            try:
                delay = float(response.json().get("retry_after", delay))
            except ValueError:
                pass
            print(f"Rate limited by Discord; sleeping {delay:.1f}s")
            time.sleep(delay)
            delay = min(delay * 2, retry_max_seconds)
            continue

        if response.status_code >= 500 and attempt < max_retries:
            print(f"{method} {url} returned {response.status_code}; retrying in {delay:.1f}s")
            time.sleep(delay)
            delay = min(delay * 2, retry_max_seconds)
            continue

        response.raise_for_status()
        return response.json()

    assert last_response is not None
    last_response.raise_for_status()


def find_channel_id(
    session: requests.Session,
    *,
    api_base: str,
    guild_id: str,
    channel_id: str | None,
    channel_name: str | None,
    request_options: dict[str, Any],
) -> str:
    if channel_id:
        return channel_id
    if not channel_name:
        raise SystemExit("Provide --channel-id or --channel-name, or keep a parsable backup.log.")

    channels = request_json(
        session,
        "GET",
        f"{api_base}/guilds/{guild_id}/channels",
        **request_options,
    )
    matches = [
        channel
        for channel in channels
        if channel.get("type") == 0 and channel.get("name") == channel_name
    ]

    if not matches:
        raise SystemExit(f"Could not find text channel named {channel_name!r}.")
    if len(matches) > 1:
        print(f"Found {len(matches)} text channels named {channel_name!r}; using the first one.")

    return str(matches[0]["id"])


def fetch_messages(
    session: requests.Session,
    *,
    api_base: str,
    channel_id: str,
    request_options: dict[str, Any],
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    before: str | None = None

    while True:
        params = {"limit": 100}
        if before:
            params["before"] = before

        page = request_json(
            session,
            "GET",
            f"{api_base}/channels/{channel_id}/messages",
            params=params,
            **request_options,
        )
        if not page:
            break

        messages.extend(page)
        before = page[-1]["id"]
        if len(page) < 100:
            break

    return messages


def collect_chunk_attachments(
    messages: list[dict[str, Any]], archive_name: str, expected_chunks: int | None
) -> list[ChunkAttachment]:
    pattern = re.compile(rf"^{re.escape(archive_name)}\.part(\d+)of(\d+)$")
    by_index: dict[int, ChunkAttachment] = {}
    duplicates: list[int] = []

    for message in messages:
        for attachment in message.get("attachments", []):
            filename = attachment.get("filename", "")
            match = pattern.match(filename)
            if not match:
                continue

            index = int(match.group(1))
            total = int(match.group(2))
            chunk = ChunkAttachment(
                index=index,
                total=total,
                filename=filename,
                url=attachment["url"],
                size=int(attachment["size"]),
                message_id=message["id"],
            )

            if index in by_index:
                duplicates.append(index)
                continue
            by_index[index] = chunk

    if duplicates:
        duplicate_list = ", ".join(str(index) for index in sorted(set(duplicates)))
        print(f"Ignoring duplicate chunk index(es), keeping newest message copy: {duplicate_list}")

    chunks = [by_index[index] for index in sorted(by_index)]
    if not chunks:
        raise SystemExit(f"No chunk attachments found for archive {archive_name!r}.")

    totals = {chunk.total for chunk in chunks}
    if len(totals) != 1:
        raise SystemExit(f"Chunk filenames disagree on total chunk count: {sorted(totals)}")

    inferred_chunks = totals.pop()
    expected = expected_chunks or inferred_chunks
    if inferred_chunks != expected:
        raise SystemExit(f"Chunk filenames say {inferred_chunks} chunks, expected {expected}.")
    if len(chunks) != expected:
        raise SystemExit(f"Found {len(chunks)} chunks, expected {expected}.")

    expected_indices = list(range(1, expected + 1))
    actual_indices = [chunk.index for chunk in chunks]
    if actual_indices != expected_indices:
        raise SystemExit(f"Chunk index mismatch: found {actual_indices}, expected {expected_indices}.")

    return chunks


def download_chunk(
    *,
    url: str,
    destination: Path,
    expected_size: int,
    timeout: int,
    max_retries: int,
    retry_initial_seconds: float,
    retry_max_seconds: float,
) -> None:
    delay = retry_initial_seconds

    for attempt in range(1, max_retries + 1):
        try:
            with requests.get(url, stream=True, timeout=timeout) as response:
                if response.status_code == 429:
                    try:
                        delay = float(response.json().get("retry_after", delay))
                    except ValueError:
                        pass
                    print(f"Rate limited while downloading; sleeping {delay:.1f}s")
                    time.sleep(delay)
                    delay = min(delay * 2, retry_max_seconds)
                    continue

                if response.status_code >= 500 and attempt < max_retries:
                    print(
                        f"Download returned {response.status_code}; retrying in {delay:.1f}s"
                    )
                    time.sleep(delay)
                    delay = min(delay * 2, retry_max_seconds)
                    continue

                response.raise_for_status()
                temporary = destination.with_suffix(destination.suffix + ".tmp")
                with temporary.open("wb") as output:
                    for block in response.iter_content(chunk_size=1024 * 1024):
                        if block:
                            output.write(block)

                actual_size = temporary.stat().st_size
                if actual_size != expected_size:
                    temporary.unlink(missing_ok=True)
                    raise RuntimeError(
                        f"{destination.name} size mismatch: got {actual_size}, expected {expected_size}"
                    )

                temporary.replace(destination)
                return
        except Exception as exc:
            if attempt == max_retries:
                raise
            print(
                f"{destination.name} failed attempt {attempt}/{max_retries}: {exc}; "
                f"retrying in {delay:.1f}s"
            )
            time.sleep(delay)
            delay = min(delay * 2, retry_max_seconds)


def download_chunks(
    chunks: list[ChunkAttachment],
    *,
    chunk_dir: Path,
    overwrite: bool,
    request_options: dict[str, Any],
) -> None:
    chunk_dir.mkdir(parents=True, exist_ok=True)
    total = len(chunks)

    for chunk in chunks:
        destination = chunk_dir / chunk.filename
        if not overwrite and destination.exists() and destination.stat().st_size == chunk.size:
            print(f"skip {chunk.index:02d}/{total}: {chunk.filename} already downloaded")
            continue

        print(f"download {chunk.index:02d}/{total}: {chunk.filename} ({chunk.size:,} bytes)")
        download_chunk(
            url=chunk.url,
            destination=destination,
            expected_size=chunk.size,
            **request_options,
        )


def combine_chunks(chunks: list[ChunkAttachment], *, chunk_dir: Path, output_path: Path) -> str:
    digest = hashlib.sha256()
    bytes_written = 0

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as output:
        for chunk in chunks:
            chunk_path = chunk_dir / chunk.filename
            with chunk_path.open("rb") as source:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    output.write(block)
                    digest.update(block)
                    bytes_written += len(block)

    print(f"restored_size={bytes_written}")
    return digest.hexdigest()


def assert_safe_tar_members(archive_path: Path) -> None:
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive:
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise SystemExit(f"Refusing to extract unsafe tar member: {member.name}")
            if member.islnk():
                link_path = Path(member.linkname)
                if link_path.is_absolute() or ".." in link_path.parts:
                    raise SystemExit(
                        f"Refusing to extract unsafe hardlink member: {member.name}"
                    )


def backup_restore_filter(member: tarfile.TarInfo, destination: str) -> tarfile.TarInfo:
    del destination
    return member


def extract_archive(archive_path: Path, *, output_dir: Path, extract_dir: Path | None, overwrite: bool) -> Path:
    assert_safe_tar_members(archive_path)

    if extract_dir is None:
        output_dir.mkdir(parents=True, exist_ok=True)
        extract_dir = Path(
            tempfile.mkdtemp(prefix=f"{archive_path.name}.extracted-", dir=output_dir)
        )
    else:
        if extract_dir.exists():
            if not overwrite:
                raise SystemExit(f"Extraction directory already exists: {extract_dir}")
            shutil.rmtree(extract_dir)
        extract_dir.mkdir(parents=True)

    with tarfile.open(archive_path, "r:gz") as archive:
        archive.extractall(extract_dir, filter=backup_restore_filter)

    return extract_dir


def main() -> int:
    args = parser().parse_args()
    defaults = parse_latest_backup_log(args.backup_log)

    load_dotenv(args.env_file)
    token = os.getenv("DISCORD_TOKEN")
    guild_id = args.guild_id or os.getenv("GUILD_ID")
    channel_name = normalize_channel_name(args.channel_name) or defaults.channel_name
    archive_name = args.archive_name or defaults.archive_name
    expected_size = args.expected_size if args.expected_size is not None else defaults.expected_size
    expected_chunks = (
        args.expected_chunks if args.expected_chunks is not None else defaults.expected_chunks
    )
    expected_sha256 = args.expected_sha256 or defaults.expected_sha256

    if not token:
        raise SystemExit(f"DISCORD_TOKEN is missing from {args.env_file}.")
    if not guild_id and not args.channel_id:
        raise SystemExit("GUILD_ID is missing. Set it in .env or pass --guild-id.")
    if not archive_name:
        raise SystemExit("Archive name is missing. Pass --archive-name or keep a parsable backup.log.")

    request_options = {
        "timeout": args.timeout,
        "max_retries": args.max_retries,
        "retry_initial_seconds": args.retry_initial_seconds,
        "retry_max_seconds": args.retry_max_seconds,
    }

    session = requests.Session()
    session.headers.update({"Authorization": f"Bot {token}"})

    print(f"archive={archive_name}")
    channel_id = find_channel_id(
        session,
        api_base=args.api_base,
        guild_id=str(guild_id),
        channel_id=args.channel_id,
        channel_name=channel_name,
        request_options=request_options,
    )
    print(f"channel_id={channel_id}")

    messages = fetch_messages(
        session,
        api_base=args.api_base,
        channel_id=channel_id,
        request_options=request_options,
    )
    print(f"messages_fetched={len(messages)}")

    chunks = collect_chunk_attachments(messages, archive_name, expected_chunks)
    print(f"chunks_found={len(chunks)}")

    chunk_dir = args.output_dir / f"{archive_name}.chunks"
    restored_path = args.output_dir / archive_name
    download_chunks(
        chunks,
        chunk_dir=chunk_dir,
        overwrite=args.overwrite,
        request_options=request_options,
    )

    print(f"combining={restored_path}")
    actual_sha256 = combine_chunks(chunks, chunk_dir=chunk_dir, output_path=restored_path)
    actual_size = restored_path.stat().st_size

    print(f"restored_path={restored_path}")
    print(f"restored_sha256={actual_sha256}")
    if expected_size is not None:
        print(f"expected_size={expected_size}")
        if actual_size != expected_size:
            raise SystemExit(f"Restored size mismatch: got {actual_size}, expected {expected_size}")
    if expected_sha256 is not None:
        print(f"expected_sha256={expected_sha256}")
        if actual_sha256 != expected_sha256:
            raise SystemExit("Restored SHA256 mismatch.")

    print("archive_verified=True")

    if args.extract:
        extract_dir = extract_archive(
            restored_path,
            output_dir=args.output_dir,
            extract_dir=args.extract_dir,
            overwrite=args.overwrite,
        )
        print(f"extracted_dir={extract_dir}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
