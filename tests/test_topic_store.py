"""Tests for app/topic_store.py."""

from app.topic_store import TopicStore


def _make_store(tmp_path):
    return TopicStore(str(tmp_path / "topics.sqlite3"))


def test_upsert_and_lookup_by_max_chat(tmp_path):
    store = _make_store(tmp_path)

    mapping = store.upsert_mapping(-100, 42, 123, "Alice")

    assert mapping.max_chat_id == "42"
    assert store.get_by_max_chat(-100, 42) == mapping
    store.close()


def test_lookup_by_thread(tmp_path):
    store = _make_store(tmp_path)
    store.upsert_mapping(-100, 42, 123, "Alice")

    mapping = store.get_by_thread(-100, 123)

    assert mapping is not None
    assert mapping.max_chat_id == "42"
    assert mapping.topic_name == "Alice"
    store.close()


def test_upsert_updates_existing_mapping(tmp_path):
    store = _make_store(tmp_path)
    store.upsert_mapping(-100, 42, 123, "Alice")

    store.upsert_mapping(-100, 42, 456, "Alice New")

    assert store.get_by_thread(-100, 123) is None
    mapping = store.get_by_max_chat(-100, 42)
    assert mapping is not None
    assert mapping.message_thread_id == 456
    assert mapping.topic_name == "Alice New"
    store.close()


def test_topic_name_exists_can_exclude_current_chat(tmp_path):
    store = _make_store(tmp_path)
    store.upsert_mapping(-100, 42, 123, "Alice")

    assert store.topic_name_exists(-100, "Alice") is True
    assert store.topic_name_exists(-100, "Alice", exclude_max_chat_id=42) is False
    store.close()


def test_delete_by_max_chat(tmp_path):
    store = _make_store(tmp_path)
    store.upsert_mapping(-100, 42, 123, "Alice")

    store.delete_by_max_chat(-100, 42)

    assert store.get_by_max_chat(-100, 42) is None
    assert store.get_by_thread(-100, 123) is None
    store.close()


def test_delete_by_thread(tmp_path):
    store = _make_store(tmp_path)
    store.upsert_mapping(-100, 42, 123, "Alice")

    store.delete_by_thread(-100, 123)

    assert store.get_by_max_chat(-100, 42) is None
    assert store.get_by_thread(-100, 123) is None
    store.close()


def test_profiles_isolate_same_max_chat_and_thread(tmp_path):
    store = _make_store(tmp_path)
    alpha = store.upsert_mapping(-100, 42, 123, "Alice", profile_id="alpha")
    beta = store.upsert_mapping(-100, 42, 456, "Bob", profile_id="beta")

    assert store.get_by_max_chat(-100, 42, profile_id="alpha") == alpha
    assert store.get_by_max_chat(-100, 42, profile_id="beta") == beta
    assert store.get_by_thread(-100, 123, profile_id="alpha") == alpha
    assert store.get_by_thread(-100, 123, profile_id="beta") is None
    store.close()
