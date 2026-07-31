"""Tests for kernel/tools/confirmation.py: the single-slot pending-action store."""

import time

from kernel.tools.confirmation import ConfirmationStore


def test_consume_with_nothing_pending_returns_none_and_not_expired():
    store = ConfirmationStore()

    pending, expired = store.consume()

    assert pending is None
    assert expired is False


def test_propose_then_consume_returns_the_pending_action_exactly_once():
    store = ConfirmationStore()
    store.propose("open_application", "notepad")

    pending, expired = store.consume()

    assert expired is False
    assert pending.action == "open_application"
    assert pending.resource_key == "notepad"

    # Consuming again finds nothing - it was cleared by the first consume().
    pending_again, expired_again = store.consume()
    assert pending_again is None
    assert expired_again is False


def test_expired_confirmation_is_reported_as_expired_not_absent():
    store = ConfirmationStore(ttl_seconds=0.05)
    store.propose("run_registered_script", "backup")
    time.sleep(0.1)

    pending, expired = store.consume()

    assert pending is None
    assert expired is True


def test_a_new_proposal_replaces_any_existing_pending_action():
    store = ConfirmationStore()
    store.propose("open_application", "notepad")
    store.propose("run_registered_script", "backup")

    pending, expired = store.consume()

    assert expired is False
    assert pending.action == "run_registered_script"
    assert pending.resource_key == "backup"


def test_cancel_clears_a_pending_action_and_reports_it_was_pending():
    store = ConfirmationStore()
    store.propose("open_application", "notepad")

    had_pending = store.cancel()

    assert had_pending is True
    pending, expired = store.consume()
    assert pending is None
    assert expired is False


def test_cancel_with_nothing_pending_is_safe_and_reports_nothing_pending():
    store = ConfirmationStore()

    had_pending = store.cancel()

    assert had_pending is False
