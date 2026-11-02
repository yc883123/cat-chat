"""Recipe-owned SQLite authority; no migrations of the application database."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


class RecipeStore:
    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "super_agent" / "recipes.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _LOCKS_GUARD:
            self.lock = _LOCKS.setdefault(str(self.path.resolve()), threading.RLock())
        with self.transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS recipe_batches (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, request_key TEXT NOT NULL,
                    payload_hash TEXT NOT NULL, thread_id TEXT NOT NULL DEFAULT '',
                    data TEXT NOT NULL, UNIQUE(owner,request_key));
                CREATE UNIQUE INDEX IF NOT EXISTS recipe_thread ON recipe_batches(thread_id)
                    WHERE thread_id != '';
                CREATE TABLE IF NOT EXISTS recipe_commands (
                    owner TEXT NOT NULL, batch_id TEXT NOT NULL, request_key TEXT NOT NULL,
                    payload_hash TEXT NOT NULL, response TEXT NOT NULL,
                    PRIMARY KEY(owner,batch_id,request_key));
                CREATE TABLE IF NOT EXISTS recipe_revisions (
                    batch_id TEXT NOT NULL, revision INTEGER NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY(batch_id,revision));
                CREATE TABLE IF NOT EXISTS recipe_definitions (
                    owner TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY(owner,id));
            """)

    @contextmanager
    def transaction(self):
        with self.lock:
            db = sqlite3.connect(str(self.path), timeout=30)
            db.row_factory = sqlite3.Row
            try:
                db.execute("BEGIN IMMEDIATE")
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

    def batches(self) -> list[dict[str, Any]]:
        with self.transaction() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT data FROM recipe_batches ORDER BY rowid")]

    def get(self, owner: str, thread_id: str) -> dict[str, Any]:
        with self.transaction() as db:
            row = db.execute("SELECT data FROM recipe_batches WHERE owner=? AND thread_id=?", (owner, thread_id)).fetchone()
        if row is None:
            raise LookupError("配方任务不存在或无权访问")
        return json.loads(row[0])

    def creation(self, owner: str, key: str, payload_hash: str) -> dict[str, Any] | None:
        with self.transaction() as db:
            row = db.execute("SELECT payload_hash,data FROM recipe_batches WHERE owner=? AND request_key=?", (owner, key)).fetchone()
        if row is None:
            return None
        if row[0] != payload_hash:
            raise ValueError("request_id 已用于不同创建内容")
        return json.loads(row[1])

    def insert(self, batch: dict[str, Any], key: str, payload_hash: str) -> None:
        with self.transaction() as db:
            db.execute("INSERT INTO recipe_batches(id,owner,request_key,payload_hash,data) VALUES(?,?,?,?,?)", (batch["id"], batch["owner_id"], key, payload_hash, canonical(batch)))
            self._revision(db, batch)

    @staticmethod
    def _revision(db, batch):
        db.execute("INSERT OR IGNORE INTO recipe_revisions VALUES(?,?,?)", (batch["id"], batch["revision"], canonical(batch)))

    def save(self, batch: dict[str, Any]) -> None:
        with self.transaction() as db:
            db.execute("UPDATE recipe_batches SET thread_id=?,data=? WHERE id=? AND owner=?", (batch["thread_id"], canonical(batch), batch["id"], batch["owner_id"]))
            self._revision(db, batch)

    def command(self, owner: str, batch_id: str, key: str, payload_hash: str) -> dict[str, Any] | None:
        with self.transaction() as db:
            row = db.execute("SELECT payload_hash,response FROM recipe_commands WHERE owner=? AND batch_id=? AND request_key=?", (owner, batch_id, key)).fetchone()
        if row is None:
            return None
        if row[0] != payload_hash:
            raise ValueError("request_id 已用于不同操作内容")
        return json.loads(row[1])

    def commit_command(self, batch: dict[str, Any], key: str, payload_hash: str, response: dict[str, Any], *, expected_data: str) -> dict[str, Any]:
        with self.transaction() as db:
            prior = db.execute("SELECT payload_hash,response FROM recipe_commands WHERE owner=? AND batch_id=? AND request_key=?", (batch["owner_id"], batch["id"], key)).fetchone()
            if prior is not None:
                if prior[0] != payload_hash:
                    raise ValueError("request_id 已用于不同操作内容")
                return json.loads(prior[1])
            updated = db.execute("UPDATE recipe_batches SET data=? WHERE id=? AND owner=? AND data=?", (canonical(batch), batch["id"], batch["owner_id"], expected_data))
            if updated.rowcount != 1:
                raise ValueError("stale revision：配方已并发变化，请刷新")
            self._revision(db, batch)
            db.execute("INSERT INTO recipe_commands VALUES(?,?,?,?,?)", (batch["owner_id"], batch["id"], key, payload_hash, canonical(response)))
            return response

    def definitions(self, owner: str) -> list[dict[str, Any]]:
        with self.transaction() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT data FROM recipe_definitions WHERE owner=?", (owner,))]

    def save_definition(self, owner: str, recipe: dict[str, Any]) -> None:
        with self.transaction() as db:
            row = db.execute("SELECT data FROM recipe_definitions WHERE owner=? AND id=?", (owner, recipe["id"])).fetchone()
            if row is not None and row[0] != canonical(recipe):
                raise ValueError("不可变配方 ID 内容冲突")
            db.execute("INSERT OR IGNORE INTO recipe_definitions VALUES(?,?,?)", (owner, recipe["id"], canonical(recipe)))
