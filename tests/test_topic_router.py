"""Tests for app/topic_router.py."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.topic_router import TopicRouter
from app.topic_store import TopicStore


def _make_router(tmp_path):
    store = TopicStore(str(tmp_path / "topics.sqlite3"))
    sender = SimpleNamespace(
        chat_id="-100",
        create_forum_topic=AsyncMock(
            side_effect=[
                SimpleNamespace(message_thread_id=10),
                SimpleNamespace(message_thread_id=20),
            ]
        ),
        edit_forum_topic=AsyncMock(return_value=True),
    )
    return TopicRouter(store, sender), store, sender


@pytest.mark.asyncio
async def test_creates_topic_once_and_reuses_mapping(tmp_path):
    router, store, sender = _make_router(tmp_path)

    first = await router.ensure_topic(42, "Alice")
    second = await router.ensure_topic(42, "Alice")

    assert first == 10
    assert second == 10
    sender.create_forum_topic.assert_awaited_once_with("Alice")
    assert store.get_by_max_chat(-100, 42).message_thread_id == 10
    store.close()


@pytest.mark.asyncio
async def test_duplicate_topic_name_gets_max_id_suffix(tmp_path):
    router, store, sender = _make_router(tmp_path)
    await router.ensure_topic(42, "Alice")

    second = await router.ensure_topic(55, "Alice")

    assert second == 20
    assert sender.create_forum_topic.await_args_list[1].args == ("Alice [55]",)
    store.close()


@pytest.mark.asyncio
async def test_renames_existing_topic_when_display_name_changes(tmp_path):
    router, store, sender = _make_router(tmp_path)
    await router.ensure_topic(42, "Alice")

    thread_id = await router.ensure_topic(42, "Alice Smith")

    assert thread_id == 10
    sender.edit_forum_topic.assert_awaited_once_with(10, "Alice Smith")
    assert store.get_by_max_chat(-100, 42).topic_name == "Alice Smith"
    store.close()
