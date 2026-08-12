"""Tests for kernel/tools/file_safety.py: shared exact-file resolution
and the symlink/reparse-point classification it depends on.

Milestone 43 P1 correction: the real-symlink-creation tests in
test_file_metadata.py/test_read_text_file.py skip on a Windows account
without permission to create a symlink (common without Developer Mode or
elevation) - a core safety property must not be proven ONLY by a test that
can skip on this development machine. classify_stat_mode() is a pure
function (no filesystem access) extracted specifically so its
classification logic - including the FILE_ATTRIBUTE_REPARSE_POINT check,
which catches reparse points S_ISLNK alone would miss (e.g. a OneDrive
cloud placeholder) - always executes, and resolve_approved_file()'s own
lstat()-mocked test below proves the classifier is actually wired into
the real resolution path, again without needing filesystem symlink
permission."""

import stat as stat_module
from pathlib import Path

import pytest

from kernel.tools.config import FileSpec, ToolsConfig
from kernel.tools.file_safety import (
    FileResourceError,
    StatClassification,
    classify_stat_mode,
    resolve_approved_directory,
    resolve_approved_file,
)


# --- classify_stat_mode(): pure, non-skippable coverage ---------------------


def test_regular_file_mode_classifies_as_regular_file():
    mode = stat_module.S_IFREG | 0o644
    assert classify_stat_mode(mode) is StatClassification.REGULAR_FILE


def test_symlink_mode_classifies_as_symlink_or_reparse_point():
    mode = stat_module.S_IFLNK | 0o777
    assert classify_stat_mode(mode) is StatClassification.SYMLINK_OR_REPARSE_POINT


def test_directory_mode_classifies_as_other():
    mode = stat_module.S_IFDIR | 0o755
    assert classify_stat_mode(mode) is StatClassification.OTHER


def test_regular_looking_mode_with_reparse_attribute_bit_is_still_rejected():
    """The exact gap this correction closes: a reparse point CPython does
    NOT map to S_IFLNK (e.g. a OneDrive placeholder or deduplication
    reparse point) still reports an ordinary S_IFREG mode - the raw
    FILE_ATTRIBUTE_REPARSE_POINT bit is what actually catches it."""

    mode = stat_module.S_IFREG | 0o644
    attributes = stat_module.FILE_ATTRIBUTE_REPARSE_POINT
    assert classify_stat_mode(mode, attributes) is StatClassification.SYMLINK_OR_REPARSE_POINT


def test_regular_file_mode_with_no_reparse_attribute_bit_is_a_regular_file():
    mode = stat_module.S_IFREG | 0o644
    attributes = 0  # no bits set, including no reparse point
    assert classify_stat_mode(mode, attributes) is StatClassification.REGULAR_FILE


def test_regular_file_mode_with_other_unrelated_attribute_bits_is_still_a_regular_file():
    mode = stat_module.S_IFREG | 0o644
    attributes = stat_module.FILE_ATTRIBUTE_ARCHIVE | stat_module.FILE_ATTRIBUTE_HIDDEN
    assert classify_stat_mode(mode, attributes) is StatClassification.REGULAR_FILE


def test_none_attributes_never_raises_and_falls_back_to_mode_only():
    # Simulates a non-Windows stat() result, where st_file_attributes does
    # not exist at all.
    mode = stat_module.S_IFREG | 0o644
    assert classify_stat_mode(mode, None) is StatClassification.REGULAR_FILE


# --- resolve_approved_file(): the real path, with lstat() mocked -----------
# Proves the classifier above is actually wired into resolution, without
# needing real filesystem symlink-creation permission.


class _FakeStatResult:
    def __init__(self, st_mode, st_size=123, st_mtime=1_700_000_000.0, st_file_attributes=None):
        self.st_mode = st_mode
        self.st_size = st_size
        self.st_mtime = st_mtime
        if st_file_attributes is not None:
            self.st_file_attributes = st_file_attributes


def _config(approved_files):
    return ToolsConfig(
        approved_directories={},
        approved_applications={},
        approved_scripts={},
        approved_files=approved_files,
    )


def test_resolve_approved_file_rejects_a_mocked_symlink_without_real_symlink_permission(
    tmp_path, monkeypatch
):
    target = tmp_path / "target.txt"
    target.write_bytes(b"content")
    fake_stat = _FakeStatResult(stat_module.S_IFLNK | 0o777)
    monkeypatch.setattr(Path, "lstat", lambda self: fake_stat)

    config = _config({"k": FileSpec(path=str(target))})

    with pytest.raises(FileResourceError):
        resolve_approved_file("k", config)


def test_resolve_approved_file_rejects_a_mocked_non_symlink_reparse_point(tmp_path, monkeypatch):
    """The exact scenario the FILE_ATTRIBUTE_REPARSE_POINT check exists
    for: a mode that looks like an ordinary regular file, but the
    Windows-only reparse-point attribute bit is set."""

    target = tmp_path / "target.txt"
    target.write_bytes(b"content")
    fake_stat = _FakeStatResult(
        stat_module.S_IFREG | 0o644,
        st_file_attributes=stat_module.FILE_ATTRIBUTE_REPARSE_POINT,
    )
    monkeypatch.setattr(Path, "lstat", lambda self: fake_stat)

    config = _config({"k": FileSpec(path=str(target))})

    with pytest.raises(FileResourceError):
        resolve_approved_file("k", config)


def test_resolve_approved_file_accepts_a_mocked_ordinary_regular_file(tmp_path, monkeypatch):
    target = tmp_path / "target.txt"
    target.write_bytes(b"content")
    fake_stat = _FakeStatResult(stat_module.S_IFREG | 0o644, st_size=7, st_file_attributes=0)
    monkeypatch.setattr(Path, "lstat", lambda self: fake_stat)

    config = _config({"k": FileSpec(path=str(target))})

    resolved = resolve_approved_file("k", config)

    assert resolved.key == "k"
    assert resolved.size_bytes == 7


def test_resolve_approved_file_rejects_a_mocked_directory(tmp_path, monkeypatch):
    target = tmp_path / "target"
    fake_stat = _FakeStatResult(stat_module.S_IFDIR | 0o755)
    monkeypatch.setattr(Path, "lstat", lambda self: fake_stat)

    config = _config({"k": FileSpec(path=str(target))})

    with pytest.raises(FileResourceError):
        resolve_approved_file("k", config)


# --- ordinary, non-symlink-permission-dependent behavior --------------------


def test_unregistered_key_is_rejected():
    config = _config({})

    with pytest.raises(FileResourceError):
        resolve_approved_file("missing", config)


def test_none_resource_key_is_rejected():
    config = _config({})

    with pytest.raises(FileResourceError):
        resolve_approved_file(None, config)


def test_missing_configured_file_fails_safely(tmp_path):
    config = _config({"k": FileSpec(path=str(tmp_path / "does_not_exist.txt"))})

    with pytest.raises(FileResourceError):
        resolve_approved_file("k", config)


# --- resolve_approved_directory() (Milestone 43 P2): the real path, with
# --- lstat() mocked - proves create_directory.py's parent and copy_file.py's
# --- destination-directory reparse-point rejection is wired correctly,
# --- without needing real filesystem symlink/junction-creation permission
# --- (test_create_directory.py/test_copy_file.py's own real-symlink tests
# --- for the same behavior can skip on this account; these can never skip).


def _directory_config(approved_directories):
    return ToolsConfig(
        approved_directories=approved_directories,
        approved_applications={},
        approved_scripts={},
    )


def test_resolve_approved_directory_rejects_a_mocked_symlink_parent(tmp_path, monkeypatch):
    target = tmp_path / "parent_dir"
    target.mkdir()
    fake_stat = _FakeStatResult(stat_module.S_IFLNK | 0o777)
    monkeypatch.setattr(Path, "lstat", lambda self: fake_stat)

    config = _directory_config({"documents": str(target)})

    with pytest.raises(FileResourceError):
        resolve_approved_directory("documents", config)


def test_resolve_approved_directory_rejects_a_mocked_non_symlink_reparse_point(
    tmp_path, monkeypatch
):
    """The same gap test_resolve_approved_file_rejects_a_mocked_non_symlink_reparse_point
    closes for files, applied to directories: a reparse point CPython does
    NOT map to S_IFLNK (e.g. a mount point/cloud-sync placeholder
    directory) still reports an ordinary S_IFDIR mode - only the raw
    FILE_ATTRIBUTE_REPARSE_POINT bit catches it."""

    target = tmp_path / "parent_dir"
    target.mkdir()
    fake_stat = _FakeStatResult(
        stat_module.S_IFDIR | 0o755,
        st_file_attributes=stat_module.FILE_ATTRIBUTE_REPARSE_POINT,
    )
    monkeypatch.setattr(Path, "lstat", lambda self: fake_stat)

    config = _directory_config({"documents": str(target)})

    with pytest.raises(FileResourceError):
        resolve_approved_directory("documents", config)


def test_resolve_approved_directory_rejects_a_mocked_non_directory(tmp_path, monkeypatch):
    target = tmp_path / "parent_dir"
    target.mkdir()
    fake_stat = _FakeStatResult(stat_module.S_IFREG | 0o644, st_file_attributes=0)
    monkeypatch.setattr(Path, "lstat", lambda self: fake_stat)

    config = _directory_config({"documents": str(target)})

    with pytest.raises(FileResourceError):
        resolve_approved_directory("documents", config)


def test_resolve_approved_directory_accepts_a_mocked_ordinary_directory(tmp_path, monkeypatch):
    target = tmp_path / "parent_dir"
    target.mkdir()
    fake_stat = _FakeStatResult(stat_module.S_IFDIR | 0o755, st_file_attributes=0)
    monkeypatch.setattr(Path, "lstat", lambda self: fake_stat)

    config = _directory_config({"documents": str(target)})

    resolved = resolve_approved_directory("documents", config)

    assert resolved.key == "documents"


def test_resolve_approved_directory_unregistered_key_is_rejected():
    config = _directory_config({})

    with pytest.raises(FileResourceError):
        resolve_approved_directory("missing", config)


def test_resolve_approved_directory_none_key_is_rejected():
    config = _directory_config({})

    with pytest.raises(FileResourceError):
        resolve_approved_directory(None, config)
