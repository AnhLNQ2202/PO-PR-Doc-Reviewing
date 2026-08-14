from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


logger = logging.getLogger(__name__)
REQUEST_PREFIX = "po-pr-request-"
TESSERACT_PREFIX = "tess_"
STARTUP_CLEANUP_PREFIXES = (REQUEST_PREFIX, TESSERACT_PREFIX)


def prepare_temp_root(root: Path, stale_seconds: int) -> None:
    """Create TEMP_DIR and remove only safely identified abandoned workdirs."""

    root.mkdir(parents=True, exist_ok=True)
    root_resolved = root.resolve(strict=True)

    # Verify write/delete access without retaining data.
    handle, probe_name = tempfile.mkstemp(prefix=".ready-", dir=root_resolved)
    os.close(handle)
    Path(probe_name).unlink(missing_ok=True)

    cutoff = time.time() - stale_seconds
    removed = 0
    for child in root_resolved.iterdir():
        if not child.name.startswith(STARTUP_CLEANUP_PREFIXES):
            continue
        # Validate the lexical parent, not the symlink target. We never recurse
        # through symlinks.
        if child.parent.resolve(strict=True) != root_resolved:
            continue
        try:
            stat = child.lstat()
        except FileNotFoundError:
            continue
        if stat.st_mtime > cutoff:
            continue

        try:
            if child.is_symlink() or child.is_file():
                child.unlink(missing_ok=True)
            elif child.is_dir():
                shutil.rmtree(child)
            removed += 1
        except OSError:
            # Another replica/request may own the entry. Do not broaden scope
            # or retry destructively.
            logger.warning("event=stale_temp_cleanup_failed")
    if removed:
        logger.info("event=stale_temp_cleanup removed=%s", removed)


@contextmanager
def request_workdir(root: Path) -> Iterator[Path]:
    """A per-request directory that is removed on return, error, or cancel."""

    with tempfile.TemporaryDirectory(
        prefix=REQUEST_PREFIX,
        dir=root,
        ignore_cleanup_errors=False,
    ) as directory:
        yield Path(directory)
