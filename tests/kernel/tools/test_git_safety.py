"""
Tests for kernel/tools/git_safety.py - the local git-execution hardening
extracted from repo_health.py in Milestone 35 and reused by
repository_backup.py. See test_repo_health.py for the full end-to-end
proof (real repositories, injected env vars) that these protections
actually stop a configured fsmonitor hook / trace variable / injection
variable from taking effect; this file only covers the module directly.
"""

import os

from kernel.tools import git_safety


def _has_env_key(env, name):
    return any(k.upper() == name.upper() for k in env)


def test_git_safe_prefix_shape():
    assert git_safety.GIT_SAFE_PREFIX == [
        "git",
        "--no-optional-locks",
        "--no-pager",
        "--no-replace-objects",
        "-c",
        "core.fsmonitor=false",
    ]


def test_sanitized_git_env_retains_path():
    env = git_safety.sanitized_git_env()

    assert _has_env_key(env, "PATH")


def test_sanitized_git_env_sets_the_controlled_values():
    env = git_safety.sanitized_git_env()

    assert env["GIT_OPTIONAL_LOCKS"] == "0"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull


def test_sanitized_git_env_returns_a_fresh_copy_each_call():
    first = git_safety.sanitized_git_env()
    first["GIT_OPTIONAL_LOCKS"] = "tampered"

    second = git_safety.sanitized_git_env()

    assert second["GIT_OPTIONAL_LOCKS"] == "0"


def test_sanitized_git_env_removes_injection_variables(monkeypatch):
    for var in (
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
        "GIT_EXEC_PATH",
        "GIT_ASKPASS",
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "SSH_ASKPASS",
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
        "GIT_SSL_NO_VERIFY",
        "GIT_CURL_VERBOSE",
        "GIT_TRACE",
        "GIT_TRACE_CURL",
        "GIT_TRACE2",
        "GIT_REDIRECT_STDIN",
        "GIT_REDIRECT_STDOUT",
        "GIT_REDIRECT_STDERR",
    ):
        monkeypatch.setenv(var, "malicious-value")

    env = git_safety.sanitized_git_env()

    for var in (
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
        "GIT_EXEC_PATH",
        "GIT_ASKPASS",
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "SSH_ASKPASS",
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
        "GIT_SSL_NO_VERIFY",
        "GIT_CURL_VERBOSE",
        "GIT_TRACE",
        "GIT_TRACE_CURL",
        "GIT_TRACE2",
        "GIT_REDIRECT_STDIN",
        "GIT_REDIRECT_STDOUT",
        "GIT_REDIRECT_STDERR",
    ):
        assert not _has_env_key(env, var)


def test_sanitized_git_env_removal_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("git_config_key_5", "core.pager")
    monkeypatch.setenv("git_dir", "C:/malicious")
    monkeypatch.setenv("git_trace_curl", "1")

    env = git_safety.sanitized_git_env()

    assert not _has_env_key(env, "GIT_CONFIG_KEY_5")
    assert not _has_env_key(env, "GIT_DIR")
    assert not _has_env_key(env, "GIT_TRACE_CURL")


def test_repo_health_reuses_the_same_prefix_and_env_function():
    from kernel.tools.handlers import repo_health

    assert repo_health._GIT_SAFE_PREFIX is git_safety.GIT_SAFE_PREFIX
    assert repo_health._sanitized_git_env is git_safety.sanitized_git_env
