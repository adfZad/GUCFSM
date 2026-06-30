-- ============================================================================
-- GUC Field Service Bot — T-SQL Schema (SQL Server / Azure SQL)
-- Converted from SQLite (setup_db.py::EXPECTED_SCHEMA)
-- ============================================================================

-- ── master_units ──────────────────────────────────────────────────────────
-- Resident bot lookup: phone/TG ID → unit list
IF OBJECT_ID('dbo.master_units', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.master_units (
        id               INT IDENTITY(1,1) PRIMARY KEY,
        phone_number     NVARCHAR(30)   DEFAULT '',
        phone_display    NVARCHAR(50)   DEFAULT '',
        telegram_user_id NVARCHAR(50)   DEFAULT '',
        owner_name       NVARCHAR(255)  NULL,
        units            NVARCHAR(MAX)  NOT NULL
    );
END;
GO

-- ── submissions ───────────────────────────────────────────────────────────
-- Central workflow table. Written by both bots.
IF OBJECT_ID('dbo.submissions', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.submissions (
        id                      INT IDENTITY(1,1) PRIMARY KEY,
        telegram_user_id        NVARCHAR(50)   NULL,
        phone_number            NVARCHAR(30)   NOT NULL DEFAULT '',
        unit                    NVARCHAR(200)  NOT NULL,
        compound                NVARCHAR(100)  NULL,
        request_type            NVARCHAR(50)   NOT NULL,
        category                NVARCHAR(100)  NULL,
        service                 NVARCHAR(200)  NULL,
        sub_service             NVARCHAR(200)  NULL,
        issue_description       NVARCHAR(MAX)  NULL,
        photo_path              NVARCHAR(500)  NULL,
        photo_file_id           NVARCHAR(500)  NULL,
        status                  NVARCHAR(50)   DEFAULT 'submitted',
        priority                NVARCHAR(20)   DEFAULT 'normal',
        submitted_at            DATETIME2      DEFAULT SYSUTCDATETIME(),
        -- Approval & work completion
        required_approvals      INT            DEFAULT 2,
        work_done_by            NVARCHAR(50)   NULL,
        work_done_at            DATETIME2      NULL,
        work_done_note          NVARCHAR(MAX)  NULL,
        actual_cost             NVARCHAR(MAX)  NULL,
        completion_photo_path   NVARCHAR(500)  NULL,
        completion_photo_file_id NVARCHAR(500) NULL,
        closed_by               NVARCHAR(50)   NULL,
        closed_at               DATETIME2      NULL,
        close_note              NVARCHAR(MAX)  NULL,
        -- Cost estimate / confirmed (approver workflow)
        cost_estimate           NVARCHAR(MAX)  NULL,
        cost_confirmed          NVARCHAR(MAX)  NULL
    );
END;
GO

-- ── master_units_hierarchy ────────────────────────────────────────────────
-- Agent bot: compound/type/villa/building/flat with field agent assignment
IF OBJECT_ID('dbo.master_units_hierarchy', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.master_units_hierarchy (
        id              INT IDENTITY(1,1) PRIMARY KEY,
        compound        NVARCHAR(100)  NOT NULL,
        unit_type       NVARCHAR(20)   NOT NULL,    -- 'Villa' / 'Apartment'
        villa_number    NVARCHAR(50)   NULL,
        building_number NVARCHAR(50)   NULL,
        flat_number     NVARCHAR(50)   NULL,
        full_label      NVARCHAR(200)  NOT NULL,
        assigned_to     NVARCHAR(50)   NULL
    );
END;
GO

-- ── unit_agents ───────────────────────────────────────────────────────────
IF OBJECT_ID('dbo.unit_agents', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.unit_agents (
        id               INT IDENTITY(1,1) PRIMARY KEY,
        full_label       NVARCHAR(200) NOT NULL,
        telegram_user_id NVARCHAR(50)  NOT NULL
    );
END;
GO

-- ── services ──────────────────────────────────────────────────────────────
-- 3-level service hierarchy (main_category → category → sub_category)
IF OBJECT_ID('dbo.services', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.services (
        id            INT IDENTITY(1,1) PRIMARY KEY,
        main_category NVARCHAR(100) NOT NULL,
        category      NVARCHAR(100) NOT NULL,
        sub_category  NVARCHAR(200) NOT NULL
    );
END;
GO

-- ── form_state ────────────────────────────────────────────────────────────
-- Per-user conversation state (currently unused by runtime)
IF OBJECT_ID('dbo.form_state', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.form_state (
        telegram_user_id NVARCHAR(50)  PRIMARY KEY,
        current_step     NVARCHAR(50)  NOT NULL,
        data             NVARCHAR(MAX) DEFAULT '{}',
        updated_at       DATETIME2     DEFAULT SYSUTCDATETIME()
    );
END;
GO

-- ── agents ────────────────────────────────────────────────────────────────
-- Role registry. One row per (telegram_user_id, role, compound).
-- field_agent → compound = NULL. approver_1/2 → compound = compound name.
IF OBJECT_ID('dbo.agents', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.agents (
        id               INT IDENTITY(1,1) PRIMARY KEY,
        telegram_user_id NVARCHAR(50)  NOT NULL,
        name             NVARCHAR(100) NOT NULL,
        role             NVARCHAR(50)  NOT NULL DEFAULT 'field_agent',
        compound         NVARCHAR(100) NULL,
        active           BIT           DEFAULT 1
    );
END;
GO

-- ── approvals ─────────────────────────────────────────────────────────────
-- Immutable audit trail. One row per approval/rejection action.
IF OBJECT_ID('dbo.approvals', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.approvals (
        id            INT IDENTITY(1,1) PRIMARY KEY,
        submission_id INT           NOT NULL,
        level         INT           NOT NULL,         -- 1 or 2
        action        NVARCHAR(50)  NOT NULL,         -- 'approve' or 'reject'
        actor_id      NVARCHAR(50)  NOT NULL,
        actor_note    NVARCHAR(MAX) NULL,
        acted_at      DATETIME2     DEFAULT SYSUTCDATETIME()
    );
END;
GO

-- ── conversation_state ────────────────────────────────────────────────────
-- State persistence for Azure Functions (Phase 2). Stores PTB context data.
IF OBJECT_ID('dbo.conversation_state', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.conversation_state (
        entity_type NVARCHAR(10)   NOT NULL,   -- 'user' | 'chat' | 'bot' | 'conv'
        entity_id   NVARCHAR(255)  NOT NULL,
        data        NVARCHAR(MAX)  NOT NULL,   -- JSON blob
        updated_at  DATETIME2      DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_conversation_state PRIMARY KEY (entity_type, entity_id)
    );
END;
GO

-- ── Indexes ───────────────────────────────────────────────────────────────
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_submissions_user_status')
    CREATE INDEX IX_submissions_user_status
        ON dbo.submissions (telegram_user_id, status, compound);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_submissions_unit_status')
    CREATE INDEX IX_submissions_unit_status
        ON dbo.submissions (unit, status);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_agents_user_role')
    CREATE INDEX IX_agents_user_role
        ON dbo.agents (telegram_user_id, role, compound, active);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_hierarchy_full_label')
    CREATE INDEX IX_hierarchy_full_label
        ON dbo.master_units_hierarchy (full_label, assigned_to);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_hierarchy_assigned')
    CREATE INDEX IX_hierarchy_assigned
        ON dbo.master_units_hierarchy (assigned_to, compound);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_approvals_submission')
    CREATE INDEX IX_approvals_submission
        ON dbo.approvals (submission_id, level);
GO

PRINT '✓ Schema created successfully.';
