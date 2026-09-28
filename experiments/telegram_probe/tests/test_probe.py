import unittest

from telegram_probe.core import Entity, Invocation, Person, Probe, mention, parse_probe


class Transport:
    def __init__(self, timeout_after_accept=False, on_send=None):
        self.calls = []
        self.timeout_after_accept = timeout_after_accept
        self.on_send = on_send

    async def send(self, **kwargs):
        self.calls.append(kwargs)
        if self.on_send:
            self.on_send(len(self.calls))
        if self.timeout_after_accept:
            raise TimeoutError("accepted then timed out")


async def member(_chat, _user):
    return True


def invocation(
    text="@ActualBot #probe", *, chat=-1, author=9, update=10, thread=7, forwarded=False
):
    return Invocation(
        update, chat, 44, thread, Person(author, "Caller"), text, forwarded
    )


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_aliases_and_forbidden_forms(self):
        for text in ("@ActualBot #probe", "@actualbot @probe details"):
            self.assertTrue(parse_probe(text, "ActualBot"))
        for text in (
            "#probe",
            "@OtherBot #probe",
            "hello @ActualBot #probe",
            "`@ActualBot #probe`",
        ):
            self.assertFalse(parse_probe(text, "ActualBot"))

    async def test_utf16_entities_and_code_are_handled(self):
        text = "@ActualBot #probe 😀"
        self.assertTrue(parse_probe(text, "ActualBot", (Entity("mention", 0, 10),)))
        prefixed = "😀 @ActualBot #probe"
        self.assertFalse(
            parse_probe(prefixed, "ActualBot", (Entity("mention", 3, 10),))
        )
        self.assertFalse(
            parse_probe("@ActualBot #probe", "ActualBot", (Entity("code", 0, 17),))
        )
        self.assertFalse(
            parse_probe(
                "@ActualBot #probe",
                "ActualBot",
                (Entity("mention", 0, 10), Entity("code", 11, 6)),
            )
        )

    async def test_chat_isolation_author_and_bot_exclusion_and_escaping(self):
        probe = Probe(frozenset({-1, -2}), frozenset({1}))
        probe.assign(-1, 1, Person(9, "Caller"))
        probe.assign(-1, 1, Person(2, "<Alice & Bob>"))
        probe.assign(-2, 1, Person(3, "Elsewhere"))
        transport = Transport()
        await probe.invoke(invocation(), "ActualBot", transport, member)
        self.assertEqual(len(transport.calls), 1)
        self.assertIn("tg://user?id=2", transport.calls[0]["text"])
        self.assertIn("&lt;Alice &amp; Bob&gt;", transport.calls[0]["text"])
        self.assertNotIn("Elsewhere", transport.calls[0]["text"])
        self.assertNotIn("Caller</a>", transport.calls[0]["text"])

    async def test_context_and_aliases_have_same_members(self):
        probe = Probe(frozenset({-1}), frozenset({1}))
        probe.assign(-1, 1, Person(2, "No Username"))
        transports = []
        for update, text in enumerate(("@ActualBot #probe", "@ActualBot @probe"), 1):
            transport = Transport()
            transports.append(transport)
            await probe.invoke(
                invocation(text, update=update), "ActualBot", transport, member
            )
        self.assertEqual(transports[0].calls[0], transports[1].calls[0])
        self.assertEqual(transports[0].calls[0]["reply_to"], 44)
        self.assertEqual(transports[0].calls[0]["thread_id"], 7)

    async def test_departure_between_chunks_and_return_does_not_restore(self):
        probe = Probe(frozenset({-1}), frozenset({1}), chunk_size=1)
        probe.assign(-1, 1, Person(2, "First"))
        probe.assign(-1, 1, Person(3, "Second"))
        transport = Transport(
            on_send=lambda count: probe.leave(-1, 3) if count == 1 else None
        )
        await probe.invoke(invocation(), "ActualBot", transport, member)
        self.assertEqual(len(transport.calls), 1)
        probe.return_to_chat(-1, 3)
        self.assertNotIn(3, probe.assignments[-1])

    async def test_timeout_is_uncertain_and_duplicate_update_is_not_resent(self):
        probe = Probe(frozenset({-1}), frozenset({1}))
        probe.assign(-1, 1, Person(2, "Member"))
        transport = Transport(timeout_after_accept=True)
        self.assertEqual(
            await probe.invoke(invocation(), "ActualBot", transport, member),
            "uncertain",
        )
        self.assertEqual(
            await probe.invoke(invocation(), "ActualBot", transport, member),
            "uncertain",
        )
        self.assertEqual(len(transport.calls), 1)

    async def test_forward_bot_unknown_chat_and_operator_are_rejected(self):
        probe = Probe(frozenset({-1}), frozenset({1}))
        with self.assertRaises(PermissionError):
            probe.assign(-2, 1, Person(2, "X"))
        with self.assertRaises(PermissionError):
            probe.assign(-1, 2, Person(2, "X"))
        transport = Transport()
        await probe.invoke(invocation(forwarded=True), "ActualBot", transport, member)
        await probe.invoke(
            invocation(chat=-2, update=11), "ActualBot", transport, member
        )
        self.assertEqual(transport.calls, [])

    def test_numeric_id_mention(self):
        self.assertEqual(
            mention(Person(123, "Имя 😀")), '<a href="tg://user?id=123">Имя 😀</a>'
        )


if __name__ == "__main__":
    unittest.main()
