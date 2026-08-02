"""
Tests for kernel/tools/handlers/repo_health.py.

Like tests/kernel/tools/handlers/test_run_registered_script.py, most of
these tests deliberately run real, short-lived `git` subprocesses against
real temporary local repositories - repo_health's entire job is
inspecting real git state, which can't be meaningfully verified without
it. Every "remote" used here is a local bare repository (a plain
filesystem path, not a URL) - no test in this file ever contacts GitHub
or any real network service. The two exceptions (unreachable/timeout
handling) are explicitly mocked, since a real network hang would make the
suite slow and flaky.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from kernel.tools.config import RepoSpec, ToolsConfig
from kernel.tools.handlers import repo_health
from kernel.tools.process_control import CapturedResult
from kernel.tools.types import ActionRequest


def _run_git(args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    _run_git(["init", "-q"], path)
    _run_git(["symbolic-ref", "HEAD", "refs/heads/main"], path)
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


def _head_sha(path):
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(path), check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _config(approved_repositories):
    return ToolsConfig(
        approved_directories={},
        approved_applications={},
        approved_scripts={},
        approved_repositories=approved_repositories,
    )


def _request(key="repo"):
    return ActionRequest(action="repo_health", resource_key=key)


def test_unregistered_key_is_rejected(tmp_path):
    config = _config({})

    result = repo_health.run(_request(), config)

    assert result.success is False
    assert result.outcome == "rejected"
    assert "not registered" in result.message


def test_nonexistent_path_is_not_available(tmp_path):
    config = _config({"repo": RepoSpec(path=str(tmp_path / "does_not_exist"), main_branch="main")})

    result = repo_health.run(_request(), config)

    assert result.success is False
    assert result.outcome == "failed"
    assert "not available" in result.message


def test_directory_that_is_not_a_git_repo_is_not_available(tmp_path):
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    config = _config({"repo": RepoSpec(path=str(plain_dir), main_branch="main")})

    result = repo_health.run(_request(), config)

    assert result.success is False
    assert result.outcome == "failed"


def test_configured_subdirectory_of_a_worktree_is_rejected(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")
    subdir = repo / "subdir"
    subdir.mkdir()

    config = _config({"repo": RepoSpec(path=str(subdir), main_branch="main")})

    result = repo_health.run(_request(), config)

    assert result.success is False
    assert result.outcome == "failed"


def _with_mocked_ls_remote(monkeypatch, response):
    """Patches repo_health.run_capturing_stdout so the ls-remote call
    returns `response` (a CapturedResult) without ever actually running
    git against a transport - every other call (all local, read-only git
    commands against a real temporary repository) still runs for real.
    Matches this repository's fixed remote policy: production only ever
    permits https, so tests must not depend on a real - even local-file -
    ls-remote succeeding."""

    original = repo_health.run_capturing_stdout

    def fake(argv, cwd, timeout_seconds, env=None, max_output_bytes=4096):
        if "ls-remote" in argv:
            return response
        return original(argv, cwd, timeout_seconds, env=env, max_output_bytes=max_output_bytes)

    monkeypatch.setattr(repo_health, "run_capturing_stdout", fake)


def test_clean_repo_on_main_up_to_date_with_remote(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")
    _run_git(["remote", "add", "origin", "https://github.com/owner/repo.git"], repo)
    local_sha = _head_sha(repo)

    _with_mocked_ls_remote(
        monkeypatch,
        CapturedResult(
            success=True,
            timed_out=False,
            stdout=f"{local_sha}\trefs/heads/main\n".encode("utf-8"),
            returncode=0,
        ),
    )

    config = _config({"repo": RepoSpec(path=str(repo), main_branch="main")})

    result = repo_health.run(_request(), config)

    assert result.success is True
    assert result.outcome == "executed"
    assert "Repository: repo" in result.message
    assert "Branch: main" in result.message
    assert "Working tree: clean" in result.message
    assert "HEAD matches local main: yes" in result.message
    assert "Local main vs GitHub main: up to date" in result.message
    assert "Latest commit:" in result.message
    assert "first commit" in result.message


def test_remote_differing_sha_reports_differs(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")
    _run_git(["remote", "add", "origin", "https://github.com/owner/repo.git"], repo)

    _with_mocked_ls_remote(
        monkeypatch,
        CapturedResult(
            success=True,
            timed_out=False,
            stdout=b"a" * 40 + b"\trefs/heads/main\n",
            returncode=0,
        ),
    )

    config = _config({"repo": RepoSpec(path=str(repo), main_branch="main")})

    result = repo_health.run(_request(), config)

    assert result.success is True
    assert "Local main vs GitHub main: differs" in result.message


def test_remote_malformed_output_reports_remote_branch_unavailable(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")
    _run_git(["remote", "add", "origin", "https://github.com/owner/repo.git"], repo)

    _with_mocked_ls_remote(
        monkeypatch,
        CapturedResult(success=True, timed_out=False, stdout=b"not-hex-at-all\n", returncode=0),
    )

    config = _config({"repo": RepoSpec(path=str(repo), main_branch="main")})

    result = repo_health.run(_request(), config)

    assert result.success is True
    assert "Local main vs GitHub main: remote branch unavailable" in result.message


def test_dirty_working_tree_is_detected_and_filenames_are_never_relayed(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")

    (repo / "untracked_secret_name.txt").write_text("x", encoding="utf-8")

    config = _config({"repo": RepoSpec(path=str(repo), main_branch="main")})

    result = repo_health.run(_request(), config)

    assert result.success is True
    assert "Working tree: dirty" in result.message
    assert "untracked_secret_name.txt" not in result.message


def test_detached_head_reports_detached_branch(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")
    _commit(repo, "second commit")
    first_sha = subprocess.run(
        ["git", "rev-list", "--max-parents=0", "HEAD"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    _run_git(["checkout", "-q", first_sha], repo)

    config = _config({"repo": RepoSpec(path=str(repo), main_branch="main")})

    result = repo_health.run(_request(), config)

    assert result.success is True
    assert "Branch: detached" in result.message
    # Detached at a commit that is not main's tip.
    assert "HEAD matches local main: no" in result.message


def test_missing_local_main_branch_is_reported_without_failing(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _run_git(["checkout", "-q", "-b", "trunk"], repo)
    _commit(repo, "only commit on trunk")

    config = _config({"repo": RepoSpec(path=str(repo), main_branch="main")})

    result = repo_health.run(_request(), config)

    assert result.success is True
    assert result.outcome == "executed"
    assert "HEAD matches local main: local branch not found" in result.message
    assert "Local main vs GitHub main: local branch not found" in result.message


def test_remote_branch_unavailable_when_remote_has_no_matching_ref(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")
    _run_git(["remote", "add", "origin", "https://github.com/owner/repo.git"], repo)

    _with_mocked_ls_remote(
        monkeypatch, CapturedResult(success=True, timed_out=False, stdout=b"", returncode=0)
    )

    config = _config({"repo": RepoSpec(path=str(repo), main_branch="main")})

    result = repo_health.run(_request(), config)

    assert result.success is True
    assert "Local main vs GitHub main: remote branch unavailable" in result.message


def test_remote_timeout_reports_github_unreachable_but_command_still_succeeds(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")
    _run_git(["remote", "add", "origin", "https://github.com/owner/repo.git"], repo)

    _with_mocked_ls_remote(monkeypatch, CapturedResult(success=False, timed_out=True))

    config = _config({"repo": RepoSpec(path=str(repo), main_branch="main")})

    result = repo_health.run(_request(), config)

    assert result.success is True
    assert result.outcome == "executed"
    assert "Local main vs GitHub main: GitHub unreachable" in result.message


def test_remote_nonzero_exit_also_reports_github_unreachable(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")
    _run_git(["remote", "add", "origin", "https://github.com/owner/repo.git"], repo)

    _with_mocked_ls_remote(
        monkeypatch, CapturedResult(success=False, timed_out=False, returncode=128)
    )

    config = _config({"repo": RepoSpec(path=str(repo), main_branch="main")})

    result = repo_health.run(_request(), config)

    assert result.success is True
    assert "Local main vs GitHub main: GitHub unreachable" in result.message


def test_malformed_branch_name_fails_closed_rather_than_being_displayed(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")

    original = repo_health.run_capturing_stdout

    def fake_run_capturing_stdout(argv, cwd, timeout_seconds, env=None, max_output_bytes=4096):
        if "--abbrev-ref" in argv:
            return CapturedResult(success=True, timed_out=False, stdout=b"weird branch\x07name\n", returncode=0)
        return original(argv, cwd, timeout_seconds, env=env, max_output_bytes=max_output_bytes)

    monkeypatch.setattr(repo_health, "run_capturing_stdout", fake_run_capturing_stdout)

    config = _config({"repo": RepoSpec(path=str(repo), main_branch="main")})

    result = repo_health.run(_request(), config)

    assert result.success is False
    assert result.outcome == "failed"
    assert "not available" in result.message


def test_malformed_head_sha_fails_closed(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")

    original = repo_health.run_capturing_stdout

    def fake_run_capturing_stdout(argv, cwd, timeout_seconds, env=None, max_output_bytes=4096):
        if argv[-2:] == ["rev-parse", "HEAD"]:
            return CapturedResult(success=True, timed_out=False, stdout=b"not-a-valid-sha!!\n", returncode=0)
        return original(argv, cwd, timeout_seconds, env=env, max_output_bytes=max_output_bytes)

    monkeypatch.setattr(repo_health, "run_capturing_stdout", fake_run_capturing_stdout)

    config = _config({"repo": RepoSpec(path=str(repo), main_branch="main")})

    result = repo_health.run(_request(), config)

    assert result.success is False
    assert result.outcome == "failed"


def test_commit_subject_is_sanitized_and_capped(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    # A blank line separates git's "subject" (first paragraph, what %s
    # returns) from the body - the weird characters live in the subject
    # itself so this test isolates repo_health's own sanitization from
    # git's own subject-extraction behavior (which joins a wrapped
    # paragraph's lines with spaces on its own).
    weird_subject = "weird\tsubject\x07with  control   chars " + ("x" * 200)
    body = "this body paragraph must never appear in the reply"
    _commit(repo, f"{weird_subject}\n\n{body}")

    config = _config({"repo": RepoSpec(path=str(repo), main_branch="main")})

    result = repo_health.run(_request(), config)

    assert result.success is True
    commit_line = next(line for line in result.message.splitlines() if line.startswith("Latest commit:"))
    assert "\n" not in commit_line
    assert "\t" not in commit_line
    assert "\x07" not in commit_line
    assert "this body paragraph" not in commit_line
    assert "  " not in commit_line  # whitespace normalized to single spaces
    # "Latest commit: <hash> " prefix plus a subject capped at 120 chars.
    assert len(commit_line) <= len("Latest commit: ") + 40 + 1 + 120


def test_neither_repository_path_nor_remote_url_ever_appears_in_the_reply(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")
    remote_url = "https://github.com/owner/repo.git"
    _run_git(["remote", "add", "origin", remote_url], repo)

    _with_mocked_ls_remote(
        monkeypatch,
        CapturedResult(
            success=True, timed_out=False, stdout=f"{_head_sha(repo)}\trefs/heads/main\n".encode("utf-8"), returncode=0
        ),
    )

    config = _config({"repo": RepoSpec(path=str(repo), main_branch="main")})

    result = repo_health.run(_request(), config)

    assert str(repo) not in result.message
    assert remote_url not in result.message
    assert "github.com" not in result.message


def _has_env_key(env, name):
    return any(k.upper() == name.upper() for k in env)


def _record_calls(monkeypatch, ls_remote_response=None):
    """Patches repo_health.run_capturing_stdout to record every call's
    (argv, cwd, env) and, for the ls-remote call specifically, return
    `ls_remote_response` (a CapturedResult) instead of ever running it for
    real - so tests can inspect exactly what would have been sent to the
    network without ever actually contacting it. Every other call (all
    local, read-only git commands, including the neutral-directory
    work-tree probe) still runs for real against real temporary
    directories."""

    calls = []
    original = repo_health.run_capturing_stdout

    def recording(argv, cwd, timeout_seconds, env=None, max_output_bytes=4096):
        calls.append((list(argv), cwd, dict(env) if env is not None else None))
        if "ls-remote" in argv:
            return ls_remote_response or CapturedResult(success=True, timed_out=False, stdout=b"", returncode=0)
        return original(argv, cwd, timeout_seconds, env=env, max_output_bytes=max_output_bytes)

    monkeypatch.setattr(repo_health, "run_capturing_stdout", recording)
    return calls


def test_every_git_call_uses_the_safety_prefix_and_environment(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")
    _run_git(["remote", "add", "origin", "https://github.com/owner/repo.git"], repo)

    calls = _record_calls(monkeypatch)

    config = _config({"repo": RepoSpec(path=str(repo), main_branch="main")})
    result = repo_health.run(_request(), config)

    assert result.success is True

    local_calls = [c for c in calls if "ls-remote" not in c[0]]
    remote_calls = [c for c in calls if "ls-remote" in c[0]]
    # 7 checks against the repo itself + reading remote.origin.url (also
    # against the repo) + the neutral-directory work-tree probe = 9.
    assert len(local_calls) == 9
    assert len(remote_calls) == 1

    # Every single git subprocess - local and remote alike - carries the
    # fixed safety prefix, GIT_OPTIONAL_LOCKS=0, GIT_CONFIG_NOSYSTEM=1,
    # and GIT_CONFIG_GLOBAL=os.devnull, and starts from a real copy of
    # this process's environment (proven by a variable no test ever
    # sets, like PATH), never a fabricated minimal mapping.
    for argv, _cwd, env in calls:
        assert argv[:6] == [
            "git", "--no-optional-locks", "--no-pager", "--no-replace-objects", "-c", "core.fsmonitor=false",
        ]
        assert env is not None
        assert env.get("GIT_OPTIONAL_LOCKS") == "0"
        assert env.get("GIT_CONFIG_NOSYSTEM") == "1"
        assert env.get("GIT_CONFIG_GLOBAL") == os.devnull
        assert _has_env_key(env, "PATH")

    # Every call that touches the approved repository itself uses it as
    # cwd - except the remote call, which must not.
    canonical_repo = str(repo.resolve())
    repo_calls = [c for c in local_calls if canonical_repo == c[1]]
    assert len(repo_calls) == 8

    # Local calls never have GIT_CEILING_DIRECTORIES set at all (only the
    # remote call sets its own controlled value, below).
    for _argv, _cwd, env in local_calls:
        assert "GIT_CEILING_DIRECTORIES" not in env

    remote_argv, remote_cwd, remote_env = remote_calls[0]
    assert remote_argv[-2] == "https://github.com/owner/repo.git"
    assert remote_cwd != canonical_repo
    assert "protocol.allow=never" in remote_argv
    assert "protocol.https.allow=always" in remote_argv
    assert "protocol.file.allow=always" not in remote_argv
    assert "credential.helper=" in remote_argv
    assert "core.askPass=" in remote_argv
    assert "http.extraHeader=" in remote_argv
    assert "http.proxy=" in remote_argv
    assert "http.sslVerify=true" in remote_argv
    assert remote_env.get("GIT_TERMINAL_PROMPT") == "0"
    assert remote_env.get("GCM_INTERACTIVE") == "Never"
    assert remote_env.get("GIT_CONFIG_NOSYSTEM") == "1"
    assert remote_env.get("GIT_CONFIG_GLOBAL") == os.devnull
    assert remote_env.get("GIT_CEILING_DIRECTORIES") == remote_cwd
    assert _has_env_key(remote_env, "PATH")


def test_repository_configured_fsmonitor_hook_never_runs(tmp_path):
    # A malicious or misconfigured repository could set core.fsmonitor to
    # an arbitrary command that git would otherwise invoke on every
    # status-touching command. Prove it never runs: point it at a command
    # that would leave an unmistakable marker file if git ever executed
    # it, then confirm that marker is absent after a normal report.
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")
    marker = tmp_path / "fsmonitor_ran.txt"
    hook_cmd = f'{sys.executable} -c "open(r\'{marker}\', \'w\').write(\'1\')"'
    _run_git(["config", "core.fsmonitor", hook_cmd], repo)

    config = _config({"repo": RepoSpec(path=str(repo), main_branch="main")})

    result = repo_health.run(_request(), config)

    assert result.success is True
    assert not marker.exists()


def test_repository_configured_protocol_ext_helper_never_runs(tmp_path):
    # A repository could configure a remote using a non-file, non-https
    # protocol (e.g. an "ext::" remote helper that runs an arbitrary
    # command) - origin validation rejects this outright (it isn't a
    # bare github.com https URL at all), so ls-remote is never even
    # attempted, let alone the ext:: helper ever invoked.
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")
    marker = tmp_path / "ext_helper_ran.txt"
    ext_url = f'ext::{sys.executable} -c "open(r\'{marker}\', \'w\').write(\'1\')"'
    _run_git(["remote", "add", "origin", ext_url], repo)

    config = _config({"repo": RepoSpec(path=str(repo), main_branch="main")})

    result = repo_health.run(_request(), config)

    assert result.success is True
    assert "Local main vs GitHub main: GitHub unreachable" in result.message
    assert not marker.exists()


@pytest.mark.parametrize(
    "invalid_branch",
    [
        "-main",
        "/main",
        "main/",
        ".main",
        "feature/.hidden",
        "main.",
        "feature.lock",
        "feature/test.lock",
        "main..old",
        "feature//test",
        "main@{1}",
        "@",
        "has space",
        "has\tcontrol",
    ],
)
def test_invalid_main_branch_fails_closed_at_runtime(tmp_path, invalid_branch):
    # Bypasses kernel/tools/config.py's own load-time validation entirely
    # by constructing the RepoSpec directly - proves repo_health's own
    # redundant runtime check (not just the config loader) rejects these.
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")

    config = _config({"repo": RepoSpec(path=str(repo), main_branch=invalid_branch)})

    result = repo_health.run(_request(), config)

    assert result.success is False
    assert result.outcome == "failed"


def test_local_file_remote_is_rejected_now_that_file_protocol_is_denied(tmp_path):
    # Unmocked, end-to-end proof: a real, fully local bare-repository
    # remote (the "file" transport) is rejected exactly like any other
    # unreachable remote, even though nothing else about it is wrong.
    # Origin validation rejects it first (its URL isn't even an https
    # github.com URL), and the protocol allowlist would reject it too
    # even if origin validation somehow didn't - defense in depth. No
    # network is contacted either way.
    remote = tmp_path / "remote.git"
    _run_git(["init", "-q", "--bare", str(remote)], tmp_path)

    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")
    _run_git(["remote", "add", "origin", str(remote)], repo)
    _run_git(["push", "-q", "origin", "main"], repo)

    config = _config({"repo": RepoSpec(path=str(repo), main_branch="main")})

    result = repo_health.run(_request(), config)

    assert result.success is True
    assert result.outcome == "executed"
    assert "Local main vs GitHub main: GitHub unreachable" in result.message


def test_sanitized_git_env_retains_path():
    env = repo_health._sanitized_git_env()

    assert _has_env_key(env, "PATH")


def test_sanitized_git_env_sets_the_controlled_values():
    env = repo_health._sanitized_git_env()

    assert env["GIT_OPTIONAL_LOCKS"] == "0"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull


@pytest.mark.parametrize(
    "blocked_var",
    [
        # config/credential injection
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_KEY_17",
        "GIT_CONFIG_VALUE_0",
        "GIT_CONFIG_VALUE_17",
        "GIT_EXEC_PATH",
        "GIT_ASKPASS",
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "SSH_ASKPASS",
        # repository/ref/object/index redirection
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_NAMESPACE",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_CEILING_DIRECTORIES",
        "GIT_REPLACE_REF_BASE",
        # transport security / tracing / stdio redirection
        "GIT_SSL_NO_VERIFY",
        "GIT_CURL_VERBOSE",
        "GIT_TRACE",
        "GIT_TRACE_CURL",
        "GIT_TRACE2",
        "GIT_TRACE2_EVENT",
        "GIT_REDIRECT_STDIN",
        "GIT_REDIRECT_STDOUT",
        "GIT_REDIRECT_STDERR",
    ],
)
def test_sanitized_git_env_removes_injection_variables(monkeypatch, blocked_var):
    monkeypatch.setenv(blocked_var, "malicious-value")

    env = repo_health._sanitized_git_env()

    assert not _has_env_key(env, blocked_var)


def test_sanitized_git_env_removal_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("git_config_key_5", "core.pager")
    monkeypatch.setenv("git_dir", "C:/malicious")
    monkeypatch.setenv("git_trace_curl", "1")

    env = repo_health._sanitized_git_env()

    assert not _has_env_key(env, "GIT_CONFIG_KEY_5")
    assert not _has_env_key(env, "GIT_DIR")
    assert not _has_env_key(env, "GIT_TRACE_CURL")


def test_git_safe_prefix_contains_no_replace_objects():
    assert "--no-replace-objects" in repo_health._GIT_SAFE_PREFIX


def test_no_marker_file_is_created_via_inherited_trace_variable(tmp_path, monkeypatch):
    # GIT_TRACE, when set to a path, makes git append trace output
    # (including transport detail) to that file on every invocation. If
    # this variable were not stripped before spawning any real git
    # subprocess, this file would exist after a normal report.
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")
    trace_marker = tmp_path / "git_trace_output.log"
    monkeypatch.setenv("GIT_TRACE", str(trace_marker))

    config = _config({"repo": RepoSpec(path=str(repo), main_branch="main")})
    result = repo_health.run(_request(), config)

    assert result.success is True
    assert not trace_marker.exists()


def test_no_marker_file_is_created_via_inherited_curl_trace_variable(tmp_path, monkeypatch):
    # No origin is configured, so the local report already short-circuits
    # to "GitHub unreachable" without ever attempting ls-remote - no
    # network is contacted regardless of whether the trace variable is
    # honored.
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")
    trace_marker = tmp_path / "git_trace_curl_output.log"
    monkeypatch.setenv("GIT_TRACE_CURL", str(trace_marker))

    config = _config({"repo": RepoSpec(path=str(repo), main_branch="main")})
    result = repo_health.run(_request(), config)

    assert result.success is True
    assert not trace_marker.exists()


def test_git_config_injection_env_vars_never_reach_a_real_git_subprocess(tmp_path, monkeypatch):
    # Proves the defense holds regardless of how an injection-shaped
    # variable ended up in this process's environment (e.g. inherited
    # from a parent shell) - none of it ever reaches a git subprocess
    # call this handler makes, local or remote.
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")
    _run_git(["remote", "add", "origin", "https://github.com/owner/repo.git"], repo)

    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'core.pager=malicious'")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.pager")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "malicious-command")
    monkeypatch.setenv("GIT_ASKPASS", "malicious-askpass")
    monkeypatch.setenv("GIT_SSH_COMMAND", "malicious-ssh")
    monkeypatch.setenv("GIT_EXEC_PATH", str(tmp_path / "malicious"))

    calls = _record_calls(monkeypatch)

    config = _config({"repo": RepoSpec(path=str(repo), main_branch="main")})
    result = repo_health.run(_request(), config)

    assert result.success is True
    injected_names = {
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
        "GIT_ASKPASS",
        "GIT_SSH_COMMAND",
        "GIT_EXEC_PATH",
    }
    assert len(calls) == 10  # 9 local + 1 remote
    for _argv, _cwd, env in calls:
        present = {k for k in env if k.upper() in injected_names}
        assert present == set()


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://github.com/owner/repo.git", "https://github.com/owner/repo.git"),
        ("https://github.com/owner/repo", "https://github.com/owner/repo.git"),
        ("https://GitHub.COM/owner/repo.git", "https://github.com/owner/repo.git"),
        ("https://GITHUB.COM/Owner-Name/repo_name", "https://github.com/Owner-Name/repo_name.git"),
        ("https://github.com/a/b", "https://github.com/a/b.git"),
    ],
)
def test_parse_github_origin_accepts_and_normalizes(raw, expected):
    assert repo_health._parse_github_origin(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not a url at all",
        "https://gitlab.com/owner/repo.git",
        "https://github.com.evil.com/owner/repo.git",
        "https://evil.com/github.com/owner/repo.git",
        "https://user@github.com/owner/repo.git",
        "https://user:pass@github.com/owner/repo.git",
        "https://:pass@github.com/owner/repo.git",
        "https://github.com:443/owner/repo.git",
        "https://github.com:8443/owner/repo.git",
        "https://github.com/owner/repo.git?ref=main",
        "https://github.com/owner/repo.git#section",
        "https://github.com/owner/re%zzpo.git",
        "https://github.com/owner/repo%2",
        "ssh://git@github.com/owner/repo.git",
        "git@github.com:owner/repo.git",
        "git://github.com/owner/repo.git",
        "file:///some/local/path",
        "file://github.com/owner/repo.git",
        "ext::sh -c 'evil'",
        "https://github.com/owner",
        "https://github.com/owner/repo/extra",
        "https://github.com/",
        "https://github.com",
        "https://github.com/owner/",
        "https://github.com/ow ner/repo.git",
        "https://github.com/owner/re\tpo.git",
        "https://github.com/.hidden/repo.git",
        "https://github.com/owner/.git",
        "https://github.com/owner/.hidden",
        "https://github.com/-owner/repo.git",
    ],
)
def test_parse_github_origin_rejects(raw):
    assert repo_health._parse_github_origin(raw) is None


def _with_mocked_origin_read(monkeypatch, stdout: bytes):
    """Patches repo_health.run_capturing_stdout so the origin-URL read
    (`git config ... --get-all -z remote.origin.url`) returns `stdout`
    verbatim (success, zero-exit) - every other call, including the real
    ls-remote-argv-building path, still runs/executes as normal (ls-remote
    itself is never reached here since a rejected origin short-circuits
    before it)."""

    original = repo_health.run_capturing_stdout

    def fake(argv, cwd, timeout_seconds, env=None, max_output_bytes=4096):
        if "remote.origin.url" in argv:
            return CapturedResult(success=True, timed_out=False, stdout=stdout, returncode=0)
        return original(argv, cwd, timeout_seconds, env=env, max_output_bytes=max_output_bytes)

    monkeypatch.setattr(repo_health, "run_capturing_stdout", fake)


def test_multiple_origin_url_values_in_mocked_nul_delimited_output_are_rejected(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")

    _with_mocked_origin_read(
        monkeypatch,
        b"https://github.com/owner/repo.git\x00https://github.com/other/repo.git\x00",
    )

    config = _config({"repo": RepoSpec(path=str(repo), main_branch="main")})
    result = repo_health.run(_request(), config)

    assert result.success is True
    assert result.outcome == "executed"
    assert "Local main vs GitHub main: GitHub unreachable" in result.message


def test_repository_with_multiple_real_origin_urls_is_rejected_as_unreachable(tmp_path):
    # Real, unmocked: `git config --local --get` on a multi-valued key
    # behaves inconsistently across git versions (a warning plus the
    # last value, or an outright error) - --get-all -z sidesteps that
    # entirely by letting us positively enumerate every value ourselves.
    # This proves the real git invocation and our real parsing correctly
    # detect and reject the multi-value case end to end.
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")
    _run_git(["remote", "add", "origin", "https://github.com/owner/repo.git"], repo)
    _run_git(["config", "--add", "remote.origin.url", "https://github.com/owner/other.git"], repo)

    config = _config({"repo": RepoSpec(path=str(repo), main_branch="main")})
    result = repo_health.run(_request(), config)

    assert result.success is True
    assert result.outcome == "executed"
    assert "Local main vs GitHub main: GitHub unreachable" in result.message
    # The local portion of the report is unaffected.
    assert "Branch: main" in result.message
    assert "Working tree: clean" in result.message


def test_zero_origin_url_values_are_rejected(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")

    _with_mocked_origin_read(monkeypatch, b"")

    config = _config({"repo": RepoSpec(path=str(repo), main_branch="main")})
    result = repo_health.run(_request(), config)

    assert result.success is True
    assert "Local main vs GitHub main: GitHub unreachable" in result.message


def test_malformed_origin_url_output_is_rejected(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")

    _with_mocked_origin_read(monkeypatch, b"not a url at all\x00")

    config = _config({"repo": RepoSpec(path=str(repo), main_branch="main")})
    result = repo_health.run(_request(), config)

    assert result.success is True
    assert "Local main vs GitHub main: GitHub unreachable" in result.message


def test_origin_url_output_missing_nul_termination_is_rejected(tmp_path, monkeypatch):
    # -z always NUL-terminates every value, including the last - stdout
    # that doesn't end with NUL is truncated or malformed and must never
    # be trusted, even though it looks like exactly one valid value.
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")

    _with_mocked_origin_read(monkeypatch, b"https://github.com/owner/repo.git")

    config = _config({"repo": RepoSpec(path=str(repo), main_branch="main")})
    result = repo_health.run(_request(), config)

    assert result.success is True
    assert "Local main vs GitHub main: GitHub unreachable" in result.message


def test_origin_read_argv_uses_get_all_no_includes_and_z(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")
    _run_git(["remote", "add", "origin", "https://github.com/owner/repo.git"], repo)

    calls = _record_calls(monkeypatch)

    config = _config({"repo": RepoSpec(path=str(repo), main_branch="main")})
    repo_health.run(_request(), config)

    origin_calls = [c for c in calls if "remote.origin.url" in c[0]]
    assert len(origin_calls) == 1
    origin_argv = origin_calls[0][0]
    assert "--get-all" in origin_argv
    assert "--no-includes" in origin_argv
    assert "-z" in origin_argv
    assert "--get" not in origin_argv


def test_local_report_still_succeeds_when_origin_is_not_github(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")
    _run_git(["remote", "add", "origin", "https://gitlab.com/owner/repo.git"], repo)

    config = _config({"repo": RepoSpec(path=str(repo), main_branch="main")})
    result = repo_health.run(_request(), config)

    assert result.success is True
    assert result.outcome == "executed"
    assert "Repository: repo" in result.message
    assert "Branch: main" in result.message
    assert "Working tree: clean" in result.message
    assert "Latest commit:" in result.message
    assert "HEAD matches local main: yes" in result.message
    assert "Local main vs GitHub main: GitHub unreachable" in result.message


def test_local_report_still_succeeds_when_origin_is_absent(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")
    # No `git remote add` at all.

    config = _config({"repo": RepoSpec(path=str(repo), main_branch="main")})
    result = repo_health.run(_request(), config)

    assert result.success is True
    assert result.outcome == "executed"
    assert "Working tree: clean" in result.message
    assert "Local main vs GitHub main: GitHub unreachable" in result.message


def test_repository_configured_url_rewrite_cannot_affect_the_ls_remote_argv(tmp_path, monkeypatch):
    # url.<base>.insteadOf lets a repository silently rewrite any URL
    # matching a prefix to a different one - configuring it here, in the
    # target repository's own local config, must have no effect: the
    # remote call runs from a neutral directory with system/global config
    # disabled and GIT_CEILING_DIRECTORIES pinned there, so it never even
    # discovers this repository's .git/config for that subprocess.
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")
    _run_git(["remote", "add", "origin", "https://github.com/owner/repo.git"], repo)
    _run_git(
        ["config", "url.https://evil.example/hijacked.insteadOf", "https://github.com/"], repo
    )

    calls = _record_calls(monkeypatch)

    config = _config({"repo": RepoSpec(path=str(repo), main_branch="main")})
    result = repo_health.run(_request(), config)

    assert result.success is True
    remote_calls = [c for c in calls if "ls-remote" in c[0]]
    assert len(remote_calls) == 1
    remote_argv, remote_cwd, _env = remote_calls[0]
    assert "https://github.com/owner/repo.git" in remote_argv
    assert not any("evil.example" in arg for arg in remote_argv)
    assert remote_cwd != str(repo.resolve())


def test_repository_configured_http_and_credential_settings_do_not_reach_the_remote_call(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")
    _run_git(["remote", "add", "origin", "https://github.com/owner/repo.git"], repo)
    _run_git(["config", "http.extraHeader", "Authorization: Bearer malicious-token"], repo)
    _run_git(["config", "http.proxy", "http://attacker.example:8080"], repo)
    _run_git(["config", "credential.helper", f"{sys.executable} -c \"print('malicious-token')\""], repo)
    _run_git(
        ["config", "url.https://evil.example/.insteadOf", "https://github.com/"], repo
    )

    calls = _record_calls(monkeypatch)

    config = _config({"repo": RepoSpec(path=str(repo), main_branch="main")})
    result = repo_health.run(_request(), config)

    assert result.success is True
    remote_calls = [c for c in calls if "ls-remote" in c[0]]
    assert len(remote_calls) == 1
    remote_argv, remote_cwd, remote_env = remote_calls[0]

    # Our own fixed overrides are present, unconditionally...
    assert "credential.helper=" in remote_argv
    assert "core.askPass=" in remote_argv
    assert "http.extraHeader=" in remote_argv
    assert "http.proxy=" in remote_argv
    # ...and the repository's own settings never appear anywhere in argv.
    joined = " ".join(remote_argv)
    assert "malicious-token" not in joined
    assert "attacker.example" not in joined
    assert "evil.example" not in joined
    # The call runs outside the repository entirely, with system/global
    # config disabled and the ceiling pinned - so even if our overrides
    # were somehow absent, this repository's config could not be found.
    assert remote_cwd != str(repo.resolve())
    assert remote_env.get("GIT_CONFIG_NOSYSTEM") == "1"
    assert remote_env.get("GIT_CONFIG_GLOBAL") == os.devnull
    assert remote_env.get("GIT_CEILING_DIRECTORIES") == remote_cwd


@pytest.mark.parametrize(
    "proxy_var", ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]
)
def test_proxy_environment_variables_are_removed_for_the_remote_call(tmp_path, monkeypatch, proxy_var):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")
    _run_git(["remote", "add", "origin", "https://github.com/owner/repo.git"], repo)
    monkeypatch.setenv(proxy_var, "http://attacker.example:8080")

    calls = _record_calls(monkeypatch)

    config = _config({"repo": RepoSpec(path=str(repo), main_branch="main")})
    result = repo_health.run(_request(), config)

    assert result.success is True
    remote_calls = [c for c in calls if "ls-remote" in c[0]]
    assert len(remote_calls) == 1
    _argv, _cwd, remote_env = remote_calls[0]
    assert not _has_env_key(remote_env, proxy_var)
    assert not _has_env_key(remote_env, "HTTP_PROXY")
    assert not _has_env_key(remote_env, "HTTPS_PROXY")
    assert not _has_env_key(remote_env, "ALL_PROXY")
    assert _has_env_key(remote_env, "PATH")


def test_neutral_cwd_rejects_a_directory_inside_the_approved_repository(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")
    subdir = repo / "would_be_neutral"
    subdir.mkdir()

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(subdir))

    result = repo_health._neutral_cwd(repo.resolve())

    assert result is None


def test_neutral_cwd_rejects_a_directory_that_is_itself_a_worktree(tmp_path, monkeypatch):
    other_repo = _init_repo(tmp_path / "other_repo")
    _commit(other_repo, "first commit")
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(other_repo))

    result = repo_health._neutral_cwd(repo.resolve())

    assert result is None


def test_neutral_cwd_accepts_the_real_system_temp_directory(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "first commit")

    result = repo_health._neutral_cwd(repo.resolve())

    assert result is not None
    assert result == Path(tempfile.gettempdir()).resolve()
