"""Milestone 44 (Browser Worker) whole-milestone acceptance and
security-closure tests for kernel/tools/browser_safety.py,
kernel/tools/handlers/browser_read_page.py, kernel/tools/dns_resolver_worker.py,
kernel/tools/config.py, kernel/tools/registry.py, and
kernel/task_planner/catalog.py.

This module does NOT re-prove what tests/kernel/tools/test_browser_safety.py,
tests/kernel/tools/handlers/test_browser_read_page.py,
tests/kernel/tools/test_milestone_44_p1_integration.py, and
tests/kernel/task_execution/test_milestone_44_p1_e2e.py already cover in
detail (per-function edge cases, real-browser redirect/resource-class
fixtures, the wall-clock DNS subprocess-timeout proof, the worst-case
serialization proof). It instead proves the small number of WHOLE-MILESTONE
properties no single implementation-level file was ever positioned to
prove on its own: the final registry shape, the exact-one-browser-action
surface, the config authority shape, the exact-document/stylesheet-only
authority contract as one consolidated policy sweep, the final network-
bound constants, DNS's structural (not thread-based) shape, the absence of
any JavaScript/WebSocket/redirect/session/interaction production surface,
and a positive regression documenting that Milestone 44 deliberately closes
WITHOUT bounded browser interaction (P2) - see docs/architecture.md's
Milestone 44 entry for the full reasoning: safe execution authority for
dynamic browser behavior (JavaScript, WebSockets, workers, dynamic DOM
identity, selectors, clicks, forms, state-changing navigation,
authenticated sessions, downloads/uploads) requires its own explicit
security design, deferred beyond this milestone, not merely "not yet
built"."""

import inspect
from dataclasses import fields
from pathlib import Path

from kernel.tools import browser_safety, dns_resolver_worker, process_control
from kernel.tools.config import ApprovedPageSpec, ToolsConfig, load_tools_config
from kernel.tools.handlers import browser_read_page
from kernel.tools.registry import ActionRegistry, ResourceKeyRequirement
from kernel.tools.browser_safety import Origin, PageAuthority, is_request_permitted

# ============================================================================
# 1. Final registry action set: exactly ONE browser action, non-sensitive
# ============================================================================


def test_registry_contains_browser_read_page_included():
    # This test originally asserted an exact count of 12 (the M44 P1
    # closing count) - that count assertion moved to
    # tests/kernel/tools/test_milestone_43_acceptance.py's
    # test_final_registry_action_set_and_sensitivity_matrix(), which
    # already documents its own exact count needing to grow with each
    # later milestone (Milestone 45 P1 added two further actions after
    # this M44 file was written - ActionRegistry is a single shared
    # allowlist, not a milestone-scoped snapshot). This test keeps only
    # the M44-specific assertion: browser_read_page is present.
    registry = ActionRegistry()
    descriptors = registry.descriptors()

    assert "browser_read_page" in {d.name for d in descriptors}


def test_browser_read_page_final_sensitivity_and_resource_key_contract():
    registry = ActionRegistry()
    by_name = {d.name: d for d in registry.descriptors()}

    assert by_name["browser_read_page"].sensitive is False
    assert by_name["browser_read_page"].resource_key_requirement == ResourceKeyRequirement.REQUIRED


def test_final_sensitive_action_set_is_exactly_the_five_m43_write_actions():
    """browser_read_page must NOT appear in the sensitive set - the final
    M44 contract is read-only, no confirmation required."""

    registry = ActionRegistry()
    sensitive_names = {d.name for d in registry.descriptors() if d.sensitive}

    assert sensitive_names == {
        "open_application",
        "run_registered_script",
        "repository_backup",
        "create_directory",
        "copy_file",
    }
    assert "browser_read_page" not in sensitive_names


def test_exactly_one_browser_action_exists_no_interaction_surface():
    """Whole-milestone negative-capability search (design section 20): no
    registered action name suggests any bounded-interaction (P2) or
    unbounded browser capability - only the one read-only action closes
    Milestone 44."""

    registry = ActionRegistry()
    action_names = {d.name for d in registry.descriptors()}

    browser_actions = {name for name in action_names if "browser" in name}
    assert browser_actions == {"browser_read_page"}

    forbidden_substrings = (
        "click",
        "submit",
        "fill",
        "download",
        "upload",
        "screenshot",
        "javascript",
        "script_exec",
        "open_page",
        "list_links",
    )
    for name in action_names:
        for forbidden in forbidden_substrings:
            assert forbidden not in name.lower(), f"unexpected capability-shaped action name: {name}"


# ============================================================================
# 2. Config authority acceptance: approved_pages shape, no unsafe switches
# ============================================================================


def test_approved_page_spec_has_exactly_url_and_allowed_stylesheet_origins_fields():
    """Structural proof (mirrors test_milestone_43_acceptance.py's own
    fields()-based "no PID/process resource type" pattern): the config
    schema itself has no field for JavaScript-enabling, headed mode,
    WebSocket-enabling, redirect-enabling, session persistence, a
    selector, or a form value - not merely that no handler reads one."""

    field_names = {f.name for f in fields(ApprovedPageSpec)}
    assert field_names == {"url", "allowed_stylesheet_origins"}

    forbidden_substrings = (
        "javascript",
        "js_enabled",
        "headed",
        "headless",
        "websocket",
        "redirect",
        "session",
        "selector",
        "form",
        "cookie",
        "profile",
    )
    for field_name in field_names:
        for forbidden in forbidden_substrings:
            assert forbidden not in field_name.lower()


def test_tools_config_has_no_browser_switch_fields_beyond_approved_pages():
    """ToolsConfig's own top-level schema carries no browser-specific
    override field (e.g. no allow_private_network/allow_javascript/
    headed_mode) - approved_pages is the entire browser-facing config
    surface."""

    field_names = {f.name for f in fields(ToolsConfig)}
    browser_related = {name for name in field_names if "page" in name or "browser" in name}
    assert browser_related == {"approved_pages"}


def test_tools_example_yaml_approved_pages_shape_matches_the_final_contract():
    example_path = Path(__file__).resolve().parents[3] / "kernel" / "config" / "tools.example.yaml"
    config = load_tools_config(example_path)

    assert config.approved_pages
    for spec in config.approved_pages.values():
        assert isinstance(spec, ApprovedPageSpec)
        assert spec.url.startswith("https://")
        assert isinstance(spec.allowed_stylesheet_origins, tuple)


# ============================================================================
# 3. Exact-document authority: one consolidated policy-level sweep
# ============================================================================


def _authority_for(url: str, stylesheet_origins=()):
    normalized_url, origin = browser_safety.parse_https_url(url, field_name="url")
    return PageAuthority(
        document_url=normalized_url, document_origin=origin, stylesheet_origins=stylesheet_origins
    )


def test_omitted_and_explicit_default_port_both_authorize_the_same_document():
    authority_omitted = _authority_for("https://example.com/docs")
    authority_explicit = _authority_for("https://example.com:443/docs")

    for authority in (authority_omitted, authority_explicit):
        assert is_request_permitted(
            authority, url="https://example.com/docs", method="GET",
            resource_type="document", is_main_frame=True, document_consumed=False,
        )


def test_non_default_configured_port_remains_distinct_from_default():
    authority = _authority_for("https://example.com:8443/docs")

    assert not is_request_permitted(
        authority, url="https://example.com/docs", method="GET",
        resource_type="document", is_main_frame=True, document_consumed=False,
    )


def test_same_origin_different_path_is_not_authorized_as_the_document():
    authority = _authority_for("https://example.com/docs")

    assert not is_request_permitted(
        authority, url="https://example.com/other-page", method="GET",
        resource_type="document", is_main_frame=True, document_consumed=False,
    )


def test_same_path_different_query_is_not_authorized_as_the_document():
    authority = _authority_for("https://example.com/docs?a=1")

    assert not is_request_permitted(
        authority, url="https://example.com/docs?a=2", method="GET",
        resource_type="document", is_main_frame=True, document_consumed=False,
    )
    assert not is_request_permitted(
        authority, url="https://example.com/docs", method="GET",
        resource_type="document", is_main_frame=True, document_consumed=False,
    )


# ============================================================================
# 4. Second-document / navigation acceptance
# ============================================================================


def test_second_identical_main_frame_document_is_denied():
    authority = _authority_for("https://example.com/docs")

    assert not is_request_permitted(
        authority, url="https://example.com/docs", method="GET",
        resource_type="document", is_main_frame=True, document_consumed=True,
    )


def test_second_different_main_frame_document_is_denied():
    authority = _authority_for("https://example.com/docs")

    assert not is_request_permitted(
        authority, url="https://example.com/elsewhere", method="GET",
        resource_type="document", is_main_frame=True, document_consumed=True,
    )


def test_stylesheet_approved_origin_never_authorizes_a_document():
    static_origin = Origin(scheme="https", host="static.example.com", port=443)
    authority = _authority_for("https://example.com/docs", stylesheet_origins=(static_origin,))

    assert not is_request_permitted(
        authority, url="https://static.example.com/index.html", method="GET",
        resource_type="document", is_main_frame=True, document_consumed=False,
    )


def test_subframe_document_is_always_denied():
    authority = _authority_for("https://example.com/docs")

    assert not is_request_permitted(
        authority, url="https://example.com/docs", method="GET",
        resource_type="document", is_main_frame=False, document_consumed=False,
    )


# ============================================================================
# 5. Stylesheet-only authority: the load-bearing separation
# ============================================================================


def test_stylesheet_on_explicitly_approved_origin_is_allowed():
    static_origin = Origin(scheme="https", host="static.example.com", port=443)
    authority = _authority_for("https://example.com/docs", stylesheet_origins=(static_origin,))

    assert is_request_permitted(
        authority, url="https://static.example.com/style.css", method="GET",
        resource_type="stylesheet", is_main_frame=False, document_consumed=True,
    )


def test_stylesheet_on_pages_own_origin_denied_when_not_explicitly_listed():
    authority = _authority_for("https://example.com/docs")  # empty stylesheet_origins

    assert not is_request_permitted(
        authority, url="https://example.com/style.css", method="GET",
        resource_type="stylesheet", is_main_frame=False, document_consumed=True,
    )


def test_document_on_an_approved_stylesheet_origin_is_denied():
    static_origin = Origin(scheme="https", host="static.example.com", port=443)
    authority = _authority_for("https://example.com/docs", stylesheet_origins=(static_origin,))

    assert not is_request_permitted(
        authority, url="https://static.example.com/page.html", method="GET",
        resource_type="document", is_main_frame=True, document_consumed=False,
    )


def test_non_stylesheet_resource_on_an_approved_stylesheet_origin_is_denied():
    static_origin = Origin(scheme="https", host="static.example.com", port=443)
    authority = _authority_for("https://example.com/docs", stylesheet_origins=(static_origin,))

    for resource_type in ("script", "image", "font", "media", "xhr", "fetch", "websocket", "other"):
        assert not is_request_permitted(
            authority, url="https://static.example.com/anything", method="GET",
            resource_type=resource_type, is_main_frame=False, document_consumed=True,
        )


# ============================================================================
# 6. Network-bound constants: the final M44 P1 boundedness contract
# ============================================================================


def test_final_network_bound_constants():
    assert browser_safety.MAX_STYLESHEET_REQUESTS == 8
    assert browser_safety.MAX_TOTAL_STYLESHEET_BYTES == 256 * 1024
    assert browser_safety.MAX_STYLESHEET_RESPONSE_BYTES == 64 * 1024
    assert browser_safety.MAX_DOCUMENT_RESPONSE_BYTES == 256 * 1024
    # Structural: the cumulative bound is deliberately below
    # requests * per-response, so the two bounds are not purely redundant.
    assert (
        browser_safety.MAX_TOTAL_STYLESHEET_BYTES
        < browser_safety.MAX_STYLESHEET_REQUESTS * browser_safety.MAX_STYLESHEET_RESPONSE_BYTES
    )


def test_fetch_timeout_is_fixed_code_owned_and_not_playwright_default():
    assert isinstance(browser_read_page._FETCH_TIMEOUT_MS, int)
    assert 0 < browser_read_page._FETCH_TIMEOUT_MS < 30_000  # never Playwright's own 30s default


# ============================================================================
# 7. DNS structural acceptance (complements, does not duplicate, the
#    detailed wall-clock/cleanup proofs in test_browser_safety.py)
# ============================================================================


def test_dns_check_no_longer_uses_thread_pool_executor():
    # Mentions of "ThreadPoolExecutor" as prose (documenting the removed,
    # proven-broken prior approach) remain in comments/docstrings - the
    # precise, meaningful check is that the module no longer IMPORTS the
    # primitive at all, since that is what would make it reachable code.
    source = inspect.getsource(browser_safety)
    assert "from concurrent.futures import" not in source
    assert "import concurrent.futures" not in source


def test_dns_resolution_goes_through_the_fixed_internal_worker_via_process_control():
    assert browser_safety._DNS_WORKER_SCRIPT.exists()
    assert browser_safety._DNS_WORKER_SCRIPT.name == "dns_resolver_worker.py"
    assert browser_safety.process_control is process_control
    source = inspect.getsource(browser_safety.fresh_dns_safety_check)
    assert "process_control.run_capturing_stdout" in source
    assert "shell=True" not in source
    assert "os.system" not in source


def test_dns_worker_script_has_no_shell_or_arbitrary_execution():
    # The module's own docstring documents (as prose) the shell=False
    # invocation CONTRACT the CALLER must use, and mentions "subprocess"
    # describing how the CALLER invokes this script - both benign
    # documentation, not code. Parsed via ast rather than raw substring
    # search specifically to avoid docstring prose producing a false
    # positive: the precise, meaningful check is the module's own actual
    # import statements and top-level names, which must never include
    # subprocess/os.system/eval/exec.
    import ast

    tree = ast.parse(inspect.getsource(dns_resolver_worker))
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported_names.add(node.module)

    assert imported_names == {"socket", "sys"}

    called_names = {
        ast.dump(node.func)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert not any("eval" in name or "exec" in name or "system" in name for name in called_names)


def test_dns_timeout_is_fixed_and_code_owned():
    assert isinstance(browser_safety.DNS_RESOLUTION_TIMEOUT_SECONDS, float)
    assert 0 < browser_safety.DNS_RESOLUTION_TIMEOUT_SECONDS <= 10


def test_dns_check_fails_closed_on_a_private_result_via_the_real_mechanism():
    """One behavioral confirmation through the real subprocess mechanism -
    not a re-proof of wall-clock bounding or cleanup (see
    test_browser_safety.py's own dedicated, detailed tests for those)."""

    fixtures_dir = Path(__file__).resolve().parent / "fixtures"
    private_worker = fixtures_dir / "print_private_ip_worker.py"

    try:
        browser_safety.fresh_dns_safety_check(
            "rebound.example.com", field_name="host", worker_script=private_worker
        )
        raise AssertionError("expected BrowserSafetyError for a private DNS result")
    except browser_safety.BrowserSafetyError:
        pass


# ============================================================================
# 8. JavaScript / WebSocket / redirect production-surface acceptance
# ============================================================================


def test_java_script_is_disabled_in_the_real_context_creation_call():
    source = inspect.getsource(browser_read_page._execute_read)
    assert "java_script_enabled=False" in source
    assert "java_script_enabled=True" not in source


def test_no_production_route_web_socket_page_evaluate_or_execute_script():
    handler_source = inspect.getsource(browser_read_page)
    safety_source = inspect.getsource(browser_safety)

    for forbidden in ("route_web_socket", "page.evaluate", "execute_script", ".evaluate("):
        assert forbidden not in handler_source
        assert forbidden not in safety_source


def test_route_fetch_uses_max_redirects_zero_and_no_route_continue():
    source = inspect.getsource(browser_read_page)
    assert "max_redirects=0" in source
    assert "route.continue_" not in source


def test_service_workers_block_is_the_only_service_worker_configuration():
    source = inspect.getsource(browser_read_page._execute_read)
    assert 'service_workers="block"' in source
    assert 'service_workers="allow"' not in source


# ============================================================================
# 9. Session-state acceptance: no persistence across or within actions
# ============================================================================


def test_no_persistent_browser_profile_or_storage_state_in_production_source():
    source = inspect.getsource(browser_read_page)
    for forbidden in (
        "launch_persistent_context",
        "user_data_dir",
        "storage_state",
        "channel=\"chrome\"",
        "channel=\"msedge\"",
    ):
        assert forbidden not in source


def test_context_is_created_fresh_with_no_reuse_across_calls():
    """_execute_read() must create its own browser/context/page inside the
    function body (never accept one as an injected, reusable parameter) -
    the structural guarantee that no cross-action or cross-step browser
    state can exist."""

    signature = inspect.signature(browser_read_page._execute_read)
    param_names = set(signature.parameters)
    assert param_names == {"authority", "page_key"}
    assert "browser" not in param_names
    assert "context" not in param_names
    assert "page" not in param_names


# ============================================================================
# 10. Audit privacy acceptance: unchanged schema
# ============================================================================


def test_audit_record_signature_remains_action_resource_key_outcome_only():
    from kernel.tools import audit

    signature = inspect.signature(audit.record)
    param_names = list(signature.parameters)
    # log_path is an existing test-injection seam, not a new field.
    assert param_names[:3] == ["action", "resource_key", "outcome"]


# ============================================================================
# 11. Prompt-injection boundary: browser_read_page is a dead-end DATA leaf
# ============================================================================


def test_run_entry_point_accepts_only_action_request_and_tools_config():
    """resource_key is the ONLY input browser_read_page.run() ever accepts
    - no page content, request text, or model output can reach it as a
    parameter, structurally ruling out a page-content -> new-action-
    authority path at the function-signature level."""

    signature = inspect.signature(browser_read_page.run)
    assert list(signature.parameters) == ["request", "tools_config"]


def test_execute_read_never_constructs_a_new_action_request():
    # ActionRequest is imported only for run()'s own parameter type - the
    # module never constructs a NEW ActionRequest anywhere in its own body
    # (which would be the concrete mechanism by which page content could
    # otherwise smuggle itself into a fresh action-selection decision).
    source = inspect.getsource(browser_read_page)
    assert source.count("ActionRequest(") == 0


# ============================================================================
# 12. Dependency acceptance
# ============================================================================


def test_playwright_is_the_only_new_direct_dependency():
    pyproject = (Path(__file__).resolve().parents[3] / "pyproject.toml").read_text(encoding="utf-8")
    assert '"playwright>=1.62.0"' in pyproject
    # The pre-existing M43-era direct dependencies remain present and
    # untouched (a coarse regression against an accidental unrelated
    # dependency change).
    for existing in ("anthropic", "psutil", "python-dotenv", "pyyaml"):
        assert existing in pyproject


# ============================================================================
# 13. M42 / M45+ boundary acceptance
# ============================================================================


def test_task_execution_service_and_respond_have_no_browser_specific_surface():
    from kernel.task_execution import respond, service

    assert "browser" not in inspect.getsource(service).lower()
    assert "browser" not in inspect.getsource(respond).lower()


def test_no_desktop_mutation_or_m46_plus_capability_named_in_the_registry():
    """Originally forbade the bare substring "desktop" outright, as a
    positive regression proving Milestone 45 had not prematurely leaked
    into the registry while this M44 file was written. Milestone 45 P1 has
    since been implemented - desktop_target_status/desktop_control_status
    are real, approved, read-only actions (see
    tests/kernel/tools/test_registry.py's own dedicated M45 P1 coverage) -
    so the bare substring is no longer the right guard. What must still
    never appear is any DESKTOP MUTATION shape (M45 P1 is read-only by
    design - see docs/architecture.md's Milestone 45 entry for why
    semantic invocation was evaluated and rejected) or any M46+ shape."""

    registry = ActionRegistry()
    action_names = {d.name for d in registry.descriptors()}

    forbidden_substrings = (
        "desktop_invoke",
        "desktop_click",
        "desktop_type",
        "desktop_hotkey",
        "desktop_set",
        "desktop_close",
        "desktop_focus",
        "desktop_screenshot",
        "desktop_capture",
        "window_control",
        "mouse",
        "keyboard",
        "whatsapp_task",
        "reconcile",
    )
    for name in action_names:
        for forbidden in forbidden_substrings:
            assert forbidden not in name.lower()


# ============================================================================
# 14. Positive regression: Milestone 44 closes WITHOUT bounded interaction
# ============================================================================


def test_milestone_44_closes_without_bounded_browser_interaction_by_design():
    """Documents Milestone 44's explicit closure decision: bounded browser
    interaction (P2) was evaluated and deferred, not merely unbuilt - safe
    execution authority for dynamic browser behavior (JavaScript,
    WebSockets, workers, dynamic DOM identity, selectors, clicks, forms,
    state-changing navigation, authenticated sessions, downloads/uploads)
    requires its own explicit security design beyond this milestone (see
    docs/architecture.md's Milestone 44 entry). This is a positive
    regression: it fails loudly if a future change ever quietly introduces
    interactive browser capability without a fresh design/security
    review."""

    registry = ActionRegistry()
    action_names = {d.name for d in registry.descriptors()}

    # desktop_target_status/desktop_control_status (Milestone 45 P1) were
    # added to this same shared registry after this M44 file was written -
    # both are themselves read-only/non-sensitive (see
    # test_no_desktop_mutation_or_m46_plus_capability_named_in_the_registry
    # above for the still-enforced guard against any DESKTOP MUTATION
    # shape), so their presence does not weaken this test's actual claim:
    # no bounded BROWSER interaction capability exists.
    assert action_names == {
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
        "desktop_target_status",
        "desktop_control_status",
    }
