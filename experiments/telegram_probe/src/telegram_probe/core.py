from __future__ import annotations

import asyncio
import logging
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
logger = logging.getLogger(__name__)


def _utf16_slice(text: str, offset: int, length: int) -> str:
    encoded = text.encode("utf-16-le")
    return encoded[offset * 2 : (offset + length) * 2].decode("utf-16-le")


def parse_probe(
    text: str, bot_username: str, entities: tuple[Entity, ...] = ()
) -> bool:
    """Accept a first-significant bot mention and an immediately following role."""
    stripped = text.lstrip()
    leading_chars = len(text) - len(stripped)
    words = stripped.split()
    if len(words) < 2 or words[0].casefold() != f"@{bot_username}".casefold():
        return False
    role = words[1].rstrip(",")
    if role.casefold() not in {"#probe", "@probe"}:
        return False

    prefix = text[:leading_chars]
    mention_offset = len(prefix.encode("utf-16-le")) // 2
    mention_length = len(words[0].encode("utf-16-le")) // 2
    role_start_chars = text.find(words[1], leading_chars + len(words[0]))
    control_end = len(text[: role_start_chars + len(words[1])].encode("utf-16-le")) // 2
    if entities:
        mentions = [entity for entity in entities if entity.kind == "mention"]
        if not any(
            entity.offset == mention_offset
            and entity.length == mention_length
            and _utf16_slice(text, entity.offset, entity.length).casefold()
            == f"@{bot_username}".casefold()
            for entity in mentions
        ):
            return False
        forbidden = {"code", "pre", "blockquote", "expandable_blockquote"}
        if any(
            entity.kind in forbidden
            and entity.offset < control_end
            and entity.offset + entity.length > mention_offset
            for entity in entities
        ):
            return False
    return True


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
    membership_revisions: dict[tuple[int, int], int] = field(default_factory=dict)
    authorization_revisions: dict[tuple[int, int], int] = field(default_factory=dict)
    authorization_checks: dict[tuple[int, int], int] = field(default_factory=dict)
    authorization_revocations: dict[tuple[int, int], int] = field(default_factory=dict)

    def assign(self, chat_id: int, operator_id: int, person: Person) -> None:
        self._authorize(chat_id, operator_id)
        if person.is_bot:
            raise ValueError("bots cannot be assigned")
        self.assignments.setdefault(chat_id, {})[person.user_id] = person
        self.departed.setdefault(chat_id, set()).discard(person.user_id)

    def leave(self, chat_id: int, user_id: int) -> None:
        self._advance_membership(chat_id, user_id)
        self.assignments.setdefault(chat_id, {}).pop(user_id, None)
        self.departed.setdefault(chat_id, set()).add(user_id)

    def return_to_chat(self, chat_id: int, user_id: int) -> None:
        # Intentionally does not restore an assignment in this process-only probe.
        self._advance_membership(chat_id, user_id)
        self.assignments.setdefault(chat_id, {}).pop(user_id, None)
        self.departed.setdefault(chat_id, set()).discard(user_id)

    def membership_revision(self, chat_id: int, user_id: int) -> int:
        return self.membership_revisions.get((chat_id, user_id), 0)

    def authorization_revision(self, chat_id: int, user_id: int) -> int:
        return self.authorization_revisions.get((chat_id, user_id), 0)

    def begin_authorization_check(self, chat_id: int, user_id: int) -> int:
        key = (chat_id, user_id)
        check = self.authorization_checks.get(key, 0) + 1
        self.authorization_checks[key] = check
        return check

    def observe_authorization(
        self, chat_id: int, user_id: int, check: int, authorized: bool
    ) -> None:
        """Record ordered API evidence without letting a stale success restore rights."""
        key = (chat_id, user_id)
        revoked_at = self.authorization_revocations.get(key)
        if not authorized:
            self.authorization_revocations[key] = max(check, revoked_at or check)
            self._advance_authorization(chat_id, user_id)
        elif revoked_at is not None and check > revoked_at:
            self.authorization_revocations.pop(key, None)

    def authorization_changed(
        self, chat_id: int, user_id: int, authorized: bool
    ) -> None:
        """Invalidate in-flight work after an observed Telegram rights transition."""
        key = (chat_id, user_id)
        if authorized:
            self.authorization_revocations.pop(key, None)
        else:
            self.authorization_revocations[key] = self.authorization_checks.get(key, 0)
        self._advance_authorization(chat_id, user_id)

    def _advance_membership(self, chat_id: int, user_id: int) -> None:
        key = (chat_id, user_id)
        self.membership_revisions[key] = self.membership_revisions.get(key, 0) + 1

    def _advance_authorization(self, chat_id: int, user_id: int) -> None:
        key = (chat_id, user_id)
        self.authorization_revisions[key] = self.authorization_revisions.get(key, 0) + 1

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
        sent_parts = 0
        for index, chunk in enumerate(chunks):
            eligible = []
            for person in chunk:
                if not self._still_assigned(invocation.chat_id, person):
                    continue
                try:
                    is_member = await member_check(invocation.chat_id, person.user_id)
                except Exception as exc:
                    prefix = "partially_sent_" if sent_parts else ""
                    outcome = f"{prefix}member_check_failed"
                    self._log_failure("member_check", exc, outcome, key, person.user_id)
                    return self._finish(key, outcome)
                if not is_member:
                    self.leave(invocation.chat_id, person.user_id)
                elif self._still_assigned(invocation.chat_id, person):
                    eligible.append(person)
            # A chat_member update may have arrived while a later member was checked.
            eligible = [
                person
                for person in eligible
                if self._still_assigned(invocation.chat_id, person)
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
            except TimeoutError as exc:
                prefix = "partially_sent_" if sent_parts else ""
                outcome = f"{prefix}uncertain"
                self._log_failure("send", exc, outcome, key)
                return self._finish(key, outcome)
            except Exception as exc:
                prefix = "partially_sent_" if sent_parts else ""
                outcome = f"{prefix}send_failed"
                self._log_failure("send", exc, outcome, key)
                return self._finish(key, outcome)
            sent_parts += 1
            if index + 1 < len(chunks) and self.chunk_delay:
                await asyncio.sleep(self.chunk_delay)
        return self._finish(key, "sent" if sent_parts else "empty")

    def _still_assigned(self, chat_id: int, person: Person) -> bool:
        return self.assignments.get(chat_id, {}).get(
            person.user_id
        ) == person and person.user_id not in self.departed.get(chat_id, set())

    def _authorize(self, chat_id: int, operator_id: int) -> None:
        if chat_id not in self.allowed_chats:
            raise PermissionError("chat is not in PROBE_ALLOWED_CHAT_IDS")
        if operator_id not in self.operators:
            raise PermissionError("user is not in PROBE_OPERATOR_USER_IDS")

    def _finish(self, key: tuple[int, int], outcome: str) -> str:
        self.outcomes[key] = outcome
        return outcome

    @staticmethod
    def _log_failure(
        stage: str,
        exc: Exception,
        outcome: str,
        key: tuple[int, int],
        user_id: int | None = None,
    ) -> None:
        # Do not render the exception: Telegram errors may contain response bodies,
        # message text, or token-bearing URLs.
        logger.error(
            "probe_failure stage=%s error_type=%s outcome=%s chat_id=%s "
            "update_key=%s user_id=%s",
            stage,
            type(exc).__name__,
            outcome,
            key[0],
            key[1],
            user_id if user_id is not None else "none",
        )
