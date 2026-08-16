-- Conversation history. Applied at startup; every statement is IF NOT EXISTS
-- so a restart against an existing database is a no-op.
--
-- IF NOT EXISTS creates, it does not alter: adding a column here will not reach
-- a database that already has the table. Once this is deployed anywhere, a
-- change to these tables needs a real migration, not an edit to this file.

CREATE TABLE IF NOT EXISTS conversations (
    id           CHAR(36)    NOT NULL PRIMARY KEY,
    -- The Entra object id of the token that opened it. Every read and write
    -- filters on this column: it is the whole ownership model.
    owner_oid    VARCHAR(64) NOT NULL,
    created_at   TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_turn_at TIMESTAMP   NULL DEFAULT NULL,
    INDEX idx_conversations_owner (owner_oid, last_turn_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS turns (
    id                 BIGINT      NOT NULL AUTO_INCREMENT PRIMARY KEY,
    conversation_id    CHAR(36)    NOT NULL,
    -- 1-based position within the conversation. Unique per conversation so a
    -- concurrent second request cannot quietly produce two turn 3s.
    seq                INT         NOT NULL,
    question           TEXT        NOT NULL,
    -- What actually went to retrieval. Empty on a first turn, where the
    -- question is used as asked and no rewrite runs.
    rewritten_question TEXT        NOT NULL,
    answer             MEDIUMTEXT  NOT NULL,
    citations          JSON        NOT NULL,
    -- The sectors in force when the turn was answered, so an audit can tell
    -- what the caller could reach then rather than what they can reach now.
    sectors            JSON        NOT NULL,
    model              VARCHAR(64) NOT NULL,
    tokens_in          INT         NOT NULL DEFAULT 0,
    tokens_out         INT         NOT NULL DEFAULT 0,
    cost_usd           DECIMAL(12, 6) NOT NULL DEFAULT 0,
    -- False when the client disconnected part-way through a streamed answer.
    -- The user saw that much, so the turn is kept rather than dropped, and this
    -- says which of the two it is.
    complete           BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at         TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_turns_seq (conversation_id, seq),
    CONSTRAINT fk_turns_conversation FOREIGN KEY (conversation_id)
        REFERENCES conversations (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
