#!/usr/bin/env python3
"""Discord backup uploader.

Watches signal.txt. When the file contains any bytes, clears it, creates a
timestamped Discord channel under the Backups category, and uploads the newest
home_backup_*.tar.gz archive in restore-friendly chunks.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import tarfile
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, TypeVar

import aiohttp
import discord
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
SIGNAL_FILE = BASE_DIR / "signal.txt"
BACKUP_LOG = BASE_DIR / "backup.log"
BACKUP_PATTERN = "home_backup_*.tar.gz"

CATEGORY_NAME = "Backups"
CHANNEL_PREFIX = "backup"
CHUNK_SIZE = 10_000_000
CHUNK_LABEL = "10 MB"
UPLOAD_DELAY_SECONDS = 1
POLL_SECONDS = 15
MAX_DISCORD_MESSAGE = 2000
DISCORD_RETRY_ATTEMPTS = 6
DISCORD_RETRY_INITIAL_SECONDS = 10
DISCORD_RETRY_MAX_SECONDS = 300


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("backup-discord-bot")
T = TypeVar("T")


def human_size(num_bytes: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(num_bytes)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{num_bytes} B"
        size /= 1024
    return f"{num_bytes} B"


def parse_guild_id(raw_guild_id: str | None) -> int | None:
    if not raw_guild_id:
        return None
    try:
        return int(raw_guild_id)
    except ValueError as exc:
        raise RuntimeError("GUILD_ID must be a Discord snowflake integer") from exc


def append_backup_log(message: str) -> None:
    BACKUP_LOG.parent.mkdir(parents=True, exist_ok=True)
    with BACKUP_LOG.open("a", encoding="utf-8") as log_file:
        log_file.write(message)
        log_file.write("\n\n")


def signal_is_set_and_clear() -> bool:
    SIGNAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    SIGNAL_FILE.touch(exist_ok=True)

    if SIGNAL_FILE.stat().st_size == 0:
        return False

    with SIGNAL_FILE.open("r+b") as signal:
        signal.seek(0)
        signal.truncate()
    return True


def latest_backup() -> Path | None:
    backups = [path for path in BASE_DIR.glob(BACKUP_PATTERN) if path.is_file()]
    if not backups:
        return None
    return max(backups, key=lambda path: path.stat().st_mtime)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def count_archive_files(path: Path) -> tuple[int | None, int | None]:
    regular_files = 0
    total_entries = 0
    try:
        with tarfile.open(path, "r:gz") as archive:
            for member in archive:
                total_entries += 1
                if member.isfile():
                    regular_files += 1
    except (tarfile.TarError, OSError) as exc:
        logger.warning("Could not count archive members in %s: %s", path, exc)
        return None, None
    return regular_files, total_entries


def is_retryable_discord_error(exc: Exception) -> bool:
    if isinstance(exc, discord.HTTPException):
        status = getattr(exc, "status", None)
        if status is None:
            return True
        return status == 408 or status == 409 or status == 429 or status >= 500

    return isinstance(
        exc,
        (
            aiohttp.ClientError,
            asyncio.TimeoutError,
            ConnectionError,
            discord.ConnectionClosed,
        ),
    )


async def with_discord_retries(label: str, operation: Callable[[], Awaitable[T]]) -> T:
    delay = DISCORD_RETRY_INITIAL_SECONDS

    for attempt in range(1, DISCORD_RETRY_ATTEMPTS + 1):
        try:
            return await operation()
        except Exception as exc:
            if attempt == DISCORD_RETRY_ATTEMPTS or not is_retryable_discord_error(exc):
                logger.exception("%s failed permanently on attempt %s", label, attempt)
                raise

            logger.warning(
                "%s failed on attempt %s/%s with %s: %s; retrying in %s seconds",
                label,
                attempt,
                DISCORD_RETRY_ATTEMPTS,
                type(exc).__name__,
                exc,
                delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, DISCORD_RETRY_MAX_SECONDS)

    raise RuntimeError(f"{label} failed unexpectedly")


async def send_discord_message(channel: discord.TextChannel, content: str) -> discord.Message:
    return await with_discord_retries(
        f"send message to #{channel.name}",
        lambda: channel.send(content),
    )


async def send_discord_file(
    channel: discord.TextChannel, *, content: str, path: Path, filename: str
) -> discord.Message:
    async def send_once() -> discord.Message:
        with path.open("rb") as file:
            return await channel.send(
                content=content,
                file=discord.File(file, filename=filename),
            )

    return await with_discord_retries(f"upload {filename} to #{channel.name}", send_once)


async def get_target_guild(client: discord.Client) -> discord.Guild:
    guild_id = getattr(client, "guild_id", None)
    if guild_id is not None:
        guild = client.get_guild(guild_id)
        if guild is None:
            available = ", ".join(f"{guild.name} ({guild.id})" for guild in client.guilds)
            raise RuntimeError(
                f"GUILD_ID={guild_id} was not found in the bot's connected servers. "
                f"Available servers: {available or 'none'}"
            )
        return guild

    if not client.guilds:
        raise RuntimeError("The bot is not connected to any Discord servers.")
    return client.guilds[0]


async def get_or_create_backups_category(guild: discord.Guild) -> discord.CategoryChannel:
    async def get_or_create() -> discord.CategoryChannel:
        existing = discord.utils.get(guild.categories, name=CATEGORY_NAME)
        if existing:
            return existing
        return await guild.create_category(
            CATEGORY_NAME, reason="Create backup archive category"
        )

    return await with_discord_retries("get or create Backups category", get_or_create)


async def create_backup_channel(
    guild: discord.Guild, category: discord.CategoryChannel, timestamp: datetime
) -> discord.TextChannel:
    base_name = f"{CHANNEL_PREFIX}-{timestamp.strftime('%Y%m%d-%H%M%S')}"
    channel_name = base_name
    suffix = 2

    existing_names = {channel.name for channel in guild.text_channels}
    while channel_name in existing_names:
        channel_name = f"{base_name}-{suffix}"
        suffix += 1

    async def create_channel() -> discord.TextChannel:
        existing = discord.utils.get(
            guild.text_channels, name=channel_name, category=category
        )
        if existing:
            return existing

        return await guild.create_text_channel(
            channel_name,
            category=category,
            topic=f"Backup upload started {timestamp.isoformat(timespec='seconds')}",
            reason="Upload triggered by signal.txt",
        )

    return await with_discord_retries(f"create backup channel #{channel_name}", create_channel)


async def upload_archive_chunks(channel: discord.TextChannel, archive: Path) -> tuple[int, int]:
    archive_size = archive.stat().st_size
    total_chunks = max(1, math.ceil(archive_size / CHUNK_SIZE))
    width = len(str(total_chunks))

    with tempfile.TemporaryDirectory(prefix="discord-backup-chunks-") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        with archive.open("rb") as source:
            for index in range(1, total_chunks + 1):
                chunk = source.read(CHUNK_SIZE)
                chunk_name = f"{archive.name}.part{index:0{width}d}of{total_chunks:0{width}d}"
                chunk_path = temp_dir / chunk_name
                chunk_path.write_bytes(chunk)

                await send_discord_file(
                    channel,
                    content=f"Chunk {index}/{total_chunks}: `{chunk_name}`",
                    path=chunk_path,
                    filename=chunk_name,
                )
                chunk_path.unlink(missing_ok=True)

                if index < total_chunks:
                    await asyncio.sleep(UPLOAD_DELAY_SECONDS)

    return total_chunks, archive_size


def build_event_message(
    *,
    archive: Path,
    archive_size: int,
    archive_hash: str,
    regular_files: int | None,
    total_entries: int | None,
    chunk_count: int,
    channel: discord.TextChannel,
    started_at: datetime,
    finished_at: datetime,
    duration_seconds: float,
) -> str:
    file_count = "unknown" if regular_files is None else f"{regular_files:,}"
    entry_count = "unknown" if total_entries is None else f"{total_entries:,}"
    restore_command = f"cat '{archive.name}.part'* > '{archive.name}'"

    return "\n".join(
        (
            "Backup uploaded to Discord",
            f"Started: {started_at.isoformat(timespec='seconds')}",
            f"Finished: {finished_at.isoformat(timespec='seconds')}",
            f"Duration: {duration_seconds:.1f} seconds",
            f"Channel: #{channel.name}",
            f"Archive: {archive.name}",
            f"Archive path: {archive}",
            f"Size: {human_size(archive_size)} ({archive_size:,} bytes)",
            f"Archive files: {file_count}",
            f"Archive entries: {entry_count}",
            f"Chunks: {chunk_count:,} x up to {CHUNK_LABEL} ({CHUNK_SIZE:,} bytes)",
            f"SHA256: {archive_hash}",
            f"Restore: {restore_command}",
            "Verify: sha256sum the restored file and compare it with the SHA256 above.",
        )
    )


def split_discord_message(message: str) -> list[str]:
    if len(message) <= MAX_DISCORD_MESSAGE:
        return [message]

    chunks: list[str] = []
    current = ""
    for line in message.splitlines():
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > MAX_DISCORD_MESSAGE:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


async def handle_backup_signal(client: discord.Client) -> None:
    started_at = datetime.now().astimezone()
    monotonic_start = time.monotonic()

    guild = await get_target_guild(client)
    category = await get_or_create_backups_category(guild)
    channel = await create_backup_channel(guild, category, started_at)

    archive = latest_backup()
    if archive is None:
        message = "\n".join(
            (
                "Backup upload failed",
                f"Time: {started_at.isoformat(timespec='seconds')}",
                f"Channel: #{channel.name}",
                f"Reason: no files matching {BACKUP_PATTERN} in {BASE_DIR}",
            )
        )
        append_backup_log(message)
        await send_discord_message(channel, message)
        return

    await send_discord_message(
        channel, f"Uploading latest backup `{archive.name}` in {CHUNK_LABEL} chunks."
    )

    regular_files, total_entries = count_archive_files(archive)
    archive_hash = sha256_file(archive)
    chunk_count, archive_size = await upload_archive_chunks(channel, archive)

    finished_at = datetime.now().astimezone()
    message = build_event_message(
        archive=archive,
        archive_size=archive_size,
        archive_hash=archive_hash,
        regular_files=regular_files,
        total_entries=total_entries,
        chunk_count=chunk_count,
        channel=channel,
        started_at=started_at,
        finished_at=finished_at,
        duration_seconds=time.monotonic() - monotonic_start,
    )

    append_backup_log(message)
    for part in split_discord_message(message):
        await send_discord_message(channel, part)


async def signal_loop(client: discord.Client) -> None:
    await client.wait_until_ready()
    logger.info("Watching %s every %s seconds", SIGNAL_FILE, POLL_SECONDS)

    while not client.is_closed():
        try:
            if signal_is_set_and_clear():
                logger.info("Signal detected; starting backup upload")
                await handle_backup_signal(client)
        except Exception as exc:
            logger.exception("Backup upload failed")
            append_backup_log(
                "\n".join(
                    (
                        "Backup upload failed",
                        f"Time: {datetime.now().astimezone().isoformat(timespec='seconds')}",
                        f"Reason: {type(exc).__name__}: {exc}",
                    )
                )
            )

        await asyncio.sleep(POLL_SECONDS)


class BackupClient(discord.Client):
    def __init__(self, *, guild_id: int | None, **options: object) -> None:
        super().__init__(**options)
        self.guild_id = guild_id

    async def setup_hook(self) -> None:
        self.signal_task = asyncio.create_task(signal_loop(self))

    async def on_ready(self) -> None:
        guild_names = ", ".join(guild.name for guild in self.guilds) or "none"
        logger.info("Logged in as %s; connected guilds: %s", self.user, guild_names)


def main() -> None:
    load_dotenv(BASE_DIR / ".env")
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError(f"DISCORD_TOKEN is missing from {BASE_DIR / '.env'}")
    guild_id = parse_guild_id(os.getenv("GUILD_ID"))

    intents = discord.Intents.default()
    client = BackupClient(guild_id=guild_id, intents=intents)
    client.run(token)


if __name__ == "__main__":
    main()
