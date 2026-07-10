"""Tests for the SQLite state module."""

import sqlite3

import pytest

from subscriber.state import State


@pytest.fixture
def state(tmp_path):
    return State(tmp_path / "test.db")


class TestSeenItems:
    def test_unseen_item(self, state):
        assert not state.is_seen("src", "1")

    def test_mark_and_check(self, state):
        state.mark_seen("src", "1")
        assert state.is_seen("src", "1")

    def test_different_source_not_seen(self, state):
        state.mark_seen("src_a", "1")
        assert not state.is_seen("src_b", "1")

    def test_mark_idempotent(self, state):
        state.mark_seen("src", "1")
        state.mark_seen("src", "1")
        assert state.is_seen("src", "1")


class TestPageSnapshots:
    def test_no_snapshot(self, state):
        assert state.get_snapshot("page") is None

    def test_save_and_get(self, state):
        state.save_snapshot("page", "hello")
        assert state.get_snapshot("page") == "hello"

    def test_overwrite(self, state):
        state.save_snapshot("page", "v1")
        state.save_snapshot("page", "v2")
        assert state.get_snapshot("page") == "v2"


class TestKV:
    def test_missing_key(self, state):
        assert state.kv_get("nope") is None

    def test_set_and_get(self, state):
        state.kv_set("k", "v")
        assert state.kv_get("k") == "v"

    def test_overwrite(self, state):
        state.kv_set("k", "old")
        state.kv_set("k", "new")
        assert state.kv_get("k") == "new"


def test_persistence(tmp_path):
    db_path = tmp_path / "persist.db"
    s1 = State(db_path)
    s1.mark_seen("src", "1")
    s1.kv_set("k", "v")
    s1.save_snapshot("p", "snap")
    s1.db.close()

    s2 = State(db_path)
    assert s2.is_seen("src", "1")
    assert s2.kv_get("k") == "v"
    assert s2.get_snapshot("p") == "snap"
