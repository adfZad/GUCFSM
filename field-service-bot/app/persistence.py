"`"`"
State persistence for GUC Field Service Bot.
Stores PTB conversation state in Azure SQL / local SQL Server.
Replaces in-memory context.user_data — survives Azure Function cold starts.

Usage:
    from persistence import AzureSqlPersistence
    persistence = AzureSqlPersistence()
    app = Application.builder().token(TOKEN).persistence(persistence).build()

Table: dbo.conversation_state (must exist — created by migration/sql/schema.sql)
"`"`"

import json
import pyodbc
import os
import asyncio

from telegram.ext import BasePersistence, PersistenceInput

DB_CONNECTION_STRING = os.environ.get(
    "DB_CONNECTION_STRING",
    "Driver={ODBC Driver 18 for SQL Server};Server=localhost;"
    "Database=GUCFSM;Trusted_Connection=yes;TrustServerCertificate=yes;"
)


class AzureSqlPersistence(BasePersistence):
    "`"`"
    Stores user_data, chat_data, bot_data, and conversations
    in the conversation_state table via pyodbc.

    All data is serialized as JSON. Keys are converted to/from strings
    for DB storage (user_id, chat_id → str; conversation key tuples → JSON).
    "`"`"

    def __init__(self, conn_string=None):
        super().__init__(store_data=PersistenceInput(user_data=True, chat_data=True, bot_data=True))
        self._conn_string = conn_string or DB_CONNECTION_STRING
        self._pending_tasks = set()

    def _track(self, coro):
        task = asyncio.create_task(coro)
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)
        return task

    async def flush(self):
        if self._pending_tasks:
            await asyncio.gather(*self._pending_tasks, return_exceptions=True)

    # ── Low-level DB helpers ──────────────────────────────────────────

    def _connect(self):
        return pyodbc.connect(self._conn_string, autocommit=False, timeout=10)

    def _load(self, entity_type, entity_id):
        "`"`"Load JSON data for a single entity. Returns parsed dict or default."`"`"
        conn = self._connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT data FROM dbo.conversation_state WHERE entity_type=? AND entity_id=?",
                (entity_type, entity_id)
            )
            row = cursor.fetchone()
            return json.loads(row[0]) if row else {}
        finally:
            conn.close()

    def _save(self, entity_type, entity_id, data):
        "`"`"Upsert JSON data for a single entity. Empty data deletes the row."`"`"
        if not data:
            self._delete(entity_type, entity_id)
            return
        conn = self._connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) FROM dbo.conversation_state WHERE entity_type=? AND entity_id=?",
                (entity_type, entity_id)
            )
            exists = cursor.fetchone()[0] > 0
            try:
                serialized = json.dumps(data, ensure_ascii=False)
            except TypeError as e:
                print(f"[PERSISTENCE] JSON dump failed for {entity_type}/{entity_id}: {e}")
                return
            if exists:
                cursor.execute(
                    "UPDATE dbo.conversation_state SET data=?, updated_at=SYSUTCDATETIME() "
                    "WHERE entity_type=? AND entity_id=?",
                    (serialized, entity_type, entity_id)
                )
            else:
                cursor.execute(
                    "INSERT INTO dbo.conversation_state (entity_type, entity_id, data) "
                    "VALUES (?, ?, ?)",
                    (entity_type, entity_id, serialized)
                )
            conn.commit()
        finally:
            conn.close()

    def _delete(self, entity_type, entity_id):
        "`"`"Delete a single entity row."`"`"
        conn = self._connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM dbo.conversation_state WHERE entity_type=? AND entity_id=?",
                (entity_type, entity_id)
            )
            conn.commit()
        finally:
            conn.close()

    def _load_all(self, entity_type):
        "`"`"Load all rows of a given entity_type. Returns dict keyed by entity_id (parsed)."`"`"
        conn = self._connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT entity_id, data FROM dbo.conversation_state WHERE entity_type=?",
                (entity_type,)
            )
            result = {}
            for row in cursor.fetchall():
                key = row[0]
                val = json.loads(row[1]) if row[1] else {}
                result[key] = val
            return result
        finally:
            conn.close()

    # ── User data ─────────────────────────────────────────────────────

    async def get_user_data(self):
        raw = await asyncio.to_thread(self._load_all, "user")
        return {int(k): v for k, v in raw.items()}

    async def update_user_data(self, user_id, data):
        await self._track(asyncio.to_thread(self._save, "user", str(user_id), data))

    async def refresh_user_data(self, user_id, user_data):
        fresh = await asyncio.to_thread(self._load, "user", str(user_id))
        user_data.clear()
        user_data.update(fresh)

    async def drop_user_data(self, user_id):
        await asyncio.to_thread(self._delete, "user", str(user_id))

    # ── Chat data ─────────────────────────────────────────────────────

    async def get_chat_data(self):
        raw = await asyncio.to_thread(self._load_all, "chat")
        return {int(k): v for k, v in raw.items()}

    async def update_chat_data(self, chat_id, data):
        await self._track(asyncio.to_thread(self._save, "chat", str(chat_id), data))

    async def refresh_chat_data(self, chat_id, chat_data):
        fresh = await asyncio.to_thread(self._load, "chat", str(chat_id))
        chat_data.clear()
        chat_data.update(fresh)

    async def drop_chat_data(self, chat_id):
        await asyncio.to_thread(self._delete, "chat", str(chat_id))

    # ── Bot data ──────────────────────────────────────────────────────

    async def get_bot_data(self):
        return await asyncio.to_thread(self._load, "bot", "bot")

    async def update_bot_data(self, data):
        await self._track(asyncio.to_thread(self._save, "bot", "bot", data))

    async def refresh_bot_data(self, bot_data):
        fresh = await asyncio.to_thread(self._load, "bot", "bot")
        bot_data.clear()
        bot_data.update(fresh)

    # ── Conversations ─────────────────────────────────────────────────

    async def get_conversations(self, name):
        "`"`"
        PTB stores conversations per handler name.
        Key is a tuple (user_id, user_id, ...) — serialized as JSON string key.
        Returns dict of {key: state}.
        "`"`"
        raw = await asyncio.to_thread(self._load, "conv", name)
        result = {}
        for key_str, state in raw.items():
            key_tuple = tuple(json.loads(key_str))
            result[key_tuple] = state
        return result

    async def update_conversation(self, name, key, new_state=None):
        raw = await asyncio.to_thread(self._load, "conv", name)
        key_str = json.dumps(key)
        if new_state is None:
            raw.pop(key_str, None)
        else:
            raw[key_str] = new_state
        await self._track(asyncio.to_thread(self._save, "conv", name, raw))

    # ── Callback data ─────────────────────────────────────────────────

    async def get_callback_data(self):
        return None

    async def update_callback_data(self, data):
        pass

    # ── Flush ─────────────────────────────────────────────────────────


