"""Shared helpers: atomic writes, slugs, time."""

import os
import re
import tempfile
from datetime import datetime
from pathlib import Path


def atomic_write(path: Path, content: str) -> None:
    """Write via tmp file + os.replace so a crash never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def slugify(text: str) -> str:
    slug = re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')
    return slug[:64] or 'untitled'


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec='seconds')


def today_str() -> str:
    return datetime.now().strftime('%Y-%m-%d')
