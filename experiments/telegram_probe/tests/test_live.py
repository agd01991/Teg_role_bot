import io
import logging
import unittest
from unittest.mock import AsyncMock, patch

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import ClientDecodeError
from aiogram.methods import GetChatMember, SendMessage
from aiogram.types import (
    Chat,
    ChatMemberAdministrator,
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberRestricted,
    ChatMemberUpdated,
    Message,
    MessageEntity,
    Update,
    User,
)

from telegram_probe.config import Config
from telegram_probe.core import Person
from telegram_probe.live import (
    FrameworkLogFilter,
    assignment_command,
    build_router,
    is_actual_member,
    run,
)


class Session(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.closed = False
        self.members = {}

    async def close(self):
        self.closed = True

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, GetChatMember):
            return self.members.get(
                method.user_id,
                ChatMemberMember(
                    user=User(id=method.user_id, is_bot=False, first_name="U")
                ),
            )
        if isinstance(method, SendMessage):
            return Message(
                message_id=999,
                date=0,
                chat=Chat(id=method.chat_id, type="group", title="Test"),
                from_user=User(id=777, is_bot=True, first_name="Probe"),
                text=method.text,
            )
        raise AssertionError(type(method).__name__)

    async def stream_content(self, url, headers, timeout, chunk_size):
        if False:
            yield b""


def user(user_id, *, bot=False):
    return User(id=user_id, is_bot=bot, first_name=f"User {user_id}")


def restricted_member(user_id, is_member):
    return ChatMemberRestricted(
        user=user(user_id),
        is_member=is_member,
        can_send_messages=False,
        can_send_audios=False,
        can_send_documents=False,
        can_send_photos=False,
        can_send_videos=False,
        can_send_video_notes=False,
        can_send_voice_notes=False,
        can_send_polls=False,
        can_send_other_messages=False,
        can_add_web_page_previews=False,
        can_change_info=False,
        can_invite_users=False,
        can_pin_messages=False,
        can_manage_topics=False,
        until_date=0,
    )


def message(text, sender=1, *, reply=None, entities=None, forward=False, message_id=10):
    data = dict(
        message_id=message_id,
        date=0,
        chat=Chat(id=-100, type="supergroup", title="Test"),
        from_user=user(sender),
        text=text,
        entities=entities,
        reply_to_message=reply,
    )
    if forward:
        data["forward_origin"] = {
            "type": "user",
            "sender_user": user(8).model_dump(),
            "date": 0,
        }
    return Message(**data)


class LiveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = Session()
        self.bot = Bot("123456:TEST_TOKEN_MARKER", session=self.session)
        self.config = Config("unused", frozenset({-100}), frozenset({1}), 2, 0)
        me = user(777, bot=True).model_copy(update={"username": "ActualBot"})
        router, self.probe = build_router(self.bot, self.config, me)
        self.dp = Dispatcher()
        self.dp.include_router(router)
        self.session.members[1] = ChatMemberAdministrator(
            user=user(1),
            can_be_edited=False,
            is_anonymous=False,
            can_manage_chat=True,
            can_delete_messages=False,
            can_manage_video_chats=False,
            can_restrict_members=False,
            can_promote_members=False,
            can_change_info=False,
            can_invite_users=False,
            can_post_stories=False,
            can_edit_stories=False,
            can_delete_stories=False,
        )

    async def asyncTearDown(self):
        await self.bot.session.close()

    async def feed(self, msg, update_id=1):
        await self.dp.feed_update(self.bot, Update(update_id=update_id, message=msg))

    async def test_assignment_forms_and_other_bot(self):
        target = message("hello", sender=2, message_id=9)
        await self.feed(message("/probe_assign", reply=target), 1)
        self.assertIn(2, self.probe.assignments[-100])
        target2 = message("hello", sender=3, message_id=8)
        await self.feed(message("/probe_assign@ActualBot", reply=target2), 2)
        self.assertIn(3, self.probe.assignments[-100])
        target3 = message("hello", sender=4, message_id=7)
        await self.feed(message("/probe_assign@OtherBot", reply=target3), 3)
        self.assertNotIn(4, self.probe.assignments[-100])

    async def test_forwarded_command_and_target_are_rejected(self):
        target = message("hello", sender=2)
        await self.feed(message("/probe_assign", reply=target, forward=True), 1)
        self.assertNotIn(2, self.probe.assignments.get(-100, {}))
        forwarded_target = message("hello", sender=3, forward=True)
        await self.feed(message("/probe_assign", reply=forwarded_target), 2)
        self.assertNotIn(3, self.probe.assignments.get(-100, {}))

    async def test_real_entities_dispatch_invocation(self):
        self.probe.assign(-100, 1, Person(2, "Recipient"))
        text = "  @ActualBot #probe"
        entity = MessageEntity(type="mention", offset=2, length=10)
        await self.feed(message(text, entities=[entity]), 1)
        sent = [call for call in self.session.calls if isinstance(call, SendMessage)]
        self.assertEqual(len(sent), 1)
        self.assertIn("tg://user?id=2", sent[0].text)

    async def test_restricted_events_and_return_do_not_restore(self):
        self.probe.assign(-100, 1, Person(2, "Recipient"))
        common = dict(
            chat=Chat(id=-100, type="supergroup", title="Test"),
            from_user=user(1),
            date=0,
        )
        restricted = restricted_member(2, True)
        event = ChatMemberUpdated(
            **common,
            old_chat_member=ChatMemberMember(user=user(2)),
            new_chat_member=restricted,
        )
        await self.dp.feed_update(self.bot, Update(update_id=20, chat_member=event))
        self.assertIn(2, self.probe.assignments[-100])

        event = event.model_copy(
            update={
                "new_chat_member": restricted.model_copy(update={"is_member": False})
            }
        )
        await self.dp.feed_update(self.bot, Update(update_id=21, chat_member=event))
        self.assertNotIn(2, self.probe.assignments[-100])

        returned = ChatMemberUpdated(
            **common,
            old_chat_member=ChatMemberLeft(user=user(2)),
            new_chat_member=ChatMemberMember(user=user(2)),
        )
        await self.dp.feed_update(self.bot, Update(update_id=22, chat_member=returned))
        self.assertNotIn(2, self.probe.assignments[-100])


class MembershipAndSafetyTests(unittest.IsolatedAsyncioTestCase):
    def test_restricted_membership(self):
        yes = restricted_member(2, True)
        self.assertTrue(is_actual_member(yes))
        self.assertFalse(is_actual_member(yes.model_copy(update={"is_member": False})))

    def test_command_parser(self):
        self.assertTrue(assignment_command("/probe_assign", "ActualBot"))
        self.assertTrue(assignment_command("/probe_assign@actualbot", "ActualBot"))
        self.assertFalse(assignment_command("/probe_assign@OtherBot", "ActualBot"))

    def test_framework_exception_log_is_redacted(self):
        marker = "PRIVATE_MESSAGE_MARKER"
        token = "123456:SYNTHETIC_TOKEN"
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.addFilter(FrameworkLogFilter())
        logger = logging.getLogger("aiogram.dispatcher")
        logger.handlers[:] = [handler]
        logger.propagate = False
        try:
            raise ClientDecodeError("decode failed", ValueError("bad"), marker + token)
        except ClientDecodeError:
            logger.exception("update=%s", marker)
        output = stream.getvalue()
        self.assertIn("ClientDecodeError", output)
        self.assertNotIn(marker, output)
        self.assertNotIn(token, output)

    async def test_session_closes_on_preflight_failure(self):
        fake_bot = AsyncMock()
        fake_bot.get_me.side_effect = RuntimeError("preflight")
        fake_bot.session.close = AsyncMock()
        config = Config("unused", frozenset(), frozenset(), 2, 0)
        with (
            patch("telegram_probe.config.Config.from_env", return_value=config),
            patch("aiogram.Bot", return_value=fake_bot),
        ):
            with self.assertRaises(RuntimeError):
                await run(False)
        fake_bot.session.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
