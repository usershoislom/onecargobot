"""Turn Telegram messages into model-ready items (text, images, PDFs, audio)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from telegram import Message

log = logging.getLogger(__name__)

# Telegram's Bot API will only hand bots files up to 20 MB.
MAX_FILE_BYTES = 20 * 1024 * 1024

TEXT_MIMES = {"text/plain", "text/csv", "application/json", "text/markdown"}


@dataclass
class Item:
    kind: str  # text | image | pdf | audio | video | textfile
    header: str = ""
    text: str | None = None
    data: bytes | None = None
    mime: str | None = None
    filename: str | None = None

    def describe(self) -> str:
        """Plain-text stand-in used for the sheet's 'Original Message' column."""
        if self.kind == "text":
            return f"{self.header}{self.text}"
        name = f" {self.filename}" if self.filename else ""
        return f"{self.header}[{self.kind}{name}]"


def forward_header(msg: Message, tz: ZoneInfo) -> str:
    origin = getattr(msg, "forward_origin", None)
    if origin is None:
        return ""
    who = "unknown"
    if getattr(origin, "sender_user", None):
        who = origin.sender_user.full_name
    elif getattr(origin, "sender_user_name", None):
        who = origin.sender_user_name
    elif getattr(origin, "sender_chat", None):
        who = origin.sender_chat.title or "a chat"
    elif getattr(origin, "chat", None):
        who = origin.chat.title or "a channel"
    when = origin.date.astimezone(tz).strftime("%m/%d/%Y %I:%M %p") if origin.date else ""
    return f"[Forwarded from {who}{', ' + when if when else ''}] "


async def _download(msg: Message, file_id: str, size: int | None) -> bytes | None:
    if size and size > MAX_FILE_BYTES:
        return None
    tg_file = await msg.get_bot().get_file(file_id)
    return bytes(await tg_file.download_as_bytearray())


async def items_from_message(msg: Message, tz: ZoneInfo) -> tuple[list[Item], list[str]]:
    """Return (items, problems). Problems are human-readable notes about skipped files."""
    header = forward_header(msg, tz)
    items: list[Item] = []
    problems: list[str] = []

    text = msg.text or msg.caption
    if text:
        items.append(Item(kind="text", header=header, text=text))

    async def add_file(kind: str, file_id: str, size: int | None, mime: str, filename: str | None = None):
        data = await _download(msg, file_id, size)
        if data is None:
            problems.append(f"{filename or kind} is larger than 20 MB (Telegram's limit for bots), skipped.")
            return
        if kind == "textfile":
            items.append(Item(kind="text", header=header, text=f"[File {filename}]\n" + data.decode("utf-8", "replace")))
        else:
            items.append(Item(kind=kind, header=header, data=data, mime=mime, filename=filename))

    if msg.photo:
        p = msg.photo[-1]  # largest size
        await add_file("image", p.file_id, p.file_size, "image/jpeg")
    if msg.voice:
        await add_file("audio", msg.voice.file_id, msg.voice.file_size, msg.voice.mime_type or "audio/ogg", "voice note")
    if msg.audio:
        await add_file("audio", msg.audio.file_id, msg.audio.file_size, msg.audio.mime_type or "audio/mpeg", msg.audio.file_name)
    if msg.video_note:
        await add_file("video", msg.video_note.file_id, msg.video_note.file_size, "video/mp4", "video note")
    if msg.video:
        await add_file("video", msg.video.file_id, msg.video.file_size, msg.video.mime_type or "video/mp4", msg.video.file_name)
    if msg.document:
        d = msg.document
        mime = (d.mime_type or "").lower()
        name = d.file_name or "document"
        if mime == "application/pdf" or name.lower().endswith(".pdf"):
            await add_file("pdf", d.file_id, d.file_size, "application/pdf", name)
        elif mime.startswith("image/"):
            await add_file("image", d.file_id, d.file_size, mime, name)
        elif mime.startswith("audio/"):
            await add_file("audio", d.file_id, d.file_size, mime, name)
        elif mime in TEXT_MIMES or name.lower().endswith((".txt", ".csv")):
            await add_file("textfile", d.file_id, d.file_size, mime, name)
        else:
            problems.append(f"{name}: file type '{mime or 'unknown'}' isn't supported. Send a PDF, image, or screenshot instead.")
    if msg.contact:
        c = msg.contact
        items.append(Item(kind="text", header=header, text=f"[Contact] {c.first_name} {c.last_name or ''} {c.phone_number}".strip()))
    if msg.location:
        items.append(Item(kind="text", header=header, text=f"[Location] {msg.location.latitude}, {msg.location.longitude}"))

    if not items and not problems:
        problems.append("I couldn't find anything to read in that message.")
    return items, problems
