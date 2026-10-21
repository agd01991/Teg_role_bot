import logging
import os
import unittest
from unittest.mock import patch

from aiogram.types import Chat, Message, MessageEntity, User

from rolebot.__main__ import SafeLogs
from rolebot.config import Config
from rolebot.parsing import code, parse


def message(text, *, command=False, entities=None, **changes):
    first = text.lstrip().split()[0]
    prefix = text[: len(text) - len(text.lstrip())]
    data = {
        "message_id": 1,
        "date": 0,
        "chat": Chat(id=-100, type="supergroup"),
        "from_user": User(id=1, is_bot=False, first_name="User"),
        "text": text,
        "entities": (
            entities
            if entities is not None
            else [
                MessageEntity(
                    type="bot_command" if command else "mention",
                    offset=len(prefix.encode("utf-16-le")) // 2,
                    length=len(first.encode("utf-16-le")) // 2,
                )
            ]
        ),
    }
    data.update(changes)
    return Message(**data)


class ParsingTests(unittest.TestCase):
    def test_codes(self):
        for original, expected in [("Frontend", "frontend"), ("свЕТ_01", "свет_01")]:
            self.assertEqual(code(original), expected)
        for original in [
            "all",
            "everyone",
            "here",
            "a",
            "x" * 33,
            "frоntend",
            "ＡＢ",
            "a b",
            "9abc",
            "ab\u200b",
            "абc",
            "optout",
            "admin",
        ]:
            with self.subTest(original=original), self.assertRaises(ValueError):
                code(original)

    def test_single_command_and_explicit_explanation_boundary(self):
        for text in [
            "@ActualBot @frontend",
            "  @actualbot @Frontend Please @alice",
            "@ActualBot @frontend -- @alice please",
            "@ActualBot @frontend Пояснение @другая",
        ]:
            with self.subTest(text=text):
                self.assertEqual(parse(message(text), "ActualBot").argument, "frontend")
        for text in [
            "@ActualBot @frontend @alice please",
            "@ActualBot @frontend,@other",
            "@ActualBot @frontend @frontend",
            "@ActualBot @frontend, @other",
            "@ActualBot #frontend",
        ]:
            with self.subTest(text=text):
                self.assertEqual(parse(message(text), "ActualBot").kind, "invalid_call")

    def test_wrong_bot_and_bare_code_ignored(self):
        for text in ["@OtherBot @frontend", "@frontend", "Text @ActualBot @frontend"]:
            self.assertIsNone(parse(message(text), "ActualBot"))
        self.assertIsNone(
            parse(message("/assign@OtherBot frontend", command=True), "ActualBot")
        )

    def test_entities_required_and_utf16_offsets(self):
        self.assertIsNone(
            parse(message("@ActualBot @frontend", entities=[]), "ActualBot")
        )
        self.assertIsNone(
            parse(
                message(
                    "@ActualBot @frontend",
                    entities=[MessageEntity(type="mention", offset=1, length=10)],
                ),
                "ActualBot",
            )
        )
        for kind in ["code", "pre", "blockquote", "expandable_blockquote"]:
            entities = [
                MessageEntity(type="mention", offset=0, length=10),
                MessageEntity(type=kind, offset=11, length=9),
            ]
            self.assertIsNone(
                parse(message("@ActualBot @frontend", entities=entities), "ActualBot")
            )
        self.assertEqual(
            parse(message("\u2003@ActualBot @frontend 👍 @alice"), "ActualBot").kind,
            "invoke",
        )

    def test_forward_bot_and_anonymous_sender_ignored(self):
        cases = [
            {"from_user": User(id=8, is_bot=True, first_name="Bot")},
            {"sender_chat": Chat(id=-123, type="channel")},
            {
                "forward_origin": {
                    "type": "hidden_user",
                    "date": 0,
                    "sender_user_name": "Hidden",
                }
            },
        ]
        for case in cases:
            self.assertIsNone(
                parse(message("@ActualBot @frontend", **case), "ActualBot")
            )

    def test_slash_entity_covers_only_command(self):
        self.assertEqual(
            parse(
                message("/assign@ActualBot frontend", command=True), "ActualBot"
            ).kind,
            "assign",
        )
        self.assertIsNone(
            parse(message("/assign frontend", command=True, entities=[]), "ActualBot")
        )
        self.assertIsNone(
            parse(
                message(
                    "/assign frontend",
                    command=True,
                    entities=[MessageEntity(type="bot_command", offset=0, length=16)],
                ),
                "ActualBot",
            )
        )

    def test_scope_configuration_is_explicit_and_chat_specific(self):
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN": "fixture",
                "ALPHA_CHAT_ADMINS": "-100:1,2;-200:3",
                "ALPHA_DATABASE_URL": "postgresql://fixture",
            },
            clear=True,
        ):
            self.assertEqual(
                Config.load().admins, {-100: frozenset({1, 2}), -200: frozenset({3})}
            )
        for scope in ["", "123:1", "-100:0", "-100:1;-100:2", "-100:abc"]:
            with patch.dict(
                os.environ,
                {"TELEGRAM_BOT_TOKEN": "fixture", "ALPHA_CHAT_ADMINS": scope},
                clear=True,
            ), self.assertRaises(ValueError):
                Config.load()

    def test_framework_log_cannot_render_secret_or_traceback(self):
        marker = "FIXTURE-SENSITIVE-RESPONSE"
        exception = RuntimeError(marker)
        record = logging.LogRecord(
            "aiogram.dispatcher",
            logging.ERROR,
            "",
            0,
            "%s",
            (exception,),
            (RuntimeError, exception, None),
        )
        record.stack_info = marker
        SafeLogs().filter(record)
        self.assertNotIn(marker, logging.Formatter().format(record))
        self.assertIsNone(record.stack_info)
