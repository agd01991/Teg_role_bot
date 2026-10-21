import asyncio
import re
import unittest

import test_database as fixtures
import test_parsing as parsing_fixtures
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import GetChatMember, SendMessage
from aiogram.types import Chat, ChatMemberMember, ChatMemberOwner, Message, Update, User

from rolebot.__main__ import Boundary, router_for
from rolebot.service import Gateway


class Session(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.members = {}
        self.hook = None

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):  # noqa: ASYNC109
        self.calls.append(method)
        if self.hook:
            result = await self.hook(method)
            if result is not None:
                return result
        if isinstance(method, GetChatMember):
            return self.members.get(
                (method.chat_id, method.user_id),
                ChatMemberMember(
                    user=User(id=method.user_id, is_bot=False, first_name="User")
                ),
            )
        if isinstance(method, SendMessage):
            return Message(
                message_id=1000 + len(self.calls),
                date=0,
                chat=Chat(id=int(method.chat_id), type="supergroup"),
                text=method.text,
            )
        raise AssertionError(type(method).__name__)

    async def stream_content(self, url, **kwargs):
        yield b""


@unittest.skipUnless(fixtures.URL, "real PostgreSQL integration URL required")
class DispatcherTests(unittest.IsolatedAsyncioTestCase):
    member = staticmethod(fixtures.DatabaseTests.member)
    create = fixtures.DatabaseTests.create
    worker_once = fixtures.DatabaseTests.worker_once

    async def asyncSetUp(self):
        await fixtures.DatabaseTests.asyncSetUp(self)
        self.session = Session()
        self.bot = Bot("999:" + "A" * 35, session=self.session)
        self.service.gateway = Gateway(self.bot, interval=0)
        self.dp = Dispatcher()
        self.dp.update.outer_middleware(Boundary(self.service))
        self.dp.include_router(router_for(self.service))

    async def asyncTearDown(self):
        await self.bot.session.close()
        await fixtures.DatabaseTests.asyncTearDown(self)

    async def feed(
        self, text, *, actor=1, chat=-100, update=100, message_id=None, reply=None
    ):
        msg = parsing_fixtures.message(
            text,
            command=text.lstrip().startswith("/"),
            from_user=User(id=actor, is_bot=False, first_name="Tester"),
            chat=Chat(id=chat, type="private" if chat > 0 else "supergroup"),
            message_id=message_id or update,
            reply_to_message=reply,
        )
        await self.dp.feed_update(self.bot, Update(update_id=update, message=msg))

    async def test_create_confirm_assign_and_member_call_end_to_end(self):
        await self.feed("/create dev <Development>")
        output = [
            call.text for call in self.session.calls if isinstance(call, SendMessage)
        ]
        token = re.search(r"/confirm ([A-Za-z0-9_-]+)", output[-1]).group(1)
        self.assertIn("&lt;Development&gt;", output[-1])
        await self.feed("/confirm " + token, update=101)
        target = parsing_fixtures.message(
            "Target text", from_user=User(id=2, is_bot=False, first_name="Target")
        )
        await self.feed("/assign dev", update=102, reply=target)
        await self.feed("@ActualBot @dev", actor=3, update=103, message_id=500)
        await self.feed("@ActualBot @dev", actor=3, update=103, message_id=500)
        await self.feed("@ActualBot @dev", actor=3, update=104, message_id=500)
        self.assertEqual(await self.store.pool.fetchval("SELECT count(*) FROM jobs"), 1)
        await self.worker_once("done")
        mentions = [
            call
            for call in self.session.calls
            if isinstance(call, SendMessage) and "tg://user?id=" in call.text
        ]
        self.assertEqual(len(mentions), 1)
        self.assertIn("id=2", mentions[0].text)
        self.assertFalse(self.service.failed.is_set())

    async def test_native_owner_cannot_create_without_product_grant(self):
        self.session.members[(-100, 3)] = ChatMemberOwner(
            user=User(id=3, is_bot=False, first_name="Owner"), is_anonymous=False
        )
        await self.feed("/create forbidden Title", actor=3)
        self.assertIsNone(await self.store.pool.fetchval("SELECT 1 FROM pending"))
        await self.feed("/create forbidden Title", actor=1, chat=-200, update=101)
        self.assertIsNone(await self.store.pool.fetchval("SELECT 1 FROM pending"))

    async def test_foreign_chats_other_bot_and_quoted_commands_do_not_touch_inbox(self):
        await self.feed("/roles", chat=-999)
        await self.feed("/assign@OtherBot frontend", update=101)
        await self.feed("An explanation @ActualBot @frontend", update=102)
        self.assertEqual(
            await self.store.pool.fetchval("SELECT count(*) FROM inbox"), 0
        )
        self.assertEqual(self.session.calls, [])

    async def test_two_initial_codes_reject_before_creating_a_job(self):
        await self.feed("@ActualBot @frontend @other")
        self.assertEqual(await self.store.pool.fetchval("SELECT count(*) FROM jobs"), 0)
        output = [
            call.text for call in self.session.calls if isinstance(call, SendMessage)
        ]
        self.assertTrue(output)
        self.assertFalse(any("tg://user?id=" in text for text in output))

    async def test_operator_leave_during_assignment_denies_save(self):
        await self.store.pool.execute("DELETE FROM assignments")

        async def hook(method):
            if isinstance(method, GetChatMember) and method.user_id == 2:
                await self.store.observe(-100, 1, "Operator", False, event=200)

        self.session.hook = hook
        target = parsing_fixtures.message(
            "Target", from_user=User(id=2, is_bot=False, first_name="Target")
        )
        await self.feed("/assign frontend", reply=target)
        self.assertEqual(
            await self.store.pool.fetchval("SELECT count(*) FROM assignments"), 0
        )
        self.assertFalse(self.service.failed.is_set())

    async def test_optout_applies_to_already_queued_call(self):
        await self.feed("@ActualBot @frontend")
        await self.feed("/optout", actor=2, update=101)
        await self.worker_once("done")
        output = [
            call.text for call in self.session.calls if isinstance(call, SendMessage)
        ]
        self.assertFalse(any("tg://user?id=" in text for text in output))

    async def test_profile_delete_through_dispatcher_does_not_fail_running_worker(self):
        await self.feed("@ActualBot @frontend")
        original = self.service.gateway.send
        waiting = asyncio.Event()
        release = asyncio.Event()

        async def gate(chat, text, *args, **kwargs):
            if callable(text):
                waiting.set()
                await release.wait()
            return await original(chat, text, *args, **kwargs)

        self.service.gateway.send = gate
        worker = asyncio.create_task(self.service.worker())
        try:
            await asyncio.wait_for(waiting.wait(), timeout=3)
            await self.feed("/delete_me confirm", actor=2, update=101)
            self.assertTrue(self.journal.healthy)
            self.assertFalse(self.service.failed.is_set())
            release.set()
            for _ in range(100):
                if await self.store.pool.fetchval("SELECT status FROM jobs") == "done":
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(
                await self.store.pool.fetchval("SELECT status FROM jobs"), "done"
            )
            output = [
                call.text
                for call in self.session.calls
                if isinstance(call, SendMessage)
            ]
            self.assertFalse(any("tg://user?id=" in text for text in output))
        finally:
            release.set()
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass

    async def test_left_member_can_erase_own_profile_in_private_chat(self):
        await self.store.observe(-100, 2, "Former member", False, event=99)
        await self.feed("/delete_me confirm", actor=2, chat=2, update=100)
        self.assertTrue(
            await self.store.pool.fetchval("SELECT blocked FROM people WHERE id=2")
        )
        self.assertEqual(
            await self.store.pool.fetchval(
                "SELECT count(*) FROM members WHERE person=2"
            ),
            0,
        )
        self.assertFalse(self.service.failed.is_set())

    async def test_unknown_private_user_cannot_write_inbox_or_get_chat_data(self):
        await self.feed("/roles", actor=77, chat=77)
        self.assertEqual(
            await self.store.pool.fetchval("SELECT count(*) FROM inbox"), 0
        )
        self.assertEqual(self.session.calls, [])
