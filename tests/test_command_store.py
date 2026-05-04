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
    assert leased.attempt_count == 1

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
    assert leased.attempt_count == 1

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
    assert leased.attempt_count == 1

    store.ack(leased.id)
    assert store.count() == 0
    store.close()


def test_mark_failed_requeues_then_dead_letters_after_max_attempts(tmp_path):
    store = _make_store(tmp_path)
    queued = store.enqueue(42, "hello")

    first_lease = store.lease_next()
    assert first_lease is not None
    assert first_lease.attempt_count == 1

    first_failure = store.mark_failed(first_lease.id, error="temporary", max_attempts=2)
    assert first_failure is not None
    assert first_failure.attempt_count == 1
    assert first_failure.dead_lettered is False

    second_lease = store.lease_next()
    assert second_lease is not None
    assert second_lease.id == queued.id
    assert second_lease.attempt_count == 2

    second_failure = store.mark_failed(second_lease.id, error="permanent", max_attempts=2)
    assert second_failure is not None
    assert second_failure.attempt_count == 2
    assert second_failure.dead_lettered is True
    assert store.lease_next() is None

    row = store._conn.execute(
        "SELECT failed_at, last_error FROM max_commands WHERE id = ?",
        (queued.id,),
    ).fetchone()
    assert row["failed_at"] is not None
    assert row["last_error"] == "permanent"
    store.close()


def test_lease_next_filters_by_profile(tmp_path):
    store = _make_store(tmp_path)
    alpha = store.enqueue(42, "alpha", profile_id="alpha")
    beta = store.enqueue(42, "beta", profile_id="beta")

    leased_beta = store.lease_next(profile_id="beta")
    assert leased_beta is not None
    assert leased_beta.id == beta.id
    assert leased_beta.profile_id == "beta"

    leased_alpha = store.lease_next(profile_id="alpha")
    assert leased_alpha is not None
    assert leased_alpha.id == alpha.id
    assert leased_alpha.profile_id == "alpha"
    store.close()
