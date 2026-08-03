"""
Secure source-root validation, deterministic traversal, and race-resistant
file reading for kernel/knowledge_base/ (Milestone 36).

A caller (ingest.py) may only supply a symbolic source key, already
resolved by config.py to a SourceSpec(path, recursive). Everything in
this module operates on that one configured absolute path - never on a
caller-supplied path, glob, or extension list.

Identity checks always inspect the *unresolved* path first (os.lstat),
before ever calling Path.resolve() - resolving first and asking whether
the original was a symlink afterward would already have lost that
information. This applies to the configured source root itself and to
every candidate discovered underneath it.

Any symlink, junction, reparse point, or other special file encountered
anywhere during traversal - the root, a subdirectory, or a candidate file
- causes the whole source to be rejected. Nothing here silently follows a
link or partially ingests a source that traversal couldn't fully trust.
Unsupported file extensions are the one case that is genuinely ignored
(not rejected): a stray `.png` sitting in an approved docs tree is simply
outside this milestone's scope, not a safety violation.
"""

import os
import stat as stat_module
from dataclasses import dataclass
from pathlib import Path

from kernel.knowledge_base.types import (
    DatabaseUnavailableError,
    InvalidSourceContentError,
    SourceLimitExceededError,
    SourceUnavailableError,
)

SUPPORTED_SUFFIXES = {".md", ".txt"}

MAX_FILES_PER_SOURCE = 5_000
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_SOURCE_BYTES = 500 * 1024 * 1024
MAX_RECURSION_DEPTH = 16

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_FILE_ATTRIBUTE_HIDDEN = 0x2
_FILE_ATTRIBUTE_SYSTEM = 0x4


def _is_reparse_point(lst: os.stat_result) -> bool:
    attrs = getattr(lst, "st_file_attributes", None)
    if attrs is None:
        return False
    return bool(attrs & _FILE_ATTRIBUTE_REPARSE_POINT)


def _is_windows_hidden_or_system(lst: os.stat_result) -> bool:
    attrs = getattr(lst, "st_file_attributes", None)
    if attrs is None:
        return False
    return bool(attrs & (_FILE_ATTRIBUTE_HIDDEN | _FILE_ATTRIBUTE_SYSTEM))


def _classify(lst: os.stat_result) -> str:
    """Classify a stat/lstat result into one of: "symlink", "reparse",
    "directory", "regular", "special". Always called with a *not-followed*
    stat (os.lstat or entry.stat(follow_symlinks=False) or os.fstat of an
    O_NOFOLLOW-opened descriptor) - never with a followed stat."""

    mode = lst.st_mode
    if stat_module.S_ISLNK(mode):
        return "symlink"
    if _is_reparse_point(lst):
        return "reparse"
    if stat_module.S_ISDIR(mode):
        return "directory"
    if stat_module.S_ISREG(mode):
        return "regular"
    return "special"


def resolve_canonical_root(configured_path: str) -> Path:
    """Validate and canonicalize one configured source root. Inspects the
    *original* (unresolved) path's identity first, then resolves it
    strictly. Raises SourceUnavailableError for anything unsafe or
    missing."""

    original = Path(configured_path)
    try:
        lst = os.lstat(original)
    except OSError as exc:
        raise SourceUnavailableError("source root is not available") from exc

    kind = _classify(lst)
    if kind not in ("regular", "directory"):
        raise SourceUnavailableError("source root has an unsafe identity")

    try:
        canonical = original.resolve(strict=True)
    except OSError as exc:
        raise SourceUnavailableError("source root is not available") from exc

    if kind == "directory" and not canonical.is_dir():
        raise SourceUnavailableError("source root is not available")
    if kind == "regular" and not canonical.is_file():
        raise SourceUnavailableError("source root is not available")

    return canonical


def validate_existing_directory(configured_path: Path) -> Path:
    """Validate that `configured_path` (already absolute) exists, is an
    actual directory, and is not a symlink/junction/reparse point/special
    file. Used by db.py to validate the configured knowledge storage
    directory before placing the SQLite database inside it. Never creates
    the directory."""

    try:
        lst = os.lstat(configured_path)
    except OSError as exc:
        raise DatabaseUnavailableError(
            "knowledge storage directory is not available"
        ) from exc

    if _classify(lst) != "directory":
        raise DatabaseUnavailableError("knowledge storage directory is not available")

    try:
        canonical = configured_path.resolve(strict=True)
    except OSError as exc:
        raise DatabaseUnavailableError(
            "knowledge storage directory is not available"
        ) from exc

    if not canonical.is_dir():
        raise DatabaseUnavailableError("knowledge storage directory is not available")

    return canonical


@dataclass(frozen=True)
class CandidateFile:
    """One approved, in-scope, safety-checked candidate discovered during
    traversal. `relative_path` is always POSIX-form and relative to the
    source's canonical root - never absolute."""

    canonical_path: Path
    relative_path: str
    lstat: os.stat_result


class _TraversalState:
    def __init__(self) -> None:
        self.candidates: list[CandidateFile] = []
        self.total_bytes = 0


def _walk_directory(
    current_dir: Path,
    canonical_root: Path,
    recursive: bool,
    depth: int,
    state: _TraversalState,
) -> None:
    if depth > MAX_RECURSION_DEPTH:
        raise SourceLimitExceededError("source exceeds the recursion depth limit")

    try:
        entries = sorted(os.scandir(current_dir), key=lambda e: e.name)
    except OSError as exc:
        raise SourceUnavailableError("source is not available") from exc

    for entry in entries:
        name = entry.name
        if name.startswith("."):
            continue

        try:
            lst = entry.stat(follow_symlinks=False)
        except OSError:
            # Vanished mid-scan - skip rather than fail the whole source.
            continue

        if _is_windows_hidden_or_system(lst):
            continue

        kind = _classify(lst)

        if kind in ("symlink", "reparse"):
            raise SourceUnavailableError("source contains an unsafe file")

        if kind == "directory":
            if not recursive:
                continue
            child_path = Path(entry.path)
            try:
                child_canonical = child_path.resolve(strict=True)
            except OSError as exc:
                raise SourceUnavailableError("source is not available") from exc
            if not (
                child_canonical == canonical_root
                or child_canonical.is_relative_to(canonical_root)
            ):
                raise SourceUnavailableError("source escapes the approved root")
            _walk_directory(child_path, canonical_root, recursive, depth + 1, state)
            continue

        if kind == "regular":
            suffix = Path(name).suffix.lower()
            if suffix not in SUPPORTED_SUFFIXES:
                continue

            child_path = Path(entry.path)
            try:
                child_canonical = child_path.resolve(strict=True)
            except OSError as exc:
                raise SourceUnavailableError("source is not available") from exc
            if not child_canonical.is_relative_to(canonical_root):
                raise SourceUnavailableError("source escapes the approved root")

            if lst.st_size > MAX_FILE_BYTES:
                raise SourceLimitExceededError("source file exceeds the size limit")
            state.total_bytes += lst.st_size
            if state.total_bytes > MAX_TOTAL_SOURCE_BYTES:
                raise SourceLimitExceededError("source exceeds the total size limit")

            rel = child_canonical.relative_to(canonical_root).as_posix()
            state.candidates.append(
                CandidateFile(canonical_path=child_canonical, relative_path=rel, lstat=lst)
            )
            if len(state.candidates) > MAX_FILES_PER_SOURCE:
                raise SourceLimitExceededError("source exceeds the file count limit")
            continue

        # kind == "special": socket, FIFO, device, or another special file.
        raise SourceUnavailableError("source contains an unsafe file")


def list_source_candidates(canonical_root: Path, recursive: bool) -> list[CandidateFile]:
    """Enumerate every approved candidate beneath an already-validated
    canonical root (see resolve_canonical_root()), in deterministic order.
    Raises SourceUnavailableError / SourceLimitExceededError for anything
    unsafe or over a fixed limit. An empty result (a source with zero
    supported files) is valid, not an error."""

    if canonical_root.is_file():
        try:
            lst = os.lstat(canonical_root)
        except OSError as exc:
            raise SourceUnavailableError("source root is not available") from exc
        if _classify(lst) != "regular":
            raise SourceUnavailableError("source root has an unsafe identity")
        if lst.st_size > MAX_FILE_BYTES:
            raise SourceLimitExceededError("source file exceeds the size limit")
        return [
            CandidateFile(
                canonical_path=canonical_root,
                relative_path=canonical_root.name,
                lstat=lst,
            )
        ]

    state = _TraversalState()
    _walk_directory(canonical_root, canonical_root, recursive, 0, state)
    return state.candidates


def read_source_file(canonical_root: Path, candidate: CandidateFile) -> tuple[bytes, os.stat_result]:
    """Race-resistantly read one candidate's raw bytes. Re-validates
    identity immediately before opening (not reusing the lstat captured
    during traversal, which may be stale), opens with the strictest
    available no-follow flags, re-confirms identity and size via fstat,
    reads a bounded amount, and re-confirms identity/size/mtime again
    after the read completes. Any mismatch at any step means the file was
    replaced or mutated mid-ingestion, and raises
    InvalidSourceContentError - the caller (ingest.py) must treat that as
    a whole-source failure, never a partial read."""

    path = candidate.canonical_path

    try:
        pre_lst = os.lstat(path)
    except OSError as exc:
        raise InvalidSourceContentError("source file is not available") from exc
    if _classify(pre_lst) != "regular":
        raise InvalidSourceContentError("source file has an unsafe identity")

    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise InvalidSourceContentError("source file is not available") from exc
    if resolved != path or not (
        resolved == canonical_root or resolved.is_relative_to(canonical_root)
    ):
        raise InvalidSourceContentError("source file is not available")

    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise InvalidSourceContentError("source file is not available") from exc

    try:
        opened_lst = os.fstat(fd)
        if not stat_module.S_ISREG(opened_lst.st_mode):
            raise InvalidSourceContentError("source file has an unsafe identity")
        if opened_lst.st_dev != pre_lst.st_dev or opened_lst.st_ino != pre_lst.st_ino:
            raise InvalidSourceContentError("source file changed during ingestion")
        if opened_lst.st_size > MAX_FILE_BYTES:
            raise SourceLimitExceededError("source file exceeds the size limit")

        chunks: list[bytes] = []
        total = 0
        while True:
            block = os.read(fd, 65536)
            if not block:
                break
            chunks.append(block)
            total += len(block)
            if total > MAX_FILE_BYTES:
                raise SourceLimitExceededError("source file exceeds the size limit")
        content = b"".join(chunks)

        post_lst = os.fstat(fd)
        if (
            post_lst.st_size != opened_lst.st_size
            or post_lst.st_mtime_ns != opened_lst.st_mtime_ns
            or post_lst.st_ino != opened_lst.st_ino
            or post_lst.st_dev != opened_lst.st_dev
        ):
            raise InvalidSourceContentError("source file changed during ingestion")

        return content, opened_lst
    finally:
        os.close(fd)
