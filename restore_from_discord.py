#!/usr/bin/env python3
"""Restore a Discord-uploaded backup archive from chunk attachments.

This uses Discord's REST API directly with the bot token from .env. It does not
use the running backup bot process or open a Discord gateway connection.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional on fresh restore hosts
    def load_dotenv(*_args: Any, **_kwargs: Any) -> bool:
        return False


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


@dataclass
class AttachmentInfo:
    filename: str
    size: int
    url: str
    message_id: str


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


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


def retry_delay_from_response(response: requests.Response, default_delay: float) -> float:
    try:
        return float(response.json().get("retry_after", default_delay))
    except ValueError:
        return default_delay


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
            delay = retry_delay_from_response(response, delay)
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


def fetch_guilds(
    session: requests.Session,
    *,
    api_base: str,
    request_options: dict[str, Any],
) -> list[dict[str, Any]]:
    return request_json(
        session,
        "GET",
        f"{api_base}/users/@me/guilds",
        **request_options,
    )


def fetch_guild_channels(
    session: requests.Session,
    *,
    api_base: str,
    guild_id: str,
    request_options: dict[str, Any],
) -> list[dict[str, Any]]:
    return request_json(
        session,
        "GET",
        f"{api_base}/guilds/{guild_id}/channels",
        **request_options,
    )


def latest_backup_channel_in_channels(channels: list[dict[str, Any]]) -> dict[str, Any] | None:
    backup_category_ids = {
        str(channel["id"])
        for channel in channels
        if channel.get("type") == 4 and channel.get("name") == "Backups"
    }
    candidates = [
        channel
        for channel in channels
        if channel.get("type") == 0
        and channel.get("parent_id") in backup_category_ids
        and channel.get("name", "").startswith("backup-")
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda channel: int(channel["id"]))


def resolve_restore_channel(
    session: requests.Session,
    *,
    api_base: str,
    guild_id: str | None,
    channel_id: str | None,
    channel_name: str | None,
    request_options: dict[str, Any],
) -> tuple[str, str | None, str | None]:
    if channel_id:
        return channel_id, channel_name, guild_id

    guilds: list[dict[str, Any]] | None = None
    if not guild_id:
        guilds = fetch_guilds(
            session,
            api_base=api_base,
            request_options=request_options,
        )
        if not guilds:
            raise SystemExit("This bot is not currently in any guilds.")

    if channel_name:
        if guild_id:
            resolved_id = find_channel_id(
                session,
                api_base=api_base,
                guild_id=guild_id,
                channel_id=None,
                channel_name=channel_name,
                request_options=request_options,
            )
            return resolved_id, channel_name, guild_id

        matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
        assert guilds is not None
        for guild in guilds:
            channels = fetch_guild_channels(
                session,
                api_base=api_base,
                guild_id=str(guild["id"]),
                request_options=request_options,
            )
            for channel in channels:
                if channel.get("type") == 0 and channel.get("name") == channel_name:
                    matches.append((guild, channel))
        if not matches:
            raise SystemExit(f"Could not find text channel named {channel_name!r} in any guild.")
        selected_guild, selected_channel = max(matches, key=lambda item: int(item[1]["id"]))
        return str(selected_channel["id"]), selected_channel.get("name"), str(selected_guild["id"])

    if guild_id:
        channels = fetch_guild_channels(
            session,
            api_base=api_base,
            guild_id=guild_id,
            request_options=request_options,
        )
        selected = latest_backup_channel_in_channels(channels)
        if selected is None:
            raise SystemExit(
                f"Could not find any text channels starting with 'backup-' under a 'Backups' category in guild {guild_id}."
            )
        return str(selected["id"]), selected.get("name"), guild_id

    assert guilds is not None
    candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for guild in guilds:
        channels = fetch_guild_channels(
            session,
            api_base=api_base,
            guild_id=str(guild["id"]),
            request_options=request_options,
        )
        selected = latest_backup_channel_in_channels(channels)
        if selected is not None:
            candidates.append((guild, selected))
    if not candidates:
        raise SystemExit(
            "Could not find any text channels starting with 'backup-' under a 'Backups' category in any guild."
        )
    selected_guild, selected_channel = max(candidates, key=lambda item: int(item[1]["id"]))
    return str(selected_channel["id"]), selected_channel.get("name"), str(selected_guild["id"])


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


def attachment_infos(messages: list[dict[str, Any]]) -> list[AttachmentInfo]:
    attachments: list[AttachmentInfo] = []
    for message in messages:
        for attachment in message.get("attachments", []):
            attachments.append(
                AttachmentInfo(
                    filename=attachment["filename"],
                    size=int(attachment["size"]),
                    url=attachment["url"],
                    message_id=message["id"],
                )
            )
    return attachments


def find_manifest_attachment(
    messages: list[dict[str, Any]], archive_name: str | None = None
) -> AttachmentInfo | None:
    attachments = [
        attachment
        for attachment in attachment_infos(messages)
        if attachment.filename.endswith(".manifest.json")
    ]
    if not attachments:
        return None
    if archive_name is None:
        return attachments[0]

    archive_stem = archive_name.removesuffix(".tar.gz")
    for attachment in attachments:
        if attachment.filename.startswith(archive_stem):
            return attachment
    return attachments[0]


def find_attachment_by_name(messages: list[dict[str, Any]], filename: str) -> AttachmentInfo | None:
    for attachment in attachment_infos(messages):
        if attachment.filename == filename:
            return attachment
    return None


def download_small_attachment(
    session: requests.Session,
    attachment: AttachmentInfo,
    *,
    timeout: int,
    max_retries: int,
    retry_initial_seconds: float,
    retry_max_seconds: float,
) -> bytes:
    delay = retry_initial_seconds
    last_response: requests.Response | None = None

    for attempt in range(1, max_retries + 1):
        response = session.get(attachment.url, timeout=timeout)
        last_response = response
        if response.status_code == 429:
            delay = retry_delay_from_response(response, delay)
            print(f"Rate limited while downloading {attachment.filename}; sleeping {delay:.1f}s")
            time.sleep(delay)
            delay = min(delay * 2, retry_max_seconds)
            continue
        if response.status_code >= 500 and attempt < max_retries:
            print(
                f"Download returned {response.status_code} for {attachment.filename}; "
                f"retrying in {delay:.1f}s"
            )
            time.sleep(delay)
            delay = min(delay * 2, retry_max_seconds)
            continue
        response.raise_for_status()
        return response.content

    assert last_response is not None
    last_response.raise_for_status()


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
    session: requests.Session,
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
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.unlink(missing_ok=True)

    for attempt in range(1, max_retries + 1):
        try:
            with session.get(url, stream=True, timeout=timeout) as response:
                if response.status_code == 429:
                    delay = retry_delay_from_response(response, delay)
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
            temporary.unlink(missing_ok=True)
            if attempt == max_retries:
                raise
            print(
                f"{destination.name} failed attempt {attempt}/{max_retries}: {exc}; "
                f"retrying in {delay:.1f}s"
            )
            time.sleep(delay)
            delay = min(delay * 2, retry_max_seconds)


def download_chunks(
    session: requests.Session,
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
            session,
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


def download_archive_from_messages(
    session: requests.Session,
    *,
    messages: list[dict[str, Any]],
    archive_name: str,
    expected_chunks: int | None,
    expected_size: int | None,
    expected_sha256: str | None,
    output_dir: Path,
    overwrite: bool,
    request_options: dict[str, Any],
) -> tuple[Path, str]:
    chunks = collect_chunk_attachments(messages, archive_name, expected_chunks)
    print(f"chunks_found={len(chunks)}")

    chunk_dir = output_dir / f"{archive_name}.chunks"
    restored_path = output_dir / archive_name
    download_chunks(
        session,
        chunks,
        chunk_dir=chunk_dir,
        overwrite=overwrite,
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

    return restored_path, actual_sha256


def assert_safe_tar_members(archive_path: Path) -> int:
    member_count = 0
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive:
            member_count += 1
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise SystemExit(f"Refusing to extract unsafe tar member: {member.name}")
            if member.islnk():
                link_path = Path(member.linkname)
                if link_path.is_absolute() or ".." in link_path.parts:
                    raise SystemExit(
                        f"Refusing to extract unsafe hardlink member: {member.name}"
                    )
    return member_count


def backup_restore_filter(member: tarfile.TarInfo, destination: str) -> tarfile.TarInfo:
    del destination
    return member


def default_extract_dir_for(archive_path: Path) -> Path:
    archive_stem = archive_path.name
    for suffix in (".tar.gz", ".tgz", ".tar"):
        if archive_stem.endswith(suffix):
            archive_stem = archive_stem[: -len(suffix)]
            break
    return Path.home() / f"restored-{archive_stem}"


def prepare_extract_dir(extract_dir: Path, *, overwrite: bool) -> Path:
    if extract_dir.exists():
        if not overwrite:
            raise SystemExit(f"Extraction directory already exists: {extract_dir}")
        shutil.rmtree(extract_dir)
    extract_dir.mkdir(parents=True)
    return extract_dir


def extract_archive(archive_path: Path, *, extract_dir: Path | None, overwrite: bool) -> Path:
    member_count = assert_safe_tar_members(archive_path)
    target_dir = prepare_extract_dir(
        extract_dir or default_extract_dir_for(archive_path),
        overwrite=overwrite,
    )

    print(f"extracting={archive_path} target={target_dir} total_members={member_count}")
    with tarfile.open(archive_path, "r:gz") as archive:
        archive.extractall(target_dir, filter=backup_restore_filter)

    return target_dir


def remove_existing_member_target(
    extract_dir: Path,
    member: tarfile.TarInfo,
    changed_modes: dict[Path, int],
) -> None:
    """Make room for a tar member without deleting mergeable directories."""
    relative = Path(member.name)
    target = extract_dir / relative

    if member.isdir():
        if target.exists() and not target.is_dir():
            target.unlink()
        elif target.is_symlink():
            target.unlink()
        return

    if target.is_dir() and not target.is_symlink():
        make_directory_tree_writable(target, changed_modes)
        shutil.rmtree(target)
    elif target.exists() or target.is_symlink():
        target.unlink()


def make_directory_tree_writable(directory: Path, changed_modes: dict[Path, int]) -> None:
    if not directory.exists() or not directory.is_dir() or directory.is_symlink():
        return

    current_mode = directory.stat().st_mode & 0o7777
    changed_modes.setdefault(directory, current_mode)
    directory.chmod(current_mode | 0o700)
    with os.scandir(directory) as scanner:
        for child in scanner:
            if child.is_dir(follow_symlinks=False):
                make_directory_tree_writable(Path(child.path), changed_modes)


def make_directory_writable(directory: Path, changed_modes: dict[Path, int]) -> None:
    if not directory.exists() or not directory.is_dir() or directory.is_symlink():
        return

    current_mode = directory.stat().st_mode & 0o7777
    changed_modes.setdefault(directory, current_mode)
    writable_mode = current_mode | 0o700
    if writable_mode == current_mode:
        return

    directory.chmod(writable_mode)


def restore_directory_modes(changed_modes: dict[Path, int]) -> None:
    for directory, mode in sorted(
        changed_modes.items(),
        key=lambda item: len(item[0].parts),
        reverse=True,
    ):
        if directory.exists() and directory.is_dir() and not directory.is_symlink():
            directory.chmod(mode)


def prepare_existing_member_targets(
    archive_path: Path,
    extract_dir: Path,
    *,
    member_count: int,
    changed_modes: dict[Path, int],
) -> set[Path]:
    print(f"preparing_overlay={archive_path} target={extract_dir} total_members={member_count}")
    archived_directories: set[Path] = set()
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive:
            relative = Path(member.name)
            target = extract_dir / relative

            if member.isdir():
                archived_directories.add(target)
                make_directory_writable(target, changed_modes)
            if target.parent not in archived_directories:
                make_directory_writable(target.parent, changed_modes)

            remove_existing_member_target(extract_dir, member, changed_modes)

    return archived_directories


def apply_deleted_paths(extract_dir: Path, deleted_paths_file: Path) -> None:
    for line in deleted_paths_file.read_text(encoding="utf-8").splitlines():
        path = line.strip()
        if not path:
            continue
        relative = Path(path[2:] if path.startswith("./") else path)
        target = extract_dir / relative
        if target.is_symlink() or target.is_file():
            target.unlink(missing_ok=True)
        elif target.is_dir():
            shutil.rmtree(target, ignore_errors=True)


def extract_into_existing_directory(archive_path: Path, extract_dir: Path) -> None:
    member_count = assert_safe_tar_members(archive_path)
    changed_modes: dict[Path, int] = {}
    archived_directories: set[Path] = set()
    extraction_succeeded = False
    try:
        archived_directories = prepare_existing_member_targets(
            archive_path,
            extract_dir,
            member_count=member_count,
            changed_modes=changed_modes,
        )
        print(f"extracting_overlay={archive_path} target={extract_dir} total_members={member_count}")
        with tarfile.open(archive_path, "r:gz") as archive:
            archive.extractall(extract_dir, filter=backup_restore_filter)
        extraction_succeeded = True
    finally:
        if extraction_succeeded:
            for directory in archived_directories:
                changed_modes.pop(directory, None)
        restore_directory_modes(changed_modes)


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

    request_options = {
        "timeout": args.timeout,
        "max_retries": args.max_retries,
        "retry_initial_seconds": args.retry_initial_seconds,
        "retry_max_seconds": args.retry_max_seconds,
    }

    with requests.Session() as session:
        session.headers.update({"Authorization": f"Bot {token}"})

        channel_id, channel_name, guild_id = resolve_restore_channel(
            session,
            api_base=args.api_base,
            guild_id=str(guild_id) if guild_id else None,
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

        manifest_attachment = find_manifest_attachment(messages, archive_name)
        manifest_data: dict | None = None
        if manifest_attachment is not None:
            manifest_data = json.loads(
                download_small_attachment(session, manifest_attachment, **request_options).decode("utf-8")
            )
            print(f"manifest={manifest_attachment.filename}")
            archive_name = archive_name or manifest_data.get("archive_name")
            expected_size = expected_size if expected_size is not None else manifest_data.get("archive_size")
            expected_chunks = (
                expected_chunks if expected_chunks is not None else manifest_data.get("upload", {}).get("chunk_count")
            )
            expected_sha256 = expected_sha256 or manifest_data.get("archive_sha256")

        if not archive_name:
            raise SystemExit("Archive name is missing. Pass --archive-name or keep a parsable backup.log.")

        print(f"archive={archive_name}")
        restored_path, actual_sha256 = download_archive_from_messages(
            session,
            messages=messages,
            archive_name=archive_name,
            expected_chunks=expected_chunks,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
            output_dir=args.output_dir,
            overwrite=args.overwrite,
            request_options=request_options,
        )

        backup_type = manifest_data.get("backup_type") if manifest_data else "full"
        if expected_size is not None or expected_sha256 is not None:
            print("archive_verified=True")
        else:
            print("archive_verified=False")

        if manifest_data and backup_type == "differential":
            if not args.extract:
                raise SystemExit("Differential restore requires --extract to reconstruct the full tree.")

            basis = manifest_data.get("basis") or {}
            basis_channel_id = basis.get("channel_id")
            basis_archive_name = basis.get("archive_name")
            if not basis_channel_id or not basis_archive_name:
                raise SystemExit("Differential manifest is missing basis channel information.")

            print(f"basis_channel_id={basis_channel_id}")
            basis_messages = fetch_messages(
                session,
                api_base=args.api_base,
                channel_id=str(basis_channel_id),
                request_options=request_options,
            )
            basis_manifest_attachment = find_manifest_attachment(basis_messages, basis_archive_name)
            basis_manifest_data: dict | None = None
            if basis_manifest_attachment is not None:
                basis_manifest_data = json.loads(
                    download_small_attachment(session, basis_manifest_attachment, **request_options).decode("utf-8")
                )
            basis_restored_path, _ = download_archive_from_messages(
                session,
                messages=basis_messages,
                archive_name=basis_archive_name,
                expected_chunks=(basis_manifest_data or {}).get("upload", {}).get("chunk_count"),
                expected_size=(basis_manifest_data or {}).get("archive_size"),
                expected_sha256=(basis_manifest_data or {}).get("archive_sha256"),
                output_dir=args.output_dir,
                overwrite=args.overwrite,
                request_options=request_options,
            )
            final_extract_dir = args.extract_dir or default_extract_dir_for(restored_path)
            extract_dir = extract_archive(
                basis_restored_path,
                extract_dir=final_extract_dir,
                overwrite=args.overwrite,
            )
            extract_into_existing_directory(restored_path, extract_dir)

            deleted_name = manifest_data.get("deleted_paths_name")
            if deleted_name:
                deleted_attachment = find_attachment_by_name(messages, deleted_name)
                if deleted_attachment is None:
                    raise SystemExit(f"Deleted-paths attachment {deleted_name!r} is missing.")
                deleted_path = args.output_dir / deleted_name
                deleted_path.write_bytes(
                    download_small_attachment(session, deleted_attachment, **request_options)
                )
                expected_deleted_sha = manifest_data.get("deleted_paths_sha256")
                if expected_deleted_sha and sha256_file(deleted_path) != expected_deleted_sha:
                    raise SystemExit("Deleted-paths manifest SHA256 mismatch.")
                apply_deleted_paths(extract_dir, deleted_path)
            print(f"extracted_dir={extract_dir}")
            return 0

        if args.extract:
            extract_dir = extract_archive(
                restored_path,
                extract_dir=args.extract_dir,
                overwrite=args.overwrite,
            )
            print(f"extracted_dir={extract_dir}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
