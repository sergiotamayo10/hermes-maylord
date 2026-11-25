#!/usr/bin/env python3
"""Seed persona, memory and skills into HERMES_HOME without overwriting edits.

Copies every regular file under ``--seed`` (default ``/app/seed``) to the same
relative path under ``--home`` (default ``$HERMES_HOME``) **only if the target
does not exist**, so anything the owner has edited is never overwritten. A file
the owner deletes is seeded again on the next boot; to retire a seeded skill,
empty its contents instead of deleting it.

Safety: symlinks (in the seed tree or at any target path component) are refused,
matching start.sh's refusal to follow links inside /data/.hermes.

Stdlib only: it runs under the image's wrapper interpreter before server.py.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path


def seed(seed_dir: Path, home: Path) -> tuple[list[str], list[str]]:
    """Return (copied, skipped) relative paths."""
    copied: list[str] = []
    skipped: list[str] = []
    if not seed_dir.is_dir():
        return copied, skipped
    home = Path(home)
    for root, dirs, files in os.walk(seed_dir, followlinks=False):
        root_path = Path(root)
        dirs[:] = sorted(d for d in dirs if not (root_path / d).is_symlink())
        for name in sorted(files):
            src = root_path / name
            if src.is_symlink() or not src.is_file():
                skipped.append(str(src.relative_to(seed_dir)))
                continue
            rel = src.relative_to(seed_dir)
            dst = home / rel
            # Refuse a symlinked component anywhere between home and the target.
            probe = home
            blocked = False
            for part in rel.parts[:-1]:
                probe = probe / part
                if probe.is_symlink():
                    blocked = True
                    break
            if blocked or dst.is_symlink():
                skipped.append(str(rel))
                continue
            if dst.exists():
                skipped.append(str(rel))
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            os.chmod(dst, 0o600)
            copied.append(str(rel))
    return copied, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", default="/app/seed")
    parser.add_argument("--home", default=os.environ.get("HERMES_HOME", "/data/.hermes"))
    args = parser.parse_args(argv)
    copied, skipped = seed(Path(args.seed), Path(args.home))
    if copied:
        print(f"[seed-defaults] seeded {len(copied)} file(s): {', '.join(copied)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
