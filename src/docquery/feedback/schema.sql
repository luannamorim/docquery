-- Outdated-document reports. Applied at startup; every statement is IF NOT
-- EXISTS so a restart against an existing database is a no-op.
--
-- Lives in MySQL, never in the Qdrant payload: re-ingest wipes a source's
-- chunks, and a report must outlive the re-ingest it is asking for.
--
-- IF NOT EXISTS creates, it does not alter: adding a column here will not reach
-- a database that already has the table. Once this is deployed anywhere, a
-- change to these tables needs a real migration, not an edit to this file.

CREATE TABLE IF NOT EXISTS document_reports (
    id           BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
    -- The full source, for display. TEXT rather than VARCHAR: remote URIs run
    -- long, and the keying identity is the hash below, never this column.
    source       TEXT         NOT NULL,
    -- sha256 of source. A UNIQUE key over (source, reporter_oid) directly
    -- would exceed InnoDB's 3072-byte index budget under utf8mb4.
    source_hash  CHAR(64)     NOT NULL,
    -- The document's sector at report time. The review list filters on this
    -- column: it is the whole read-access model of the table.
    sector       VARCHAR(255) NOT NULL,
    reporter_oid VARCHAR(64)  NOT NULL,
    comment      VARCHAR(500) NOT NULL DEFAULT '',
    created_at   TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_reports_source_reporter (source_hash, reporter_oid),
    INDEX idx_reports_sector (sector, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
