from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

RESERVED = frozenset(
    "all everyone here start help roles create confirm assign unassign stop jobs status "
    "id optout optin delete_me cancel ping tag me admin".split()
)
FORBIDDEN = {"code", "pre", "blockquote", "expandable_blockquote"}


def code(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    if normalized != value.casefold() or normalized in RESERVED:
        raise ValueError("reserved or ambiguous code")
    latin = re.fullmatch(r"[a-z][a-z0-9_]{1,31}", normalized)
    cyrillic = re.fullmatch(r"[а-яё][а-яё0-9_]{1,31}", normalized)
    if not (latin or cyrillic):
        raise ValueError(
            "code must be 2..32 letters of one alphabet, digits or underscore"
        )
    return normalized


@dataclass(frozen=True)
class Action:
    kind: str
    argument: str = ""


def _guard(message, kind: str, start: int, length: int, end: int) -> bool:
    entities = message.entities or []
    return any(
        getattr(entity.type, "value", entity.type) == kind
        and entity.offset == start
        and entity.length == length
        for entity in entities
    ) and not any(
        getattr(entity.type, "value", entity.type) in FORBIDDEN
        and entity.offset < end
        and entity.offset + entity.length > start
        for entity in entities
    )


def parse(message, username: str) -> Action | None:
    if (
        not message.text
        or not message.from_user
        or message.from_user.is_bot
        or message.sender_chat
        or message.forward_origin
    ):
        return None
    text = message.text
    stripped = text.lstrip()
    if not stripped:
        return None
    first, *tail = stripped.split(maxsplit=1)
    offset = len(text[: len(text) - len(stripped)].encode("utf-16-le")) // 2
    length = len(first.encode("utf-16-le")) // 2
    argument = tail[0].strip() if tail else ""
    if first.startswith("/"):
        name, _, addressed = first.partition("@")
        if addressed and addressed.casefold() != username.casefold():
            return None
        if not _guard(message, "bot_command", offset, length, offset + length):
            return None
        return Action(name[1:].casefold(), argument)
    if first.casefold() != f"@{username}".casefold():
        return None
    if not _guard(message, "mention", offset, length, offset + length):
        return None
    if not argument:
        return Action("help")
    token, *reason = argument.split(maxsplit=1)
    if not token.startswith("@") or "," in token:
        return Action("invalid_call")
    remaining = reason[0].lstrip() if reason else ""
    # A normal word starts explanation; '--' permits an explanation starting with @.
    if remaining.startswith(("@", "#", "~", ",")):
        return Action("invalid_call")
    end = (
        len(
            text[
                : text.find(token, len(text) - len(stripped) + len(first)) + len(token)
            ].encode("utf-16-le")
        )
        // 2
    )
    if not _guard(message, "mention", offset, length, end):
        return None
    try:
        return Action("invoke", code(token[1:]))
    except ValueError:
        return Action("invalid_call")
