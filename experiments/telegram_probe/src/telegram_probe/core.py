from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from html import escape
from typing import Awaitable, Callable, Protocol


@dataclass(frozen=True)
class Person:
    user_id: int
    display_name: str
    is_bot: bool = False


@dataclass(frozen=True)
class Entity:
    kind: str
    offset: int
    length: int


@dataclass(frozen=True)
class Invocation:
    update_id: int
    chat_id: int
    message_id: int
    thread_id: int | None
    author: Person
    text: str
    is_forwarded: bool = False
    entities: tuple[Entity, ...] = ()


class Transport(Protocol):
    async def send(
        self, *, chat_id: int, text: str, reply_to: int, thread_id: int | None
    ) -> None: ...


MemberCheck = Callable[[int, int], Awaitable[bool]]


def _utf16_slice(text: str, offset: int, length: int) -> str:
    encoded = text.encode("utf-16-le")
    return encoded[offset * 2 : (offset + length) * 2].decode("utf-16-le")


def parse_probe(
    text: str, bot_username: str, entities: tuple[Entity, ...] = ()
) -> bool:
    """Accept only a leading bot mention followed by #probe or @probe."""
    meaningful = {
        entity
        for entity in entities
        if entity.kind
        in {"mention", "code", "pre", "blockquote", "expandable_blockquote"}
    }
    if meaningful:
        if any(
            entity.kind in {"code", "pre", "blockquote", "expandable_blockquote"}
            for entity in meaningful
        ):
            return False
        leading = min(meaningful, key=lambda entity: entity.offset)
        if leading.kind != "mention" or leading.offset != 0:
            return False
        if (
            _utf16_slice(text, leading.offset, leading.length).casefold()
            != f"@{bot_username}".casefold()
        ):
            return False
    words = text.strip().split()
    if len(words) < 2 or words[0].casefold() != f"@{bot_username}".casefold():
        return False
    return words[1].casefold().rstrip(",") in {"#probe", "@probe"}


def mention(person: Person) -> str:
    return f'<a href="tg://user?id={person.user_id}">{escape(person.display_name)}</a>'


@dataclass
class Probe:
    allowed_chats: frozenset[int]
    operators: frozenset[int]
    chunk_size: int = 2
    chunk_delay: float = 0
    assignments: dict[int, dict[int, Person]] = field(default_factory=dict)
    departed: dict[int, set[int]] = field(default_factory=dict)
    processed: set[tuple[int, int]] = field(default_factory=set)
    outcomes: dict[tuple[int, int], str] = field(default_factory=dict)

    def assign(self, chat_id: int, operator_id: int, person: Person) -> None:
        self._authorize(chat_id, operator_id)
        if person.is_bot:
            raise ValueError("bots cannot be assigned")
        self.assignments.setdefault(chat_id, {})[person.user_id] = person
        self.departed.setdefault(chat_id, set()).discard(person.user_id)

    def leave(self, chat_id: int, user_id: int) -> None:
        self.departed.setdefault(chat_id, set()).add(user_id)

    def return_to_chat(self, chat_id: int, user_id: int) -> None:
        # Intentionally does not restore an assignment in this process-only probe.
        self.assignments.setdefault(chat_id, {}).pop(user_id, None)
        self.departed.setdefault(chat_id, set()).discard(user_id)

    async def invoke(
        self,
        invocation: Invocation,
        bot_username: str,
        transport: Transport,
        member_check: MemberCheck,
    ) -> str:
        key = (invocation.chat_id, invocation.update_id)
        if key in self.processed:
            return self.outcomes.get(key, "processing")
        self.processed.add(key)
        if (
            invocation.chat_id not in self.allowed_chats
            or invocation.author.is_bot
            or invocation.is_forwarded
        ):
            return self._finish(key, "ignored")
        if not parse_probe(invocation.text, bot_username, invocation.entities):
            return self._finish(key, "ignored")

        recipients = [
            person
            for person in self.assignments.get(invocation.chat_id, {}).values()
            if person.user_id != invocation.author.user_id and not person.is_bot
        ]
        chunks = [
            recipients[i : i + self.chunk_size]
            for i in range(0, len(recipients), self.chunk_size)
        ]
        for index, chunk in enumerate(chunks):
            eligible = [
                person
                for person in chunk
                if person.user_id not in self.departed.get(invocation.chat_id, set())
                and await member_check(invocation.chat_id, person.user_id)
            ]
            if not eligible:
                continue
            payload = "Проверка роли: " + " ".join(
                mention(person) for person in eligible
            )
            try:
                await transport.send(
                    chat_id=invocation.chat_id,
                    text=payload,
                    reply_to=invocation.message_id,
                    thread_id=invocation.thread_id,
                )
            except TimeoutError:
                return self._finish(key, "uncertain")
            except Exception:
                self._finish(key, "failed")
                raise
            if index + 1 < len(chunks) and self.chunk_delay:
                await asyncio.sleep(self.chunk_delay)
        return self._finish(key, "sent" if chunks else "empty")

    def _authorize(self, chat_id: int, operator_id: int) -> None:
        if chat_id not in self.allowed_chats:
            raise PermissionError("chat is not in PROBE_ALLOWED_CHAT_IDS")
        if operator_id not in self.operators:
            raise PermissionError("user is not in PROBE_OPERATOR_USER_IDS")

    def _finish(self, key: tuple[int, int], outcome: str) -> str:
        self.outcomes[key] = outcome
        return outcome
