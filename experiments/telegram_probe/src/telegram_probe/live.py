from __future__ import annotations

import argparse
import asyncio
import logging

from .config import Config
from .core import Entity, Invocation, Person, Probe


async def run(discovery: bool) -> None:
    from aiogram import Bot, Dispatcher, Router
    from aiogram.enums import ChatMemberStatus, ParseMode
    from aiogram.exceptions import TelegramAPIError, TelegramNetworkError
    from aiogram.types import Message, ReplyParameters

    config = Config.from_env(discovery=discovery)
    bot = Bot(config.token)
    try:
        me = await bot.get_me()
        webhook = await bot.get_webhook_info()
        if webhook.url:
            raise RuntimeError(
                "a webhook is configured; remove it explicitly before polling (pending updates were not changed)"
            )
    except TelegramAPIError as exc:
        raise RuntimeError(
            f"Telegram API unavailable or token invalid: {type(exc).__name__}"
        ) from exc
    if not me.username:
        raise RuntimeError("getMe returned no bot username")
    print(f"Authenticated as @{me.username}; no token or message text is logged.")

    router = Router()
    if discovery:

        @router.message()
        async def show_ids(message: Message) -> None:
            sender = message.from_user
            print(
                f"chat_id={message.chat.id}; user_id={sender.id if sender else 'unavailable'}"
            )
    else:
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
                    # Delivery may already have reached Telegram; never retry automatically.
                    raise TimeoutError("send outcome is uncertain") from exc

        async def member_check(chat_id: int, user_id: int) -> bool:
            member = await bot.get_chat_member(chat_id, user_id)
            return member.status not in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}

        @router.message()
        async def assign_or_invoke(message: Message) -> None:
            sender = message.from_user
            if not sender or message.chat.id not in config.chats:
                return
            if message.text == "/probe_assign":
                if not message.reply_to_message:
                    await message.reply(
                        "Команда эксперимента работает только ответом на сообщение участника."
                    )
                    return
                operator = await bot.get_chat_member(message.chat.id, sender.id)
                if operator.status not in {
                    ChatMemberStatus.ADMINISTRATOR,
                    ChatMemberStatus.CREATOR,
                }:
                    await message.reply(
                        "Назначение отклонено: нужны права администратора Telegram."
                    )
                    return
                target = message.reply_to_message.from_user
                if not target:
                    await message.reply(
                        "Нельзя определить отправителя исходного сообщения."
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
                    Entity(entity.type, entity.offset, entity.length)
                    for entity in (message.entities or ())
                ),
            )
            try:
                outcome = await probe.invoke(
                    invocation, me.username, LiveTransport(), member_check
                )
            except TelegramAPIError as exc:
                logging.error(
                    "Telegram rejected member check or send; verify bot rights/API availability: %s chat_id=%s",
                    type(exc).__name__,
                    message.chat.id,
                )
                return
            logging.info(
                "probe outcome=%s chat_id=%s update_key=%s",
                outcome,
                message.chat.id,
                message.message_id,
            )

        @router.chat_member()
        async def membership(event) -> None:
            status = event.new_chat_member.status
            user_id = event.new_chat_member.user.id
            if status in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}:
                probe.leave(event.chat.id, user_id)
            elif event.old_chat_member.status in {
                ChatMemberStatus.LEFT,
                ChatMemberStatus.KICKED,
            }:
                probe.return_to_chat(event.chat.id, user_id)

    dp = Dispatcher()
    dp.include_router(router)
    try:
        await dp.start_polling(bot, allowed_updates=["message", "chat_member"])
    finally:
        await bot.session.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="RES-001 disposable Telegram probe")
    parser.add_argument(
        "--discover-ids",
        action="store_true",
        help="print numeric IDs from received updates",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        asyncio.run(run(args.discover_ids))
    except (ValueError, RuntimeError) as exc:
        raise SystemExit(f"Configuration/startup error: {exc}") from None


if __name__ == "__main__":
    main()
