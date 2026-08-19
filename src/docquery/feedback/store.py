"""Outdated-document reports in MySQL.

One table, written as SQL for the same reason the history store is. The DSN
parsing and script splitting are imported from there rather than duplicated —
feedback deliberately shares history's database (and its `history_dsn`
setting, whose name the error messages therefore still use).

The access model differs from history's, which is why this is a separate
store and not more methods on ConversationStore: a conversation is owned by
the oid that opened it, while a report is written once per (document, oid)
and read back by *sector* — any member of the document's compartment sees it.
The sector predicate is still a WHERE clause, never a Python check, so a
document outside the caller's sectors is indistinguishable from one nobody
reported.
"""

import hashlib
from pathlib import Path

import pymysql

from docquery.history.store import _dsn_to_kwargs, _statements

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def source_hash(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _clean(sectors: list[str] | None) -> list[str] | None:
    """Drop blank sectors, the same rule retrieval applies before filtering.

    A chunk ingested outside any sector carries "" and is unreachable by any
    role; a blank in the caller's list must not become a predicate that
    matches those rows.
    """
    if sectors is None:
        return None
    return [s for s in sectors if s]


class FeedbackStore:
    def __init__(self, dsn: str) -> None:
        self._kwargs = _dsn_to_kwargs(dsn)

    def _connect(self):
        """One connection per operation — see ConversationStore._connect."""
        return pymysql.connect(**self._kwargs, cursorclass=pymysql.cursors.DictCursor)

    def init_schema(self) -> None:
        """Apply schema.sql. Every statement is IF NOT EXISTS, so this is a
        no-op against a database that already has the table."""
        with self._connect() as conn, conn.cursor() as cur:
            for statement in _statements(SCHEMA_PATH.read_text()):
                cur.execute(statement)

    def reset_for_tests(self) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM document_reports")

    def report(self, source: str, sector: str, reporter: str, comment: str = "") -> bool:
        """Record a report. True when it is this reporter's first for the source.

        A repeat report is an update, not a duplicate: the UNIQUE key on
        (source_hash, reporter_oid) turns it into ON DUPLICATE KEY UPDATE.
        rowcount is 1 for an insert and 2 for an update — and 0 when the
        update changed nothing (same comment within the timestamp's second),
        so anything other than 1 reads as "already reported".
        """
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO document_reports
                    (source, source_hash, sector, reporter_oid, comment)
                VALUES (%s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    comment = %s, sector = %s, updated_at = CURRENT_TIMESTAMP
                """,
                (source, source_hash(source), sector, reporter, comment,
                 comment, sector),
            )
            return cur.rowcount == 1

    def list_reports(
        self, sectors: list[str] | None, limit: int = 200
    ) -> list[dict]:
        """Reported documents the caller may see, newest activity first.

        None means "do not filter" (auth off never reaches here, but the store
        keeps the same three-state contract as retrieval); [] reads nothing and
        never touches MySQL. Aggregated per (source_hash, sector) — a document
        re-ingested into another sector groups apart, which is the snapshot
        semantics, not a bug. ANY_VALUE because MySQL 8 defaults to
        ONLY_FULL_GROUP_BY and every row in a group shares the same source.
        """
        sectors = _clean(sectors)
        if sectors is not None and not sectors:
            return []
        predicate, params = self._sector_predicate(sectors)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT source_hash, ANY_VALUE(source) AS source, sector,
                       COUNT(*) AS report_count, MAX(updated_at) AS last_reported_at
                FROM document_reports
                {predicate}
                GROUP BY source_hash, sector
                ORDER BY last_reported_at DESC
                LIMIT %s
                """,
                (*params, limit),
            )
            rows = list(cur.fetchall())
            # Comments in a second query rather than GROUP_CONCAT: separator
            # collisions and empty-comment filtering are both bugs waiting.
            for row in rows:
                cur.execute(
                    f"""
                    SELECT comment FROM document_reports
                    WHERE source_hash = %s AND sector = %s AND comment <> ''
                    {predicate.replace("WHERE", "AND", 1)}
                    ORDER BY updated_at DESC
                    """,
                    (row["source_hash"], row["sector"], *params),
                )
                row["comments"] = [r["comment"] for r in cur.fetchall()]
                del row["source_hash"]
        return rows

    def resolve(self, source: str, sectors: list[str] | None) -> bool:
        """Erase every report for the source within the caller's sectors.

        False when nothing was erased — outside the sector or never reported,
        deliberately the same answer.
        """
        sectors = _clean(sectors)
        if sectors is not None and not sectors:
            return False
        predicate, params = self._sector_predicate(sectors)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"DELETE FROM document_reports WHERE source_hash = %s "
                f"{predicate.replace('WHERE', 'AND', 1)}",
                (source_hash(source), *params),
            )
            return cur.rowcount > 0

    @staticmethod
    def _sector_predicate(sectors: list[str] | None) -> tuple[str, tuple]:
        """A WHERE clause over sectors, or nothing at all for None."""
        if sectors is None:
            return "", ()
        placeholders = ", ".join(["%s"] * len(sectors))
        return f"WHERE sector IN ({placeholders})", tuple(sectors)
