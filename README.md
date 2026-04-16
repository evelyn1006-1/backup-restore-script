# Backup Restore Script

Small Discord-backed home-directory backup system.

It has three main pieces:

- `backup_home.sh` creates a timestamped `home_backup_YYYYMMDD_HHMMSS.tar.gz`.
- `app.py` watches `signal.txt`; when nonempty, it clears the signal, creates a Discord channel under `Backups`, uploads the newest archive in 10 MB chunks, and records a summary.
- `restore_from_discord.py` uses Discord's REST API to download chunk attachments, recombine them, verify the restored archive, and optionally extract it.

## Setup

Install dependencies:

```bash
python3 -m pip install -r requirements.txt
```

Create `.env` from the example:

```bash
cp .env.example .env
```

Set:

```bash
DISCORD_TOKEN=...
GUILD_ID=...
```

The bot needs Discord permissions for channel creation, sending messages, and attaching files.

## Backup

Run:

```bash
./backup_home.sh
```

The script is configured for `/home/evelyn` and `/home/evelyn/backups`; edit the constants at the top if deploying elsewhere.

To trigger a Discord upload after a backup exists:

```bash
printf '1' > signal.txt
```

## Bot Service

Install the systemd unit, then reload and start:

```bash
sudo install -o root -g root -m 0644 systemd/backup-discord-bot.service /etc/systemd/system/backup-discord-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now backup-discord-bot.service
```

Watch logs:

```bash
sudo journalctl -u backup-discord-bot.service -f
```

## Restore

On a fresh machine, export a bot token and the Discord backup target, then run:

```bash
export DISCORD_TOKEN='...'
export RESTORE_CHANNEL_ID='...'
curl -fsSL https://raw.githubusercontent.com/evelyn1006-1/backup-restore-script/main/install_restore.sh | sh
```

You can also use a guild ID and channel name instead of a channel ID:

```bash
export DISCORD_TOKEN='...'
export RESTORE_GUILD_ID='...'
export RESTORE_CHANNEL_NAME='backup-YYYYMMDD-HHMMSS'
curl -fsSL https://raw.githubusercontent.com/evelyn1006-1/backup-restore-script/main/install_restore.sh | sh
```

If `RESTORE_ARCHIVE_NAME` is not set, the bootstrap script scans the channel attachments and auto-detects a complete chunk set shaped like:

```text
some-archive-name.tar.gz.part01of51
some-archive-name.tar.gz.part02of51
...
```

If the channel contains multiple complete archives, set `RESTORE_ARCHIVE_NAME` explicitly:

```bash
export RESTORE_ARCHIVE_NAME='home_backup_YYYYMMDD_HHMMSS.tar.gz'
```

For integrity checks, the bootstrap script first looks for the bot's summary message in the same Discord channel and auto-detects:

```text
Size: ... (... bytes)
Chunks: ...
SHA256: ...
```

You can override those detected values with env vars:

```bash
export RESTORE_EXPECTED_CHUNKS='...'
export RESTORE_EXPECTED_SIZE='...'
export RESTORE_EXPECTED_SHA256='...'
```

The script downloads the chunks from Discord, recombines them, checks detected or supplied expected values, and extracts the backup into a directory in your home folder. The default extraction path is derived from the archive name, like:

```bash
~/restored-home_backup_YYYYMMDD_HHMMSS
```

The bootstrap script needs only `sh`, `curl`, `python3`, and `tar`. It does not need `pip`, `requests`, `python-dotenv`, `backup.log`, or a local copy of this repo.

Useful overrides:

```bash
RESTORE_EXTRACT_DIR="$HOME/restored-backup" \
RESTORE_OUTPUT_DIR="$HOME/discord-backup-restore" \
curl -fsSL https://raw.githubusercontent.com/evelyn1006-1/backup-restore-script/main/install_restore.sh | sh
```

Set `RESTORE_OVERWRITE=1` to replace an existing extraction directory.

If `backup.log` is present, the restore script can usually infer the latest channel, archive, size, chunk count, and SHA256:

```bash
./restore_from_discord.py
```

To also extract the restored archive:

```bash
./restore_from_discord.py --extract
```

Without `backup.log`, provide the target manually:

```bash
./restore_from_discord.py \
  --channel-name backup-YYYYMMDD-HHMMSS \
  --archive-name home_backup_YYYYMMDD_HHMMSS.tar.gz
```

You can use `--channel-id` instead of `--channel-name` when no `GUILD_ID` is available.
