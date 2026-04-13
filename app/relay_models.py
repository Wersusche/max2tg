from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RelayOperation:
    kind: str
    text: str = ""
    filename: str | None = None
    attachment_field: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"kind": self.kind}
        if self.text:
            data["text"] = self.text
        if self.filename is not None:
            data["filename"] = self.filename
        if self.attachment_field is not None:
            data["attachment_field"] = self.attachment_field
        return data

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RelayOperation":
        return cls(
            kind=str(payload["kind"]),
            text=str(payload.get("text", "")),
            filename=payload.get("filename"),
            attachment_field=payload.get("attachment_field"),
        )


@dataclass(frozen=True)
class TelegramBatch:
    max_chat_id: str
    topic_name: str | None
    operations: list[RelayOperation] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_chat_id": self.max_chat_id,
            "topic_name": self.topic_name,
            "operations": [operation.to_dict() for operation in self.operations],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TelegramBatch":
        operations = [RelayOperation.from_dict(item) for item in payload.get("operations", [])]
        return cls(
            max_chat_id=str(payload["max_chat_id"]),
            topic_name=payload.get("topic_name"),
            operations=operations,
        )


@dataclass(frozen=True)
class MaxCommand:
    id: int
    max_chat_id: str
    text: str
    elements: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "max_chat_id": self.max_chat_id,
            "text": self.text,
            "elements": self.elements,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MaxCommand":
        return cls(
            id=int(payload["id"]),
            max_chat_id=str(payload["max_chat_id"]),
            text=str(payload["text"]),
            elements=list(payload.get("elements") or []),
        )


class RelayOperationBuilder:
    def __init__(self):
        self.operations: list[RelayOperation] = []
        self.attachments: dict[str, tuple[str, bytes]] = {}

    @property
    def is_empty(self) -> bool:
        return not self.operations

    async def send(
        self,
        text: str,
        reply_markup=None,
        message_thread_id: int | None = None,
        raise_bad_request: bool = False,
    ):
        del reply_markup, message_thread_id, raise_bad_request
        if text:
            self.operations.append(RelayOperation(kind="text", text=text))
        return {"ok": True}

    async def send_photo(
        self,
        data: bytes,
        caption: str = "",
        filename: str = "photo.jpg",
        reply_markup=None,
        message_thread_id: int | None = None,
        raise_bad_request: bool = False,
    ):
        del reply_markup, message_thread_id, raise_bad_request
        self._add_media("photo", data, caption, filename)
        return {"ok": True}

    async def send_document(
        self,
        data: bytes,
        caption: str = "",
        filename: str = "file",
        reply_markup=None,
        message_thread_id: int | None = None,
        raise_bad_request: bool = False,
    ):
        del reply_markup, message_thread_id, raise_bad_request
        self._add_media("document", data, caption, filename)
        return {"ok": True}

    async def send_video(
        self,
        data: bytes,
        caption: str = "",
        filename: str = "video.mp4",
        reply_markup=None,
        message_thread_id: int | None = None,
        raise_bad_request: bool = False,
    ):
        del reply_markup, message_thread_id, raise_bad_request
        self._add_media("video", data, caption, filename)
        return {"ok": True}

    async def send_voice(
        self,
        data: bytes,
        caption: str = "",
        reply_markup=None,
        message_thread_id: int | None = None,
        raise_bad_request: bool = False,
    ):
        del reply_markup, message_thread_id, raise_bad_request
        self._add_media("voice", data, caption, "voice.ogg")
        return {"ok": True}

    async def send_sticker(
        self,
        data: bytes,
        reply_markup=None,
        message_thread_id: int | None = None,
        raise_bad_request: bool = False,
    ):
        del reply_markup, message_thread_id, raise_bad_request
        self._add_media("sticker", data, "", "sticker.webp")
        return {"ok": True}

    def build_batch(self, max_chat_id: Any, topic_name: str | None) -> TelegramBatch:
        return TelegramBatch(
            max_chat_id=str(max_chat_id),
            topic_name=topic_name,
            operations=list(self.operations),
        )

    def _add_media(self, kind: str, data: bytes, text: str, filename: str) -> None:
        attachment_field = f"file{len(self.attachments)}"
        self.attachments[attachment_field] = (filename, data)
        self.operations.append(
            RelayOperation(
                kind=kind,
                text=text,
                filename=filename,
                attachment_field=attachment_field,
            )
        )
