"""
list_files action handler: a non-recursive listing of one registered,
symbolic directory key's immediate contents.

The caller never supplies a path - only a key already validated against
kernel/config/tools.yaml's list_files.approved_directories. The configured
root is canonicalized once per call, and every listed entry is
independently re-resolved and checked against that canonical root, so a
symlink, junction, or other reparse point cannot make an entry appear to
live outside the approved directory.
"""

import os
from pathlib import Path

from kernel.tools.types import ActionRequest, ActionResult

MAX_ENTRIES = 100


def _is_within(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def run(request: ActionRequest, tools_config) -> ActionResult:
    key = request.resource_key
    configured_path = tools_config.approved_directories.get(key)
    if configured_path is None:
        return ActionResult(False, "That directory is not registered.", "rejected")

    try:
        canonical_root = Path(configured_path).resolve(strict=True)
    except OSError:
        return ActionResult(False, "That directory is not available.", "failed")

    if not canonical_root.is_dir():
        return ActionResult(False, "That directory is not available.", "failed")

    names: list[str] = []
    truncated = False
    try:
        with os.scandir(canonical_root) as entries:
            for entry in entries:
                try:
                    resolved_entry = Path(entry.path).resolve(strict=True)
                except OSError:
                    # A broken symlink or an entry that vanished mid-scan -
                    # skip it rather than fail the whole listing.
                    continue
                if not _is_within(resolved_entry, canonical_root):
                    # Escapes the approved root (e.g. a symlink/junction
                    # pointing elsewhere) - never disclosed.
                    continue
                if len(names) >= MAX_ENTRIES:
                    truncated = True
                    break
                names.append(entry.name)
    except OSError:
        return ActionResult(False, "That directory is not available.", "failed")

    names.sort()
    if not names:
        return ActionResult(True, f"'{key}' is empty.", "executed")

    header = f"{len(names)} item(s) in '{key}'" + (" (showing first 100)" if truncated else "")
    return ActionResult(True, "\n".join([header, *names]), "executed")
