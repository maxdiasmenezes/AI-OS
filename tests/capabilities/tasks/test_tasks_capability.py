"""Tests for capabilities/tasks/capability.py: TasksCapability."""

import subprocess
import sys

from kernel.tools.config import ApplicationSpec, RepoBackupSpec, RepoSpec, ToolsConfig, ToolsConfigError
from kernel.tools.confirmation import ConfirmationStore

from capabilities.tasks.capability import (
    HELP_TEXT,
    TasksCapability,
)


def _make_capability(tools_config=None, confirmation_store=None, ttl_seconds=120.0):
    if confirmation_store is None:
        confirmation_store = ConfirmationStore(ttl_seconds=ttl_seconds)
    loader = (lambda: tools_config) if tools_config is not None else (lambda: ToolsConfig({}, {}, {}))
    return TasksCapability(
        None, None, None, confirmation_store=confirmation_store, tools_config_loader=loader
    ), confirmation_store


def _init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(
        ["git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=str(path), check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=str(path), check=True, capture_output=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), check=True, capture_output=True)
    (path / "file.txt").write_text("content", encoding="utf-8")
    subprocess.run(["git", "add", "file.txt"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "first commit"], cwd=str(path), check=True, capture_output=True
    )
    return path


def test_requires_computer_actions_is_true():
    capability, _ = _make_capability()

    assert capability.requires_computer_actions is True
    assert capability.id == "tasks"


def test_unrecognized_command_returns_a_generic_error():
    capability, _ = _make_capability()

    response = capability.handle("not a task command")

    assert "Unrecognized command" in response


def test_help_returns_the_fixed_help_text():
    capability, _ = _make_capability()

    assert capability.handle("/task help") == HELP_TEXT
    assert capability.handle("/task") == HELP_TEXT


def test_status_executes_immediately_without_confirmation(deterministic_system_status):
    # deterministic_system_status (tests/conftest.py) patches every real
    # machine/service reading - this never contacts a real Ollama or
    # ngrok, and never depends on either being running.
    capability, store = _make_capability()

    response = capability.handle("/task status")

    assert response == deterministic_system_status
    pending, _ = store.consume()
    assert pending is None  # status is not sensitive - nothing was proposed


def test_files_with_unregistered_key_is_rejected():
    capability, _ = _make_capability(tools_config=ToolsConfig({}, {}, {}))

    response = capability.handle("/task files documents")

    assert "not registered" in response


def test_files_with_registered_key_lists_contents(tmp_path):
    (tmp_path / "a.txt").write_text("a")
    config = ToolsConfig(approved_directories={"documents": str(tmp_path)}, approved_applications={}, approved_scripts={})
    capability, _ = _make_capability(tools_config=config)

    response = capability.handle("/task files documents")

    assert "a.txt" in response


def test_repo_with_unregistered_key_is_rejected():
    capability, _ = _make_capability(tools_config=ToolsConfig({}, {}, {}, {}))

    response = capability.handle("/task repo ai_os")

    assert "not registered" in response


def test_repo_with_registered_key_executes_immediately_without_confirmation(tmp_path):
    repo_path = _init_repo(tmp_path / "repo")
    config = ToolsConfig(
        approved_directories={},
        approved_applications={},
        approved_scripts={},
        approved_repositories={"ai_os": RepoSpec(path=str(repo_path), main_branch="main")},
    )
    capability, store = _make_capability(tools_config=config)

    response = capability.handle("/task repo ai_os")

    assert "Repository: ai_os" in response
    assert "Branch: main" in response
    pending, _ = store.consume()
    assert pending is None  # repo_health is not sensitive - nothing was proposed


def test_sensitive_action_is_proposed_not_executed_immediately(tmp_path):
    config = ToolsConfig(
        approved_directories={},
        approved_applications={"notepad": ApplicationSpec(executable=str(tmp_path / "notepad.exe"), cwd=str(tmp_path))},
        approved_scripts={},
    )
    capability, store = _make_capability(tools_config=config)

    response = capability.handle("/task open notepad")

    assert "confirm" in response.lower()
    pending, expired = store.consume()
    assert expired is False
    assert pending.action == "open_application"
    assert pending.resource_key == "notepad"


def test_confirm_with_nothing_pending_says_so():
    capability, _ = _make_capability()

    response = capability.handle("/task confirm")

    assert "no pending action" in response.lower()


def test_confirm_with_expired_confirmation_says_expired(tmp_path):
    config = ToolsConfig(
        approved_directories={},
        approved_applications={"notepad": ApplicationSpec(executable=str(tmp_path / "notepad.exe"), cwd=str(tmp_path))},
        approved_scripts={},
    )
    capability, store = _make_capability(tools_config=config, ttl_seconds=0.01)

    capability.handle("/task open notepad")
    import time

    time.sleep(0.05)
    response = capability.handle("/task confirm")

    assert "expired" in response.lower()


def test_confirm_executes_the_pending_action_exactly_once(tmp_path):
    config = ToolsConfig(
        approved_directories={},
        approved_applications={"sleeper": ApplicationSpec(executable=sys.executable, cwd=str(tmp_path))},
        approved_scripts={},
    )
    capability, store = _make_capability(tools_config=config)

    propose_response = capability.handle("/task open sleeper")
    assert "launched" not in propose_response.lower()

    confirm_response = capability.handle("/task confirm")
    assert "launched" in confirm_response.lower()

    # A second confirm has nothing left to consume - proves the
    # confirmation cannot be replayed.
    second_confirm_response = capability.handle("/task confirm")
    assert "no pending action" in second_confirm_response.lower()


def test_cancel_clears_a_pending_action(tmp_path):
    config = ToolsConfig(
        approved_directories={},
        approved_applications={"notepad": ApplicationSpec(executable=str(tmp_path / "notepad.exe"), cwd=str(tmp_path))},
        approved_scripts={},
    )
    capability, store = _make_capability(tools_config=config)

    capability.handle("/task open notepad")
    cancel_response = capability.handle("/task cancel")

    assert "cancelled" in cancel_response.lower()
    confirm_response = capability.handle("/task confirm")
    assert "no pending action" in confirm_response.lower()


def test_cancel_with_nothing_pending_says_so():
    capability, _ = _make_capability()

    response = capability.handle("/task cancel")

    assert "no pending action" in response.lower()


def test_invalid_tools_config_produces_a_safe_generic_error_for_resource_actions():
    def raising_loader():
        raise ToolsConfigError("malformed")

    store = ConfirmationStore()
    capability = TasksCapability(
        None, None, None, confirmation_store=store, tools_config_loader=raising_loader
    )

    response = capability.handle("/task files documents")

    assert response == "The task system is temporarily unavailable."
    assert "malformed" not in response


def _backup_config(repo_path, dest_path):
    return ToolsConfig(
        approved_directories={},
        approved_applications={},
        approved_scripts={},
        approved_repositories={"ai_os": RepoSpec(path=str(repo_path), main_branch="main")},
        approved_backups={"ai_os": RepoBackupSpec(destination_directory=str(dest_path))},
    )


def test_backup_with_unregistered_key_is_rejected_on_confirm():
    # repository_backup is sensitive, so - exactly like open_application -
    # the propose step never validates the resource key; only executing
    # the confirmed action does.
    capability, _ = _make_capability(tools_config=ToolsConfig({}, {}, {}, {}, {}))

    capability.handle("/task backup ai_os")
    response = capability.handle("/task confirm")

    assert "not registered" in response.lower()


def test_backup_is_proposed_not_executed_immediately(tmp_path):
    repo_path = _init_repo(tmp_path / "repo")
    dest_path = tmp_path / "dest"
    dest_path.mkdir()
    config = _backup_config(repo_path, dest_path)
    capability, store = _make_capability(tools_config=config)

    response = capability.handle("/task backup ai_os")

    assert "confirm" in response.lower()
    pending, expired = store.consume()
    assert expired is False
    assert pending.action == "repository_backup"
    assert pending.resource_key == "ai_os"
    # Nothing was actually written yet.
    assert list(dest_path.iterdir()) == []


def test_backup_confirm_executes_the_pending_action_exactly_once(tmp_path):
    repo_path = _init_repo(tmp_path / "repo")
    dest_path = tmp_path / "dest"
    dest_path.mkdir()
    config = _backup_config(repo_path, dest_path)
    capability, store = _make_capability(tools_config=config)

    propose_response = capability.handle("/task backup ai_os")
    assert "backup created" not in propose_response.lower()

    confirm_response = capability.handle("/task confirm")
    assert "backup created" in confirm_response.lower()
    assert "ai_os" in confirm_response

    created_files = list(dest_path.iterdir())
    assert len(created_files) == 1
    assert created_files[0].name.endswith(".bundle")

    # A second confirm has nothing left to consume - proves the
    # confirmation cannot be replayed, and no second bundle is written.
    second_confirm_response = capability.handle("/task confirm")
    assert "no pending action" in second_confirm_response.lower()
    assert len(list(dest_path.iterdir())) == 1


def test_backup_cancel_clears_pending_action(tmp_path):
    repo_path = _init_repo(tmp_path / "repo")
    dest_path = tmp_path / "dest"
    dest_path.mkdir()
    config = _backup_config(repo_path, dest_path)
    capability, store = _make_capability(tools_config=config)

    capability.handle("/task backup ai_os")
    cancel_response = capability.handle("/task cancel")

    assert "cancelled" in cancel_response.lower()
    confirm_response = capability.handle("/task confirm")
    assert "no pending action" in confirm_response.lower()
    assert list(dest_path.iterdir()) == []


def test_backup_confirm_with_expired_confirmation_says_expired_and_does_not_execute(tmp_path):
    repo_path = _init_repo(tmp_path / "repo")
    dest_path = tmp_path / "dest"
    dest_path.mkdir()
    config = _backup_config(repo_path, dest_path)
    capability, store = _make_capability(tools_config=config, ttl_seconds=0.01)

    capability.handle("/task backup ai_os")
    import time

    time.sleep(0.05)
    response = capability.handle("/task confirm")

    assert "expired" in response.lower()
    assert list(dest_path.iterdir()) == []


def test_status_is_unaffected_by_an_invalid_tools_config(deterministic_system_status):
    def raising_loader():
        raise ToolsConfigError("malformed")

    store = ConfirmationStore()
    capability = TasksCapability(
        None, None, None, confirmation_store=store, tools_config_loader=raising_loader
    )

    response = capability.handle("/task status")

    assert response == deterministic_system_status
