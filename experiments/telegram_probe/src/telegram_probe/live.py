from __future__ import annotations

import argparse
import asyncio
import logging
from enum import Enum
from typing import Any

from .config import Config
from .core import Entity, Invocation, Person, Probe

_MEMBER_STATUSES = {"creator", "administrator", "member"}


def _enum_value(value: Any) -> str:
    """Return an API value for both parsed strings and aiogram enums."""
    if isinstance(value, Enum):
        value = value.value
    return value if isinstance(value, str) else ""


def is_actual_member(member: Any) -> bool:
    """Interpret Bot API membership consistently and deny unknown states."""
    status = _enum_value(getattr(member, "status", "")).casefold()
    if status in _MEMBER_STATUSES:
        return True
    if status == "restricted":
        return bool(getattr(member, "is_member", False))
    return False


def assignment_command(text: str | None, bot_username: str) -> bool:
    if not text:
        return False
    command = text.strip().casefold()
    return command in {
        "/probe_assign",
        f"/probe_assign@{bot_username}".casefold(),
    }


class FrameworkLogFilter(logging.Filter):
    """Keep framework diagnostics without rendering updates or HTTP bodies."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == "aiogram" or record.name.startswith("aiogram."):
            if record.levelno < logging.ERROR:
                record.msg = "aiogram event info"
                record.args = ()
            else:
                error_type = self._error_type(record)
                record.msg = "aiogram event failed error_type=%s"
                record.args = (error_type,)
            record.exc_info = None
            record.exc_text = None
        return True

    @staticmethod
    def _error_type(record: logging.LogRecord) -> str:
        if record.exc_info:
            exc_type = record.exc_info[0]
            if exc_type is not None:
                return exc_type.__name__
        args = record.args if isinstance(record.args, tuple) else (record.args,)
        for arg in args:
            if isinstance(arg, BaseException):
                return type(arg).__name__
        return "unknown"


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.addFilter(FrameworkLogFilter())
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


def build_router(bot: Any, config: Config, me: Any):
    from aiogram import Router
    from aiogram.enums import ParseMode
    from aiogram.exceptions import TelegramNetworkError
    from aiogram.types import Message, ReplyParameters

    router = Router()
    probe = Probe(config.chats, config.operators, config.chunk_size, config.delay)

    class LiveTransport:
        async def send(
            self, *, chat_id: int, text: str, reply_to: int, thread_id: int | None
        ) -> None:
            try:
                await bot.send_message(
                    chat_id,
                    text,
                    parse_mode=ParseMode.HTML,
                    reply_parameters=ReplyParameters(
                        message_id=reply_to, allow_sending_without_reply=False
                    ),
                    message_thread_id=thread_id,
                )
            except TelegramNetworkError as exc:
                raise TimeoutError("send outcome is uncertain") from exc

    async def member_check(chat_id: int, user_id: int) -> bool:
        member = await bot.get_chat_member(chat_id, user_id)
        return is_actual_member(member)

    @router.message()
    async def assign_or_invoke(message: Message) -> None:
        sender = message.from_user
        if not sender or sender.is_bot or message.chat.id not in config.chats:
            return
        if assignment_command(message.text, me.username):
            target_message = message.reply_to_message
            if message.forward_origin is not None or not target_message:
                await message.reply(
                    "Назначение отклонено: нужна обычная reply-команда."
                )
                return
            if target_message.forward_origin is not None:
                await message.reply(
                    "Назначение отклонено: пересланный target запрещён."
                )
                return
            target = target_message.from_user
            if not target or target.is_bot:
                await message.reply(
                    "Назначение отклонено: участник не определён или является ботом."
                )
                return
            try:
                operator = await bot.get_chat_member(message.chat.id, sender.id)
            except Exception as exc:
                logging.error(
                    "operator_check_failed error_type=%s chat_id=%s",
                    type(exc).__name__,
                    message.chat.id,
                )
                return
            if _enum_value(operator.status).casefold() not in {
                "administrator",
                "creator",
            }:
                await message.reply(
                    "Назначение отклонено: нужны права администратора Telegram."
                )
                return
            try:
                target_present = await member_check(message.chat.id, target.id)
            except Exception as exc:
                logging.error(
                    "target_member_check_failed error_type=%s outcome=denied "
                    "chat_id=%s user_id=%s update_key=%s",
                    type(exc).__name__,
                    message.chat.id,
                    target.id,
                    message.message_id,
                )
                return
            if not target_present:
                await message.reply(
                    "Назначение отклонено: участник сейчас отсутствует в чате."
                )
                return
            try:
                probe.assign(
                    message.chat.id,
                    sender.id,
                    Person(target.id, target.full_name, target.is_bot),
                )
                await message.reply(
                    "Тестовое назначение сохранено до перезапуска процесса."
                )
            except (PermissionError, ValueError) as exc:
                await message.reply(f"Назначение отклонено: {exc}")
            return
        if not message.text:
            return
        invocation = Invocation(
            message.message_id,
            message.chat.id,
            message.message_id,
            message.message_thread_id,
            Person(sender.id, sender.full_name, sender.is_bot),
            message.text,
            is_forwarded=message.forward_origin is not None,
            entities=tuple(
                Entity(_enum_value(entity.type), entity.offset, entity.length)
                for entity in (message.entities or ())
            ),
        )
        outcome = await probe.invoke(
            invocation, me.username, LiveTransport(), member_check
        )
        logging.info(
            "probe outcome=%s chat_id=%s update_key=%s",
            outcome,
            message.chat.id,
            message.message_id,
        )

    @router.chat_member()
    async def membership(event: Any) -> None:
        if event.chat.id not in config.chats:
            return
        member = event.new_chat_member
        user_id = member.user.id
        present = is_actual_member(member)
        was_present = is_actual_member(event.old_chat_member)
        if not present:
            probe.leave(event.chat.id, user_id)
            action = "deactivated"
        elif not was_present:
            probe.return_to_chat(event.chat.id, user_id)
            action = "assignment_not_restored"
        else:
            action = "kept"
        logging.info(
            "membership_event chat_id=%s user_id=%s status=%s is_member=%s action=%s",
            event.chat.id,
            user_id,
            _enum_value(member.status),
            getattr(member, "is_member", None),
            action,
        )

    return router, probe


async def run(discovery: bool) -> None:
    from aiogram import Bot, Dispatcher, Router
    from aiogram.exceptions import TelegramAPIError
    from aiogram.types import Message

    config = Config.from_env(discovery=discovery)
    bot = Bot(config.token)
    try:
        try:
            me = await bot.get_me()
            webhook = await bot.get_webhook_info()
            if webhook.url:
                raise RuntimeError(
                    "a webhook is configured; remove it explicitly before polling "
                    "(pending updates were not changed)"
                )
        except TelegramAPIError as exc:
            raise RuntimeError(
                f"Telegram API unavailable or token invalid: {type(exc).__name__}"
            ) from exc
        if not me.username:
            raise RuntimeError("getMe returned no bot username")
        print(f"Authenticated as @{me.username}; no token or message text is logged.")

        if discovery:
            router = Router()

            @router.message()
            async def show_ids(message: Message) -> None:
                sender = message.from_user
                print(
                    f"chat_id={message.chat.id}; "
                    f"user_id={sender.id if sender else 'unavailable'}"
                )

        else:
            router, _ = build_router(bot, config, me)

        dp = Dispatcher()
        dp.include_router(router)
        await dp.start_polling(bot, allowed_updates=["message", "chat_member"])
    finally:
        await bot.session.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="RES-001 disposable Telegram probe")
    parser.add_argument(
        "--discover-ids", action="store_true", help="print numeric IDs from updates"
    )
    args = parser.parse_args()
    configure_logging()
    try:
        asyncio.run(run(args.discover_ids))
    except (ValueError, RuntimeError) as exc:
        raise SystemExit(f"Configuration/startup error: {exc}") from None


if __name__ == "__main__":
    main()
