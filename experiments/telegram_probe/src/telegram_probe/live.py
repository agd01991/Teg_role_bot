from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Any

from .config import Config
from .core import Entity, Invocation, Person, Probe

_MEMBER_STATUSES = {"creator", "administrator", "member"}
_ABSENT_STATUSES = {"left", "kicked"}


def is_actual_member(member: Any) -> bool:
    """Interpret Bot API membership consistently and deny unknown states."""
    status = str(getattr(member, "status", ""))
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
            error_type = record.exc_info[0].__name__ if record.exc_info else "none"
            record.msg = "aiogram event failed error_type=%s"
            record.args = (error_type,)
            record.exc_info = None
            record.exc_text = None
        return True


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.addFilter(FrameworkLogFilter())
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


def build_router(bot: Any, config: Config, me: Any):
    from aiogram import Router
    from aiogram.enums import ChatMemberStatus, ParseMode
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
            if str(operator.status) not in {
                str(ChatMemberStatus.ADMINISTRATOR),
                str(ChatMemberStatus.CREATOR),
            }:
                await message.reply(
                    "Назначение отклонено: нужны права администратора Telegram."
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
                Entity(str(entity.type), entity.offset, entity.length)
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
        member = event.new_chat_member
        user_id = member.user.id
        present = is_actual_member(member)
        if not present:
            probe.leave(event.chat.id, user_id)
        elif str(event.old_chat_member.status) in _ABSENT_STATUSES:
            probe.return_to_chat(event.chat.id, user_id)
        logging.info(
            "membership_event chat_id=%s user_id=%s status=%s is_member=%s action=%s",
            event.chat.id,
            user_id,
            member.status,
            getattr(member, "is_member", None),
            "kept" if present else "deactivated",
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
