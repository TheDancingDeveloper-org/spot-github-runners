#!/usr/bin/env python3
"""Remove only the completed OxiDex job's default Cargo build directory."""

import os
import shutil
import stat
import sys
from pathlib import Path


def cleanup(work_root: Path, env: dict[str, str]) -> bool:
    if env.get("OXIDEX_CLEAN_TARGET") != "1":
        print("OxiDex target cleanup disabled")
        return False
    if env.get("GITHUB_REPOSITORY") != "swack-tools/oxidex":
        print("OxiDex target cleanup skipped: other repository")
        return False
    expected = work_root / "oxidex" / "oxidex"
    if env.get("GITHUB_WORKSPACE") != str(expected):
        print("OxiDex target cleanup skipped: unexpected workspace")
        return False
    if not shutil.rmtree.avoids_symlink_attacks:
        raise RuntimeError("safe directory removal is unavailable")

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root_fd = os.open(work_root, flags)
    try:
        repo_fd = os.open("oxidex", flags, dir_fd=root_fd)
        try:
            workspace_fd = os.open("oxidex", flags, dir_fd=repo_fd)
            try:
                before = shutil.disk_usage(work_root).free
                try:
                    target = os.stat("target", dir_fd=workspace_fd, follow_symlinks=False)
                except FileNotFoundError:
                    print(f"OxiDex target absent; free bytes={before}")
                    return False
                if not stat.S_ISDIR(target.st_mode):
                    raise RuntimeError("target is not a directory")
                # rmtree uses dirfd-relative, fd-based traversal and never follows links.
                shutil.rmtree("target", dir_fd=workspace_fd)
                after = shutil.disk_usage(work_root).free
                print(f"Removed {expected}/target; free bytes before={before} after={after}")
                return True
            finally:
                os.close(workspace_fd)
        finally:
            os.close(repo_fd)
    finally:
        os.close(root_fd)


if __name__ == "__main__":
    try:
        cleanup(Path("/work"), os.environ)
    except (OSError, RuntimeError) as exc:
        print(f"OxiDex target cleanup failed: {exc}", file=sys.stderr)
        sys.exit(1)
