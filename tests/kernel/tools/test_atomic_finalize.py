"""Tests for kernel/tools/atomic_finalize.py: atomic_finalize_no_replace()."""

import os

import pytest

from kernel.tools.atomic_finalize import FinalizeCollisionError, atomic_finalize_no_replace


def test_finalize_moves_source_content_to_destination(tmp_path):
    source = tmp_path / "source.partial"
    destination = tmp_path / "final.bundle"
    source.write_bytes(b"bundle payload")

    atomic_finalize_no_replace(source, destination)

    assert destination.read_bytes() == b"bundle payload"
    assert not source.exists()


def test_finalize_raises_when_destination_already_exists(tmp_path):
    source = tmp_path / "source.partial"
    destination = tmp_path / "final.bundle"
    source.write_bytes(b"new content")
    destination.write_bytes(b"EXISTING PROTECTED CONTENT")

    with pytest.raises(FinalizeCollisionError):
        atomic_finalize_no_replace(source, destination)


def test_collision_leaves_existing_destination_byte_for_byte_unchanged(tmp_path):
    source = tmp_path / "source.partial"
    destination = tmp_path / "final.bundle"
    source.write_bytes(b"new content")
    original = b"EXISTING PROTECTED CONTENT" * 100
    destination.write_bytes(original)

    with pytest.raises(FinalizeCollisionError):
        atomic_finalize_no_replace(source, destination)

    assert destination.read_bytes() == original


def test_collision_never_deletes_the_source(tmp_path):
    source = tmp_path / "source.partial"
    destination = tmp_path / "final.bundle"
    source.write_bytes(b"new content")
    destination.write_bytes(b"existing")

    with pytest.raises(FinalizeCollisionError):
        atomic_finalize_no_replace(source, destination)

    assert source.exists()
    assert source.read_bytes() == b"new content"


def test_never_uses_os_replace(monkeypatch, tmp_path):
    source = tmp_path / "source.partial"
    destination = tmp_path / "final.bundle"
    source.write_bytes(b"content")

    def _forbidden_replace(*args, **kwargs):
        raise AssertionError("atomic_finalize_no_replace must never call os.replace()")

    monkeypatch.setattr(os, "replace", _forbidden_replace)

    atomic_finalize_no_replace(source, destination)

    assert destination.read_bytes() == b"content"


@pytest.mark.skipif(os.name == "nt", reason="Windows path uses os.rename, not os.link")
def test_posix_uses_hardlink_then_unlink(tmp_path):
    source = tmp_path / "source.partial"
    destination = tmp_path / "final.bundle"
    source.write_bytes(b"content")

    atomic_finalize_no_replace(source, destination)

    assert destination.read_bytes() == b"content"
    assert not source.exists()
