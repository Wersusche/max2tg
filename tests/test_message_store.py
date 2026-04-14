from app.message_store import MessageStore


def _make_store(tmp_path):
    return MessageStore(str(tmp_path / "messages.sqlite3"))


def test_upsert_and_lookup_by_max_message(tmp_path):
    store = _make_store(tmp_path)
    mapping = store.upsert_mapping(
        tg_chat_id=-100,
        max_chat_id=42,
        max_message_id="max-1",
        tg_message_id=7001,
        message_thread_id=55,
    )

    loaded = store.get_by_max_message(max_chat_id=42, max_message_id="max-1")

    assert loaded == mapping
    store.close()


def test_lookup_by_tg_message(tmp_path):
    store = _make_store(tmp_path)
    store.upsert_mapping(
        tg_chat_id=-100,
        max_chat_id=42,
        max_message_id="max-2",
        tg_message_id=7002,
        message_thread_id=56,
    )

    loaded = store.get_by_tg_message(tg_chat_id=-100, tg_message_id=7002)

    assert loaded is not None
    assert loaded.max_message_id == "max-2"
    assert loaded.message_thread_id == 56
    store.close()
