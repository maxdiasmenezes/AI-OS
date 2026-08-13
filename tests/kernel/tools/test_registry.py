"""Tests for kernel/tools/registry.py: the fixed action allowlist."""

import pytest

from kernel.tools.registry import ActionDescriptor, ActionRegistry, ResourceKeyRequirement


_ALL_ACTIONS = (
    "system_status",
    "list_files",
    "open_application",
    "run_registered_script",
    "repo_health",
    "repository_backup",
    "file_metadata",
    "read_text_file",
    "list_processes",
    "create_directory",
    "copy_file",
    "browser_read_page",
)


def test_exactly_the_twelve_milestone_actions_are_known():
    registry = ActionRegistry()

    for action in _ALL_ACTIONS:
        assert registry.is_known(action)

    assert registry.is_known("delete_everything") is False
    assert registry.is_known("") is False


def test_open_application_run_registered_script_and_repository_backup_are_sensitive():
    registry = ActionRegistry()

    assert registry.is_sensitive("open_application") is True
    assert registry.is_sensitive("run_registered_script") is True
    assert registry.is_sensitive("repository_backup") is True


def test_milestone_43_p2_write_actions_are_sensitive():
    registry = ActionRegistry()

    assert registry.is_sensitive("create_directory") is True
    assert registry.is_sensitive("copy_file") is True


def test_system_status_list_files_and_repo_health_are_not_sensitive():
    registry = ActionRegistry()

    assert registry.is_sensitive("system_status") is False
    assert registry.is_sensitive("list_files") is False
    assert registry.is_sensitive("repo_health") is False


def test_milestone_43_p1_read_only_actions_are_not_sensitive():
    registry = ActionRegistry()

    assert registry.is_sensitive("file_metadata") is False
    assert registry.is_sensitive("read_text_file") is False
    assert registry.is_sensitive("list_processes") is False


def test_milestone_44_p1_browser_read_page_is_not_sensitive():
    registry = ActionRegistry()

    assert registry.is_sensitive("browser_read_page") is False


def test_unknown_action_is_not_sensitive_and_has_no_handler():
    registry = ActionRegistry()

    assert registry.is_sensitive("delete_everything") is False
    assert registry.handler_for("delete_everything") is None


def test_handler_for_returns_a_callable_for_each_known_action():
    registry = ActionRegistry()

    for action in _ALL_ACTIONS:
        assert callable(registry.handler_for(action))


# --- Milestone 39: descriptors() ---------------------------------------------


def test_descriptors_are_actiondescriptor_instances():
    registry = ActionRegistry()

    for descriptor in registry.descriptors():
        assert isinstance(descriptor, ActionDescriptor)


_EXPECTED_REQUIREMENTS = {
    "system_status": ResourceKeyRequirement.FORBIDDEN,
    "list_files": ResourceKeyRequirement.REQUIRED,
    "open_application": ResourceKeyRequirement.REQUIRED,
    "run_registered_script": ResourceKeyRequirement.REQUIRED,
    "repo_health": ResourceKeyRequirement.REQUIRED,
    "repository_backup": ResourceKeyRequirement.REQUIRED,
    "file_metadata": ResourceKeyRequirement.REQUIRED,
    "read_text_file": ResourceKeyRequirement.REQUIRED,
    "list_processes": ResourceKeyRequirement.FORBIDDEN,
    "create_directory": ResourceKeyRequirement.REQUIRED,
    "copy_file": ResourceKeyRequirement.REQUIRED,
    "browser_read_page": ResourceKeyRequirement.REQUIRED,
}


def test_descriptors_cover_exactly_the_eleven_known_actions():
    registry = ActionRegistry()
    names = tuple(d.name for d in registry.descriptors())

    assert set(names) == set(_ALL_ACTIONS)
    assert len(names) == len(_ALL_ACTIONS)


def test_descriptors_order_is_deterministic_across_calls():
    registry = ActionRegistry()

    first = tuple(d.name for d in registry.descriptors())
    second = tuple(d.name for d in registry.descriptors())

    assert first == second
    assert first == _ALL_ACTIONS


def test_descriptor_sensitivity_matches_is_sensitive():
    registry = ActionRegistry()

    for descriptor in registry.descriptors():
        assert descriptor.sensitive == registry.is_sensitive(descriptor.name)


def test_descriptor_resource_key_requirement_matches_expected_shape():
    registry = ActionRegistry()
    by_name = {d.name: d for d in registry.descriptors()}

    for action, expected_requirement in _EXPECTED_REQUIREMENTS.items():
        assert by_name[action].resource_key_requirement == expected_requirement


def test_system_status_descriptor_has_no_resource_key_description():
    registry = ActionRegistry()
    by_name = {d.name: d for d in registry.descriptors()}

    assert by_name["system_status"].resource_key_description is None


def test_list_processes_descriptor_has_no_resource_key_description():
    registry = ActionRegistry()
    by_name = {d.name: d for d in registry.descriptors()}

    assert by_name["list_processes"].resource_key_description is None


def test_required_descriptors_have_a_resource_key_description():
    registry = ActionRegistry()
    by_name = {d.name: d for d in registry.descriptors()}

    for action, requirement in _EXPECTED_REQUIREMENTS.items():
        if requirement == ResourceKeyRequirement.REQUIRED:
            description = by_name[action].resource_key_description
            assert isinstance(description, str) and description.strip()


def test_descriptor_is_immutable():
    descriptor = ActionRegistry().descriptors()[0]

    with pytest.raises(Exception):
        descriptor.name = "changed"  # type: ignore[misc]


def test_descriptors_return_value_is_an_immutable_tuple():
    registry = ActionRegistry()

    result = registry.descriptors()

    assert isinstance(result, tuple)
    with pytest.raises(TypeError):
        result[0] = result[0]  # type: ignore[index]


def test_descriptors_expose_no_handler_or_configuration():
    registry = ActionRegistry()

    for descriptor in registry.descriptors():
        field_names = {f for f in vars(descriptor)}
        assert field_names == {
            "name",
            "resource_key_requirement",
            "resource_key_description",
            "sensitive",
        }
        # No field's value is callable (a handler) or looks like a path.
        assert not callable(descriptor.resource_key_description)
        if descriptor.resource_key_description is not None:
            assert "/" not in descriptor.resource_key_description
            assert "\\" not in descriptor.resource_key_description


def test_mutating_one_descriptors_call_result_never_affects_another():
    registry = ActionRegistry()

    first_call = registry.descriptors()
    second_call = registry.descriptors()

    assert first_call == second_call
    assert first_call is not second_call
