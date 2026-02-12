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
    created_at      TIMESTAMP       DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
