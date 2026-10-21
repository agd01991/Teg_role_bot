from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from pathlib import Path


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


class Journal:
    """Independent, fsynced deletion log. Restore must replay before polling/worker."""

    def __init__(self, store, directory: Path):
        self.store = store
        self.path = directory / "deletions.jsonl"
        self.lock = asyncio.Lock()
        self.healthy = False
        self.entries = []

    def read(self, expected_identity):
        if not self.path.exists():
            if expected_identity:
                raise RuntimeError("deletion journal missing; startup blocked")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            header = {"version": 1, "id": str(uuid.uuid4())}
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(canonical(header) + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
        if self.path.stat().st_size > 5 * 1024 * 1024:
            raise RuntimeError("deletion journal size limit; maintenance required")
        lines = self.path.read_bytes().splitlines(keepends=True)
        if not lines or any(not line.endswith(b"\n") for line in lines):
            raise RuntimeError("incomplete deletion journal; startup blocked")
        header = json.loads(lines[0])
        if header.get("version") != 1 or (
            expected_identity and header["id"] != expected_identity
        ):
            raise RuntimeError("wrong deletion journal; startup blocked")
        previous = ""
        entries = []
        for number, line in enumerate(lines[1:], 1):
            entry = json.loads(line)
            digest = entry.pop("digest")
            if (
                entry["seq"] != number
                or entry["prev"] != previous
                or entry["action"] not in {"delete", "allow"}
                or not isinstance(entry["person"], int)
                or entry["person"] <= 0
                or hashlib.sha256(canonical(entry)).hexdigest() != digest
            ):
                raise RuntimeError("invalid deletion journal; startup blocked")
            entries.append({**entry, "digest": digest})
            previous = digest
        return header["id"], entries

    async def start(self):
        meta = await self.store.pool.fetchrow("SELECT * FROM meta")
        identity, self.entries = await asyncio.to_thread(self.read, meta["ledger_id"])
        seq = meta["ledger_seq"]
        if (
            (seq == 0 and meta["ledger_hash"])
            or seq > len(self.entries)
            or (seq and self.entries[seq - 1]["digest"] != meta["ledger_hash"])
        ):
            raise RuntimeError("deletion journal behind database; startup blocked")
        await self.store.pool.execute("UPDATE meta SET ledger_id=$1", identity)
        for entry in self.entries[seq:]:
            await self.apply(entry)
        self.healthy = True

    def append(self, entry):
        with self.path.open("ab", buffering=0) as stream:
            payload = canonical(entry) + b"\n"
            if stream.write(payload) != len(payload):
                raise RuntimeError("incomplete deletion journal write")
            os.fsync(stream.fileno())

    async def record(self, person, action):
        async with self.lock:
            if not self.healthy:
                raise RuntimeError("privacy boundary unavailable")
            blocked = await self.store.pool.fetchval(
                "SELECT blocked FROM people WHERE id=$1", person
            )
            if blocked is not None and blocked == (action == "delete"):
                return
            if len(self.entries) >= 10000:
                raise RuntimeError("deletion journal event limit; maintenance required")
            entry = {
                "seq": len(self.entries) + 1,
                "prev": self.entries[-1]["digest"] if self.entries else "",
                "person": person,
                "action": action,
            }
            entry["digest"] = hashlib.sha256(canonical(entry)).hexdigest()
            try:
                await asyncio.to_thread(self.append, entry)
                self.entries.append(entry)
                await self.apply(entry)
            except BaseException:
                self.healthy = False
                raise

    async def apply(self, entry):
        person = entry["person"]
        async with self.store.pool.acquire() as connection, connection.transaction():
            meta = await connection.fetchrow("SELECT * FROM meta FOR UPDATE")
            if (
                entry["seq"] != meta["ledger_seq"] + 1
                or entry["prev"] != meta["ledger_hash"]
            ):
                raise RuntimeError("deletion journal sequence mismatch")
            await connection.execute(
                "INSERT INTO people(id) VALUES($1) ON CONFLICT DO NOTHING", person
            )
            if entry["action"] == "delete":
                await connection.execute(
                    "UPDATE people SET blocked=true,ever_deleted=true WHERE id=$1",
                    person,
                )
                await connection.execute("DELETE FROM pending WHERE author=$1", person)
                await connection.execute("DELETE FROM jobs WHERE author=$1", person)
                await connection.execute(
                    "UPDATE parts SET recipients=array_remove(recipients,$1),epochs=epochs-($1::bigint)::text WHERE $1=ANY(recipients)",
                    person,
                )
                await connection.execute("DELETE FROM members WHERE person=$1", person)
                await connection.execute(
                    "UPDATE audit SET actor=NULL WHERE actor=$1", person
                )
            else:
                await connection.execute(
                    "UPDATE people SET blocked=false WHERE id=$1", person
                )
            await connection.execute(
                "UPDATE meta SET ledger_seq=$1,ledger_hash=$2",
                entry["seq"],
                entry["digest"],
            )
