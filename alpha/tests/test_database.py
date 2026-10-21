import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import asyncpg
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramNetworkError,
)
from aiogram.methods import SendMessage
from aiogram.types import ChatMemberMember, User

from rolebot.config import Config
from rolebot.privacy import Journal
from rolebot.service import Gateway, Service
from rolebot.store import Denied, Store

URL = os.environ.get("ALPHA_TEST_DATABASE_URL")


@unittest.skipUnless(URL, "real PostgreSQL integration URL required")
class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if not urlsplit(URL).path.endswith("_tests"):
            raise RuntimeError("refusing non-test database")
        self.directory = tempfile.TemporaryDirectory()
        self.store = await Store.open(URL)
        await self.store.pool.execute("DROP SCHEMA IF EXISTS rolebot CASCADE")
        await self.store.migrate()
        self.admins = {-100: frozenset({1}), -200: frozenset({3})}
        await self.store.initialize(999, self.admins)
        self.journal = Journal(self.store, Path(self.directory.name))
        await self.journal.start()
        for chat, ids in [(-100, [1, 2, 3, 4]), (-200, [1, 2, 3, 4])]:
            for person in ids:
                await self.store.observe(chat, person, f"User {person}", True)
        self.bot = SimpleNamespace(
            get_chat_member=AsyncMock(side_effect=self.member),
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=99)),
        )
        self.service = Service(
            Config("fixture", self.admins, URL, Path(self.directory.name)),
            self.store,
            self.journal,
            Gateway(self.bot, interval=0),
            "ActualBot",
        )
        self.role = await self.create(-100, 1, "frontend")
        await self.store.assign(-100, 1, 1, "frontend", 2, 1)

    async def asyncTearDown(self):
        await self.store.pool.close()
        self.directory.cleanup()

    @staticmethod
    async def member(chat, person):
        return ChatMemberMember(
            user=User(id=person, is_bot=False, first_name=f"<User & {person}>")
        )

    async def create(self, chat, author, name):
        revision = await self.store.revision(chat, author)
        token = await self.store.stage(chat, author, revision, name, "<Role & title>")
        await self.store.confirm(chat, author, revision, token)
        return await self.store.pool.fetchval(
            "SELECT id FROM roles WHERE chat=$1 AND code=$2", chat, name
        )

    async def job(self, author=1, update_id=1):
        return await self.store.enqueue(
            -100,
            author,
            await self.store.revision(-100, author),
            "frontend",
            update_id,
            11,
            7,
        )

    async def worker_once(self, expected_status):
        task = asyncio.create_task(self.service.worker())
        try:
            for _ in range(200):
                status = await self.store.pool.fetchval(
                    "SELECT status FROM jobs ORDER BY id LIMIT 1"
                )
                if status == expected_status:
                    return
                if task.done():
                    await task
                await asyncio.sleep(0.01)
            self.fail(f"worker did not reach {expected_status}")
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def test_schema_is_idempotent_and_identity_bound_to_bot(self):
        await self.store.migrate()
        with self.assertRaises(Denied):
            await self.store.initialize(888, self.admins)

    async def test_database_constraints_prevent_cross_chat_assignment(self):
        with self.assertRaises(asyncpg.ForeignKeyViolationError):
            await self.store.pool.execute(
                "INSERT INTO assignments VALUES(-200,$1,2)", self.role
            )
        self.assertEqual(len(await self.store.roles(-200)), 0)

    async def test_confirmation_is_bound_to_chat_author_ttl_and_single_use(self):
        token = await self.store.stage(-100, 1, 1, "sound", "Sound")
        with self.assertRaises(Denied):
            await self.store.confirm(-100, 2, 1, token)
        with self.assertRaises(Denied):
            await self.store.confirm(-200, 1, 1, token)
        await self.store.confirm(-100, 1, 1, token)
        with self.assertRaises(Denied):
            await self.store.confirm(-100, 1, 1, token)
        token = await self.store.stage(-100, 1, 1, "expired", "Expired")
        await self.store.pool.execute(
            "UPDATE pending SET expires=now()-interval '1 second'"
        )
        with self.assertRaises(Denied):
            await self.store.confirm(-100, 1, 1, token)

    async def test_regular_member_can_invoke_without_product_admin_permission(self):
        await self.job(author=3)
        await self.worker_once("done")
        self.bot.send_message.assert_awaited_once()
        with self.assertRaises(Denied):
            await self.service.operator(-100, 3)

    async def test_product_permission_does_not_depend_on_native_admin_status(self):
        self.assertEqual(await self.service.operator(-100, 1), 1)
        with self.assertRaises(Denied):
            await self.service.operator(-200, 1)

    async def test_leave_and_return_do_not_restore_assignments_or_admin_grant(self):
        await self.store.observe(-100, 2, "Target", False, event=50)
        await self.store.observe(-100, 2, "Target", True, event=51)
        self.assertEqual(
            await self.store.pool.fetchval(
                "SELECT count(*) FROM assignments WHERE chat=-100 AND person=2"
            ),
            0,
        )
        await self.store.observe(-100, 1, "Admin", False, event=52)
        await self.store.observe(-100, 1, "Admin", True, event=53)
        with self.assertRaises(Denied):
            await self.service.operator(-100, 1)

    async def test_stale_api_success_cannot_overwrite_leave_event(self):
        revision = await self.store.revision(-100, 2)
        await self.store.observe(-100, 2, "Target", False, event=60)
        with self.assertRaises(Denied):
            await self.store.observe(-100, 2, "Old target", True, expected=revision)
        self.assertFalse(
            await self.store.pool.fetchval(
                "SELECT active FROM members WHERE chat=-100 AND person=2"
            )
        )

    async def test_old_membership_event_cannot_overwrite_newer_event(self):
        await self.store.observe(-100, 2, "Target", False, event=100)
        await self.store.observe(-100, 2, "Target", True, event=99)
        self.assertFalse(
            await self.store.pool.fetchval(
                "SELECT active FROM members WHERE chat=-100 AND person=2"
            )
        )

    async def test_assignments_idempotent_and_optout_survives_assignment(self):
        await self.store.set_opt_out(-100, 2, True)
        for _ in range(2):
            await self.store.assign(-100, 1, 1, "frontend", 2, 1)
        self.assertEqual(
            await self.store.pool.fetchval("SELECT count(*) FROM assignments"), 1
        )
        with self.assertRaises(Denied):
            await self.job()
        self.assertFalse(
            await self.store.pool.fetchval(
                "SELECT opt_out FROM members WHERE chat=-200 AND person=2"
            )
        )

    async def test_empty_role_remains_and_unknown_assignment_does_not_create_role(self):
        await self.store.assign(-100, 1, 1, "frontend", 2, None, remove=True)
        await self.store.assign(-100, 1, 1, "frontend", 2, None, remove=True)
        self.assertEqual((await self.store.roles(-100))[0]["count"], 0)
        with self.assertRaises(Denied):
            await self.store.assign(-100, 1, 1, "typo", 2, 1)

    async def test_inbox_deduplicates_across_restarts(self):
        self.assertTrue(await self.store.claim_update(10))
        self.assertFalse(await self.store.claim_update(10))
        await self.store.initialize(999, self.admins)
        self.assertFalse(await self.store.claim_update(10))

    async def test_parallel_invocations_cannot_bypass_rate_limit(self):
        results = await asyncio.gather(
            self.job(update_id=20), self.job(update_id=21), return_exceptions=True
        )
        self.assertEqual(sum(isinstance(result, Denied) for result in results), 1)
        self.assertEqual(await self.store.pool.fetchval("SELECT count(*) FROM jobs"), 1)

    async def test_role_member_limit_has_no_silent_truncation(self):
        for person in range(10, 29):
            await self.store.observe(-100, person, "Member", True)
            await self.store.assign(-100, 1, 1, "frontend", person, 1)
        await self.store.observe(-100, 30, "Extra", True)
        with self.assertRaises(Denied):
            await self.store.assign(-100, 1, 1, "frontend", 30, 1)
        self.assertEqual(
            await self.store.pool.fetchval("SELECT count(*) FROM assignments"), 20
        )

    async def test_worker_preserves_topic_reply_and_escapes_names(self):
        await self.job()
        await self.worker_once("done")
        payload = self.bot.send_message.call_args.kwargs
        self.assertEqual(payload["message_thread_id"], 7)
        self.assertEqual(payload["reply_parameters"].message_id, 11)
        self.assertFalse(payload["reply_parameters"].allow_sending_without_reply)
        self.assertIn("&lt;User &amp; 2&gt;", payload["text"])
        self.assertIn("tg://user?id=2", payload["text"])
        self.assertLess(len(payload["text"]), 3500)

    async def test_unknown_membership_never_reaches_send(self):
        original = self.bot.get_chat_member.side_effect

        async def check(chat, person):
            if person == 2:
                raise RuntimeError("FIXTURE-SENSITIVE")
            return await original(chat, person)

        self.bot.get_chat_member.side_effect = check
        await self.job()
        await self.worker_once("done")
        self.bot.send_message.assert_not_called()

    async def test_optout_during_limiter_wait_is_rechecked(self):
        original_send = self.service.gateway.send

        async def delayed(chat, text, *args, **kwargs):
            await self.store.set_opt_out(-100, 2, True)
            return await original_send(chat, text, *args, **kwargs)

        self.service.gateway.send = delayed
        await self.job()
        await self.worker_once("done")
        self.bot.send_message.assert_not_called()

    async def test_new_assignment_not_added_to_queued_snapshot(self):
        await self.job()
        await self.store.assign(-100, 1, 1, "frontend", 4, 1)
        await self.worker_once("done")
        self.assertNotIn("id=4", self.bot.send_message.call_args.kwargs["text"])

    async def test_network_timeout_is_uncertain_and_blocks_chat(self):
        self.bot.send_message.side_effect = TelegramNetworkError(
            method=SendMessage(chat_id=-100, text="fixture"),
            message="FIXTURE-SENSITIVE",
        )
        await self.job()
        await self.worker_once("uncertain")
        await self.store.initialize(999, self.admins)
        self.assertIsNone(await self.store.next_part())
        with self.assertRaises(Denied):
            await self.job(author=3, update_id=2)
        self.bot.send_message.assert_awaited_once()

    async def test_crash_after_claim_is_recovered_as_uncertain_not_resent(self):
        await self.job()
        part = await self.store.next_part()
        await self.store.prepare_part(part)
        await self.store.initialize(999, self.admins)
        self.assertEqual(
            await self.store.pool.fetchval("SELECT status FROM jobs"), "uncertain"
        )
        self.assertIsNone(await self.store.next_part())

    async def test_stop_cannot_hide_inflight_uncertainty(self):
        await self.job()
        part = await self.store.next_part()
        await self.store.prepare_part(part)
        await self.store.stop(-100, 1, False)
        await self.store.finish(part, "uncertain")
        self.assertEqual(
            await self.store.pool.fetchval("SELECT status FROM jobs"), "uncertain"
        )

    async def test_retry_after_is_persisted_and_maximum_attempts_bounded(self):
        await self.job()
        part = await self.store.next_part()
        await self.store.prepare_part(part)
        await self.store.finish(part, "pending", retry_after=60)
        self.assertIsNone(await self.store.next_part())
        self.assertEqual(
            await self.store.pool.fetchval("SELECT attempts FROM parts"), 1
        )

    async def test_closed_topic_error_has_no_fallback_send(self):
        self.bot.send_message.side_effect = TelegramBadRequest(
            method=SendMessage(chat_id=-100, text="fixture"), message="topic closed"
        )
        await self.job()
        await self.worker_once("failed")
        self.bot.send_message.assert_awaited_once()
        self.assertEqual(self.bot.send_message.call_args.kwargs["message_thread_id"], 7)

    async def test_expired_queue_never_sends(self):
        await self.job()
        await self.store.pool.execute(
            "UPDATE jobs SET created=now()-interval '6 minutes'"
        )
        self.assertIsNone(await self.store.next_part())
        self.assertEqual(
            await self.store.pool.fetchval("SELECT status FROM jobs"), "cancelled"
        )

    async def test_ordinary_user_can_stop_only_own_jobs(self):
        await self.job()
        await self.store.stop(-100, 3, False)
        self.assertEqual(
            await self.store.pool.fetchval("SELECT status FROM jobs"), "pending"
        )
        self.assertEqual(await self.store.jobs(-100, 3, False), [])
        await self.store.stop(-100, 1, False)
        self.assertEqual(
            await self.store.pool.fetchval("SELECT status FROM jobs"), "cancelled"
        )

    async def test_global_profile_deletion_erases_all_chats_and_preserves_other_user(
        self,
    ):
        await self.create(-200, 3, "sound")
        await self.store.assign(-200, 3, 1, "sound", 2, 1)
        await self.job()
        await self.journal.record(2, "delete")
        self.assertEqual(
            await self.store.pool.fetchval(
                "SELECT count(*) FROM members WHERE person=2"
            ),
            0,
        )
        self.assertEqual(
            await self.store.pool.fetchval(
                "SELECT count(*) FROM assignments WHERE person=2"
            ),
            0,
        )
        self.assertTrue(
            await self.store.pool.fetchval("SELECT blocked FROM people WHERE id=2")
        )
        self.assertEqual(
            await self.store.pool.fetchval(
                "SELECT count(*) FROM members WHERE person=3"
            ),
            2,
        )
        self.assertEqual(
            await self.store.pool.fetchval("SELECT recipients FROM parts"), []
        )

    async def test_deletion_journal_replays_after_restoring_older_database(self):
        await self.journal.record(2, "delete")
        # Emulate a pre-deletion snapshot of profiles and applied journal position.
        await self.store.pool.execute(
            "UPDATE meta SET ledger_seq=0,ledger_hash=''; UPDATE people SET blocked=false,ever_deleted=false WHERE id=2"
        )
        await self.store.observe(-100, 2, "Restored profile", True)
        await self.store.observe(-200, 2, "Restored second profile", True)
        recovered = Journal(self.store, Path(self.directory.name))
        await recovered.start()
        self.assertTrue(recovered.healthy)
        self.assertEqual(
            await self.store.pool.fetchval(
                "SELECT count(*) FROM members WHERE person=2"
            ),
            0,
        )

    async def test_missing_or_truncated_journal_blocks_startup(self):
        await self.journal.record(2, "delete")
        original = self.journal.path.read_bytes()
        self.journal.path.write_bytes(original[:-1])
        with self.assertRaises(RuntimeError):
            await Journal(self.store, Path(self.directory.name)).start()
        self.journal.path.unlink()
        with self.assertRaises(RuntimeError):
            await Journal(self.store, Path(self.directory.name)).start()

    async def test_deleted_profile_requires_explicit_consent_and_new_assignments(self):
        await self.journal.record(2, "delete")
        for chat in (-100, -200):
            with self.assertRaises(Denied):
                await self.store.observe(chat, 2, "Must not be stored", True)
        self.assertEqual(
            await self.store.pool.fetchval(
                "SELECT count(*) FROM members WHERE person=2"
            ),
            0,
        )
        with self.assertRaises(Denied):
            await self.store.assign(-100, 1, 1, "frontend", 2, 1)
        await self.journal.record(2, "allow")
        await self.store.observe(-100, 2, "Explicitly returned", True)
        self.assertEqual(
            await self.store.pool.fetchval(
                "SELECT count(*) FROM assignments WHERE person=2"
            ),
            0,
        )
        self.assertTrue(
            await self.store.pool.fetchval(
                "SELECT admin_revoked FROM members WHERE chat=-100 AND person=2"
            )
        )

    async def test_duplicate_privacy_request_does_not_grow_journal(self):
        await self.journal.record(2, "delete")
        await self.journal.record(2, "delete")
        self.assertEqual(len(self.journal.entries), 1)

    async def test_postgres_runtime_user_has_no_superuser_or_database_creation(self):
        row = await self.store.pool.fetchrow(
            "SELECT rolsuper,rolcreatedb,rolcreaterole FROM pg_roles WHERE rolname=current_user"
        )
        self.assertEqual(tuple(row), (False, False, False))

    async def test_leave_and_rejoin_does_not_revive_authors_queued_call(self):
        await self.job()
        await self.store.observe(-100, 1, "Operator", False, event=100)
        await self.store.observe(-100, 1, "Operator", True, event=101)
        self.assertIsNone(await self.store.next_part())
        self.assertEqual(
            await self.store.pool.fetchval("SELECT status FROM jobs"), "cancelled"
        )

    async def test_reassignment_after_rejoin_does_not_reuse_old_recipient_snapshot(
        self,
    ):
        await self.job()
        await self.store.observe(-100, 2, "Target", False, event=100)
        await self.store.observe(-100, 2, "Target", True, event=101)
        revision = await self.store.revision(-100, 2)
        await self.store.assign(-100, 1, 1, "frontend", 2, revision)
        await self.worker_once("done")
        self.bot.send_message.assert_not_called()

    async def test_global_erase_removes_recipient_epoch_metadata(self):
        await self.job()
        await self.journal.record(2, "delete")
        self.assertEqual(
            await self.store.pool.fetchval("SELECT epochs::text FROM parts"), "{}"
        )

    async def test_restart_cancels_unstarted_parts_and_retains_confirmed_part(self):
        for person in range(10, 15):
            await self.store.observe(-100, person, "Member", True)
            await self.store.assign(-100, 1, 1, "frontend", person, 1)
        await self.job()
        part = await self.store.next_part()
        await self.store.prepare_part(part)
        await self.store.finish(part, "sent", 99)
        await self.store.initialize(999, self.admins)
        self.assertEqual(
            await self.store.pool.fetchval("SELECT status FROM jobs"), "cancelled"
        )
        self.assertEqual(
            await self.store.pool.fetchval(
                "SELECT telegram_id FROM parts WHERE status='sent'"
            ),
            99,
        )
        self.assertIsNone(await self.store.next_part())
