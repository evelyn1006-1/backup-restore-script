#!/usr/bin/env python3
"""Create full or differential backup artifacts for the home directory."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatchcase
from pathlib import Path
from stat import S_IMODE


BASE_DIR = Path(__file__).resolve().parent
BACKUP_ROOT_DIR = BASE_DIR
ARTIFACTS_DIR = BACKUP_ROOT_DIR / "artifacts"
HOME_DIR = Path("/home/evelyn")
STATE_DIR = BACKUP_ROOT_DIR / "state"
INDEX_DIR = STATE_DIR / "indices"
MANIFEST_DIR = STATE_DIR / "manifests"
OWNERSHIP_REPAIRED = False
ARCHIVE_CHANGE_RETRIES = 3
ARCHIVE_CHANGE_RETRY_DELAY_SECONDS = 5

EXCLUDE_PATTERNS = (
    ".cache",
    ".npm/_cacache",
    ".local/share/Trash",
    ".local/share/code-server/extensions",
    ".local/share/code-server/logs",
    ".local/share/code-server/CachedProfilesData",
    ".local/share/code-server/CachedExtensionVSIXs",
    ".local/share/code-server/coder-logs",
    ".local/share/code-server/code-server-ipc.sock",
    ".local/share/code-server/heartbeat",
    ".local/share/claude/versions",
    ".local/state/claude/locks",
    "backups/artifacts",
    "backups/__pycache__",
    "discord-backup-restore",
    "restored-home_backup_*",
    "homepage/static/videos",
    ".gunicorn",
    "bootstrap",
    ".rustup",
    ".julia",
    ".u2net",
)


@dataclass
class BasisManifest:
    path: Path
    data: dict


def parser() -> argparse.ArgumentParser:
    arg_parser = argparse.ArgumentParser(
        description="Create a full or differential backup artifact in /home/evelyn/backups/artifacts.",
    )
    arg_parser.add_argument(
        "--mode",
        choices=("full", "differential", "auto"),
        default="full",
    )
    arg_parser.add_argument("--retention-days", type=int, default=30)
    arg_parser.add_argument(
        "--require-uploaded-basis",
        action="store_true",
        help="Only use a basis full backup if it already has upload metadata.",
    )
    arg_parser.add_argument("--pigz-processes", type=int, default=3)
    return arg_parser


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def local_now() -> datetime:
    return datetime.now().astimezone()


def timestamp_label(moment: datetime | None = None) -> str:
    return (moment or local_now()).strftime("%Y%m%d_%H%M%S")


def relative_label(path: Path) -> str:
    return f"./{path.as_posix()}" if path.parts else "."


def is_excluded(relative_path: Path) -> bool:
    if not relative_path.parts:
        return False
    relative_posix = relative_path.as_posix()
    return any(
        fnmatchcase(relative_posix, pattern)
        or relative_posix.startswith(f"{pattern}/")
        for pattern in EXCLUDE_PATTERNS
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def ensure_home_dir_ownership() -> None:
    global OWNERSHIP_REPAIRED
    owner = pwd.getpwnam(HOME_DIR.name)
    target_owner = f"{owner.pw_uid}:{owner.pw_gid}"
    completed = subprocess.run(
        ["sudo", "chown", "-hR", target_owner, str(HOME_DIR)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        stdout = completed.stdout.strip()
        details = stderr or stdout or "unknown chown failure"
        raise RuntimeError(f"Failed to force ownership of {HOME_DIR}: {details}")
    OWNERSHIP_REPAIRED = True


def copy_with_home_ownership_retry(source_path: Path, target_path: Path) -> None:
    try:
        shutil.copy2(source_path, target_path)
    except PermissionError:
        if OWNERSHIP_REPAIRED:
            raise
        ensure_home_dir_ownership()
        shutil.copy2(source_path, target_path)


def run_tar_with_pigz_once(
    *, source_dir: Path, archive_path: Path, excludes: tuple[str, ...], pigz_processes: int
) -> tuple[int, int, str, str]:
    tar_command = ["tar", "-C", str(source_dir)]
    for prefix in excludes:
        tar_command.append(f"--exclude=./{prefix}")
    tar_command.extend(["-cf", "-", "."])

    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with archive_path.open("wb") as output:
        tar_process = subprocess.Popen(
            tar_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
        )
        assert tar_process.stdout is not None

        pigz_process = subprocess.Popen(
            ["pigz", "-6", "-p", str(pigz_processes)],
            stdin=tar_process.stdout,
            stdout=output,
            stderr=subprocess.PIPE,
            text=False,
        )
        tar_process.stdout.close()

        tar_stderr = tar_process.stderr.read().decode("utf-8", errors="ignore") if tar_process.stderr else ""
        pigz_stderr = pigz_process.stderr.read().decode("utf-8", errors="ignore") if pigz_process.stderr else ""
        pigz_return = pigz_process.wait()
        tar_return = tar_process.wait()

    return tar_return, pigz_return, tar_stderr, pigz_stderr


def tar_run_succeeded(*, tar_return: int, pigz_return: int, tar_stderr: str) -> bool:
    nonfatal_tar_warning = (
        tar_return == 1
        and tar_stderr.strip()
        and all("file changed as we read it" in line for line in tar_stderr.splitlines() if line.strip())
    )
    return (tar_return == 0 or nonfatal_tar_warning) and pigz_return == 0


def tar_failure_mentions_permission_denied(*, tar_stderr: str, pigz_stderr: str) -> bool:
    combined = "\n".join(part for part in (tar_stderr, pigz_stderr) if part)
    return "Permission denied" in combined


def tar_failure_is_retryable_file_change(*, tar_return: int, pigz_return: int, tar_stderr: str) -> bool:
    if tar_return != 1 or pigz_return != 0 or not tar_stderr.strip():
        return False

    retryable_fragments = (
        "file changed as we read it",
        "File shrank by",
    )
    return all(
        any(fragment in line for fragment in retryable_fragments)
        for line in tar_stderr.splitlines()
        if line.strip()
    )


def format_tar_failure(*, tar_stderr: str, pigz_stderr: str) -> RuntimeError:
    return RuntimeError(
        "Archive creation failed.\n"
        + (tar_stderr.strip() + "\n" if tar_stderr.strip() else "")
        + (pigz_stderr.strip() if pigz_stderr.strip() else "")
    )


def build_index_with_home_ownership_retry(root: Path) -> dict[str, dict]:
    try:
        return build_index(root)
    except PermissionError:
        if root != HOME_DIR or OWNERSHIP_REPAIRED:
            raise
        ensure_home_dir_ownership()
        return build_index(root)


def build_index(root: Path) -> dict[str, dict]:
    entries: dict[str, dict] = {}

    def walk(current_root: Path, relative_root: Path) -> None:
        with os.scandir(current_root) as scanner:
            children = sorted(scanner, key=lambda item: item.name)

        for child in children:
            relative_child = relative_root / child.name if relative_root.parts else Path(child.name)
            if is_excluded(relative_child):
                continue

            stat = child.stat(follow_symlinks=False)
            key = relative_label(relative_child)
            common = {
                "mode": S_IMODE(stat.st_mode),
                "mtime_ns": stat.st_mtime_ns,
            }

            if child.is_symlink():
                entries[key] = {
                    "type": "symlink",
                    "target": os.readlink(child.path),
                    **common,
                }
                continue

            if child.is_dir(follow_symlinks=False):
                entries[key] = {"type": "dir", **common}
                walk(Path(child.path), relative_child)
                continue

            if child.is_file(follow_symlinks=False):
                entries[key] = {"type": "file", "size": stat.st_size, **common}

    walk(root, Path())
    return entries


def run_tar_with_pigz(*, source_dir: Path, archive_path: Path, excludes: tuple[str, ...], pigz_processes: int) -> None:
    max_attempts = ARCHIVE_CHANGE_RETRIES + 1

    for attempt in range(1, max_attempts + 1):
        tar_return, pigz_return, tar_stderr, pigz_stderr = run_tar_with_pigz_once(
            source_dir=source_dir,
            archive_path=archive_path,
            excludes=excludes,
            pigz_processes=pigz_processes,
        )
        if tar_run_succeeded(
            tar_return=tar_return,
            pigz_return=pigz_return,
            tar_stderr=tar_stderr,
        ):
            return

        if (
            source_dir == HOME_DIR
            and not OWNERSHIP_REPAIRED
            and tar_failure_mentions_permission_denied(
                tar_stderr=tar_stderr,
                pigz_stderr=pigz_stderr,
            )
        ):
            archive_path.unlink(missing_ok=True)
            ensure_home_dir_ownership()
            tar_return, pigz_return, tar_stderr, pigz_stderr = run_tar_with_pigz_once(
                source_dir=source_dir,
                archive_path=archive_path,
                excludes=excludes,
                pigz_processes=pigz_processes,
            )
            if tar_run_succeeded(
                tar_return=tar_return,
                pigz_return=pigz_return,
                tar_stderr=tar_stderr,
            ):
                return

        if (
            attempt < max_attempts
            and tar_failure_is_retryable_file_change(
                tar_return=tar_return,
                pigz_return=pigz_return,
                tar_stderr=tar_stderr,
            )
        ):
            archive_path.unlink(missing_ok=True)
            print(
                "Archive input changed during read; "
                f"retrying attempt {attempt + 1}/{max_attempts}",
                file=sys.stderr,
            )
            time.sleep(ARCHIVE_CHANGE_RETRY_DELAY_SECONDS)
            continue

        archive_path.unlink(missing_ok=True)
        raise format_tar_failure(tar_stderr=tar_stderr, pigz_stderr=pigz_stderr)


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_manifest_copies(payload: dict, *paths: Path) -> None:
    for path in paths:
        write_json(path, payload)


def load_manifest(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def manifest_created_at(manifest: dict) -> datetime:
    return datetime.fromisoformat(manifest["created_at"])


def manifest_path_for(stem: str) -> Path:
    return ARTIFACTS_DIR / f"{stem}.manifest.json"


def index_path_for(stem: str) -> Path:
    return INDEX_DIR / f"{stem}.index.json"


def deleted_paths_path_for(stem: str) -> Path:
    return ARTIFACTS_DIR / f"{stem}.deleted.txt"


def archive_path_for(archive_name: str) -> Path:
    return ARTIFACTS_DIR / archive_name


def list_known_manifests() -> list[BasisManifest]:
    manifests = []
    for path in sorted(MANIFEST_DIR.glob("*.json")):
        try:
            manifests.append(BasisManifest(path=path, data=load_manifest(path)))
        except Exception:
            continue
    return manifests


def latest_full_manifest(*, max_age_days: int, require_uploaded: bool) -> BasisManifest | None:
    cutoff = utc_now() - timedelta(days=max_age_days)
    candidates: list[BasisManifest] = []

    for manifest in list_known_manifests():
        data = manifest.data
        if data.get("backup_type") != "full":
            continue
        if manifest_created_at(data) < cutoff:
            continue
        index_name = data.get("local", {}).get("index_name")
        if not index_name:
            continue
        if not (INDEX_DIR / index_name).exists():
            continue
        if require_uploaded and not data.get("upload", {}).get("channel_id"):
            continue
        candidates.append(manifest)

    if not candidates:
        return None

    return max(candidates, key=lambda item: manifest_created_at(item.data))


def latest_uploaded_diff_for_basis(basis_archive_name: str) -> dict | None:
    candidates = []
    for manifest in list_known_manifests():
        data = manifest.data
        if data.get("backup_type") != "differential":
            continue
        if data.get("basis", {}).get("archive_name") != basis_archive_name:
            continue
        if not data.get("upload", {}).get("channel_id"):
            continue
        candidates.append(data)

    if not candidates:
        return None

    return max(candidates, key=manifest_created_at)


def path_from_relative_label(label: str) -> Path:
    return Path(label[2:] if label.startswith("./") else label)


def stage_changed_entry(*, relative_label_path: str, entry: dict, stage_root: Path) -> None:
    relative_path = path_from_relative_label(relative_label_path)
    source_path = HOME_DIR / relative_path
    target_path = stage_root / relative_path
    target_path.parent.mkdir(parents=True, exist_ok=True)

    if entry["type"] == "dir":
        target_path.mkdir(parents=True, exist_ok=True)
    elif entry["type"] == "symlink":
        target_path.unlink(missing_ok=True)
        os.symlink(os.readlink(source_path), target_path)
    elif entry["type"] == "file":
        copy_with_home_ownership_retry(source_path, target_path)


def create_full_backup(*, pigz_processes: int) -> dict:
    created_at = local_now()
    stem = f"home_backup_full_{timestamp_label(created_at)}"
    archive_name = f"{stem}.tar.gz"
    archive_path = archive_path_for(archive_name)
    manifest_path = manifest_path_for(stem)
    state_manifest_path = MANIFEST_DIR / f"{stem}.json"
    index_path = index_path_for(stem)

    index = build_index_with_home_ownership_retry(HOME_DIR)
    run_tar_with_pigz(
        source_dir=HOME_DIR,
        archive_path=archive_path,
        excludes=EXCLUDE_PATTERNS,
        pigz_processes=pigz_processes,
    )

    archive_size = archive_path.stat().st_size
    archive_sha256 = sha256_file(archive_path)
    write_json(index_path, index)

    manifest = {
        "schema_version": 1,
        "backup_type": "full",
        "created_at": created_at.isoformat(timespec="seconds"),
        "archive_name": archive_name,
        "archive_size": archive_size,
        "archive_sha256": archive_sha256,
        "regular_files": sum(1 for entry in index.values() if entry["type"] == "file"),
        "total_entries": len(index),
        "changed_paths_count": None,
        "deleted_paths_count": None,
        "basis": None,
        "previous_differential": None,
        "upload": {},
        "local": {
            "archive_path": str(archive_path),
            "manifest_name": manifest_path.name,
            "index_name": index_path.name,
        },
    }
    write_manifest_copies(manifest, manifest_path, state_manifest_path)
    return {
        "mode_used": "full",
        "archive_path": str(archive_path),
        "manifest_path": str(manifest_path),
        "deleted_paths_path": None,
    }


def create_differential_backup(*, basis: BasisManifest, pigz_processes: int) -> dict:
    created_at = local_now()
    stem = f"home_backup_diff_{timestamp_label(created_at)}"
    archive_name = f"{stem}.tar.gz"
    archive_path = archive_path_for(archive_name)
    manifest_path = manifest_path_for(stem)
    state_manifest_path = MANIFEST_DIR / f"{stem}.json"
    deleted_paths_path = deleted_paths_path_for(stem)

    basis_index_path = INDEX_DIR / basis.data["local"]["index_name"]
    basis_index = load_manifest(basis_index_path)
    current_index = build_index_with_home_ownership_retry(HOME_DIR)

    changed_paths: list[str] = []
    for path, entry in current_index.items():
        if basis_index.get(path) != entry:
            changed_paths.append(path)

    deleted_paths = sorted(path for path in basis_index if path not in current_index)

    with tempfile.TemporaryDirectory(prefix="backup-diff-stage-") as temp_dir_name:
        stage_root = Path(temp_dir_name) / "staging"
        stage_root.mkdir(parents=True, exist_ok=True)

        for relative_label_path in changed_paths:
            if relative_label_path == ".":
                continue
            stage_changed_entry(
                relative_label_path=relative_label_path,
                entry=current_index[relative_label_path],
                stage_root=stage_root,
            )

        deleted_paths_path.write_text(
            "".join(f"{path}\n" for path in deleted_paths),
            encoding="utf-8",
        )

        run_tar_with_pigz(
            source_dir=stage_root,
            archive_path=archive_path,
            excludes=(),
            pigz_processes=pigz_processes,
        )

    archive_size = archive_path.stat().st_size
    archive_sha256 = sha256_file(archive_path)
    deleted_paths_sha256 = sha256_file(deleted_paths_path)
    previous_differential = latest_uploaded_diff_for_basis(basis.data["archive_name"])

    manifest = {
        "schema_version": 1,
        "backup_type": "differential",
        "created_at": created_at.isoformat(timespec="seconds"),
        "archive_name": archive_name,
        "archive_size": archive_size,
        "archive_sha256": archive_sha256,
        "regular_files": sum(
            1 for path in changed_paths if current_index[path]["type"] == "file"
        ),
        "total_entries": len(changed_paths),
        "changed_paths_count": len(changed_paths),
        "deleted_paths_count": len(deleted_paths),
        "deleted_paths_name": deleted_paths_path.name,
        "deleted_paths_sha256": deleted_paths_sha256,
        "basis": {
            "backup_type": "full",
            "archive_name": basis.data["archive_name"],
            "archive_sha256": basis.data["archive_sha256"],
            "created_at": basis.data["created_at"],
            "channel_id": basis.data.get("upload", {}).get("channel_id"),
            "channel_name": basis.data.get("upload", {}).get("channel_name"),
            "manifest_name": basis.data["local"]["manifest_name"],
        },
        "previous_differential": None
        if previous_differential is None
        else {
            "archive_name": previous_differential["archive_name"],
            "channel_id": previous_differential.get("upload", {}).get("channel_id"),
            "channel_name": previous_differential.get("upload", {}).get("channel_name"),
            "created_at": previous_differential["created_at"],
        },
        "upload": {},
        "local": {
            "archive_path": str(archive_path),
            "manifest_name": manifest_path.name,
            "index_name": None,
        },
    }
    write_manifest_copies(manifest, manifest_path, state_manifest_path)
    return {
        "mode_used": "differential",
        "archive_path": str(archive_path),
        "manifest_path": str(manifest_path),
        "deleted_paths_path": str(deleted_paths_path),
    }


def main() -> int:
    args = parser().parse_args()

    if shutil.which("pigz") is None:
        raise SystemExit("pigz is required for backup creation.")

    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)

    basis = latest_full_manifest(
        max_age_days=args.retention_days,
        require_uploaded=args.require_uploaded_basis,
    )
    mode = args.mode
    if mode == "auto":
        mode = "differential" if basis is not None else "full"

    if mode == "differential":
        if basis is None:
            mode = "full"
        else:
            result = create_differential_backup(
                basis=basis,
                pigz_processes=args.pigz_processes,
            )
            print(json.dumps(result))
            return 0

    result = create_full_backup(pigz_processes=args.pigz_processes)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
