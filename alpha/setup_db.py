"""Local bootstrap. Passwords remain in .alpha; never printed or passed to subprocesses."""

import argparse
import asyncio
import re
import secrets
from pathlib import Path

import asyncpg


def secrets_init():
    root = Path(".alpha")
    root.mkdir(exist_ok=True)
    for name in ("db-password", "app-password"):
        path = root / name
        if not path.exists():
            with path.open("x", encoding="utf-8") as stream:
                stream.write(secrets.token_urlsafe(36))
            path.chmod(0o600)


async def prepare():
    password = (
        await asyncio.to_thread(Path(".alpha/db-password").read_text, encoding="utf-8")
    ).strip()
    application = (
        await asyncio.to_thread(Path(".alpha/app-password").read_text, encoding="utf-8")
    ).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{40,80}", application):
        raise ValueError("invalid local application secret")
    connection = await asyncpg.connect(
        host="127.0.0.1",
        port=55432,
        user="postgres",
        password=password,
        database="rolealpha",
        timeout=10,
    )
    try:
        if not await connection.fetchval(
            "SELECT 1 FROM pg_roles WHERE rolname='roleapp'"
        ):
            await connection.execute(
                "CREATE ROLE roleapp LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD '"
                + application
                + "'"
            )
        await connection.execute("REVOKE ALL ON DATABASE rolealpha FROM PUBLIC")
        await connection.execute(
            "GRANT CONNECT,CREATE ON DATABASE rolealpha TO roleapp"
        )
        await connection.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        if not await connection.fetchval(
            "SELECT 1 FROM pg_database WHERE datname='rolealpha_tests'"
        ):
            await connection.execute("CREATE DATABASE rolealpha_tests OWNER roleapp")
        await connection.execute("REVOKE ALL ON DATABASE rolealpha_tests FROM PUBLIC")
        await connection.execute("GRANT CONNECT ON DATABASE rolealpha_tests TO roleapp")
    finally:
        await connection.close()
    application_connection = await asyncpg.connect(
        host="127.0.0.1",
        port=55432,
        user="roleapp",
        password=application,
        database="rolealpha",
        timeout=10,
    )
    await application_connection.close()
    print(
        "PostgreSQL готов; runtime использует отдельную роль без superuser/create-role/create-db."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--secrets-only", action="store_true")
    args = parser.parse_args()
    try:
        secrets_init()
        if not args.secrets_only:
            asyncio.run(prepare())
    except Exception as exc:  # noqa: BLE001 - never expose connection credentials
        raise SystemExit(
            f"Подготовка БД не завершена; error_type={type(exc).__name__}"
        ) from None
