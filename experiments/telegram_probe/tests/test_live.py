import asyncio
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
    ChatMemberOwner,
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
        self.request_hook = None

    async def close(self):
        self.closed = True

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if self.request_hook:
            await self.request_hook(method)
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


def parsed_member(data):
    """Build a member as aiogram does after a JSON Bot API response."""
    from aiogram.types import ChatMemberUnion
    from pydantic import TypeAdapter

    return TypeAdapter(ChatMemberUnion).validate_python(data)


def parsed_administrator(user_id):
    return parsed_member(
        {
            "status": "administrator",
            "user": user(user_id).model_dump(),
            "can_be_edited": False,
            "is_anonymous": False,
            "can_manage_chat": True,
            "can_delete_messages": False,
            "can_manage_video_chats": False,
            "can_restrict_members": False,
            "can_promote_members": False,
            "can_change_info": False,
            "can_invite_users": False,
            "can_post_stories": False,
            "can_edit_stories": False,
            "can_delete_stories": False,
        }
    )


def message(
    text,
    sender=1,
    *,
    reply=None,
    entities=None,
    forward=False,
    message_id=10,
    sender_is_bot=False,
):
    data = dict(
        message_id=message_id,
        date=0,
        chat=Chat(id=-100, type="supergroup", title="Test"),
        from_user=user(sender, bot=sender_is_bot),
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


def membership_event(user_id, old_member, new_member, *, chat_id=-100):
    return ChatMemberUpdated(
        chat=Chat(id=chat_id, type="supergroup", title="Test"),
        from_user=user(1),
        date=0,
        old_chat_member=old_member,
        new_chat_member=new_member,
    )


class LiveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = Session()
        self.bot = Bot("123456:TEST_TOKEN_MARKER", session=self.session)
        self.config = Config("unused", frozenset({-100}), frozenset({1}), 2, 0)
        me = user(777, bot=True).model_copy(update={"username": "ActualBot"})
        router, self.probe = build_router(self.bot, self.config, me)
        self.dp = Dispatcher()
        self.dp.include_router(router)
        self.session.members[1] = parsed_administrator(1)

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

    async def test_owner_and_administrator_from_json_can_assign(self):
        target = message("hello", sender=2, message_id=9)
        await self.feed(message("/probe_assign", reply=target), 1)
        self.assertIn(2, self.probe.assignments[-100])

        self.session.members[1] = parsed_member(
            {
                "status": "creator",
                "user": user(1).model_dump(),
                "is_anonymous": False,
            }
        )
        target = message("hello", sender=3, message_id=8)
        await self.feed(message("/probe_assign", reply=target), 2)
        self.assertIn(3, self.probe.assignments[-100])

    async def test_regular_member_cannot_assign(self):
        self.session.members[1] = parsed_member(
            {"status": "member", "user": user(1).model_dump()}
        )
        await self.feed(message("/probe_assign", reply=message("hello", sender=2)), 1)
        self.assertNotIn(2, self.probe.assignments.get(-100, {}))

    async def test_absent_or_unverified_target_cannot_be_assigned(self):
        target = message("hello", sender=2)
        self.session.members[2] = parsed_member(
            {"status": "left", "user": user(2).model_dump()}
        )
        await self.feed(message("/probe_assign", reply=target), 1)
        self.assertNotIn(2, self.probe.assignments.get(-100, {}))

        self.session.members[2] = RuntimeError("PRIVATE_MESSAGE_MARKER")
        original = self.session.make_request

        async def failing(bot, method, timeout=None):
            if isinstance(method, GetChatMember) and method.user_id == 2:
                raise self.session.members[2]
            return await original(bot, method, timeout)

        self.session.make_request = failing
        await self.feed(message("/probe_assign", reply=target), 2)
        self.assertNotIn(2, self.probe.assignments.get(-100, {}))

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

    async def test_bot_message_via_dispatcher_does_not_invoke_role(self):
        self.probe.assign(-100, 1, Person(2, "Recipient"))
        text = "@ActualBot #probe"
        entity = MessageEntity(type="mention", offset=0, length=10)
        await self.feed(
            message(text, sender=8, sender_is_bot=True, entities=[entity]), 10
        )
        self.assertFalse(
            any(
                isinstance(call, (GetChatMember, SendMessage))
                for call in self.session.calls
            )
        )
        self.assertIn(2, self.probe.assignments[-100])

    async def test_operator_loses_rights_while_target_check_waits(self):
        waiting, release = asyncio.Event(), asyncio.Event()

        async def hook(method):
            if isinstance(method, GetChatMember) and method.user_id == 2:
                waiting.set()
                await release.wait()

        self.session.request_hook = hook
        task = asyncio.create_task(
            self.feed(message("/probe_assign", reply=message("hello", sender=2)))
        )
        await waiting.wait()
        self.session.members[1] = parsed_member(
            {"status": "member", "user": user(1).model_dump()}
        )
        release.set()
        await task
        self.assertNotIn(2, self.probe.assignments.get(-100, {}))
        sent = [
            call.text for call in self.session.calls if isinstance(call, SendMessage)
        ]
        self.assertFalse(any("сохранено" in text for text in sent))

    async def test_final_operator_check_error_is_safely_denied(self):
        operator_checks = 0

        async def hook(method):
            nonlocal operator_checks
            if isinstance(method, GetChatMember) and method.user_id == 1:
                operator_checks += 1
                if operator_checks == 2:
                    raise RuntimeError("PRIVATE_MESSAGE_TOKEN_MARKER")

        self.session.request_hook = hook
        with self.assertLogs(level="ERROR") as captured:
            await self.feed(message("/probe_assign", reply=message("hello", sender=2)))
        self.assertNotIn(2, self.probe.assignments.get(-100, {}))
        diagnostic = "\n".join(captured.output)
        self.assertIn("final_operator_check_failed", diagnostic)
        self.assertIn("error_type=RuntimeError", diagnostic)
        self.assertNotIn("PRIVATE_MESSAGE_TOKEN_MARKER", diagnostic)
        sent = [
            call.text for call in self.session.calls if isinstance(call, SendMessage)
        ]
        self.assertFalse(any("сохранено" in text for text in sent))

    async def _assign_while_final_check_waits(self, events):
        waiting, release = asyncio.Event(), asyncio.Event()
        operator_checks = 0

        async def hook(method):
            nonlocal operator_checks
            if isinstance(method, GetChatMember) and method.user_id == 1:
                operator_checks += 1
                if operator_checks == 2:
                    waiting.set()
                    await release.wait()

        self.session.request_hook = hook
        task = asyncio.create_task(
            self.feed(message("/probe_assign", reply=message("hello", sender=2)))
        )
        await waiting.wait()
        for update_id, event in enumerate(events, 100):
            await self.dp.feed_update(
                self.bot, Update(update_id=update_id, chat_member=event)
            )
        release.set()
        await task
        return [
            call.text for call in self.session.calls if isinstance(call, SendMessage)
        ]

    async def test_target_leaves_during_final_operator_check(self):
        left = membership_event(
            2, ChatMemberMember(user=user(2)), ChatMemberLeft(user=user(2))
        )
        sent = await self._assign_while_final_check_waits([left])
        self.assertNotIn(2, self.probe.assignments.get(-100, {}))
        self.assertFalse(any("сохранено" in text for text in sent))

    async def test_target_leaves_and_returns_during_final_operator_check(self):
        left = membership_event(
            2, ChatMemberMember(user=user(2)), ChatMemberLeft(user=user(2))
        )
        returned = membership_event(
            2, ChatMemberLeft(user=user(2)), ChatMemberMember(user=user(2))
        )
        sent = await self._assign_while_final_check_waits([left, returned])
        self.assertNotIn(2, self.probe.assignments.get(-100, {}))
        self.assertFalse(any("сохранено" in text for text in sent))

    async def test_unrelated_membership_event_does_not_block_assignment(self):
        unrelated = membership_event(
            3, ChatMemberMember(user=user(3)), ChatMemberLeft(user=user(3))
        )
        other_chat = membership_event(
            2,
            ChatMemberMember(user=user(2)),
            ChatMemberLeft(user=user(2)),
            chat_id=-200,
        )
        sent = await self._assign_while_final_check_waits([unrelated, other_chat])
        self.assertIn(2, self.probe.assignments[-100])
        self.assertTrue(any("сохранено" in text for text in sent))

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

    async def test_restricted_absent_to_present_drops_old_assignment(self):
        self.probe.assign(-100, 1, Person(2, "Recipient"))
        event = ChatMemberUpdated(
            chat=Chat(id=-100, type="supergroup", title="Test"),
            from_user=user(1),
            date=0,
            old_chat_member=restricted_member(2, False),
            new_chat_member=restricted_member(2, True),
        )
        with self.assertLogs(level="INFO") as captured:
            await self.dp.feed_update(self.bot, Update(update_id=23, chat_member=event))
        self.assertNotIn(2, self.probe.assignments[-100])
        self.assertTrue(
            any("action=assignment_not_restored" in line for line in captured.output)
        )

    async def test_foreign_chat_membership_event_is_ignored(self):
        event = ChatMemberUpdated(
            chat=Chat(id=-200, type="supergroup", title="Other"),
            from_user=user(1),
            date=0,
            old_chat_member=ChatMemberMember(user=user(2)),
            new_chat_member=ChatMemberLeft(user=user(2)),
        )
        await self.dp.feed_update(self.bot, Update(update_id=24, chat_member=event))
        self.assertNotIn(-200, self.probe.assignments)
        self.assertNotIn(-200, self.probe.departed)


class MembershipAndSafetyTests(unittest.IsolatedAsyncioTestCase):
    def test_restricted_membership(self):
        yes = restricted_member(2, True)
        self.assertTrue(is_actual_member(yes))
        self.assertFalse(is_actual_member(yes.model_copy(update={"is_member": False})))

    def test_string_and_enum_membership_statuses(self):
        members = [
            ChatMemberOwner(user=user(2), is_anonymous=False),
            ChatMemberAdministrator(
                user=user(2),
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
            ),
            ChatMemberMember(user=user(2)),
            parsed_member({"status": "member", "user": user(2).model_dump()}),
        ]
        self.assertTrue(all(is_actual_member(member) for member in members))
        for status in ("left", "kicked", "future_status"):
            member = type("Member", (), {"status": status})()
            self.assertFalse(is_actual_member(member))

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
        old_handlers, old_level, old_propagate = (
            logger.handlers[:],
            logger.level,
            logger.propagate,
        )
        logger.handlers[:] = [handler]
        logger.setLevel(logging.INFO)
        logger.propagate = False
        try:
            try:
                raise ClientDecodeError(
                    "decode failed", ValueError("bad"), marker + token
                )
            except ClientDecodeError:
                logger.exception("update=%s", marker)
        finally:
            logger.handlers[:] = old_handlers
            logger.setLevel(old_level)
            logger.propagate = old_propagate
        output = stream.getvalue()
        self.assertIn("ClientDecodeError", output)
        self.assertNotIn(marker, output)
        self.assertNotIn(token, output)

    def test_framework_info_and_error_without_exc_info_are_safe(self):
        marker = "PRIVATE_MESSAGE_MARKER"
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.addFilter(FrameworkLogFilter())
        logger = logging.getLogger("aiogram.dispatcher")
        old_handlers, old_level, old_propagate = (
            logger.handlers[:],
            logger.level,
            logger.propagate,
        )
        logger.handlers[:] = [handler]
        logger.setLevel(logging.INFO)
        logger.propagate = False
        try:
            logger.info("Start polling %s", marker)
            logger.error("Polling failed: %s", RuntimeError(marker))
        finally:
            logger.handlers[:] = old_handlers
            logger.setLevel(old_level)
            logger.propagate = old_propagate
        output = stream.getvalue()
        self.assertIn("aiogram event info", output)
        self.assertIn("error_type=RuntimeError", output)
        self.assertNotIn("event failed error_type=none", output)
        self.assertNotIn(marker, output)

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
