"""Tests for app/command_store.py."""

from app.command_store import CommandStore


def _make_store(tmp_path):
    return CommandStore(str(tmp_path / "commands.sqlite3"))


def test_enqueue_lease_and_ack(tmp_path):
    store = _make_store(tmp_path)
    queued = store.enqueue(42, "hello", [{"type": "STRONG"}])

    leased = store.lease_next()

    assert leased is not None
    assert leased.id == queued.id
    assert leased.max_chat_id == "42"
    assert leased.text == "hello"
    assert leased.elements == [{"type": "STRONG"}]

    store.ack(leased.id)
    assert store.count() == 0
    store.close()


def test_lease_next_returns_none_when_empty(tmp_path):
    store = _make_store(tmp_path)
    assert store.lease_next() is None
    store.close()


def test_enqueue_photo_lease_and_ack(tmp_path):
    store = _make_store(tmp_path)
    queued = store.enqueue_photo(42, b"image-bytes", caption="hello", filename="pic.jpg")

    leased = store.lease_next()

    assert leased is not None
    assert leased.id == queued.id
    assert leased.max_chat_id == "42"
    assert leased.kind == "photo"
    assert leased.text == "hello"
    assert leased.filename == "pic.jpg"
    assert leased.attachment == b"image-bytes"

    store.ack(leased.id)
    assert store.count() == 0
    store.close()


def test_enqueue_document_lease_and_ack(tmp_path):
    store = _make_store(tmp_path)
    queued = store.enqueue_document(42, b"file-bytes", caption="doc", filename="report.pdf")

    leased = store.lease_next()

    assert leased is not None
    assert leased.id == queued.id
    assert leased.max_chat_id == "42"
    assert leased.kind == "document"
    assert leased.text == "doc"
    assert leased.filename == "report.pdf"
    assert leased.attachment == b"file-bytes"

    store.ack(leased.id)
    assert store.count() == 0
    store.close()
