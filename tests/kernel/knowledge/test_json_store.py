"""Tests for JSONKnowledgeStore."""

import json

import pytest

from kernel.knowledge import JSONKnowledgeStore


def _write_namespace(tmp_path, namespace: str, data) -> None:
    path = tmp_path / f"{namespace}.json"
    path.write_text(json.dumps(data), encoding="utf-8")


def test_get_returns_existing_record(tmp_path):
    _write_namespace(tmp_path, "wine", {"chianti": {"region": "Tuscany"}})
    store = JSONKnowledgeStore(tmp_path)

    assert store.get("wine", "chianti") == {"region": "Tuscany"}


def test_get_returns_none_for_missing_record(tmp_path):
    _write_namespace(tmp_path, "wine", {"chianti": {"region": "Tuscany"}})
    store = JSONKnowledgeStore(tmp_path)

    assert store.get("wine", "barolo") is None


def test_list_records_returns_all_records(tmp_path):
    data = {
        "chianti": {"region": "Tuscany"},
        "barolo": {"region": "Piedmont"},
    }
    _write_namespace(tmp_path, "wine", data)
    store = JSONKnowledgeStore(tmp_path)

    assert store.list_records("wine") == data


def test_list_records_missing_namespace_returns_empty_dict(tmp_path):
    store = JSONKnowledgeStore(tmp_path)

    assert store.list_records("does-not-exist") == {}


def test_get_missing_namespace_returns_none(tmp_path):
    store = JSONKnowledgeStore(tmp_path)

    assert store.get("does-not-exist", "chianti") is None


def test_utf8_content_is_read_correctly(tmp_path):
    _write_namespace(tmp_path, "wine", {"riesling": {"region": "Mosel", "note": "café"}})
    store = JSONKnowledgeStore(tmp_path)

    assert store.get("wine", "riesling") == {"region": "Mosel", "note": "café"}


def test_malformed_json_raises_value_error(tmp_path):
    path = tmp_path / "wine.json"
    path.write_text("{not valid json", encoding="utf-8")
    store = JSONKnowledgeStore(tmp_path)

    with pytest.raises(ValueError):
        store.list_records("wine")


def test_non_object_top_level_raises_value_error(tmp_path):
    _write_namespace(tmp_path, "wine", ["chianti", "barolo"])
    store = JSONKnowledgeStore(tmp_path)

    with pytest.raises(ValueError):
        store.list_records("wine")


def test_non_object_record_raises_value_error(tmp_path):
    _write_namespace(tmp_path, "wine", {"chianti": "Tuscany"})
    store = JSONKnowledgeStore(tmp_path)

    with pytest.raises(ValueError):
        store.list_records("wine")


@pytest.mark.parametrize(
    "namespace",
    ["", ".", "..", "a/b", "a\\b", "/etc/passwd", "../secrets", "wine/../../etc"],
)
def test_invalid_namespaces_are_rejected(tmp_path, namespace):
    store = JSONKnowledgeStore(tmp_path)

    with pytest.raises(ValueError):
        store.list_records(namespace)


def test_returned_data_mutation_does_not_affect_subsequent_reads(tmp_path):
    _write_namespace(tmp_path, "wine", {"chianti": {"region": "Tuscany"}})
    store = JSONKnowledgeStore(tmp_path)

    records = store.list_records("wine")
    records["chianti"]["region"] = "Mordor"
    records["barolo"] = {"region": "Piedmont"}

    assert store.list_records("wine") == {"chianti": {"region": "Tuscany"}}

    record = store.get("wine", "chianti")
    record["region"] = "Mordor"

    assert store.get("wine", "chianti") == {"region": "Tuscany"}


def test_read_operations_do_not_create_or_modify_files(tmp_path):
    _write_namespace(tmp_path, "wine", {"chianti": {"region": "Tuscany"}})
    store = JSONKnowledgeStore(tmp_path)
    before = (tmp_path / "wine.json").read_text(encoding="utf-8")

    store.get("wine", "chianti")
    store.list_records("wine")
    store.list_records("empty-namespace")
    store.get("empty-namespace", "key")

    after_entries = sorted(p.name for p in tmp_path.iterdir())
    assert after_entries == ["wine.json"]
    assert (tmp_path / "wine.json").read_text(encoding="utf-8") == before
