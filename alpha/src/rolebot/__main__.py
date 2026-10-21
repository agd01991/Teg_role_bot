from __future__ import annotations

import argparse
import asyncio
import logging

from aiogram import BaseMiddleware, Bot, Dispatcher, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import Message, Update

from .config import Config
from .parsing import Action, parse
from .privacy import Journal
from .service import Gateway, Service, present
from .store import Denied, Store

logger = logging.getLogger(__name__)
COMMANDS = {
    "start",
    "help",
    "id",
    "create",
    "confirm",
    "assign",
    "unassign",
    "roles",
    "optout",
    "optin",
    "delete_me",
    "jobs",
    "stop",
    "invoke",
    "invalid_call",
    "ping",
    "tag",
    "migration",
}


class SafeLogs(logging.Filter):
    def filter(self, record):
        if not record.name.startswith("rolebot"):
            kind = (
                record.exc_info[0].__name__
                if record.exc_info and record.exc_info[0]
                else "none"
            )
            record.msg, record.args = "framework_event error_type=%s", (kind,)
        record.exc_info = record.exc_text = record.stack_info = None
        return True


def configure_logging():
    handler = logging.StreamHandler()
    handler.addFilter(SafeLogs())
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logging.getLogger().handlers[:] = [handler]
    logging.getLogger().setLevel(logging.WARNING)


class Boundary(BaseMiddleware):
    def __init__(self, service):
        self.service = service

    async def __call__(self, handler, event: Update, data):
        service = self.service
        message = event.message
        member = event.chat_member or event.my_chat_member
        action = None
        if message:
            if message.chat.id not in service.config.admins:
                operators = {
                    person
                    for scope in service.config.admins.values()
                    for person in scope
                }
                if message.chat.type != "private" or not message.from_user:
                    return None
                known = (
                    message.from_user.id in operators
                    or await service.store.pool.fetchval(
                        "SELECT 1 FROM people WHERE id=$1", message.from_user.id
                    )
                )
                if not known:
                    return None
            action = (
                Action("migration")
                if message.migrate_to_chat_id
                else parse(message, service.username)
            )
            if action is None or action.kind not in COMMANDS:
                return None
        elif not member or member.chat.id not in service.config.admins:
            return None
        if not service.journal.healthy:
            service.failed.set()
            return None
        try:
            if not await service.store.claim_update(
                event.update_id,
                message.chat.id if message else None,
                message.message_id if message else None,
            ):
                return None
            data["alpha_action"], data["alpha_update_id"] = action, event.update_id
            return await handler(event, data)
        except (Denied, ValueError) as exc:
            if message:
                text = (
                    str(exc)
                    if isinstance(exc, Denied)
                    else "Некорректные параметры команды. Код: 2–32 буквы одного алфавита, цифры и подчёркивание; начинается с буквы."
                )
                await service.reply(message, text)
        # Database/privacy failures stop the process without exposing credentials.
        except Exception as exc:  # noqa: BLE001
            logger.error("handler_failed_closed error_type=%s", type(exc).__name__)
            service.failed.set()
        return None


def router_for(service):
    router = Router()

    @router.message()
    async def message_handler(
        message: Message, alpha_action: Action, alpha_update_id: int
    ):
        if alpha_action.kind == "migration":
            await service.store.pool.execute(
                "UPDATE chats SET enabled=false WHERE id=$1", message.chat.id
            )
            await service.store.pool.execute(
                "UPDATE jobs SET status='cancelled' WHERE chat=$1 AND status='pending'",
                message.chat.id,
            )
            return
        await service.handle(message, alpha_action, alpha_update_id)

    @router.chat_member()
    async def member_handler(event, alpha_update_id: int):
        member = event.new_chat_member
        await service.store.observe(
            event.chat.id,
            member.user.id,
            member.user.full_name,
            present(member),
            event=alpha_update_id,
        )

    @router.my_chat_member()
    async def bot_member_handler(event):
        status = getattr(
            event.new_chat_member.status, "value", event.new_chat_member.status
        )
        if status not in {"administrator", "creator"}:
            await service.store.pool.execute(
                "UPDATE chats SET enabled=false WHERE id=$1", event.chat.id
            )
            await service.store.pool.execute(
                "UPDATE jobs SET status='cancelled' WHERE chat=$1 AND status='pending'",
                event.chat.id,
            )

    return router


async def run(args):
    config = Config.load(discovery=args.discover)
    bot = Bot(config.token, session=AiohttpSession(timeout=20))
    store = None
    lock_connection = None
    try:
        me = await bot.get_me()
        webhook = await bot.get_webhook_info()
        if webhook.url:
            raise Denied(
                "У бота настроен webhook. Удалите его вручную перед polling; ожидающие updates сохранены."
            )
        if not me.username:
            raise Denied("У бота нет username.")
        if args.discover:
            dispatcher = Dispatcher()
            router = Router()

            @router.message()
            async def discovery(message: Message):
                print(
                    f"chat_id={message.chat.id}; user_id={message.from_user.id if message.from_user else 'unknown'}",
                    flush=True,
                )

            dispatcher.include_router(router)
            print(
                f"Discovery @{me.username}: отправьте /start в ЛС и /id@{me.username} в тестовом чате. Ctrl+C — остановка. Тексты не сохраняются.",
                flush=True,
            )
            await dispatcher.start_polling(
                bot, allowed_updates=["message"], handle_as_tasks=False
            )
            return
        store = await Store.open(config.database_url)
        lock_connection = await store.pool.acquire()
        if not await lock_connection.fetchval(
            "SELECT pg_try_advisory_lock(20261020,1380928581)"
        ):
            raise Denied("Этот экземпляр базы уже используется другим процессом бота.")
        await store.migrate()
        await store.initialize(me.id, config.admins)
        journal = Journal(store, config.data_dir)
        await journal.start()
        for chat in config.admins:
            membership = await bot.get_chat_member(chat, me.id)
            if getattr(membership.status, "value", membership.status) not in {
                "administrator",
                "creator",
            }:
                raise Denied(
                    "Для тестов бот должен быть администратором каждого разрешённого чата. Лишние права выдавать не требуется."
                )
        if args.grant:
            chat, person = args.grant
            if person not in config.admins.get(chat, ()):
                raise Denied(
                    "Эта пара чат/оператор не разрешена локальной конфигурацией."
                )
            service = Service(config, store, journal, Gateway(bot), me.username)
            await service.refresh(chat, person)
            await store.pool.execute(
                "UPDATE members SET admin_revoked=false WHERE chat=$1 AND person=$2",
                chat,
                person,
            )
            print(
                "Продуктовое право оператора восстановлено локальным владельцем конфигурации."
            )
            return
        if args.resolve is not None:
            # Operator must inspect Telegram manually before this local-only operation.
            async with store.pool.acquire() as connection, connection.transaction():
                found = await connection.fetchval(
                    "UPDATE jobs SET status='cancelled' WHERE id=$1 AND status='uncertain' RETURNING id",
                    args.resolve,
                )
                if not found:
                    raise Denied("Вызов с неизвестным результатом не найден.")
                await connection.execute(
                    "UPDATE parts SET status='failed' WHERE job=$1 AND status='uncertain'",
                    args.resolve,
                )
            print("Сверка записана. Неотправленные части не возобновляются.")
            return
        service = Service(config, store, journal, Gateway(bot), me.username)
        dispatcher = Dispatcher()
        dispatcher.update.outer_middleware(Boundary(service))
        dispatcher.include_router(router_for(service))
        print(
            f"Тестовая альфа @{me.username} запущена; чатов: {len(config.admins)}. В тестовом чате: /help@{me.username}. Ctrl+C — остановка.",
            flush=True,
        )

        async def polling():
            try:
                await dispatcher.start_polling(
                    bot,
                    allowed_updates=["message", "chat_member", "my_chat_member"],
                    handle_as_tasks=False,
                    close_bot_session=False,
                )
            finally:
                worker_task.cancel()

        async with asyncio.TaskGroup() as group:
            worker_task = group.create_task(service.worker())
            group.create_task(polling())
    finally:
        if store:
            if lock_connection:
                await store.pool.release(lock_connection)
            await store.pool.close()
        await bot.session.close()


def main():
    parser = argparse.ArgumentParser(description="Closed rolebot test alpha")
    parser.add_argument("--discover", action="store_true")
    parser.add_argument("--grant", type=int, nargs=2, metavar=("CHAT", "USER"))
    parser.add_argument("--resolve", type=int, metavar="JOB")
    args = parser.parse_args()
    configure_logging()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass
    except Denied as exc:
        raise SystemExit(str(exc)) from None
    except Exception as exc:  # noqa: BLE001 - upstream messages may contain token/DSN
        raise SystemExit(
            f"Запуск/работа остановлены безопасно; error_type={type(exc).__name__}. Проверьте Docker, конфигурацию и инструкцию alpha/README.md."
        ) from None


if __name__ == "__main__":
    main()
