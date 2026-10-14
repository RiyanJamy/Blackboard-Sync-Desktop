"""Stage successful downloads, compare bytes, preserve replaced local copies."""
import hashlib
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def digest(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


class FileTooLarge(Exception):
    def __init__(self, size):
        self.size = size
        super().__init__('Downloaded file exceeded the configured size limit.')


def safe_download(client, course_id, item_id, attachment_id, target, max_file_bytes=0):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='bbsync-') as tmp:
        staged = Path(tmp) / target.name
        client.download(course_id, item_id, attachment_id, staged)
        if max_file_bytes and staged.stat().st_size > max_file_bytes:
            raise FileTooLarge(staged.stat().st_size)
        install_staged(staged, target)


def install_staged(staged, target):
    target = Path(target)
    staged = Path(staged)
    target.parent.mkdir(parents=True, exist_ok=True)
    before = digest(target) if target.exists() else None
    if before == digest(staged):
        return
    # Copy on the destination volume, so replacement is atomic.
    pending = target.with_name(target.name + '.' + uuid4().hex + '.pending')
    try:
        shutil.copyfile(staged, pending)
        if target.exists():
            if digest(target) != before:
                raise RuntimeError(f'File edited during download; leaving untouched: {target}')
            backups = target.parent / '_bbsync_backups'
            backups.mkdir(exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
            backup = backups / (target.stem + '.' + stamp + '.' + uuid4().hex[:8] + target.suffix)
            shutil.copy2(target, backup)
            if digest(backup) != before or digest(target) != before:
                raise RuntimeError(f'File edited during backup; leaving untouched: {target}')
        pending.replace(target)
    finally:
        pending.unlink(missing_ok=True)
