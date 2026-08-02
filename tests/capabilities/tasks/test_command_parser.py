"""Tests for capabilities/tasks/command_parser.py: the strict /task grammar."""

import pytest

from capabilities.tasks.command_parser import ParseError, ParsedCommand, parse_task_command


@pytest.mark.parametrize(
    "prompt,expected_verb",
    [
        ("/task status", "status"),
        ("/task confirm", "confirm"),
        ("/task cancel", "cancel"),
        ("/task help", "help"),
        ("/TASK STATUS", "status"),
        ("  /task status  ", "status"),
    ],
)
def test_recognized_no_argument_forms(prompt, expected_verb):
    result = parse_task_command(prompt)

    assert isinstance(result, ParsedCommand)
    assert result.verb == expected_verb
    assert result.action is None
    assert result.resource_key is None


def test_bare_task_prefix_is_help():
    result = parse_task_command("/task")

    assert result == ParsedCommand("help", None, None)


@pytest.mark.parametrize(
    "prompt,expected_action",
    [
        ("/task files documents", "list_files"),
        ("/task open notepad", "open_application"),
        ("/task run backup", "run_registered_script"),
        ("/task repo ai_os", "repo_health"),
    ],
)
def test_recognized_one_argument_forms(prompt, expected_action):
    result = parse_task_command(prompt)

    assert isinstance(result, ParsedCommand)
    assert result.action == expected_action
    assert result.resource_key == prompt.split()[-1]


def test_resource_key_is_casefolded():
    result = parse_task_command("/task open Notepad")

    assert result.resource_key == "notepad"


@pytest.mark.parametrize(
    "prompt",
    [
        "not a task command at all",
        "please /task status",  # must start with /task, not just contain it
        "/tasks status",  # not the exact prefix
    ],
)
def test_prompts_that_are_not_task_commands_return_a_parse_error_reason(prompt):
    result = parse_task_command(prompt)

    assert isinstance(result, ParseError)
    assert result.reason == "not_a_task_command"


@pytest.mark.parametrize(
    "prompt",
    [
        "/task status extra",
        "/task confirm now",
        "/task cancel please",
        "/task help me",
        "/task files",  # missing required argument
        "/task open",
        "/task run",
        "/task repo",
        "/task files documents extra",
        "/task open notepad extra",
        "/task repo ai_os extra",
    ],
)
def test_wrong_argument_count_is_rejected(prompt):
    result = parse_task_command(prompt)

    assert isinstance(result, ParseError)
    assert result.reason == "wrong_argument_count"


def test_unknown_verb_is_rejected():
    result = parse_task_command("/task delete_everything")

    assert isinstance(result, ParseError)
    assert result.reason == "unknown_verb"


def test_empty_prompt_is_not_a_task_command():
    result = parse_task_command("")

    assert isinstance(result, ParseError)
    assert result.reason == "not_a_task_command"
