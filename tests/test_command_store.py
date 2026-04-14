"""Tests for app/command_store.py."""

from app.command_store import CommandStore


def _make_store(tmp_path):
    return CommandStore(str(tmp_path / "commands.sqlite3"))


def test_enqueue_lease_and_ack(tmp_path):
    store = _make_store(tmp_path)
    queued = store.enqueue(
        42,
        "hello",
        [{"type": "STRONG"}],
        reply_to_max_message_id="max-1",
        tg_chat_id=-100,
        tg_message_id=7001,
        message_thread_id=55,
    )

    leased = store.lease_next()

    assert leased is not None
    assert leased.id == queued.id
    assert leased.max_chat_id == "42"
    assert leased.text == "hello"
    assert leased.elements == [{"type": "STRONG"}]
    assert leased.reply_to_max_message_id == "max-1"
    assert leased.tg_chat_id == -100
    assert leased.tg_message_id == 7001
    assert leased.message_thread_id == 55

    store.ack(leased.id)
    assert store.count() == 0
    store.close()


def test_lease_next_returns_none_when_empty(tmp_path):
    store = _make_store(tmp_path)
    assert store.lease_next() is None
    store.close()


def test_enqueue_photo_lease_and_ack(tmp_path):
    store = _make_store(tmp_path)
    queued = store.enqueue_photo(
        42,
        b"image-bytes",
        caption="hello",
        filename="pic.jpg",
        reply_to_max_message_id="max-1",
        tg_chat_id=-100,
        tg_message_id=7002,
        message_thread_id=56,
    )

    leased = store.lease_next()

    assert leased is not None
    assert leased.id == queued.id
    assert leased.max_chat_id == "42"
    assert leased.kind == "photo"
    assert leased.text == "hello"
    assert leased.filename == "pic.jpg"
    assert leased.attachment == b"image-bytes"
    assert leased.reply_to_max_message_id == "max-1"
    assert leased.tg_chat_id == -100
    assert leased.tg_message_id == 7002
    assert leased.message_thread_id == 56

    store.ack(leased.id)
    assert store.count() == 0
    store.close()


def test_enqueue_document_lease_and_ack(tmp_path):
    store = _make_store(tmp_path)
    queued = store.enqueue_document(
        42,
        b"file-bytes",
        caption="doc",
        filename="report.pdf",
        reply_to_max_message_id="max-1",
        tg_chat_id=-100,
        tg_message_id=7003,
        message_thread_id=57,
    )

    leased = store.lease_next()

    assert leased is not None
    assert leased.id == queued.id
    assert leased.max_chat_id == "42"
    assert leased.kind == "document"
    assert leased.text == "doc"
    assert leased.filename == "report.pdf"
    assert leased.attachment == b"file-bytes"
    assert leased.reply_to_max_message_id == "max-1"
    assert leased.tg_chat_id == -100
    assert leased.tg_message_id == 7003
    assert leased.message_thread_id == 57

    store.ack(leased.id)
    assert store.count() == 0
    store.close()
