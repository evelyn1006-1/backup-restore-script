#!/usr/bin/env python3
"""Discord backup uploader.

Watches signal.txt. When the file contains any bytes, clears it, creates a
timestamped Discord channel under the Backups category, and uploads the newest
home_backup_*.tar.gz archive in restore-friendly chunks.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import subprocess
import tarfile
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable, TypeVar

import aiohttp
import discord
from discord import app_commands
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
SIGNAL_FILE = BASE_DIR / "signal.txt"
BACKUP_LOG = BASE_DIR / "backup.log"
BACKUP_HELPER = BASE_DIR / "create_backup.py"
BACKUP_STATE_DIR = BASE_DIR / "state"
BACKUP_RESULT_FILE = BACKUP_STATE_DIR / "last_result.json"

CATEGORY_NAME = "Backups"
CHANNEL_PREFIX = "backup"
RESTORE_BOOTSTRAP_URL = (
    "https://raw.githubusercontent.com/"
    "evelyn1006-1/backup-restore-script/main/install_restore.sh"
)
CHUNK_SIZE = 10_000_000
CHUNK_LABEL = "10 MB"
UPLOAD_DELAY_SECONDS = 1
POLL_SECONDS = 15
MAX_DISCORD_MESSAGE = 2000
DISCORD_RETRY_ATTEMPTS = 6
DISCORD_RETRY_INITIAL_SECONDS = 10
DISCORD_RETRY_MAX_SECONDS = 300
BACKUP_WARNING_DELETE_AFTER_SECONDS = 8
BACKUP_WARNING_COOLDOWN_SECONDS = 30


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


def read_signal_command_and_clear() -> str | None:
    SIGNAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    SIGNAL_FILE.touch(exist_ok=True)

    if SIGNAL_FILE.stat().st_size == 0:
        return None

    with SIGNAL_FILE.open("r+b") as signal:
        contents = signal.read().decode("utf-8", errors="ignore")
        signal.seek(0)
        signal.truncate()
    commands = [char for char in contents if char in {"1", "2"}]
    return commands[-1] if commands else "1"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


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


async def create_backup_artifacts(mode: str) -> dict:
    if mode not in {"auto", "full"}:
        raise RuntimeError(f"Unsupported backup creation mode: {mode}")

    if not BACKUP_HELPER.exists():
        raise RuntimeError(f"Backup helper is missing: {BACKUP_HELPER}")

    command = [
        "python3",
        str(BACKUP_HELPER),
        "--mode",
        mode,
        "--retention-days",
        "7",
        "--pigz-processes",
        "3",
        "--require-uploaded-basis",
    ]

    completed = await asyncio.to_thread(
        subprocess.run,
        command,
        cwd=str(BASE_DIR),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.stderr.strip():
        logger.info("Backup helper stderr:\n%s", completed.stderr.strip())
    if completed.returncode != 0:
        raise RuntimeError(
            "Backup creation failed: "
            + (completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}")
        )

    try:
        payload = json.loads(completed.stdout.strip())
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Backup helper returned invalid JSON: {completed.stdout!r}") from exc

    BACKUP_STATE_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_RESULT_FILE.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


async def delete_discord_channel(channel: discord.abc.GuildChannel, *, reason: str) -> None:
    await with_discord_retries(
        f"delete channel #{channel.name}",
        lambda: channel.delete(reason=reason),
    )


def backup_channel_prefix(backup_type: str) -> str:
    return f"{CHANNEL_PREFIX}-full" if backup_type == "full" else f"{CHANNEL_PREFIX}-diff"


def upload_manifest_payload(
    manifest: dict,
    *,
    channel: discord.TextChannel,
    finished_at: datetime,
    chunk_count: int,
) -> dict:
    upload_info = {
        "channel_id": str(channel.id),
        "channel_name": channel.name,
        "category_name": channel.category.name if channel.category else None,
        "uploaded_at": finished_at.isoformat(timespec="seconds"),
        "chunk_count": chunk_count,
    }
    payload = json.loads(json.dumps(manifest))
    payload["upload"] = upload_info
    if payload.get("basis") is not None:
        payload["basis"].setdefault("channel_id", payload["basis"].get("channel_id"))
        payload["basis"].setdefault("channel_name", payload["basis"].get("channel_name"))
    return payload


def write_upload_manifest_file(manifest_payload: dict, destination: Path) -> None:
    destination.write_text(
        json.dumps(manifest_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def persist_manifest_update(manifest_path: Path, manifest_payload: dict) -> None:
    manifest_path.write_text(
        json.dumps(manifest_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    state_manifest_name = manifest_path.name.replace(".manifest.json", ".json")
    state_manifest_path = BACKUP_STATE_DIR / "manifests" / state_manifest_name
    state_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    state_manifest_path.write_text(
        json.dumps(manifest_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def apply_backup_channel_write_denials(
    overwrite: discord.PermissionOverwrite,
) -> discord.PermissionOverwrite:
    overwrite.send_messages = False
    overwrite.send_messages_in_threads = False
    overwrite.create_public_threads = False
    overwrite.create_private_threads = False
    return overwrite


async def set_backup_channel_write_protection(
    target: discord.abc.GuildChannel, guild: discord.Guild
) -> None:
    overwrite = apply_backup_channel_write_denials(
        target.overwrites_for(guild.default_role)
    )
    await with_discord_retries(
        f"set backup write protection on {target.name}",
        lambda: target.set_permissions(
            guild.default_role,
            overwrite=overwrite,
            reason="Keep backup channels read-only for restore safety",
        ),
    )


async def ensure_backup_write_protection(
    guild: discord.Guild, category: discord.CategoryChannel | None = None
) -> discord.CategoryChannel | None:
    category = category or discord.utils.get(guild.categories, name=CATEGORY_NAME)
    if category is None:
        return None

    await set_backup_channel_write_protection(category, guild)
    for channel in guild.text_channels:
        if channel.category_id == category.id and channel.name.startswith(f"{CHANNEL_PREFIX}-"):
            await set_backup_channel_write_protection(channel, guild)

    return category


def is_backup_text_channel(channel: discord.abc.GuildChannel | None) -> bool:
    return (
        isinstance(channel, discord.TextChannel)
        and channel.category is not None
        and channel.category.name == CATEGORY_NAME
        and channel.name.startswith(f"{CHANNEL_PREFIX}-")
    )


def build_backup_channel_warning_embed(channel: discord.TextChannel) -> discord.Embed:
    embed = discord.Embed(
        title="Backup Channel Warning",
        description=(
            "This channel is reserved for backup archive data. Messages here may be "
            "lost, ignored, or interfere with restore workflows."
        ),
        color=discord.Color.orange(),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="Please Use", value="Post in a normal chat channel instead.", inline=False)
    embed.add_field(name="Channel", value=f"`#{channel.name}`", inline=False)
    return embed


async def send_short_lived_backup_warning(message: discord.Message) -> None:
    channel = message.channel
    if not isinstance(channel, discord.TextChannel):
        return

    embed = build_backup_channel_warning_embed(channel)
    await with_discord_retries(
        f"warn user {message.author} in #{channel.name}",
        lambda: message.reply(
            embed=embed,
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
            delete_after=BACKUP_WARNING_DELETE_AFTER_SECONDS,
        ),
    )


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

    category = await with_discord_retries("get or create Backups category", get_or_create)
    await set_backup_channel_write_protection(category, guild)
    return category


async def create_backup_channel(
    guild: discord.Guild,
    category: discord.CategoryChannel,
    timestamp: datetime,
    *,
    prefix: str,
) -> discord.TextChannel:
    base_name = f"{prefix}-{timestamp.strftime('%Y%m%d-%H%M%S')}"
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

    channel = await with_discord_retries(f"create backup channel #{channel_name}", create_channel)
    await set_backup_channel_write_protection(channel, guild)
    return channel


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
    manifest: dict,
    archive: Path,
    chunk_count: int,
    channel: discord.TextChannel,
    started_at: datetime,
    finished_at: datetime,
    duration_seconds: float,
) -> str:
    archive_size = int(manifest["archive_size"])
    archive_hash = manifest["archive_sha256"]
    file_count = "unknown"
    if manifest.get("regular_files") is not None:
        file_count = f"{int(manifest['regular_files']):,}"
    entry_count = "unknown"
    if manifest.get("total_entries") is not None:
        entry_count = f"{int(manifest['total_entries']):,}"
    backup_type = manifest.get("backup_type", "full")
    deleted_paths_count = manifest.get("deleted_paths_count")
    restore_commands = (
        "Restore bootstrap:",
        "export DISCORD_TOKEN='your-bot-token'",
        f"export RESTORE_CHANNEL_ID='{channel.id}'",
        f"curl -fsSL {RESTORE_BOOTSTRAP_URL} | sh",
    )
    manual_restore_command = f"cat '{archive.name}.part'* > '{archive.name}'"

    details: list[str] = [
        "Backup uploaded to Discord",
        f"Type: {backup_type}",
        f"Started: {started_at.isoformat(timespec='seconds')}",
        f"Finished: {finished_at.isoformat(timespec='seconds')}",
        f"Duration: {duration_seconds:.1f} seconds",
        f"Channel: #{channel.name}",
        f"Archive: {archive.name}",
        f"Archive path: {archive}",
        f"Size: {human_size(archive_size)} ({archive_size:,} bytes)",
        f"Archive files: {file_count}",
        f"Archive entries: {entry_count}",
    ]
    if manifest.get("changed_paths_count") is not None:
        details.append(f"Changed paths: {int(manifest['changed_paths_count']):,}")
    if deleted_paths_count is not None:
        details.append(f"Deleted paths: {int(deleted_paths_count):,}")
    basis = manifest.get("basis")
    if basis:
        basis_line = basis.get("archive_name", "unknown")
        if basis.get("channel_name"):
            basis_line = f"#{basis['channel_name']} ({basis_line})"
        details.append(f"Basis full: {basis_line}")
    previous_differential = manifest.get("previous_differential")
    if previous_differential and previous_differential.get("archive_name"):
        previous_line = previous_differential["archive_name"]
        if previous_differential.get("channel_name"):
            previous_line = f"#{previous_differential['channel_name']} ({previous_line})"
        details.append(f"Previous differential: {previous_line}")

    details.extend(
        (
            f"Chunks: {chunk_count:,} x up to {CHUNK_LABEL} ({CHUNK_SIZE:,} bytes)",
            f"SHA256: {archive_hash}",
            *restore_commands,
            f"Manual recombine after downloading chunk files: {manual_restore_command}",
            "Verify: sha256sum the restored archive and compare it with the SHA256 above.",
        )
    )
    return "\n".join(details)


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


def summarize_channel_names(channels: list[discord.TextChannel], *, limit: int = 15) -> str:
    names = [f"`#{channel.name}`" for channel in channels[:limit]]
    if len(channels) > limit:
        names.append(f"... and {len(channels) - limit} more")
    return ", ".join(names) if names else "none"


@app_commands.command(
    name="purge_backups",
    description="Delete backup channels in Backups older than the specified number of days.",
)
@app_commands.guild_only()
@app_commands.default_permissions(manage_channels=True)
@app_commands.describe(
    days="Delete channels older than this many days. Use 0 to delete all backup channels.",
    dry_run="Preview which channels would be deleted without actually deleting them.",
)
async def purge_backups_command(
    interaction: discord.Interaction,
    days: app_commands.Range[int, 0, 3650],
    dry_run: bool = False,
) -> None:
    member = interaction.user
    permissions = getattr(member, "guild_permissions", None)
    if permissions is None or not permissions.manage_channels:
        await interaction.response.send_message(
            "You need `Manage Channels` to use this command.",
            ephemeral=True,
        )
        return

    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            "This command can only be used inside a Discord server.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    category = discord.utils.get(guild.categories, name=CATEGORY_NAME)
    if category is None:
        await interaction.followup.send(
            f"No `{CATEGORY_NAME}` category exists in this server.",
            ephemeral=True,
        )
        return

    cutoff = discord.utils.utcnow() - timedelta(days=days)
    threshold_label = (
        "all backup channels"
        if days == 0
        else f"backup channels older than {days} day(s)"
    )
    purge_candidates = sorted(
        [
            channel
            for channel in guild.text_channels
            if channel.category_id == category.id
            and channel.name.startswith(f"{CHANNEL_PREFIX}-")
            and channel.created_at <= cutoff
        ],
        key=lambda channel: channel.created_at,
    )

    if not purge_candidates:
        await interaction.followup.send(
            f"No {threshold_label} were found under `{CATEGORY_NAME}`.",
            ephemeral=True,
        )
        return

    if dry_run:
        await interaction.followup.send(
            "\n".join(
                (
                    "Dry run only; no channels were deleted.",
                    f"Would delete {len(purge_candidates)} channel(s) from {threshold_label}.",
                    f"Cutoff: {cutoff.isoformat(timespec='seconds')}",
                    f"Targets: {summarize_channel_names(purge_candidates)}",
                )
            ),
            ephemeral=True,
        )
        return

    deleted_names = [channel.name for channel in purge_candidates]
    reason = (
        f"Purged by {interaction.user} ({interaction.user.id}) "
        f"for being older than {days} day(s)"
    )

    for channel in purge_candidates:
        await delete_discord_channel(channel, reason=reason)

    timestamp = datetime.now().astimezone()
    append_backup_log(
        "\n".join(
            (
                "Backup channels purged",
                f"Time: {timestamp.isoformat(timespec='seconds')}",
                f"Requested by: {interaction.user} ({interaction.user.id})",
                f"Threshold: {threshold_label}",
                f"Days threshold: {days}",
                f"Cutoff: {cutoff.isoformat(timespec='seconds')}",
                f"Deleted channels: {len(deleted_names)}",
                f"Deleted names: {', '.join(f'#{name}' for name in deleted_names)}",
            )
        )
    )

    await interaction.followup.send(
        "\n".join(
            (
                f"Deleted {len(deleted_names)} channel(s) from {threshold_label}.",
                f"Cutoff: {cutoff.isoformat(timespec='seconds')}",
                f"Deleted: {', '.join(f'`#{name}`' for name in deleted_names[:15])}"
                + (f", ... and {len(deleted_names) - 15} more" if len(deleted_names) > 15 else ""),
            )
        ),
        ephemeral=True,
    )


async def handle_backup_signal(client: discord.Client, signal_command: str) -> None:
    started_at = datetime.now().astimezone()
    monotonic_start = time.monotonic()
    requested_mode = "full" if signal_command == "2" else "auto"

    artifacts = await create_backup_artifacts(requested_mode)
    archive = Path(artifacts["archive_path"])
    manifest_path = Path(artifacts["manifest_path"])
    manifest = load_json(manifest_path)
    deleted_paths_path = (
        Path(artifacts["deleted_paths_path"]) if artifacts.get("deleted_paths_path") else None
    )

    guild = await get_target_guild(client)
    category = await get_or_create_backups_category(guild)
    channel = await create_backup_channel(
        guild,
        category,
        started_at,
        prefix=backup_channel_prefix(manifest.get("backup_type", "full")),
    )

    await send_discord_message(
        channel,
        f"Uploading {manifest.get('backup_type', 'full')} backup `{archive.name}` in {CHUNK_LABEL} chunks.",
    )

    chunk_count, archive_size = await upload_archive_chunks(channel, archive)
    if deleted_paths_path is not None:
        await send_discord_file(
            channel,
            content=f"Deleted paths manifest: `{deleted_paths_path.name}`",
            path=deleted_paths_path,
            filename=deleted_paths_path.name,
        )

    finished_at = datetime.now().astimezone()
    upload_manifest = upload_manifest_payload(
        manifest,
        channel=channel,
        finished_at=finished_at,
        chunk_count=chunk_count,
    )
    with tempfile.TemporaryDirectory(prefix="discord-backup-manifest-") as temp_dir_name:
        temp_manifest = Path(temp_dir_name) / manifest_path.name
        write_upload_manifest_file(upload_manifest, temp_manifest)
        await send_discord_file(
            channel,
            content=f"Backup manifest: `{temp_manifest.name}`",
            path=temp_manifest,
            filename=temp_manifest.name,
        )

    persist_manifest_update(manifest_path, upload_manifest)
    message = build_event_message(
        manifest=upload_manifest,
        archive=archive,
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
            signal_command = read_signal_command_and_clear()
            if signal_command is not None:
                logger.info("Signal detected (%s); starting backup workflow", signal_command)
                await handle_backup_signal(client, signal_command)
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
        self.tree = app_commands.CommandTree(self)
        self.tree.add_command(purge_backups_command)
        self.backup_warning_times: dict[tuple[int, int], float] = {}

    def should_send_backup_warning(self, *, user_id: int, channel_id: int) -> bool:
        now = time.monotonic()
        cutoff = now - BACKUP_WARNING_COOLDOWN_SECONDS
        stale_keys = [
            key for key, warned_at in self.backup_warning_times.items() if warned_at < cutoff
        ]
        for key in stale_keys:
            del self.backup_warning_times[key]

        key = (user_id, channel_id)
        warned_at = self.backup_warning_times.get(key)
        if warned_at is not None and warned_at >= cutoff:
            return False

        self.backup_warning_times[key] = now
        return True

    async def setup_hook(self) -> None:
        self.signal_task = asyncio.create_task(signal_loop(self))
        if self.guild_id is not None:
            guild = discord.Object(id=self.guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            logger.info("Synced %s app command(s) to guild %s", len(synced), self.guild_id)
        else:
            synced = await self.tree.sync()
            logger.info("Synced %s global app command(s)", len(synced))

    async def on_ready(self) -> None:
        guild_names = ", ".join(guild.name for guild in self.guilds) or "none"
        logger.info("Logged in as %s; connected guilds: %s", self.user, guild_names)

        try:
            guild = await get_target_guild(self)
            await ensure_backup_write_protection(guild)
        except Exception:
            logger.exception("Failed to enforce backup channel write protection")

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        if not is_backup_text_channel(message.channel):
            return
        if not self.should_send_backup_warning(
            user_id=message.author.id, channel_id=message.channel.id
        ):
            return

        try:
            await send_short_lived_backup_warning(message)
        except Exception:
            logger.exception(
                "Failed to warn %s about posting in backup channel #%s",
                message.author,
                getattr(message.channel, "name", "unknown"),
            )


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
