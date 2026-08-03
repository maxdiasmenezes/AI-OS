"""Tests for kernel/knowledge_base/traversal.py: secure root validation,
deterministic traversal, safety limits, and race-resistant reading. Every
test operates entirely under tmp_path."""

import os
import sys

import pytest

from kernel.knowledge_base import traversal
from kernel.knowledge_base.traversal import (
    CandidateFile,
    list_source_candidates,
    read_source_file,
    resolve_canonical_root,
    validate_existing_directory,
)
from kernel.knowledge_base.types import (
    DatabaseUnavailableError,
    InvalidSourceContentError,
    SourceLimitExceededError,
    SourceUnavailableError,
)

pytestmark_windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows-only behavior")


def _write(path, text="content", encoding="utf-8"):
    path.write_text(text, encoding=encoding)
    return path


# --- root validation -----------------------------------------------------


def test_approved_regular_directory_resolves(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()

    canonical = resolve_canonical_root(str(docs))

    assert canonical == docs.resolve()


def test_approved_regular_file_resolves(tmp_path):
    f = _write(tmp_path / "notes.md")

    canonical = resolve_canonical_root(str(f))

    assert canonical == f.resolve()


def test_missing_root_raises(tmp_path):
    with pytest.raises(SourceUnavailableError):
        resolve_canonical_root(str(tmp_path / "does-not-exist"))


def test_root_symlink_rejected_before_resolution(tmp_path):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real_dir, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted in this environment")

    with pytest.raises(SourceUnavailableError):
        resolve_canonical_root(str(link))


def test_root_symlink_to_file_rejected(tmp_path):
    real_file = _write(tmp_path / "real.md")
    link = tmp_path / "link.md"
    try:
        link.symlink_to(real_file)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted in this environment")

    with pytest.raises(SourceUnavailableError):
        resolve_canonical_root(str(link))


@pytestmark_windows_only
def test_root_junction_rejected(tmp_path):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    junction = tmp_path / "junction"

    import subprocess

    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(real_dir)],
        check=True,
        capture_output=True,
    )

    with pytest.raises(SourceUnavailableError):
        resolve_canonical_root(str(junction))


# --- validate_existing_directory (used by db.py) --------------------------


def test_validate_existing_directory_accepts_real_directory(tmp_path):
    canonical = validate_existing_directory(tmp_path)
    assert canonical == tmp_path.resolve()


def test_validate_existing_directory_rejects_missing(tmp_path):
    with pytest.raises(DatabaseUnavailableError):
        validate_existing_directory(tmp_path / "missing")


def test_validate_existing_directory_rejects_file(tmp_path):
    f = _write(tmp_path / "not-a-dir.txt")
    with pytest.raises(DatabaseUnavailableError):
        validate_existing_directory(f)


def test_validate_existing_directory_rejects_symlink(tmp_path):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real_dir, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted in this environment")

    with pytest.raises(DatabaseUnavailableError):
        validate_existing_directory(link)


# --- deterministic traversal -----------------------------------------------


def test_recursive_directory_traversal_includes_nested_files(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    _write(root / "a.md")
    sub = root / "sub"
    sub.mkdir()
    _write(sub / "b.txt")

    canonical = resolve_canonical_root(str(root))
    candidates = list_source_candidates(canonical, recursive=True)

    rel_paths = sorted(c.relative_path for c in candidates)
    assert rel_paths == ["a.md", "sub/b.txt"]


def test_non_recursive_traversal_excludes_nested_files(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    _write(root / "a.md")
    sub = root / "sub"
    sub.mkdir()
    _write(sub / "b.txt")

    canonical = resolve_canonical_root(str(root))
    candidates = list_source_candidates(canonical, recursive=False)

    rel_paths = sorted(c.relative_path for c in candidates)
    assert rel_paths == ["a.md"]


def test_traversal_ordering_is_deterministic_across_runs(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    for name in ["zeta.md", "alpha.md", "mid.txt"]:
        _write(root / name)

    canonical = resolve_canonical_root(str(root))
    first = [c.relative_path for c in list_source_candidates(canonical, recursive=True)]
    second = [c.relative_path for c in list_source_candidates(canonical, recursive=True)]

    assert first == second


def test_single_file_source_uses_basename_as_relative_path(tmp_path):
    f = _write(tmp_path / "notes.md")

    canonical = resolve_canonical_root(str(f))
    candidates = list_source_candidates(canonical, recursive=True)

    assert len(candidates) == 1
    assert candidates[0].relative_path == "notes.md"


def test_unknown_extension_is_ignored_not_rejected(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    _write(root / "a.md")
    _write(root / "image.png")

    canonical = resolve_canonical_root(str(root))
    candidates = list_source_candidates(canonical, recursive=True)

    assert [c.relative_path for c in candidates] == ["a.md"]


def test_extension_matching_is_case_insensitive(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    _write(root / "A.MD")

    canonical = resolve_canonical_root(str(root))
    candidates = list_source_candidates(canonical, recursive=True)

    assert [c.relative_path for c in candidates] == ["A.MD"]


def test_hidden_dot_prefixed_entries_are_skipped(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    _write(root / "visible.md")
    _write(root / ".hidden.md")
    hidden_dir = root / ".git"
    hidden_dir.mkdir()
    _write(hidden_dir / "config.md")

    canonical = resolve_canonical_root(str(root))
    candidates = list_source_candidates(canonical, recursive=True)

    assert [c.relative_path for c in candidates] == ["visible.md"]


def test_empty_source_yields_no_candidates(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()

    canonical = resolve_canonical_root(str(root))
    candidates = list_source_candidates(canonical, recursive=True)

    assert candidates == []


def test_mixed_valid_and_invalid_extension_files(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    _write(root / "keep.txt")
    _write(root / "skip.bin")
    _write(root / "skip.pdf")

    canonical = resolve_canonical_root(str(root))
    candidates = list_source_candidates(canonical, recursive=True)

    assert [c.relative_path for c in candidates] == ["keep.txt"]


def test_candidate_symlink_rejected(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    real_file = _write(tmp_path / "outside.md")
    link = root / "linked.md"
    try:
        link.symlink_to(real_file)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted in this environment")

    canonical = resolve_canonical_root(str(root))
    with pytest.raises(SourceUnavailableError):
        list_source_candidates(canonical, recursive=True)


def test_candidate_directory_symlink_rejected_when_recursive(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    real_dir = tmp_path / "outside"
    real_dir.mkdir()
    _write(real_dir / "a.md")
    link_dir = root / "linked"
    try:
        link_dir.symlink_to(real_dir, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted in this environment")

    canonical = resolve_canonical_root(str(root))
    with pytest.raises(SourceUnavailableError):
        list_source_candidates(canonical, recursive=True)


def test_directory_symlink_ignored_when_non_recursive(tmp_path):
    # Out of scope entirely when non-recursive - never even inspected,
    # so it must not trigger the "unsafe file" rejection.
    root = tmp_path / "docs"
    root.mkdir()
    _write(root / "a.md")
    real_dir = tmp_path / "outside"
    real_dir.mkdir()
    link_dir = root / "linked"
    try:
        link_dir.symlink_to(real_dir, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted in this environment")

    canonical = resolve_canonical_root(str(root))
    candidates = list_source_candidates(canonical, recursive=False)

    assert [c.relative_path for c in candidates] == ["a.md"]


# --- fixed safety limits ----------------------------------------------------


def test_file_count_limit_enforced(tmp_path, monkeypatch):
    monkeypatch.setattr(traversal, "MAX_FILES_PER_SOURCE", 2)
    root = tmp_path / "docs"
    root.mkdir()
    for i in range(3):
        _write(root / f"f{i}.md")

    canonical = resolve_canonical_root(str(root))
    with pytest.raises(SourceLimitExceededError):
        list_source_candidates(canonical, recursive=True)


def test_individual_file_size_limit_enforced(tmp_path, monkeypatch):
    monkeypatch.setattr(traversal, "MAX_FILE_BYTES", 10)
    root = tmp_path / "docs"
    root.mkdir()
    _write(root / "big.md", "x" * 100)

    canonical = resolve_canonical_root(str(root))
    with pytest.raises(SourceLimitExceededError):
        list_source_candidates(canonical, recursive=True)


def test_total_source_bytes_limit_enforced(tmp_path, monkeypatch):
    monkeypatch.setattr(traversal, "MAX_FILE_BYTES", 1000)
    monkeypatch.setattr(traversal, "MAX_TOTAL_SOURCE_BYTES", 15)
    root = tmp_path / "docs"
    root.mkdir()
    _write(root / "a.md", "x" * 10)
    _write(root / "b.md", "x" * 10)

    canonical = resolve_canonical_root(str(root))
    with pytest.raises(SourceLimitExceededError):
        list_source_candidates(canonical, recursive=True)


def test_recursion_depth_limit_enforced(tmp_path, monkeypatch):
    monkeypatch.setattr(traversal, "MAX_RECURSION_DEPTH", 1)
    root = tmp_path / "docs"
    root.mkdir()
    level1 = root / "level1"
    level1.mkdir()
    level2 = level1 / "level2"
    level2.mkdir()
    _write(level2 / "deep.md")

    canonical = resolve_canonical_root(str(root))
    with pytest.raises(SourceLimitExceededError):
        list_source_candidates(canonical, recursive=True)


# --- race-resistant reading -------------------------------------------------


def test_read_source_file_returns_bytes_and_stat(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    f = _write(root / "a.md", "hello world")

    canonical = resolve_canonical_root(str(root))
    candidates = list_source_candidates(canonical, recursive=True)
    content, stat_result = read_source_file(canonical, candidates[0])

    assert content == b"hello world"
    assert stat_result.st_size == len("hello world")


def test_read_source_file_closes_descriptor(tmp_path, monkeypatch):
    root = tmp_path / "docs"
    root.mkdir()
    f = _write(root / "a.md", "hello world")
    canonical = resolve_canonical_root(str(root))
    candidates = list_source_candidates(canonical, recursive=True)

    closed_fds = []
    real_close = os.close

    def spy_close(fd):
        closed_fds.append(fd)
        return real_close(fd)

    monkeypatch.setattr(os, "close", spy_close)

    read_source_file(canonical, candidates[0])

    assert len(closed_fds) == 1


def test_read_source_file_identity_replacement_between_lstat_and_open(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    f = _write(root / "a.md", "hello world")
    canonical = resolve_canonical_root(str(root))
    candidates = list_source_candidates(canonical, recursive=True)

    # Simulate a TOCTOU replacement: the regular file is swapped out for a
    # symlink before read_source_file() re-validates identity.
    f.unlink()
    target = tmp_path / "elsewhere.md"
    _write(target, "elsewhere")
    try:
        f.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted in this environment")

    with pytest.raises(InvalidSourceContentError):
        read_source_file(canonical, candidates[0])


def test_read_source_file_mutation_during_read_detected(tmp_path, monkeypatch):
    root = tmp_path / "docs"
    root.mkdir()
    f = _write(root / "a.md", "hello world")
    canonical = resolve_canonical_root(str(root))
    candidates = list_source_candidates(canonical, recursive=True)

    real_fstat = os.fstat
    call_count = {"n": 0}

    def flaky_fstat(fd):
        call_count["n"] += 1
        result = real_fstat(fd)
        if call_count["n"] == 2:
            # Simulate the file having grown between the pre-read and
            # post-read fstat calls.
            class _Mutated:
                st_size = result.st_size + 1
                st_mtime_ns = result.st_mtime_ns + 1
                st_ino = result.st_ino
                st_dev = result.st_dev
                st_mode = result.st_mode

            return _Mutated()
        return result

    monkeypatch.setattr(os, "fstat", flaky_fstat)

    with pytest.raises(InvalidSourceContentError):
        read_source_file(canonical, candidates[0])


def test_o_nofollow_used_where_available(tmp_path, monkeypatch):
    root = tmp_path / "docs"
    root.mkdir()
    _write(root / "a.md", "content")
    canonical = resolve_canonical_root(str(root))
    candidates = list_source_candidates(canonical, recursive=True)

    captured = {}
    real_open = os.open

    def spy_open(path, flags, *args, **kwargs):
        captured["flags"] = flags
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", spy_open)
    read_source_file(canonical, candidates[0])

    if hasattr(os, "O_NOFOLLOW"):
        assert captured["flags"] & os.O_NOFOLLOW
    else:
        pytest.skip("os.O_NOFOLLOW not available on this platform")


def test_read_source_file_size_limit_enforced_at_open_time(tmp_path, monkeypatch):
    monkeypatch.setattr(traversal, "MAX_FILE_BYTES", 5)
    root = tmp_path / "docs"
    root.mkdir()
    f = _write(root / "a.md", "x" * 3)
    canonical = resolve_canonical_root(str(root))
    candidate = CandidateFile(canonical_path=f, relative_path="a.md", lstat=os.lstat(f))

    # Grow the file after traversal's own (now-stale) lstat was captured,
    # so the size limit must be caught during the fresh open/fstat.
    f.write_text("x" * 100, encoding="utf-8")

    with pytest.raises(SourceLimitExceededError):
        read_source_file(canonical, candidate)
