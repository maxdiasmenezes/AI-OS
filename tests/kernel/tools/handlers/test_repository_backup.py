"""
Tests for kernel/tools/handlers/repository_backup.py.

Like test_repo_health.py, most of these tests deliberately run real,
short-lived `git` subprocesses against real temporary local repositories -
this handler's entire job is creating and verifying a real git bundle,
which can't be meaningfully verified without it. No test in this file
ever contacts GitHub, any real network service, the real
kernel/config/tools.yaml, or a real configured backup destination -
every repository and destination directory used here is created fresh
under tmp_path.
"""

import hashlib
import os
import re
import stat
import subprocess
import time

import pytest

from kernel.tools.config import RepoBackupSpec, RepoSpec, ToolsConfig
from kernel.tools.handlers import repository_backup
from kernel.tools.process_control import CapturedResult, RunResult
from kernel.tools.types import ActionRequest


def _run_git(args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    _run_git(["init", "-q", "-b", "main"], path)
    _run_git(["config", "user.email", "test@example.com"], path)
    _run_git(["config", "user.name", "Test"], path)
    return path


_commit_counter = [0]


def _commit(path, message, filename="file.txt", content=None):
    _commit_counter[0] += 1
    if content is None:
        content = f"content-{_commit_counter[0]}"
    (path / filename).write_text(content, encoding="utf-8")
    _run_git(["add", filename], path)
    _run_git(["commit", "-q", "-m", message], path)


def _config(repo_key="ai_os", repo_path=None, dest_path=None):
    approved_repositories = {}
    approved_backups = {}
    if repo_path is not None:
        approved_repositories[repo_key] = RepoSpec(path=str(repo_path), main_branch="main")
    if dest_path is not None:
        approved_backups[repo_key] = RepoBackupSpec(destination_directory=str(dest_path))
    return ToolsConfig(
        approved_directories={},
        approved_applications={},
        approved_scripts={},
        approved_repositories=approved_repositories,
        approved_backups=approved_backups,
    )


def _request(key="ai_os"):
    return ActionRequest(action="repository_backup", resource_key=key)


def _clone(bundle_path, dest_dir):
    subprocess.run(
        ["git", "clone", "-q", str(bundle_path), str(dest_dir)], check=True, capture_output=True
    )


def _bundle_refs(bundle_path, cwd):
    result = subprocess.run(
        ["git", "bundle", "list-heads", str(bundle_path)],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )
    refs = set()
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        refs.add(line.split()[1])
    return refs


# --- Proof that `git bundle create -` is supported on this git install ---


def test_git_bundle_create_dash_produces_a_valid_bundle_on_this_git_install(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")
    _run_git(["tag", "v1"], repo)

    out_path = tmp_path / "proof.bundle"
    with open(out_path, "wb") as f:
        proc = subprocess.run(
            ["git", "bundle", "create", "-", "HEAD", "--branches", "--tags"],
            cwd=str(repo),
            stdout=f,
            stderr=subprocess.DEVNULL,
            check=True,
        )
    assert proc.returncode == 0
    assert out_path.stat().st_size > 0

    verify = subprocess.run(
        ["git", "bundle", "verify", str(out_path)], cwd=str(repo), capture_output=True
    )
    assert verify.returncode == 0


# --- Registration checks ---


def test_unregistered_repository_key_is_rejected(tmp_path):
    config = _config(repo_path=None, dest_path=tmp_path)

    result = repository_backup.run(_request(), config)

    assert result.success is False
    assert result.outcome == "rejected"
    assert "not registered" in result.message
    assert "backup" not in result.message.lower()


def test_repo_health_registered_but_backup_not_registered_is_rejected(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")
    config = _config(repo_path=repo, dest_path=None)

    result = repository_backup.run(_request(), config)

    assert result.success is False
    assert result.outcome == "rejected"
    assert "backup" in result.message.lower()


# --- Repository availability ---


def test_nonexistent_repository_path_is_unavailable(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    config = _config(repo_path=tmp_path / "does_not_exist", dest_path=dest)

    result = repository_backup.run(_request(), config)

    assert result.success is False
    assert result.outcome == "failed"
    assert "repository" in result.message.lower()


def test_non_git_directory_is_unavailable(tmp_path):
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    dest = tmp_path / "dest"
    dest.mkdir()
    config = _config(repo_path=plain_dir, dest_path=dest)

    result = repository_backup.run(_request(), config)

    assert result.success is False
    assert result.outcome == "failed"


def test_configured_subdirectory_of_a_worktree_is_rejected(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")
    subdir = repo / "subdir"
    subdir.mkdir()
    dest = tmp_path / "dest"
    dest.mkdir()
    config = _config(repo_path=subdir, dest_path=dest)

    result = repository_backup.run(_request(), config)

    assert result.success is False
    assert result.outcome == "failed"


# --- Destination availability / containment ---


def test_missing_destination_is_unavailable(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")
    config = _config(repo_path=repo, dest_path=tmp_path / "does_not_exist")

    result = repository_backup.run(_request(), config)

    assert result.success is False
    assert result.outcome == "failed"
    assert "destination" in result.message.lower()


def test_destination_that_is_a_file_is_unavailable(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")
    dest_file = tmp_path / "dest_is_a_file"
    dest_file.write_text("not a directory")
    config = _config(repo_path=repo, dest_path=dest_file)

    result = repository_backup.run(_request(), config)

    assert result.success is False
    assert result.outcome == "failed"


def test_destination_equal_to_repository_is_rejected(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")
    config = _config(repo_path=repo, dest_path=repo)

    result = repository_backup.run(_request(), config)

    assert result.success is False
    assert result.outcome == "failed"


def test_destination_inside_repository_is_rejected(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")
    inside = repo / "backups"
    inside.mkdir()
    config = _config(repo_path=repo, dest_path=inside)

    result = repository_backup.run(_request(), config)

    assert result.success is False
    assert result.outcome == "failed"


def test_repository_inside_destination_is_rejected(tmp_path):
    outer_dest = tmp_path / "outer_dest"
    outer_dest.mkdir()
    nested_repo = outer_dest / "nested_repo"
    _init_repo(nested_repo)
    _commit(nested_repo, "first commit")
    config = _config(repo_path=nested_repo, dest_path=outer_dest)

    result = repository_backup.run(_request(), config)

    assert result.success is False
    assert result.outcome == "failed"


# --- Successful creation, ref policy, and content proof ---


def test_successful_backup_creates_a_verified_bundle(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")
    _run_git(["tag", "v1"], repo)
    dest = tmp_path / "dest"
    dest.mkdir()
    config = _config(repo_path=repo, dest_path=dest)

    result = repository_backup.run(_request(), config)

    assert result.success is True
    assert result.outcome == "executed"
    assert "Repository backup created." in result.message
    assert "Repository: ai_os" in result.message
    assert re.search(r"File: ai_os-\d{8}T\d{6}Z-[0-9a-f]{16}\.bundle", result.message)
    assert re.search(r"SHA-256: [0-9a-f]{64}", result.message)
    assert "Not included: current uncommitted, untracked, or ignored files" in result.message

    files = list(dest.iterdir())
    assert len(files) == 1
    assert files[0].name.endswith(".bundle")


def test_committed_history_included_untracked_and_ignored_excluded(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit", filename="tracked.txt")
    # A file named exactly like the diagnostic gitignored files, but
    # actually committed to history - proves the bundle is history-based,
    # never filename-filtered.
    (repo / ".env").write_text("SECRET=committed_by_mistake", encoding="utf-8")
    _run_git(["add", ".env"], repo)
    _run_git(["commit", "-q", "-m", "accidentally commit .env"], repo)
    # gitignore a would-be tools.yaml and leave a real untracked one on disk
    (repo / ".gitignore").write_text("kernel_config_tools.yaml\nignored.txt\n", encoding="utf-8")
    _run_git(["add", ".gitignore"], repo)
    _run_git(["commit", "-q", "-m", "add gitignore"], repo)
    (repo / "kernel_config_tools.yaml").write_text("fake local secrets", encoding="utf-8")
    (repo / "ignored.txt").write_text("ignored content", encoding="utf-8")
    (repo / "untracked.txt").write_text("never committed", encoding="utf-8")

    dest = tmp_path / "dest"
    dest.mkdir()
    config = _config(repo_path=repo, dest_path=dest)

    result = repository_backup.run(_request(), config)
    assert result.success is True

    bundle_path = next(dest.iterdir())
    clone_dir = tmp_path / "clone"
    _clone(bundle_path, clone_dir)
    cloned_names = {p.name for p in clone_dir.iterdir() if p.name != ".git"}

    assert "tracked.txt" in cloned_names
    assert ".env" in cloned_names  # committed -> included despite the name
    assert ".gitignore" in cloned_names
    assert "kernel_config_tools.yaml" not in cloned_names  # untracked+ignored
    assert "ignored.txt" not in cloned_names  # untracked+ignored
    assert "untracked.txt" not in cloned_names  # never committed


def test_branches_and_tags_are_recoverable(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "on main")
    _run_git(["checkout", "-q", "-b", "feature"], repo)
    _commit(repo, "on feature", filename="feature.txt")
    _run_git(["tag", "v1"], repo)
    _run_git(["checkout", "-q", "main"], repo)
    _run_git(["tag", "v2"], repo)

    dest = tmp_path / "dest"
    dest.mkdir()
    config = _config(repo_path=repo, dest_path=dest)

    result = repository_backup.run(_request(), config)
    assert result.success is True

    bundle_path = next(dest.iterdir())
    clone_dir = tmp_path / "clone"
    _clone(bundle_path, clone_dir)

    branches = subprocess.run(
        ["git", "branch", "-a"], cwd=str(clone_dir), capture_output=True, text=True
    ).stdout
    assert "feature" in branches or "remotes/origin/feature" in branches
    tags = subprocess.run(
        ["git", "tag"], cwd=str(clone_dir), capture_output=True, text=True
    ).stdout
    assert "v1" in tags
    assert "v2" in tags


def test_remote_tracking_stash_notes_and_custom_refs_are_not_advertised(tmp_path):
    bare_remote = tmp_path / "remote.git"
    _run_git(["init", "-q", "--bare", str(bare_remote)], tmp_path)

    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")
    _run_git(["remote", "add", "origin", str(bare_remote)], repo)
    _run_git(["push", "-q", "origin", "main"], repo)
    _run_git(["fetch", "-q", "origin"], repo)  # populates refs/remotes/origin/main

    # A stash entry.
    (repo / "file.txt").write_text("modified but not committed", encoding="utf-8")
    _run_git(["stash", "push", "-q"], repo)

    # A note.
    _run_git(["notes", "add", "-m", "a note", "HEAD"], repo)

    # A custom ref.
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True
    ).stdout.strip()
    _run_git(["update-ref", "refs/custom/foo", head_sha], repo)

    dest = tmp_path / "dest"
    dest.mkdir()
    config = _config(repo_path=repo, dest_path=dest)

    result = repository_backup.run(_request(), config)
    assert result.success is True

    bundle_path = next(dest.iterdir())
    refs = _bundle_refs(bundle_path, repo)

    assert refs == {"HEAD", "refs/heads/main"}
    assert not any(ref.startswith("refs/remotes/") for ref in refs)
    assert "refs/stash" not in refs
    assert not any(ref.startswith("refs/notes/") for ref in refs)
    assert "refs/custom/foo" not in refs


# --- Filename generation ---


def test_generate_names_use_expected_format_and_share_a_suffix():
    partial_name, final_name = repository_backup._generate_names("ai_os")

    assert re.match(r"^\.ai_os-\d{8}T\d{6}Z-[0-9a-f]{16}\.partial$", partial_name)
    assert re.match(r"^ai_os-\d{8}T\d{6}Z-[0-9a-f]{16}\.bundle$", final_name)

    partial_suffix = partial_name.rsplit("-", 1)[1].removesuffix(".partial")
    final_suffix = final_name.rsplit("-", 1)[1].removesuffix(".bundle")
    assert partial_suffix == final_suffix
    assert len(partial_suffix) == 16


def test_generate_names_produce_fresh_suffixes_across_calls():
    _, final_a = repository_backup._generate_names("ai_os")
    _, final_b = repository_backup._generate_names("ai_os")

    assert final_a != final_b


# --- Partial-file permissions ---


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are not meaningful on Windows")
def test_partial_file_is_created_with_owner_only_permissions(tmp_path):
    # On POSIX, mode 0o600 is enforced directly by the OS at creation
    # time - no group or other access bit is ever set, regardless of the
    # process umask (umask can only clear requested bits, never add ones
    # that were never requested). On Windows this test is skipped - see
    # _create_partial_file()'s docstring for why 0o600 has no POSIX-style
    # enforcement there; ACLs are inherited from the destination
    # directory and are out of scope for this milestone.
    dest = tmp_path / "dest"
    dest.mkdir()

    created = repository_backup._create_partial_file(dest, "ai_os")
    assert created is not None
    partial_path, _final_name, fd = created
    try:
        mode = os.fstat(fd).st_mode
    finally:
        os.close(fd)
        partial_path.unlink(missing_ok=True)

    permission_bits = stat.S_IMODE(mode)
    assert permission_bits & stat.S_IRWXG == 0  # no group access
    assert permission_bits & stat.S_IRWXO == 0  # no other access
    assert permission_bits & stat.S_IRUSR
    assert permission_bits & stat.S_IWUSR


# --- Filename-safe key defense-in-depth ---


def test_filename_unsafe_key_bypassing_config_is_rejected(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")
    dest = tmp_path / "dest"
    dest.mkdir()
    # Constructed directly, bypassing load_tools_config()'s own rejection
    # of an unsafe key - proves the handler's own redundant check holds.
    config = ToolsConfig(
        approved_directories={},
        approved_applications={},
        approved_scripts={},
        approved_repositories={"../evil": RepoSpec(path=str(repo), main_branch="main")},
        approved_backups={"../evil": RepoBackupSpec(destination_directory=str(dest))},
    )

    result = repository_backup.run(_request(key="../evil"), config)

    assert result.success is False
    assert result.outcome == "failed"
    assert list(dest.iterdir()) == []


# --- Collision handling and no-overwrite finalization ---


def test_partial_name_collision_retries_with_a_fresh_suffix(tmp_path, monkeypatch):
    dest = tmp_path / "dest"
    dest.mkdir()
    names = [(".ai_os-20260101T000000Z-" + "a" * 16 + ".partial", "ai_os-20260101T000000Z-" + "a" * 16 + ".bundle")]
    # Pre-create a file at the first candidate partial name so the first
    # attempt collides.
    (dest / names[0][0]).write_bytes(b"pre-existing, unrelated file")

    calls = {"n": 0}
    real_generate = repository_backup._generate_names

    def fake_generate(key):
        calls["n"] += 1
        if calls["n"] == 1:
            return names[0]
        return real_generate(key)

    monkeypatch.setattr(repository_backup, "_generate_names", fake_generate)

    created = repository_backup._create_partial_file(dest, "ai_os")

    assert created is not None
    partial_path, final_name, fd = created
    os.close(fd)
    assert partial_path.name != names[0][0]
    assert calls["n"] == 2
    # The pre-existing file at the first candidate name is untouched.
    assert (dest / names[0][0]).read_bytes() == b"pre-existing, unrelated file"


def test_existing_completed_bundle_is_never_overwritten(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")
    dest = tmp_path / "dest"
    dest.mkdir()
    config = _config(repo_path=repo, dest_path=dest)

    fixed_names = (".ai_os-fixed.partial", "ai_os-fixed.bundle")
    monkeypatch.setattr(repository_backup, "_generate_names", lambda key: fixed_names)

    first = repository_backup.run(_request(), config)
    assert first.success is True
    final_path = dest / fixed_names[1]
    assert final_path.exists()
    original_bytes = final_path.read_bytes()

    second = repository_backup.run(_request(), config)

    assert second.success is False
    assert second.outcome == "failed"
    # Finalization-collision fail-closed behavior (Milestone 35
    # correctness pass): a final-name collision is never retried with a
    # fresh suffix - the whole attempt fails with the same generic
    # creation-failure reply as any other creation failure, not a
    # dedicated "already exists" message that might hint at the
    # destination's contents.
    assert second.message == repository_backup._CREATION_FAILED.message
    # The original completed bundle is byte-for-byte unchanged.
    assert final_path.read_bytes() == original_bytes
    # Only the one completed bundle exists - the second run's partial was
    # cleaned up, and nothing else was created.
    assert [p.name for p in dest.iterdir()] == [fixed_names[1]]


# --- Failure cleanup: creation, verification, timeout ---


def test_creation_failure_cleans_up_the_partial_file(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")
    dest = tmp_path / "dest"
    dest.mkdir()
    config = _config(repo_path=repo, dest_path=dest)

    monkeypatch.setattr(
        repository_backup,
        "run_streaming_stdout_to_file",
        lambda *a, **k: RunResult(success=False, timed_out=False, returncode=1),
    )

    result = repository_backup.run(_request(), config)

    assert result.success is False
    assert result.outcome == "failed"
    assert list(dest.iterdir()) == []


def test_creation_timeout_cleans_up_the_partial_file_and_reports_timed_out(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")
    dest = tmp_path / "dest"
    dest.mkdir()
    config = _config(repo_path=repo, dest_path=dest)

    monkeypatch.setattr(
        repository_backup,
        "run_streaming_stdout_to_file",
        lambda *a, **k: RunResult(success=False, timed_out=True),
    )

    result = repository_backup.run(_request(), config)

    assert result.success is False
    assert result.outcome == "timed_out"
    assert list(dest.iterdir()) == []


def test_verification_failure_cleans_up_the_partial_file(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")
    dest = tmp_path / "dest"
    dest.mkdir()
    config = _config(repo_path=repo, dest_path=dest)

    original = repository_backup.run_capturing_stdout

    def fake(argv, cwd, timeout_seconds, env=None, max_output_bytes=4096):
        if "verify" in argv:
            return CapturedResult(success=False, timed_out=False, returncode=1)
        return original(argv, cwd, timeout_seconds, env=env, max_output_bytes=max_output_bytes)

    monkeypatch.setattr(repository_backup, "run_capturing_stdout", fake)

    result = repository_backup.run(_request(), config)

    assert result.success is False
    assert result.outcome == "failed"
    assert "verified" in result.message.lower()
    assert list(dest.iterdir()) == []


def test_verification_timeout_cleans_up_the_partial_file_and_reports_timed_out(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")
    dest = tmp_path / "dest"
    dest.mkdir()
    config = _config(repo_path=repo, dest_path=dest)

    original = repository_backup.run_capturing_stdout

    def fake(argv, cwd, timeout_seconds, env=None, max_output_bytes=4096):
        if "verify" in argv:
            return CapturedResult(success=False, timed_out=True)
        return original(argv, cwd, timeout_seconds, env=env, max_output_bytes=max_output_bytes)

    monkeypatch.setattr(repository_backup, "run_capturing_stdout", fake)

    result = repository_backup.run(_request(), config)

    assert result.success is False
    assert result.outcome == "timed_out"
    assert list(dest.iterdir()) == []


def test_real_timeout_via_tiny_timeout_constant_reports_timed_out_and_cleans_up(tmp_path, monkeypatch):
    # An end-to-end (real git subprocess) proof, not a mocked one: an
    # effectively-zero timeout guarantees the real `git bundle create`
    # process cannot finish in time.
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")
    dest = tmp_path / "dest"
    dest.mkdir()
    config = _config(repo_path=repo, dest_path=dest)

    monkeypatch.setattr(repository_backup, "GIT_BUNDLE_CREATE_TIMEOUT_SECONDS", 0.0001)

    result = repository_backup.run(_request(), config)

    assert result.success is False
    assert result.outcome == "timed_out"
    assert list(dest.iterdir()) == []


def test_timeout_reports_fixed_reply_cleans_partial_and_leaves_unrelated_files_untouched(
    tmp_path, monkeypatch
):
    # Milestone 35 correctness fix: an end-to-end proof, using the real
    # process_control.run_streaming_stdout_to_file() -> _terminate_and_reap()
    # path (not a mocked RunResult), that a timeout during bundle creation
    # leaves the destination directory in exactly the state a caller
    # should expect - the fixed timeout reply, the partial file gone, no
    # final bundle, an unrelated pre-existing file completely untouched -
    # and that none of this changes if more time passes afterward (i.e.
    # nothing was still running and writing when the handler returned).
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")
    dest = tmp_path / "dest"
    dest.mkdir()
    unrelated = dest / "unrelated-existing-backup.bundle"
    unrelated_content = b"an older, unrelated completed backup - must never be touched"
    unrelated.write_bytes(unrelated_content)
    config = _config(repo_path=repo, dest_path=dest)

    monkeypatch.setattr(repository_backup, "GIT_BUNDLE_CREATE_TIMEOUT_SECONDS", 0.0001)

    result = repository_backup.run(_request(), config)

    assert result.success is False
    assert result.outcome == "timed_out"
    assert result.message == repository_backup._TIMED_OUT.message

    # No partial and no final bundle immediately after return.
    entries_immediately_after = sorted(p.name for p in dest.iterdir())
    assert entries_immediately_after == [unrelated.name]
    assert unrelated.read_bytes() == unrelated_content

    # Waiting afterward changes nothing - no lingering process reappears
    # a partial file or produces a final one, and the unrelated file is
    # still byte-for-byte unchanged.
    time.sleep(1.2)
    entries_after_wait = sorted(p.name for p in dest.iterdir())
    assert entries_after_wait == [unrelated.name]
    assert unrelated.read_bytes() == unrelated_content


# --- Identity/tamper detection between checkpoints ---


def test_identity_change_after_verify_is_rejected(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")
    dest = tmp_path / "dest"
    dest.mkdir()
    config = _config(repo_path=repo, dest_path=dest)

    original_verify = repository_backup._verify_bundle

    def tampering_verify(canonical_repo, partial_path, env):
        result = original_verify(canonical_repo, partial_path, env)
        partial_path.unlink()
        partial_path.write_bytes(b"tampered content, different identity")
        return result

    monkeypatch.setattr(repository_backup, "_verify_bundle", tampering_verify)

    result = repository_backup.run(_request(), config)

    assert result.success is False
    assert result.outcome == "failed"
    assert list(dest.iterdir()) == []


def test_identity_change_after_hashing_is_rejected(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")
    dest = tmp_path / "dest"
    dest.mkdir()
    config = _config(repo_path=repo, dest_path=dest)

    original_hash = repository_backup._hash_and_size

    def tampering_hash(path):
        result = original_hash(path)
        path.unlink()
        path.write_bytes(b"tampered after hashing, different identity")
        return result

    monkeypatch.setattr(repository_backup, "_hash_and_size", tampering_hash)

    result = repository_backup.run(_request(), config)

    assert result.success is False
    assert result.outcome == "failed"
    assert list(dest.iterdir()) == []


def test_stat_identity_rejects_a_symlink_at_the_expected_path(tmp_path):
    real_file = tmp_path / "real.bundle"
    real_file.write_bytes(b"content")
    destination = tmp_path
    link = tmp_path / "link.bundle"

    try:
        os.symlink(real_file, link)
    except OSError:
        pytest.skip("symlink creation is not permitted in this environment")

    identity = repository_backup._stat_identity(link, destination.resolve())

    assert identity is None


def test_stat_identity_rejects_a_directory(tmp_path):
    a_directory = tmp_path / "not_a_file"
    a_directory.mkdir()

    identity = repository_backup._stat_identity(a_directory, tmp_path.resolve())

    assert identity is None


def test_stat_identity_rejects_wrong_parent(tmp_path):
    real_parent = tmp_path / "real_parent"
    real_parent.mkdir()
    other_parent = tmp_path / "other_parent"
    other_parent.mkdir()
    file_path = real_parent / "file.bundle"
    file_path.write_bytes(b"content")

    identity = repository_backup._stat_identity(file_path, other_parent.resolve())

    assert identity is None


def test_stat_identity_accepts_a_plain_regular_file(tmp_path):
    file_path = tmp_path / "file.bundle"
    file_path.write_bytes(b"content")

    identity = repository_backup._stat_identity(file_path, tmp_path.resolve())

    assert identity is not None
    assert identity.size == len(b"content")


# --- Hashing / size ---


def test_hash_and_size_use_streaming_reads_never_loading_the_whole_file(tmp_path, monkeypatch):
    file_path = tmp_path / "data.bin"
    payload = os.urandom(3 * repository_backup.HASH_CHUNK_SIZE + 12345)
    file_path.write_bytes(payload)

    read_sizes = []
    real_open = open

    def spying_open(path, mode="r", *args, **kwargs):
        f = real_open(path, mode, *args, **kwargs)
        if "b" in mode and str(path) == str(file_path):
            real_read = f.read

            def spying_read(n=-1):
                chunk = real_read(n)
                read_sizes.append(len(chunk) if chunk else 0)
                return chunk

            f.read = spying_read
        return f

    monkeypatch.setattr("builtins.open", spying_open)

    digest, size = repository_backup._hash_and_size(file_path)

    assert size == len(payload)
    assert digest == hashlib.sha256(payload).hexdigest()
    # Every chunk read is bounded by HASH_CHUNK_SIZE - never the whole file
    # in one read.
    assert all(n <= repository_backup.HASH_CHUNK_SIZE for n in read_sizes)
    assert len(read_sizes) > 1


def test_final_size_equals_hashed_size_and_checksum_is_64_lowercase_hex(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")
    dest = tmp_path / "dest"
    dest.mkdir()
    config = _config(repo_path=repo, dest_path=dest)

    result = repository_backup.run(_request(), config)
    assert result.success is True

    match = re.search(r"SHA-256: ([0-9a-f]+)", result.message)
    digest = match.group(1)
    assert len(digest) == 64
    assert digest == digest.lower()
    assert re.fullmatch(r"[0-9a-f]{64}", digest)

    bundle_path = next(dest.iterdir())
    actual_digest = hashlib.sha256(bundle_path.read_bytes()).hexdigest()
    assert digest == actual_digest


# --- Reply content discipline ---


def test_reply_never_contains_an_absolute_repository_or_destination_path(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")
    dest = tmp_path / "dest"
    dest.mkdir()
    config = _config(repo_path=repo, dest_path=dest)

    result = repository_backup.run(_request(), config)

    assert str(repo.resolve()) not in result.message
    assert str(dest.resolve()) not in result.message


def test_reply_does_not_overstate_exclusion_of_machine_local_files(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")
    dest = tmp_path / "dest"
    dest.mkdir()
    config = _config(repo_path=repo, dest_path=dest)

    result = repository_backup.run(_request(), config)

    assert "machine-local" not in result.message.lower()
    assert "Not included: current uncommitted, untracked, or ignored files" in result.message


# --- Streaming wiring proof (integration-level, complementing process_control's tests) ---


def test_bundle_create_argv_is_streamed_to_the_caller_owned_exclusive_file(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")
    dest = tmp_path / "dest"
    dest.mkdir()
    config = _config(repo_path=repo, dest_path=dest)

    calls = []
    original = repository_backup.run_streaming_stdout_to_file

    def recording(argv, cwd, timeout_seconds, output_file, env=None):
        calls.append((list(argv), cwd, output_file, env))
        return original(argv, cwd, timeout_seconds, output_file, env=env)

    monkeypatch.setattr(repository_backup, "run_streaming_stdout_to_file", recording)

    result = repository_backup.run(_request(), config)

    assert result.success is True
    assert len(calls) == 1
    argv, cwd, output_file, env = calls[0]
    assert argv[-6:] == ["bundle", "create", "-", "HEAD", "--branches", "--tags"]
    assert cwd == str(repo.resolve())
    assert hasattr(output_file, "fileno")
    assert env is not None
    assert env.get("GIT_CONFIG_NOSYSTEM") == "1"
