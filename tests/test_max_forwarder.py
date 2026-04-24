from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.max_client import DownloadResult, MaxMessage
from app.max_forwarder import forward_max_message
from app.message_store import MessageStore
from app.resolver import ContactResolver


def _make_sender(*, message_ids: list[int]):
    send_results = [SimpleNamespace(message_id=message_id) for message_id in message_ids]
    return SimpleNamespace(
        chat_id="-100",
        send=AsyncMock(side_effect=send_results),
        send_photo=AsyncMock(),
        send_document=AsyncMock(),
        send_video=AsyncMock(),
        send_voice=AsyncMock(),
        send_sticker=AsyncMock(),
    )


def _make_resolver():
    return SimpleNamespace(
        resolve_user=AsyncMock(return_value="Alice"),
        is_dm=lambda chat_id: True,
        chat_name=lambda chat_id: "Alice",
    )


def _make_media_sender(*, send_message_id: int = 7001, voice_message_id: int = 7101):
    media_result = SimpleNamespace(message_id=send_message_id + 1)
    return SimpleNamespace(
        chat_id="-100",
        send=AsyncMock(return_value=SimpleNamespace(message_id=send_message_id)),
        send_photo=AsyncMock(return_value=media_result),
        send_document=AsyncMock(return_value=media_result),
        send_video=AsyncMock(return_value=media_result),
        send_voice=AsyncMock(return_value=SimpleNamespace(message_id=voice_message_id)),
        send_sticker=AsyncMock(),
    )


def _rest_file_attachment(
    *,
    url: str | None,
    token: str | None,
    filename: str,
    size: int = 0,
):
    payload = {}
    if url is not None:
        payload["url"] = url
    if token is not None:
        payload["token"] = token

    return {
        "type": "file",
        "payload": payload,
        "filename": filename,
        "size": size,
    }


@pytest.mark.asyncio
async def test_forward_max_message_skips_duplicate_max_message_id(tmp_path):
    sender = _make_sender(message_ids=[7001, 7002])
    resolver = _make_resolver()
    client = SimpleNamespace(download_file=AsyncMock(return_value=None))
    store = MessageStore(str(tmp_path / "messages.sqlite3"))

    try:
        msg = MaxMessage(chat_id=42, sender_id=7, text="Hello from Max", message_id="max-1")

        await forward_max_message(
            msg,
            client=client,
            sender=sender,
            resolver=resolver,
            message_store=store,
        )
        await forward_max_message(
            msg,
            client=client,
            sender=sender,
            resolver=resolver,
            message_store=store,
        )

        assert sender.send.await_count == 1
        stored = store.get_by_max_message(max_chat_id=42, max_message_id="max-1")
        assert stored is not None
        assert stored.tg_message_id == 7001
    finally:
        store.close()


@pytest.mark.asyncio
async def test_forward_max_audio_sends_voice_when_download_retry_succeeds(tmp_path):
    sender = _make_media_sender()
    resolver = _make_resolver()
    client = SimpleNamespace(
        download_file_result=AsyncMock(return_value=DownloadResult(data=b"voice-bytes", status=200, used_authorization=False)),
        fetch_message=AsyncMock(),
        download_file=AsyncMock(return_value=None),
    )
    store = MessageStore(str(tmp_path / "messages.sqlite3"))

    try:
        msg = MaxMessage(
            chat_id=42,
            sender_id=7,
            text="",
            message_id="max-audio-1",
            attaches=[
                {
                    "_type": "AUDIO",
                    "url": "https://media.okcdn.ru/stale.ogg?expires=1&sig=abc",
                    "token": "audio-token",
                    "audioId": 77,
                }
            ],
        )

        await forward_max_message(
            msg,
            client=client,
            sender=sender,
            resolver=resolver,
            message_store=store,
        )

        client.download_file_result.assert_awaited_once_with("https://media.okcdn.ru/stale.ogg?expires=1&sig=abc")
        client.fetch_message.assert_not_awaited()
        assert sender.send_voice.await_count == 1
        assert sender.send_voice.await_args.args[0] == b"voice-bytes"
        assert sender.send.await_count == 0
    finally:
        store.close()


@pytest.mark.asyncio
async def test_forward_max_audio_falls_back_to_text_when_download_retry_fails(tmp_path):
    sender = _make_media_sender(send_message_id=7201)
    resolver = _make_resolver()
    client = SimpleNamespace(
        download_file_result=AsyncMock(return_value=DownloadResult(status=403, used_authorization=False)),
        fetch_message=AsyncMock(),
        download_file=AsyncMock(return_value=None),
    )
    store = MessageStore(str(tmp_path / "messages.sqlite3"))

    try:
        msg = MaxMessage(
            chat_id=42,
            sender_id=7,
            text="",
            message_id="max-audio-3",
            attaches=[
                {
                    "_type": "AUDIO",
                    "url": "https://media.okcdn.ru/stale-fail.ogg?expires=1&sig=abc",
                    "token": "audio-token",
                    "audioId": 99,
                }
            ],
        )

        await forward_max_message(
            msg,
            client=client,
            sender=sender,
            resolver=resolver,
            message_store=store,
        )

        client.download_file_result.assert_awaited_once_with("https://media.okcdn.ru/stale-fail.ogg?expires=1&sig=abc")
        client.fetch_message.assert_not_awaited()
        assert sender.send_voice.await_count == 0
        assert sender.send.await_count == 1
        assert "[аудио]" in sender.send.await_args.args[0]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_forward_max_message_does_not_deduplicate_empty_max_message_id(tmp_path):
    sender = _make_sender(message_ids=[7001, 7002])
    resolver = _make_resolver()
    client = SimpleNamespace(download_file=AsyncMock(return_value=None))
    store = MessageStore(str(tmp_path / "messages.sqlite3"))

    try:
        msg = MaxMessage(chat_id=42, sender_id=7, text="Hello from Max", message_id="")

        await forward_max_message(
            msg,
            client=client,
            sender=sender,
            resolver=resolver,
            message_store=store,
        )
        await forward_max_message(
            msg,
            client=client,
            sender=sender,
            resolver=resolver,
            message_store=store,
        )

        assert sender.send.await_count == 2
        assert store.get_by_max_message(max_chat_id=42, max_message_id="") is None
    finally:
        store.close()


@pytest.mark.asyncio
async def test_forward_max_reply_uses_tg_to_max_mapping_for_native_reply(tmp_path):
    sender = _make_sender(message_ids=[7002])
    resolver = _make_resolver()
    client = SimpleNamespace(download_file=AsyncMock(return_value=None))
    store = MessageStore(str(tmp_path / "messages.sqlite3"))

    try:
        store.upsert_mapping(
            tg_chat_id=-100,
            max_chat_id=42,
            max_message_id="max-1",
            tg_message_id=7001,
            message_thread_id=55,
            direction="tg_to_max",
            source="telegram",
        )

        msg = MaxMessage(
            chat_id=42,
            sender_id=7,
            text="Reply from Max",
            message_id="max-2",
            link={"type": "REPLY", "mid": "max-1"},
        )

        await forward_max_message(
            msg,
            client=client,
            sender=sender,
            resolver=resolver,
            message_store=store,
        )

        assert sender.send.await_count == 1
        assert sender.send.await_args.kwargs["reply_to_message_id"] == 7001
    finally:
        store.close()


@pytest.mark.asyncio
async def test_forward_max_reply_accepts_message_id_reply_link(tmp_path):
    sender = _make_sender(message_ids=[7002])
    resolver = _make_resolver()
    client = SimpleNamespace(download_file=AsyncMock(return_value=None))
    store = MessageStore(str(tmp_path / "messages.sqlite3"))

    try:
        store.upsert_mapping(
            tg_chat_id=-100,
            max_chat_id=42,
            max_message_id="max-1",
            tg_message_id=7001,
            message_thread_id=55,
            direction="tg_to_max",
            source="telegram",
        )

        msg = MaxMessage(
            chat_id=42,
            sender_id=7,
            text="Reply from Max",
            message_id="max-2",
            link={"type": "REPLY", "messageId": "max-1"},
        )

        await forward_max_message(
            msg,
            client=client,
            sender=sender,
            resolver=resolver,
            message_store=store,
        )

        assert sender.send.await_count == 1
        assert sender.send.await_args.kwargs["reply_to_message_id"] == 7001
    finally:
        store.close()


@pytest.mark.asyncio
async def test_forward_max_message_uses_display_name_after_string_contact_resolution(tmp_path):
    sender = _make_sender(message_ids=[7001])
    resolver = ContactResolver()
    resolver._parse_contacts_response({"contacts": [{"id": "7", "firstName": "Alice"}]})
    resolver.chat_types[42] = "DIALOG"
    client = SimpleNamespace(download_file=AsyncMock(return_value=None))
    store = MessageStore(str(tmp_path / "messages.sqlite3"))

    try:
        await forward_max_message(
            MaxMessage(chat_id=42, sender_id=7, text="Hello from Max", message_id="max-1"),
            client=client,
            sender=sender,
            resolver=resolver,
            message_store=store,
        )

        payload = sender.send.await_args.args[0]
        assert "<b>Alice</b>" in payload
        assert "<b>7</b>" not in payload
    finally:
        store.close()


@pytest.mark.asyncio
async def test_forward_max_file_hydrates_document_from_fetched_message():
    sender = _make_media_sender()
    resolver = _make_resolver()
    client = SimpleNamespace(
        download_file=AsyncMock(return_value=b"file-bytes"),
        fetch_message=AsyncMock(
            return_value={
                "body": {
                    "attachments": [
                        _rest_file_attachment(
                            url="https://example.com/report.pdf",
                            token="file-token",
                            filename="report.pdf",
                            size=123,
                        )
                    ]
                }
            }
        ),
    )

    msg = MaxMessage(
        chat_id=42,
        sender_id=7,
        text="",
        message_id="max-file-1",
        attaches=[
            {
                "_type": "FILE",
                "name": "report.pdf",
                "size": 123,
                "fileId": 91,
                "token": "file-token",
            }
        ],
    )

    await forward_max_message(
        msg,
        client=client,
        sender=sender,
        resolver=resolver,
    )

    client.fetch_message.assert_awaited_once_with("max-file-1")
    client.download_file.assert_awaited_once_with("https://example.com/report.pdf")
    sender.send_document.assert_awaited_once()
    assert sender.send_document.await_args.args[0] == b"file-bytes"
    assert sender.send_document.await_args.kwargs["filename"] == "report.pdf"
    assert sender.send.await_count == 0


@pytest.mark.asyncio
async def test_forward_max_file_prefers_token_match_over_filename_match():
    sender = _make_media_sender()
    resolver = _make_resolver()
    client = SimpleNamespace(
        download_file=AsyncMock(return_value=b"file-bytes"),
        fetch_message=AsyncMock(
            return_value={
                "body": {
                    "attachments": [
                        _rest_file_attachment(
                            url="https://example.com/by-name.pdf",
                            token="other-token",
                            filename="report.pdf",
                        ),
                        _rest_file_attachment(
                            url="https://example.com/by-token.pdf",
                            token="wanted-token",
                            filename="token-wins.pdf",
                        ),
                    ]
                }
            }
        ),
    )

    msg = MaxMessage(
        chat_id=42,
        sender_id=7,
        text="",
        message_id="max-file-2",
        attaches=[
            {
                "_type": "FILE",
                "name": "report.pdf",
                "size": 100,
                "fileId": 92,
                "token": "wanted-token",
            }
        ],
    )

    await forward_max_message(
        msg,
        client=client,
        sender=sender,
        resolver=resolver,
    )

    client.download_file.assert_awaited_once_with("https://example.com/by-token.pdf")
    assert sender.send_document.await_args.kwargs["filename"] == "token-wins.pdf"


@pytest.mark.asyncio
async def test_forward_max_file_uses_filename_match_when_token_missing():
    sender = _make_media_sender()
    resolver = _make_resolver()
    client = SimpleNamespace(
        download_file=AsyncMock(return_value=b"file-bytes"),
        fetch_message=AsyncMock(
            return_value={
                "message": {
                    "body": {
                        "attachments": [
                            _rest_file_attachment(
                                url="https://example.com/other.pdf",
                                token="other-token",
                                filename="other.pdf",
                            ),
                            _rest_file_attachment(
                                url="https://example.com/by-filename.pdf",
                                token="filename-token",
                                filename="report.pdf",
                            ),
                        ]
                    }
                }
            }
        ),
    )

    msg = MaxMessage(
        chat_id=42,
        sender_id=7,
        text="",
        message_id="max-file-3",
        attaches=[
            {
                "_type": "FILE",
                "name": "report.pdf",
                "size": 100,
                "fileId": 93,
            }
        ],
    )

    await forward_max_message(
        msg,
        client=client,
        sender=sender,
        resolver=resolver,
    )

    client.download_file.assert_awaited_once_with("https://example.com/by-filename.pdf")
    assert sender.send_document.await_args.kwargs["filename"] == "report.pdf"


@pytest.mark.asyncio
async def test_forward_max_file_falls_back_to_text_when_fetched_attachment_has_no_url():
    sender = _make_media_sender(send_message_id=7201)
    resolver = _make_resolver()
    client = SimpleNamespace(
        download_file=AsyncMock(return_value=b"file-bytes"),
        fetch_message=AsyncMock(
            return_value={
                "body": {
                    "attachments": [
                        _rest_file_attachment(
                            url=None,
                            token="file-token",
                            filename="report.pdf",
                            size=123,
                        )
                    ]
                }
            }
        ),
    )

    msg = MaxMessage(
        chat_id=42,
        sender_id=7,
        text="",
        message_id="max-file-4",
        attaches=[
            {
                "_type": "FILE",
                "name": "report.pdf",
                "size": 123,
                "fileId": 94,
                "token": "file-token",
            }
        ],
    )

    await forward_max_message(
        msg,
        client=client,
        sender=sender,
        resolver=resolver,
    )

    client.fetch_message.assert_awaited_once_with("max-file-4")
    client.download_file.assert_not_awaited()
    sender.send_document.assert_not_awaited()
    assert sender.send.await_count == 1
    assert "report.pdf" in sender.send.await_args.args[0]


@pytest.mark.asyncio
async def test_forward_max_forwarded_file_uses_linked_message_id_for_hydration():
    sender = _make_media_sender()
    resolver = _make_resolver()
    client = SimpleNamespace(
        download_file=AsyncMock(return_value=b"linked-file-bytes"),
        fetch_message=AsyncMock(
            return_value={
                "body": {
                    "attachments": [
                        _rest_file_attachment(
                            url="https://example.com/linked.pdf",
                            token="linked-token",
                            filename="linked.pdf",
                        )
                    ]
                }
            }
        ),
    )

    msg = MaxMessage(
        chat_id=42,
        sender_id=7,
        text="",
        message_id="max-file-5",
        link={
            "type": "FORWARD",
            "message": {
                "id": "linked-1",
                "attaches": [
                    {
                        "_type": "FILE",
                        "name": "linked.pdf",
                        "size": 50,
                        "fileId": 95,
                        "token": "linked-token",
                    }
                ],
            },
        },
    )

    await forward_max_message(
        msg,
        client=client,
        sender=sender,
        resolver=resolver,
    )

    client.fetch_message.assert_awaited_once_with("linked-1")
    client.download_file.assert_awaited_once_with("https://example.com/linked.pdf")
    sender.send_document.assert_awaited_once()


@pytest.mark.asyncio
async def test_forward_max_file_with_existing_url_bypasses_fetch_message():
    sender = _make_media_sender()
    resolver = _make_resolver()
    client = SimpleNamespace(
        download_file=AsyncMock(return_value=b"file-bytes"),
        fetch_message=AsyncMock(return_value={}),
    )

    msg = MaxMessage(
        chat_id=42,
        sender_id=7,
        text="",
        message_id="max-file-6",
        attaches=[
            {
                "_type": "FILE",
                "name": "report.pdf",
                "size": 123,
                "token": "file-token",
                "url": "https://example.com/direct.pdf",
            }
        ],
    )

    await forward_max_message(
        msg,
        client=client,
        sender=sender,
        resolver=resolver,
    )

    client.fetch_message.assert_not_awaited()
    client.download_file.assert_awaited_once_with("https://example.com/direct.pdf")
    sender.send_document.assert_awaited_once()
