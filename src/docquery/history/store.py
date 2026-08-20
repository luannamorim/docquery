"""Conversation history in MySQL.

Two tables and a handful of statements, written as SQL rather than through an
ORM — the same reason the rest of the project talks to Qdrant and OpenAI
directly. Schema lives in schema.sql beside this module and is applied at
startup.

**Ownership is a WHERE clause, never a Python check.** Every read and every
write names the owner, so there is no path that loads a conversation first and
decides afterwards whether the caller may have it. A conversation belonging to
someone else is indistinguishable from one that does not exist, which is what
lets the endpoint answer 404 — a 403 would confirm the id to whoever is
guessing.
"""

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import pymysql
from pymysql.constants import FIELD_TYPE
from pymysql.converters import conversions, convert_datetime

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def _utc_datetime(value):
    """Decode a DATETIME/TIMESTAMP column as an aware UTC datetime.

    pymysql hands back naive datetimes in whatever time zone the session runs
    in. Naive survives Pydantic as an offset-less ISO string, which the browser
    then reparses as *its* local time — every timestamp shifts by the viewer's
    UTC offset. The session is pinned to UTC below, so attaching the tzinfo
    here is a statement of fact, and the offset reaches the JSON.
    """
    parsed = convert_datetime(value)
    if isinstance(parsed, datetime):
        return parsed.replace(tzinfo=UTC)
    return parsed


def _dsn_to_kwargs(dsn: str) -> dict:
    """mysql://user:pass@host:port/database → pymysql.connect kwargs."""
    split = urlsplit(dsn)
    if split.scheme not in ("mysql", "mysql+pymysql"):
        raise ValueError("history_dsn must start with mysql://")
    database = split.path.lstrip("/")
    if not (split.hostname and database):
        raise ValueError("history_dsn needs a host and a database name")
    return {
        "host": split.hostname,
        "port": split.port or 3306,
        "user": split.username or "",
        "password": split.password or "",
        "database": database,
        "charset": "utf8mb4",
        "autocommit": True,
        # Pinned so CURRENT_TIMESTAMP writes and reads mean the same instant
        # regardless of the container's or the server's local time zone.
        "init_command": "SET time_zone = '+00:00'",
        "conv": {
            **conversions,
            FIELD_TYPE.DATETIME: _utc_datetime,
            FIELD_TYPE.TIMESTAMP: _utc_datetime,
        },
    }


def _statements(sql: str) -> list[str]:
    """Split a script into executable statements, dropping comment-only ones.

    pymysql sends one statement per execute(), and a chunk that is nothing but
    the file's header comment is a syntax error rather than a harmless no-op.

    Comments are stripped before splitting, not after: a prose comment may well
    contain a semicolon, and splitting first would cut one in half and leave the
    tail looking like SQL.
    """
    code = "\n".join(
        line for line in sql.splitlines() if not line.strip().startswith("--")
    )
    return [chunk.strip() for chunk in code.split(";") if chunk.strip()]


class ConversationStore:
    def __init__(self, dsn: str) -> None:
        self._kwargs = _dsn_to_kwargs(dsn)

    def _connect(self):
        """One connection per operation.

        The API's routes are sync and run in Starlette's threadpool, so a shared
        connection would be handed to concurrent threads — pymysql connections
        are not safe for that. Connections are cheap next to the LLM call this
        sits behind; a pool is an optimisation to make when it measures.
        """
        return pymysql.connect(**self._kwargs, cursorclass=pymysql.cursors.DictCursor)

    def init_schema(self) -> None:
        """Apply schema.sql. Every statement is IF NOT EXISTS, so this is a
        no-op against a database that already has the tables."""
        with self._connect() as conn, conn.cursor() as cur:
            for statement in _statements(SCHEMA_PATH.read_text()):
                cur.execute(statement)

    def reset_for_tests(self) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM turns")
            cur.execute("DELETE FROM conversations")

    def create(self, owner: str) -> str:
        conversation_id = str(uuid.uuid4())
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO conversations (id, owner_oid) VALUES (%s, %s)",
                (conversation_id, owner),
            )
        return conversation_id

    def append(
        self,
        conversation_id: str,
        owner: str,
        question: str,
        answer: str,
        rewritten_question: str = "",
        citations: list | None = None,
        sectors: list | None = None,
        model: str = "",
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost_usd: float = 0.0,
        complete: bool = True,
    ) -> bool:
        """Record a turn. False when the conversation is not this owner's.

        complete=False marks an answer the client stopped receiving part-way —
        the user did see that much, so the audit trail records it rather than
        losing the turn, and the flag says which of the two it is.
        """
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM conversations WHERE id = %s AND owner_oid = %s",
                (conversation_id, owner),
            )
            if cur.fetchone() is None:
                return False
            cur.execute(
                """
                INSERT INTO turns (
                    conversation_id, seq, question, rewritten_question, answer,
                    citations, sectors, model, tokens_in, tokens_out, cost_usd,
                    complete
                )
                SELECT %s, COALESCE(MAX(seq), 0) + 1,
                       %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                FROM turns WHERE conversation_id = %s
                """,
                (
                    conversation_id,
                    question,
                    rewritten_question,
                    answer,
                    json.dumps(citations or []),
                    json.dumps(sectors or []),
                    model,
                    tokens_in,
                    tokens_out,
                    cost_usd,
                    complete,
                    conversation_id,
                ),
            )
            cur.execute(
                "UPDATE conversations SET last_turn_at = CURRENT_TIMESTAMP "
                "WHERE id = %s AND owner_oid = %s",
                (conversation_id, owner),
            )
        return True

    def turns(self, conversation_id: str, owner: str) -> list[dict] | None:
        """Every turn in order, or None when the conversation is not the owner's."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM conversations WHERE id = %s AND owner_oid = %s",
                (conversation_id, owner),
            )
            if cur.fetchone() is None:
                return None
            cur.execute(
                """
                SELECT seq, question, rewritten_question, answer, citations,
                       model, tokens_in, tokens_out, cost_usd, complete,
                       created_at
                FROM turns WHERE conversation_id = %s ORDER BY seq
                """,
                (conversation_id,),
            )
            # pymysql hands back a tuple; the annotation and every caller expect
            # a list, and "no turns yet" must be [] rather than ().
            rows = list(cur.fetchall())
        for row in rows:
            row["citations"] = json.loads(row["citations"] or "[]")
        return rows

    def list_conversations(self, owner: str, limit: int = 100) -> list[dict]:
        """The owner's conversations, most recently used first.

        Titled by their opening question — the one the user typed to start the
        thread, which is what they will recognise it by. Uses the
        (owner_oid, last_turn_at) index; a conversation with no turns yet sorts
        last and carries an empty title.
        """
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.id,
                       c.created_at,
                       c.last_turn_at,
                       (SELECT t.question FROM turns t
                         WHERE t.conversation_id = c.id
                         ORDER BY t.seq LIMIT 1) AS title
                FROM conversations c
                WHERE c.owner_oid = %s
                ORDER BY COALESCE(c.last_turn_at, c.created_at) DESC
                LIMIT %s
                """,
                (owner, limit),
            )
            rows = list(cur.fetchall())
        for row in rows:
            row["title"] = row["title"] or ""
        return rows

    def previous_questions(
        self, conversation_id: str, owner: str, limit: int
    ) -> list[str]:
        """The last `limit` questions asked, oldest first.

        Questions only — see contextualize.py for why the answers must not
        travel with them.
        """
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT t.question FROM turns t
                JOIN conversations c ON c.id = t.conversation_id
                WHERE t.conversation_id = %s AND c.owner_oid = %s
                ORDER BY t.seq DESC LIMIT %s
                """,
                (conversation_id, owner, limit),
            )
            rows = cur.fetchall()
        return [row["question"] for row in reversed(rows)]

    def delete(self, conversation_id: str, owner: str) -> bool:
        """Erase a conversation and its turns. False when it is not the owner's.

        Present regardless of the retention policy: the right to erasure (LGPD
        art. 18) belongs to the data subject and does not depend on how long we
        would otherwise keep the record.
        """
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM conversations WHERE id = %s AND owner_oid = %s",
                (conversation_id, owner),
            )
            return cur.rowcount > 0
