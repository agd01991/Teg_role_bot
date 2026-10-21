from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from html import escape

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.types import ReplyParameters

from .parsing import code
from .store import Denied

logger = logging.getLogger(__name__)

JOB_LABELS = {
    "pending": "ожидает",
    "done": "завершён",
    "cancelled": "остановлен",
    "failed": "остановлен с ошибкой",
    "uncertain": "нужно сверить результат",
}

HELP = (
    "Тестовая альфа. Только отдельный согласованный чат.\n"
    "/roles — роли\n/create код Название — создать (оператор)\n"
    "/confirm токен — подтвердить создание\n"
    "/assign код — ответом на сообщение участника\n/unassign код — снять назначение\n"
    "@{username} @код — вызвать одну роль\n"
    "@{username} @код -- @username пояснение — текст с упоминанием\n"
    "/optout — запретить свои упоминания\n/optin — разрешить их снова\n"
    "/jobs — состояние своих вызовов\n/stop — остановить оставшиеся части\n"
    "/id — свои ID\n/delete_me — удалить свой профиль во всех тестовых чатах\n"
    "Лимиты: 20 ролей × 20 участников; части по 5; срок очереди 5 минут."
)


def present(member):
    status = getattr(member.status, "value", member.status)
    return status in {"creator", "administrator", "member"} or (
        status == "restricted" and member.is_member
    )


class Gateway:
    def __init__(self, bot, interval=3.5):
        self.bot = bot
        self.interval = interval
        self.lock = asyncio.Lock()
        self.last_chat = {}
        self.last_global = 0.0

    async def send(self, chat, text, message=None, thread=None, deadline=None):
        async with self.lock:
            now = time.monotonic()
            wait = max(
                0,
                self.last_chat.get(chat, 0) + self.interval - now,
                self.last_global + 0.05 - now,
            )
            if deadline is not None and now + wait >= deadline:
                raise Denied("Истёк срок отправки.")
            await asyncio.sleep(wait)
            payload = await text() if isinstance(text, Callable) else text
            if payload is None:
                return None
            self.last_chat[chat] = self.last_global = time.monotonic()
            return await self.bot.send_message(
                chat_id=chat,
                text=payload,
                parse_mode="HTML",
                reply_parameters=(
                    ReplyParameters(
                        message_id=message, allow_sending_without_reply=False
                    )
                    if message
                    else None
                ),
                message_thread_id=thread,
            )


class Service:
    def __init__(self, config, store, journal, gateway, username):
        self.config = config
        self.store = store
        self.journal = journal
        self.gateway = gateway
        self.username = username
        self.wake = asyncio.Event()
        self.failed = asyncio.Event()

    async def refresh(self, chat, person):
        revision = await self.store.revision(chat, person)
        try:
            member = await self.gateway.bot.get_chat_member(chat, person)
        # Unknown API failures deny access; their text may include secrets.
        except Exception as exc:  # noqa: BLE001
            logger.warning("membership_unavailable error_type=%s", type(exc).__name__)
            raise Denied("Проверка членства недоступна. Повторите позже.") from None
        revision = await self.store.observe(
            chat, person, member.user.full_name, present(member), expected=revision
        )
        if not present(member):
            raise Denied("Нужно действующее членство в этом чате.")
        return revision

    async def operator(self, chat, person):
        if person not in self.config.admins.get(chat, ()):
            raise Denied("Это действие доступно оператору данного тестового чата.")
        revision = await self.refresh(chat, person)
        async with self.store.transaction(chat) as connection:
            await self.store.require(connection, chat, person, revision, admin=True)
        return revision

    async def reply(self, message, text):
        try:
            await self.gateway.send(
                message.chat.id, text, message.message_id, message.message_thread_id
            )
        except (
            TelegramRetryAfter,
            TelegramNetworkError,
            TelegramBadRequest,
            TelegramForbiddenError,
        ) as exc:
            logger.warning("reply_not_retried error_type=%s", type(exc).__name__)

    async def handle(self, message, action, update_id):
        chat, actor = message.chat.id, message.from_user.id
        if message.chat.type == "private":
            if action.kind == "delete_me":
                if action.argument == "confirm":
                    await self.journal.record(actor, "delete")
                    await self.reply(
                        message,
                        "Профиль удалён во всех чатах альфы. Минимальный ID запрета сохранён; для нового участия нужен ваш /optin и новое назначение.",
                    )
                else:
                    await self.reply(
                        message,
                        "Для удаления своего профиля во всех чатах альфы отправьте /delete_me confirm. Минимальный ID запрета останется в независимом журнале.",
                    )
            elif action.kind == "optin":
                blocked = await self.store.pool.fetchval(
                    "SELECT blocked FROM people WHERE id=$1", actor
                )
                if blocked:
                    await self.journal.record(actor, "allow")
                await self.reply(
                    message,
                    "Удалённый профиль разблокирован для нового участия. Старые назначения и права не восстанавливаются; локальные отказы меняются через /optin в нужном чате.",
                )
            elif action.kind in {"start", "help", "id"}:
                await self.reply(
                    message,
                    f"Ваш user_id: <code>{actor}</code>\n"
                    + HELP.format(username=escape(self.username)),
                )
            return
        if action.kind in {"start", "help", "id"}:
            await self.refresh(chat, actor)
            await self.reply(
                message,
                f"chat_id: <code>{chat}</code>; user_id: <code>{actor}</code>\n"
                + HELP.format(username=escape(self.username)),
            )
            return
        if action.kind == "invalid_call":
            raise Denied(
                "Используйте ровно одну команду: @бот @код. Перед пояснением, начинающимся с @, поставьте --."
            )
        if action.kind in {"ping", "tag"}:
            raise Denied("Эта форма не включена. Используйте @бот @код.")
        if action.kind == "create":
            revision = await self.operator(chat, actor)
            items = action.argument.split(maxsplit=1)
            if (
                len(items) != 2
                or not 1 <= len(items[1]) <= 64
                or any(ord(c) < 32 for c in items[1])
            ):
                raise Denied("Формат: /create код Название (до 64 символов).")
            token = await self.store.stage(
                chat, actor, revision, code(items[0]), items[1]
            )
            await self.reply(
                message,
                f"Создать @{escape(code(items[0]))} «{escape(items[1])}»? В течение 2 минут отправьте:\n<code>/confirm {token}</code>",
            )
            return
        if action.kind == "confirm":
            revision = await self.operator(chat, actor)
            created = await self.store.confirm(chat, actor, revision, action.argument)
            await self.reply(
                message,
                f"Роль @{escape(created)} создана. Назначьте участника reply-командой /assign {escape(created)}.",
            )
            return
        if action.kind in {"assign", "unassign"}:
            role_code = code(action.argument)
            target_message = message.reply_to_message
            if (
                not target_message
                or target_message.forward_origin
                or target_message.sender_chat
                or not target_message.from_user
                or target_message.from_user.is_bot
            ):
                raise Denied("Ответьте командой на обычное сообщение самого участника.")
            revision = await self.operator(chat, actor)
            target = target_message.from_user.id
            target_revision = (
                await self.refresh(chat, target) if action.kind == "assign" else None
            )
            # Recheck the operator after the asynchronous target lookup.
            current = await self.operator(chat, actor)
            if current != revision:
                raise Denied("Членство оператора изменилось.")
            await self.store.assign(
                chat,
                actor,
                current,
                role_code,
                target,
                target_revision,
                remove=action.kind == "unassign",
            )
            await self.reply(
                message,
                (
                    "Назначение сохранено."
                    if action.kind == "assign"
                    else "Назначение снято (если существовало)."
                ),
            )
            return
        if action.kind == "delete_me":
            if action.argument != "confirm":
                await self.reply(
                    message,
                    "Удалить ваш профиль, назначения и настройки во всех чатах этой альфы? Для подтверждения отправьте /delete_me confirm. Минимальный ID запрета останется в независимом журнале удаления.",
                )
                return
            await self.refresh(chat, actor)
            await self.journal.record(actor, "delete")
            await self.reply(
                message,
                "Профиль удалён во всех чатах альфы; автоматическое добавление заблокировано. Для нового участия нужен ваш /optin и новое назначение.",
            )
            return
        if action.kind == "optin":
            blocked = await self.store.pool.fetchval(
                "SELECT blocked FROM people WHERE id=$1", actor
            )
            if blocked:
                await self.journal.record(actor, "allow")
            await self.refresh(chat, actor)
            await self.store.set_opt_out(chat, actor, False)
            await self.reply(
                message,
                "Упоминания разрешены в этом чате. Удалённые назначения и права не восстанавливаются.",
            )
            return
        revision = await self.refresh(chat, actor)
        async with self.store.transaction(chat) as connection:
            await self.store.require(connection, chat, actor, revision)
        if action.kind == "optout":
            await self.store.set_opt_out(chat, actor, True)
            await self.reply(
                message,
                "Бот больше не включает вас в свои вызовы в этом чате. Повторное назначение не отменяет отказ.",
            )
        elif action.kind == "roles":
            rows = await self.store.roles(chat)
            text = "\n".join(
                f"@{escape(r['code'])} — {escape(r['title'])} · {r['count']} назначений"
                for r in rows
            )
            await self.reply(
                message,
                text or "Ролей пока нет. Оператор может создать их через /create.",
            )
        elif action.kind in {"jobs", "stop"}:
            admin = actor in self.config.admins.get(chat, ())
            if admin:
                try:
                    await self.operator(chat, actor)
                except Denied:
                    admin = False
            if action.kind == "stop":
                await self.store.stop(chat, actor, admin)
                await self.reply(
                    message,
                    "Оставшиеся части остановлены. Уже начатую отправку отменить невозможно.",
                )
            else:
                rows = await self.store.jobs(chat, actor, admin)
                await self.reply(
                    message,
                    "\n".join(
                        f"#{r['id']}: {JOB_LABELS[r['status']]} · {r['sent']}/{r['total']} частей"
                        for r in rows
                    )
                    or "Вызовов пока нет.",
                )
        elif action.kind == "invoke":
            job = await self.store.enqueue(
                chat,
                actor,
                revision,
                action.argument,
                update_id,
                message.message_id,
                message.message_thread_id,
            )
            if job is not None:
                self.wake.set()

    async def worker(self):
        cleanup_at = 0.0
        while True:
            if self.failed.is_set() or not self.journal.healthy:
                raise RuntimeError("service failed closed")
            if time.monotonic() >= cleanup_at:
                await self.store.cleanup()
                cleanup_at = time.monotonic() + 3600
            part = await self.store.next_part()
            if not part:
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=1)
                except TimeoutError:
                    pass
                continue
            try:
                await self.refresh(part["chat"], part["author"])
                for person in part["recipients"]:
                    try:
                        await self.refresh(part["chat"], person)
                    except Denied:
                        # An unverified member must not reach this part.
                        await self.store.pool.execute(
                            "UPDATE parts SET recipients=array_remove(recipients,$3) WHERE job=$1 AND number=$2",
                            part["id"],
                            part["number"],
                            person,
                        )
                # Refresh recipient IDs after exclusions; never refill with new assignments.
                recipients = await self.store.pool.fetchval(
                    "SELECT recipients FROM parts WHERE job=$1 AND number=$2",
                    part["id"],
                    part["number"],
                )
                if recipients is None:  # author erased while API checks were in flight
                    continue
                part = dict(part)
                part["recipients"] = recipients

                async def prepare(part=part):
                    async with self.journal.lock:
                        if not self.journal.healthy:
                            raise Denied("Применяется удаление данных.")
                        rows = await self.store.prepare_part(part)
                        if not rows:
                            await self.store.finish(part, "skipped")
                            return None
                        mentions = " ".join(
                            f'<a href="tg://user?id={r["person"]}">{escape(r["name"] or "Участник")}</a>'
                            for r in rows
                        )
                        return f"{escape(part['title'])} · часть {part['number']+1}\n{mentions}"

                remaining = 300 - (datetime.now(UTC) - part["created"]).total_seconds()
                result = await self.gateway.send(
                    part["chat"],
                    prepare,
                    part["message_id"],
                    part["thread_id"],
                    deadline=time.monotonic() + remaining,
                )
                if result:
                    await self.store.finish(part, "sent", result.message_id)
            except TelegramRetryAfter as exc:
                await self.store.finish(
                    part,
                    "pending" if part["attempts"] < 2 else "failed",
                    retry_after=exc.retry_after if part["attempts"] < 2 else None,
                )
            except (TelegramNetworkError, TimeoutError) as exc:
                logger.error("send_uncertain error_type=%s", type(exc).__name__)
                await self.store.finish(part, "uncertain")
            except (TelegramBadRequest, TelegramForbiddenError, Denied) as exc:
                logger.warning("job_stopped error_type=%s", type(exc).__name__)
                await self.store.finish(part, "failed")
            # Unknown send outcomes must never be retried automatically.
            except Exception as exc:  # noqa: BLE001
                logger.error("worker_failed_closed error_type=%s", type(exc).__name__)
                await self.store.finish(part, "uncertain")
                raise RuntimeError("worker failed closed") from None
