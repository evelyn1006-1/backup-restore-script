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

On a fresh machine, export a bot token that can read the backup channel, then run:

```bash
export DISCORD_TOKEN='...'
curl -fsSL https://raw.githubusercontent.com/evelyn1006-1/backup-restore-script/main/install_restore.sh | sh
```

That downloads the verified default backup chunks from Discord, recombines them, checks the SHA256, and extracts the backup into:

```bash
~/restored-home-backup-20260415_203026
```

The bootstrap script needs only `sh`, `curl`, `python3`, and `tar`. It does not need `pip`, `requests`, `python-dotenv`, `backup.log`, or `GUILD_ID`.

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
