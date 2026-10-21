from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote


@dataclass(frozen=True)
class Config:
    token: str
    admins: dict[int, frozenset[int]]
    database_url: str
    data_dir: Path

    @classmethod
    def load(cls, *, discovery: bool = False) -> Config:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required; enter it in the launcher")
        admins: dict[int, frozenset[int]] = {}
        raw = os.environ.get("ALPHA_CHAT_ADMINS", "")
        try:
            for scope in filter(None, raw.split(";")):
                chat, users = scope.split(":")
                chat_id = int(chat)
                ids = frozenset(int(item) for item in users.split(","))
                if (
                    not -(2**63) <= chat_id < 0
                    or not ids
                    or min(ids) <= 0
                    or max(ids) > 2**63 - 1
                    or chat_id in admins
                ):
                    raise ValueError
                admins[chat_id] = ids
            if len(admins) > 10 or any(len(ids) > 10 for ids in admins.values()):
                raise ValueError
        except (ValueError, TypeError) as exc:
            raise ValueError("ALPHA_CHAT_ADMINS: use chat:user,user;chat:user") from exc
        if not discovery and not admins:
            raise ValueError(
                "ALPHA_CHAT_ADMINS must explicitly allow test chats/admins"
            )
        data_dir = Path(os.environ.get("ALPHA_DATA_DIR", ".alpha")).resolve()
        database_url = os.environ.get("ALPHA_DATABASE_URL", "")
        if not database_url and not discovery:
            password = (data_dir / "app-password").read_text(encoding="utf-8").strip()
            database_url = f"postgresql://roleapp:{quote(password, safe='')}@127.0.0.1:55432/rolealpha"
        return cls(token, admins, database_url, data_dir)
