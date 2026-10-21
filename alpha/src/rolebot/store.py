from __future__ import annotations

import hashlib
import json
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg


class Denied(Exception):
    """A safe fixed user-facing explanation, never an upstream exception."""


class Store:
    def __init__(self, pool):
        self.pool = pool

    @classmethod
    async def open(cls, url: str):
        pool = await asyncpg.create_pool(
            url,
            min_size=1,
            max_size=4,
            command_timeout=15,
            server_settings={"search_path": "rolebot,pg_catalog"},
        )
        return cls(pool)

    async def migrate(self):
        async with self.pool.acquire() as connection, connection.transaction():
            if await connection.fetchval("SELECT to_regclass('rolebot.meta')"):
                if await connection.fetchval("SELECT version FROM meta") != 1:
                    raise Denied(
                        "Версия схемы не поддерживается. Автоматический откат запрещён."
                    )
            await connection.execute(
                Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
            )

    async def initialize(self, bot_id, admins):
        async with self.pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow("SELECT * FROM meta FOR UPDATE")
            if row["bot_id"] not in (None, bot_id):
                raise Denied(
                    "Эта база принадлежит другому боту. Используйте отдельную базу."
                )
            await connection.execute("UPDATE meta SET bot_id=$1", bot_id)
            await connection.execute("UPDATE chats SET enabled=false")
            for chat in admins:
                await connection.execute(
                    "INSERT INTO chats VALUES($1,true) ON CONFLICT(id) DO UPDATE SET enabled=true",
                    chat,
                )
            await connection.execute(
                "UPDATE parts SET status='uncertain' WHERE status='sending'"
            )
            await connection.execute(
                "UPDATE jobs SET status='uncertain' WHERE id IN (SELECT job FROM parts WHERE status='uncertain')"
            )
            await connection.execute(
                "UPDATE jobs SET status='cancelled' WHERE status='pending'"
            )

    @asynccontextmanager
    async def transaction(self, chat):
        async with self.pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                "SELECT enabled FROM chats WHERE id=$1 FOR UPDATE", chat
            )
            if not row or not row["enabled"]:
                raise Denied("Чат приостановлен или не разрешён.")
            yield connection

    @staticmethod
    async def require(connection, chat, person, revision=None, admin=False):
        row = await connection.fetchrow(
            "SELECT m.*,p.blocked FROM members m JOIN people p ON p.id=m.person WHERE chat=$1 AND person=$2",
            chat,
            person,
        )
        if (
            not row
            or not row["active"]
            or row["blocked"]
            or (revision is not None and row["revision"] != revision)
            or (admin and row["admin_revoked"])
        ):
            raise Denied(
                "Доступ изменился. Повторите действие после проверки членства."
            )
        return row

    async def revision(self, chat, person):
        return (
            await self.pool.fetchval(
                "SELECT revision FROM members WHERE chat=$1 AND person=$2", chat, person
            )
            or 0
        )

    async def observe(self, chat, person, name, present, expected=None, event=None):
        async with self.transaction(chat) as connection:
            await connection.execute(
                "INSERT INTO people(id) VALUES($1) ON CONFLICT DO NOTHING", person
            )
            if await connection.fetchval(
                "SELECT blocked FROM people WHERE id=$1", person
            ):
                raise Denied("Профиль удалён. Для нового участия нужен ваш /optin.")
            await connection.execute(
                "INSERT INTO members(chat,person,admin_revoked) SELECT $1,id,ever_deleted FROM people WHERE id=$2 ON CONFLICT DO NOTHING",
                chat,
                person,
            )
            row = await connection.fetchrow(
                "SELECT * FROM members WHERE chat=$1 AND person=$2 FOR UPDATE",
                chat,
                person,
            )
            if event is not None and event <= row["last_event"]:
                return row["revision"]
            if expected is not None and expected != row["revision"]:
                raise Denied(
                    "Членство изменилось во время проверки. Повторите действие."
                )
            blocked = await connection.fetchval(
                "SELECT blocked FROM people WHERE id=$1", person
            )
            changed = row["active"] != present
            await connection.execute(
                "UPDATE members SET name=$3,active=$4,revision=revision+$5,admin_revoked=admin_revoked OR NOT $4,last_event=greatest(last_event,$6) WHERE chat=$1 AND person=$2",
                chat,
                person,
                "" if blocked else name[:48],
                present,
                int(changed),
                event if event is not None else -1,
            )
            if not present:
                await connection.execute(
                    "DELETE FROM assignments WHERE chat=$1 AND person=$2", chat, person
                )
                await connection.execute(
                    "UPDATE jobs SET status='cancelled' WHERE chat=$1 AND author=$2 AND status='pending'",
                    chat,
                    person,
                )
            return row["revision"] + int(changed)

    async def claim_update(self, update_id, chat=None, message_id=None):
        return (
            await self.pool.fetchval(
                "INSERT INTO inbox(update_id,chat,message_id) VALUES($1,$2,$3) ON CONFLICT DO NOTHING RETURNING update_id",
                update_id,
                chat,
                message_id,
            )
            is not None
        )

    async def stage(self, chat, author, revision, role_code, title):
        token = secrets.token_urlsafe(12)
        async with self.transaction(chat) as connection:
            await self.require(connection, chat, author, revision, admin=True)
            await connection.execute(
                "INSERT INTO pending(digest,chat,author,code,title) VALUES($1,$2,$3,$4,$5) ON CONFLICT(chat,author) DO UPDATE SET digest=excluded.digest,code=excluded.code,title=excluded.title,expires=now()+interval '2 minutes'",
                hashlib.sha256(token.encode()).hexdigest(),
                chat,
                author,
                role_code,
                title,
            )
        return token

    async def confirm(self, chat, author, revision, token):
        async with self.transaction(chat) as connection:
            await self.require(connection, chat, author, revision, admin=True)
            row = await connection.fetchrow(
                "DELETE FROM pending WHERE chat=$1 AND author=$2 AND digest=$3 AND expires>now() RETURNING *",
                chat,
                author,
                hashlib.sha256(token.encode()).hexdigest(),
            )
            if not row:
                raise Denied(
                    "Подтверждение устарело или принадлежит другому пользователю."
                )
            if (
                await connection.fetchval(
                    "SELECT count(*) FROM roles WHERE chat=$1", chat
                )
                >= 20
            ):
                raise Denied("Лимит альфы: 20 ролей на чат.")
            created = await connection.fetchval(
                "INSERT INTO roles(chat,code,title) VALUES($1,$2,$3) ON CONFLICT(chat,code) DO NOTHING RETURNING id",
                chat,
                row["code"],
                row["title"],
            )
            if created is None:
                raise Denied("Этот код уже занят. Существующая роль не изменена.")
            await connection.execute(
                "INSERT INTO audit(chat,actor,action) VALUES($1,$2,'role.create')",
                chat,
                author,
            )
            return row["code"]

    async def assign(
        self, chat, author, revision, role_code, target, target_revision, remove=False
    ):
        async with self.transaction(chat) as connection:
            await self.require(connection, chat, author, revision, admin=True)
            role = await connection.fetchval(
                "SELECT id FROM roles WHERE chat=$1 AND code=$2", chat, role_code
            )
            if role is None:
                raise Denied(
                    "Доступная роль не найдена. Сначала создайте её через /create."
                )
            if remove:
                await connection.execute(
                    "DELETE FROM assignments WHERE chat=$1 AND role=$2 AND person=$3",
                    chat,
                    role,
                    target,
                )
            else:
                await self.require(connection, chat, target, target_revision)
                exists = await connection.fetchval(
                    "SELECT 1 FROM assignments WHERE chat=$1 AND role=$2 AND person=$3",
                    chat,
                    role,
                    target,
                )
                if (
                    not exists
                    and await connection.fetchval(
                        "SELECT count(*) FROM assignments WHERE chat=$1 AND role=$2",
                        chat,
                        role,
                    )
                    >= 20
                ):
                    raise Denied("Лимит альфы: 20 получателей на роль.")
                await connection.execute(
                    "INSERT INTO assignments VALUES($1,$2,$3) ON CONFLICT DO NOTHING",
                    chat,
                    role,
                    target,
                )
            await connection.execute(
                "INSERT INTO audit(chat,actor,action) VALUES($1,$2,$3)",
                chat,
                author,
                "role.unassign" if remove else "role.assign",
            )

    async def roles(self, chat):
        return await self.pool.fetch(
            "SELECT r.code,r.title,count(a.person) AS count FROM roles r LEFT JOIN assignments a ON a.chat=r.chat AND a.role=r.id WHERE r.chat=$1 GROUP BY r.id ORDER BY r.code LIMIT 20",
            chat,
        )

    async def enqueue(
        self, chat, author, revision, role_code, update_id, message_id, thread_id
    ):
        async with self.transaction(chat) as connection:
            await self.require(connection, chat, author, revision)
            role = await connection.fetchrow(
                "SELECT * FROM roles WHERE chat=$1 AND code=$2", chat, role_code
            )
            if not role:
                raise Denied("Доступная роль не найдена.")
            uncertain = await connection.fetchval(
                "SELECT 1 FROM jobs WHERE chat=$1 AND status='uncertain' LIMIT 1", chat
            )
            if uncertain:
                raise Denied(
                    "Есть отправка с неизвестным результатом. Оператор должен сверить её через /jobs."
                )
            count = await connection.fetchval(
                "SELECT count(*) FROM jobs WHERE chat=$1 AND status='pending'", chat
            )
            throttled = await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM jobs WHERE chat=$1 AND ((author=$2 AND created>now()-interval '60 seconds') OR (role=$3 AND created>now()-interval '5 minutes'))) OR (SELECT count(*)>=5 FROM jobs WHERE chat=$1 AND author=$2 AND created>now()-interval '1 hour') OR (SELECT count(*)>=10 FROM jobs WHERE chat=$1 AND created>now()-interval '1 hour')",
                chat,
                author,
                role["id"],
            )
            if count >= 3 or throttled:
                raise Denied(
                    "Лимит: 1 вызов в минуту на человека, 5 в час; 1 в 5 минут на роль; 10 в час на чат; очередь до 3."
                )
            people = await connection.fetch(
                "SELECT a.person,m.revision FROM assignments a JOIN members m ON m.chat=a.chat AND m.person=a.person JOIN people p ON p.id=a.person WHERE a.chat=$1 AND a.role=$2 AND m.active AND NOT m.opt_out AND NOT p.blocked AND a.person<>$3 ORDER BY a.person",
                chat,
                role["id"],
                author,
            )
            if not people:
                raise Denied("Сейчас в роли нет участников, доступных для упоминания.")
            job = await connection.fetchval(
                "INSERT INTO jobs(chat,author,role,role_version,update_id,message_id,thread_id,author_revision) VALUES($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT DO NOTHING RETURNING id",
                chat,
                author,
                role["id"],
                role["version"],
                update_id,
                message_id,
                thread_id,
                revision,
            )
            if job is None:
                return None
            ids = [row["person"] for row in people]
            for number, start in enumerate(range(0, len(ids), 5)):
                await connection.execute(
                    "INSERT INTO parts(job,number,recipients,epochs) VALUES($1,$2,$3,$4)",
                    job,
                    number,
                    ids[start : start + 5],
                    json.dumps(
                        {
                            str(row["person"]): row["revision"]
                            for row in people[start : start + 5]
                        }
                    ),
                )
            return job

    async def next_part(self):
        await self.pool.execute(
            "UPDATE jobs SET status='cancelled' WHERE status='pending' AND created<now()-interval '5 minutes'"
        )
        return await self.pool.fetchrow(
            "SELECT j.*,p.number,p.recipients,p.epochs,p.attempts,r.title FROM jobs j JOIN chats c ON c.id=j.chat JOIN parts p ON p.job=j.id JOIN roles r ON r.id=j.role AND r.chat=j.chat WHERE j.status='pending' AND c.enabled AND j.not_before<=now() AND p.status='pending' AND NOT EXISTS(SELECT 1 FROM parts old WHERE old.job=j.id AND old.number<p.number AND old.status NOT IN ('sent','skipped')) AND NOT EXISTS(SELECT 1 FROM jobs old WHERE old.chat=j.chat AND old.id<j.id AND old.status IN ('pending','uncertain')) ORDER BY j.id,p.number LIMIT 1"
        )

    async def prepare_part(self, part):
        async with self.transaction(part["chat"]) as connection:
            job = await connection.fetchrow(
                "SELECT * FROM jobs WHERE id=$1 FOR UPDATE", part["id"]
            )
            if not job or job["status"] != "pending":
                return []
            await self.require(
                connection, job["chat"], job["author"], job["author_revision"]
            )
            valid = await connection.fetchval(
                "SELECT 1 FROM roles WHERE chat=$1 AND id=$2 AND version=$3",
                job["chat"],
                job["role"],
                job["role_version"],
            )
            if not valid:
                raise Denied("Версия роли изменилась.")
            rows = await connection.fetch(
                "SELECT m.person,m.name FROM members m JOIN people p ON p.id=m.person JOIN assignments a ON a.chat=m.chat AND a.person=m.person WHERE m.chat=$1 AND a.role=$2 AND m.person=ANY($3::bigint[]) AND m.active AND NOT m.opt_out AND NOT p.blocked AND m.revision=($4::jsonb->>m.person::text)::bigint ORDER BY m.person",
                job["chat"],
                job["role"],
                part["recipients"],
                part["epochs"],
            )
            if rows:
                await connection.execute(
                    "UPDATE parts SET status='sending',attempts=attempts+1 WHERE job=$1 AND number=$2 AND status='pending'",
                    job["id"],
                    part["number"],
                )
            return rows

    async def finish(self, part, status, telegram_id=None, retry_after=None):
        async with self.pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "UPDATE parts SET status=$3,telegram_id=$4 WHERE job=$1 AND number=$2",
                part["id"],
                part["number"],
                status,
                telegram_id,
            )
            if retry_after is not None:
                await connection.execute(
                    "UPDATE jobs SET not_before=now()+$2*interval '1 second' WHERE id=$1 AND status='pending'",
                    part["id"],
                    float(retry_after),
                )
            elif status == "uncertain":
                await connection.execute(
                    "UPDATE jobs SET status='uncertain' WHERE id=$1", part["id"]
                )
            elif status == "failed":
                await connection.execute(
                    "UPDATE jobs SET status=$2 WHERE id=$1 AND status='pending'",
                    part["id"],
                    status,
                )
            else:
                await connection.execute(
                    "UPDATE jobs SET status='done' WHERE id=$1 AND status='pending' AND NOT EXISTS(SELECT 1 FROM parts WHERE job=$1 AND status NOT IN ('sent','skipped'))",
                    part["id"],
                )

    async def stop(self, chat, actor, admin):
        return await self.pool.execute(
            "UPDATE jobs SET status='cancelled' WHERE chat=$1 AND status='pending' AND (author=$2 OR $3)",
            chat,
            actor,
            admin,
        )

    async def jobs(self, chat, actor, admin):
        return await self.pool.fetch(
            "SELECT j.id,j.status,count(p.*) FILTER(WHERE p.status='sent') AS sent,count(p.*) AS total FROM jobs j LEFT JOIN parts p ON p.job=j.id WHERE j.chat=$1 AND (j.author=$2 OR $3) GROUP BY j.id ORDER BY j.id DESC LIMIT 10",
            chat,
            actor,
            admin,
        )

    async def set_opt_out(self, chat, person, value):
        await self.pool.execute(
            "UPDATE members SET opt_out=$3 WHERE chat=$1 AND person=$2",
            chat,
            person,
            value,
        )

    async def cleanup(self):
        async with self.pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "DELETE FROM inbox WHERE created<now()-interval '7 days'"
            )
            await connection.execute("DELETE FROM pending WHERE expires<now()")
            await connection.execute(
                "DELETE FROM jobs WHERE created<now()-interval '7 days' AND status IN ('done','failed','cancelled')"
            )
            await connection.execute(
                "DELETE FROM audit WHERE created<now()-interval '30 days'"
            )
