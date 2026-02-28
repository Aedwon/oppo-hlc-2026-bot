-- ============================================================
-- OPPO HLC Discord Bot — Database Schema
-- ============================================================

CREATE TABLE IF NOT EXISTS guild_config (
    guild_id       BIGINT UNSIGNED NOT NULL,
    config_key     VARCHAR(64)     NOT NULL,
    config_value   TEXT            NOT NULL,
    PRIMARY KEY (guild_id, config_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ============================================================
-- Feature 1: Verification
-- ============================================================

CREATE TABLE IF NOT EXISTS teams (
    id          INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    guild_id    BIGINT UNSIGNED NOT NULL,
    team_name   VARCHAR(100)    NOT NULL,
    created_at  TIMESTAMP       DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_guild_team (guild_id, team_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS verified_users (
    id          INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    guild_id    BIGINT UNSIGNED NOT NULL,
    discord_id  BIGINT UNSIGNED NOT NULL,
    team_name   VARCHAR(100)    NOT NULL,
    game_uid    VARCHAR(50)     NOT NULL,
    server      VARCHAR(50)     NOT NULL,
    verified_at TIMESTAMP       DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_guild_user (guild_id, discord_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ============================================================
-- Feature 2: Ticketing
-- ============================================================

CREATE TABLE IF NOT EXISTS active_tickets (
    channel_id      BIGINT UNSIGNED PRIMARY KEY,
    guild_id        BIGINT UNSIGNED NOT NULL,
    creator_id      BIGINT UNSIGNED NOT NULL,
    category_key    VARCHAR(2)      NOT NULL,
    subject         VARCHAR(200)    DEFAULT NULL,
    claimed         BOOLEAN         NOT NULL DEFAULT FALSE,
    claimed_by      BIGINT UNSIGNED DEFAULT NULL,
    reminded_24h    BOOLEAN         NOT NULL DEFAULT FALSE,
    escalated_48h   BOOLEAN         NOT NULL DEFAULT FALSE,
    is_test         BOOLEAN         NOT NULL DEFAULT FALSE,
    added_users     JSON            DEFAULT NULL,
    created_at      TIMESTAMP       DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_guild (guild_id),
    INDEX idx_unclaimed (claimed, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS ticket_ratings (
    id              INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    guild_id        BIGINT UNSIGNED NOT NULL,
    ticket_name     VARCHAR(100)    NOT NULL,
    user_id         BIGINT UNSIGNED NOT NULL,
    user_name       VARCHAR(100)    NOT NULL,
    handler_id      BIGINT UNSIGNED DEFAULT NULL,
    stars           TINYINT UNSIGNED NOT NULL,
    remarks         TEXT            DEFAULT NULL,
    rated_at        TIMESTAMP       DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_guild (guild_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ============================================================
-- Feature 3: Scheduled Embeds
-- ============================================================

CREATE TABLE IF NOT EXISTS scheduled_embeds (
    id              INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    identifier      VARCHAR(10)     NOT NULL UNIQUE,
    guild_id        BIGINT UNSIGNED NOT NULL,
    channel_id      BIGINT UNSIGNED NOT NULL,
    user_id         BIGINT UNSIGNED NOT NULL,
    content         TEXT            DEFAULT NULL,
    embeds_json     MEDIUMTEXT      NOT NULL,
    components_json MEDIUMTEXT      DEFAULT NULL,
    schedule_for    DATETIME        NOT NULL,
    created_at      TIMESTAMP       DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_pending (schedule_for)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ============================================================
-- Feature 5: Auto-create Voice Channels
-- ============================================================

CREATE TABLE IF NOT EXISTS autocreate_vc_config (
    id              INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    guild_id        BIGINT UNSIGNED NOT NULL,
    trigger_channel_id BIGINT UNSIGNED NOT NULL,
    UNIQUE KEY uq_guild_channel (guild_id, trigger_channel_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS spawned_vcs (
    channel_id      BIGINT UNSIGNED PRIMARY KEY,
    guild_id        BIGINT UNSIGNED NOT NULL,
    owner_id        BIGINT UNSIGNED NOT NULL,
    team_name       VARCHAR(100)    DEFAULT NULL,
    created_at      TIMESTAMP       DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ============================================================
-- Feature 7: Thread-Role Links (auto-add on role assignment)
-- ============================================================

CREATE TABLE IF NOT EXISTS thread_role_links (
    id              INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    guild_id        BIGINT UNSIGNED NOT NULL,
    thread_id       BIGINT UNSIGNED NOT NULL,
    role_id         BIGINT UNSIGNED NOT NULL,
    channel_id      BIGINT UNSIGNED NOT NULL,
    created_at      TIMESTAMP       DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_thread_role (thread_id, role_id),
    INDEX idx_guild_role (guild_id, role_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ============================================================
-- Feature 8: Pending Ratings (survives bot restarts)
-- ============================================================

CREATE TABLE IF NOT EXISTS pending_ratings (
    id              INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    guild_id        BIGINT UNSIGNED NOT NULL,
    ticket_name     VARCHAR(100)    NOT NULL,
    handler_id      BIGINT UNSIGNED DEFAULT NULL,
    handler_mention VARCHAR(100)    DEFAULT NULL,
    is_test         BOOLEAN         NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMP       DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ============================================================
-- Feature 9: League Ops Manual Entries (verification fallback)
-- ============================================================

CREATE TABLE IF NOT EXISTS lops_entries (
    id              INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    guild_id        BIGINT UNSIGNED NOT NULL,
    uid             VARCHAR(50)     NOT NULL,
    server          VARCHAR(50)     NOT NULL,
    ign             VARCHAR(100)    NOT NULL,
    added_by        BIGINT UNSIGNED NOT NULL,
    created_at      TIMESTAMP       DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_guild_uid_server (guild_id, uid, server)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ============================================================
-- Feature 10: Match Management
-- ============================================================

CREATE TABLE IF NOT EXISTS match_sessions (
    id                  INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    guild_id            BIGINT UNSIGNED NOT NULL,
    channel_id          BIGINT UNSIGNED NOT NULL,
    marshal_id          BIGINT UNSIGNED NOT NULL,
    best_of             TINYINT UNSIGNED NOT NULL DEFAULT 3,
    status              ENUM('ongoing','checking_ack','ended') NOT NULL DEFAULT 'ongoing',
    is_disputed         BOOLEAN         NOT NULL DEFAULT FALSE,
    ack_start_time      DATETIME        DEFAULT NULL,
    dispute_start_time  DATETIME        DEFAULT NULL,
    total_dispute_seconds INT UNSIGNED  NOT NULL DEFAULT 0,
    last_message_id     BIGINT UNSIGNED DEFAULT NULL,
    started_at          TIMESTAMP       DEFAULT CURRENT_TIMESTAMP,
    ended_at            DATETIME        DEFAULT NULL,
    UNIQUE KEY uq_active_channel (channel_id, status),
    INDEX idx_guild_status (guild_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS match_games (
    id                  INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    session_id          INT UNSIGNED    NOT NULL,
    game_number         TINYINT UNSIGNED NOT NULL,
    result              VARCHAR(200)    NOT NULL,
    ack_team1           VARCHAR(50)     DEFAULT NULL,
    ack_team1_user      VARCHAR(100)    DEFAULT NULL,
    ack_team1_at        DATETIME        DEFAULT NULL,
    ack_team2           VARCHAR(50)     DEFAULT NULL,
    ack_team2_user      VARCHAR(100)    DEFAULT NULL,
    ack_team2_at        DATETIME        DEFAULT NULL,
    created_at          TIMESTAMP       DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_session (session_id),
    CONSTRAINT fk_game_session FOREIGN KEY (session_id) REFERENCES match_sessions(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ============================================================
-- Feature 11: Challonge Bracket Integration
-- ============================================================

CREATE TABLE IF NOT EXISTS challonge_brackets (
    id                  INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    guild_id            BIGINT UNSIGNED NOT NULL,
    channel_id          BIGINT UNSIGNED NOT NULL,
    tournament_slug     VARCHAR(200)    NOT NULL,
    tournament_name     VARCHAR(200)    NOT NULL,
    tournament_url      VARCHAR(500)    DEFAULT NULL,
    state               VARCHAR(50)     DEFAULT 'unknown',
    linked_by           BIGINT UNSIGNED NOT NULL,
    created_at          TIMESTAMP       DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_guild_channel (guild_id, channel_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
