from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def _ids(name: str) -> frozenset[int]:
    value = os.getenv(name, "")
    try:
        return frozenset(int(item.strip()) for item in value.split(",") if item.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must contain comma-separated numeric IDs") from exc


@dataclass(frozen=True)
class Config:
    token: str
    chats: frozenset[int]
    operators: frozenset[int]
    chunk_size: int
    delay: float

    @classmethod
    def from_env(cls, *, discovery: bool = False) -> "Config":
        load_dotenv()
        token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        if not token or token == "replace-locally":
            raise ValueError(
                "TELEGRAM_BOT_TOKEN is missing; copy .env.example to .env and set it locally"
            )
        chats, operators = (
            _ids("PROBE_ALLOWED_CHAT_IDS"),
            _ids("PROBE_OPERATOR_USER_IDS"),
        )
        if not discovery and (not chats or not operators):
            raise ValueError(
                "allowed chat and operator ID lists are required outside discovery mode"
            )
        size = int(os.getenv("PROBE_CHUNK_SIZE", "2"))
        delay = float(os.getenv("PROBE_CHUNK_DELAY_SECONDS", "3"))
        if not 1 <= size <= 5 or not 0 <= delay <= 30:
            raise ValueError("chunk size must be 1..5 and delay must be 0..30 seconds")
        return cls(token, chats, operators, size, delay)
