"""Zip a Roku channel source directory for sideloading."""

from __future__ import annotations

import fnmatch
import logging
import os
import zipfile
from pathlib import Path, PurePosixPath

from .config import ConfigError

log = logging.getLogger("rce_harness")


def _excluded(rel: PurePosixPath, patterns: list[str]) -> bool:
    """A pattern matches the relative POSIX path or any single component of it."""
    text = rel.as_posix()
    return any(fnmatch.fnmatch(text, pat) or any(fnmatch.fnmatch(part, pat) for part in rel.parts)
               for pat in patterns)


def zip_channel(src_dir: Path, exclude: list[str], dest: Path) -> Path:
    """Zip `src_dir` into `dest` with `manifest` at the archive root. Returns `dest`."""
    src_dir = Path(src_dir).resolve()
    dest = Path(dest).resolve()
    if not (src_dir / "manifest").is_file():
        raise ConfigError(f"no 'manifest' at {src_dir}; is channel.path the channel root?")

    count = 0
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(src_dir):
            root_path = Path(root)
            rel_root = PurePosixPath(root_path.relative_to(src_dir).as_posix())
            # Prune in place so excluded trees (node_modules, .git) are never walked.
            dirs[:] = sorted(d for d in dirs
                             if not (root_path / d).is_symlink() and not _excluded(rel_root / d, exclude))
            for name in sorted(files):
                full = root_path / name
                rel = rel_root / name
                if full.is_symlink() or full.resolve() == dest or _excluded(rel, exclude):
                    continue
                zf.write(full, rel.as_posix())
                count += 1
    log.debug("zipped %d files from %s into %s (%d bytes)", count, src_dir, dest, dest.stat().st_size)
    return dest
