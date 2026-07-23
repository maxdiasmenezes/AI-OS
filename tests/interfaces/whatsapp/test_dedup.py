"""Tests for the bounded, thread-safe FIFO message-ID dedup cache."""

import pytest

from interfaces.whatsapp.dedup import DEFAULT_CAPACITY, SeenMessageCache


def test_default_capacity_is_256():
    assert DEFAULT_CAPACITY == 256

    cache = SeenMessageCache()
    for i in range(256):
        assert cache.add_if_new(f"msg-{i}") is True

    # The 257th distinct ID evicts the oldest (msg-0), which becomes
    # re-admittable - proving the default bound is exactly 256, not more.
    cache.add_if_new("msg-256")
    assert cache.add_if_new("msg-0") is True


def test_first_reservation_of_an_id_returns_true():
    cache = SeenMessageCache(max_size=10)

    assert cache.add_if_new("msg-1") is True


def test_second_reservation_of_the_same_id_returns_false():
    cache = SeenMessageCache(max_size=10)
    cache.add_if_new("msg-1")

    assert cache.add_if_new("msg-1") is False


def test_distinct_ids_are_each_tracked_independently():
    cache = SeenMessageCache(max_size=10)

    assert cache.add_if_new("msg-1") is True
    assert cache.add_if_new("msg-2") is True
    assert cache.add_if_new("msg-1") is False
    assert cache.add_if_new("msg-2") is False


def test_oldest_id_is_evicted_once_max_size_is_exceeded():
    cache = SeenMessageCache(max_size=2)
    cache.add_if_new("msg-1")
    cache.add_if_new("msg-2")
    cache.add_if_new("msg-3")  # evicts msg-1

    assert cache.add_if_new("msg-1") is True  # re-admitted, no longer tracked
    assert cache.add_if_new("msg-3") is False  # still tracked


def test_discard_releases_a_reservation_for_later_redelivery():
    cache = SeenMessageCache(max_size=10)
    cache.add_if_new("msg-1")

    cache.discard("msg-1")

    assert cache.add_if_new("msg-1") is True


def test_discard_of_an_unknown_id_does_not_raise():
    cache = SeenMessageCache(max_size=10)

    cache.discard("never-seen")  # should not raise


def test_max_size_must_be_positive():
    with pytest.raises(ValueError):
        SeenMessageCache(max_size=0)
