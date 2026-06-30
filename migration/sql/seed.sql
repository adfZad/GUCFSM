-- ============================================================================
-- GUC Field Service Bot — T-SQL Seed Data
-- Idempotent — skips if data already exists.
-- ============================================================================

SET NOCOUNT ON;

-- ── Seed agents ───────────────────────────────────────────────────────────
-- Same users as setup_db.py::SEED_AGENTS (38 rows total)
IF NOT EXISTS (SELECT 1 FROM dbo.agents)
BEGIN
    -- Field agents (compound = NULL)
    INSERT INTO dbo.agents (telegram_user_id, name, role, compound, active)
    VALUES
        ('8976446718', 'Afsal Khan', 'field_agent', NULL, 1),
        ('8580506857', 'Riaz',       'field_agent', NULL, 1);

    -- Approver 1: Riaz + Fasil (all 9 compounds)
    DECLARE @c TABLE (compound NVARCHAR(100));
    INSERT INTO @c VALUES ('Cascade I'),('Cascade II'),('Ruby Compound'),
        ('Sapphire Compound'),('Diamond Compound'),('Pearl Compound'),
        ('Twin Villa'),('Ewan Compound'),('Najma Flat');

    INSERT INTO dbo.agents (telegram_user_id, name, role, compound, active)
    SELECT '8580506857', 'Riaz',  'approver_1', compound, 1 FROM @c;

    INSERT INTO dbo.agents (telegram_user_id, name, role, compound, active)
    SELECT '7228949233', 'Fasil', 'approver_1', compound, 1 FROM @c;

    -- Approver 2: Riaz + Shahbaz (all 9 compounds)
    INSERT INTO dbo.agents (telegram_user_id, name, role, compound, active)
    SELECT '8580506857', 'Riaz',    'approver_2', compound, 1 FROM @c;

    INSERT INTO dbo.agents (telegram_user_id, name, role, compound, active)
    SELECT '8767995042', 'Shahbaz', 'approver_2', compound, 1 FROM @c;

    PRINT '✓ Seeded 38 agent rows.';
END
ELSE
    PRINT 'ℹ agents table already populated — skipping seed.';
GO

-- ── Seed services (from services.csv) ─────────────────────────────────────
IF NOT EXISTS (SELECT 1 FROM dbo.services)
BEGIN
    INSERT INTO dbo.services (main_category, category, sub_category) VALUES
        ('Maintenance', 'Air Conditioning',   'AC Not Working'),
        ('Maintenance', 'Carpentry',          'Curtain'),
        ('Maintenance', 'Carpentry',          'Kitchen Cabinet'),
        ('Maintenance', 'Carpentry',          'Door related'),
        ('Maintenance', 'Carpentry',          'Window related'),
        ('Maintenance', 'Carpentry',          'Bedroom Cabinet'),
        ('Maintenance', 'Carpentry',          'Living room Cabinet'),
        ('Maintenance', 'Common Area',        'Building related'),
        ('Maintenance', 'Common Area',        'Elevator'),
        ('Maintenance', 'Common Area',        'Tiles'),
        ('Maintenance', 'Common Area',        'Parking tent repair'),
        ('Maintenance', 'Common Area',        'Fountain'),
        ('Maintenance', 'Electrical',         'Electrical Panel'),
        ('Maintenance', 'Electrical',         'Door Bell'),
        ('Maintenance', 'Electrical',         'Exhaust Fan'),
        ('Maintenance', 'Electrical',         'Lamp'),
        ('Maintenance', 'Electrical',         'Power Outlet Points'),
        ('Maintenance', 'Electrical',         'Tube light'),
        ('Maintenance', 'Electrical',         'Water Heater'),
        ('Maintenance', 'Plumbing',           'Bathtub/shower'),
        ('Maintenance', 'Plumbing',           'Broken Tap / Mixer'),
        ('Maintenance', 'Plumbing',           'Drains Issues'),
        ('Maintenance', 'Plumbing',           'Leakage Issue'),
        ('Maintenance', 'Plumbing',           'Water Seepage'),
        ('Maintenance', 'Plumbing',           'Water tank'),
        ('Maintenance', 'Painting',           'Full Paint'),
        ('Maintenance', 'Painting',           'Touch up'),
        ('Maintenance', 'Appliances',         'Cooker'),
        ('Maintenance', 'Appliances',         'Refrigerator'),
        ('Maintenance', 'Appliances',         'Washing Machine'),
        ('Maintenance', 'Appliances',         'Dish washer'),
        ('Facilities',   'Cleaning',          'Corridor / Stair'),
        ('Facilities',   'Cleaning',          'Elevator'),
        ('Facilities',   'Cleaning',          'Swimming Pool'),
        ('Facilities',   'Cleaning',          'Parking Area'),
        ('Facilities',   'Cleaning',          'General Cleaning issues'),
        ('Facilities',   'Garbage',           'Drum Issues'),
        ('Facilities',   'Garbage',           'Waste Collection'),
        ('Facilities',   'Clubhouse / Gym',   'Gym schedule'),
        ('Facilities',   'Clubhouse / Gym',   'Machine out of service'),
        ('Facilities',   'Clubhouse / Gym',   'Sauna Room'),
        ('Facilities',   'Landscaping',       'Changing Plants / tree'),
        ('Facilities',   'Landscaping',       'Cutting branches'),
        ('Facilities',   'Landscaping',       'Irrigation Pipeline'),
        ('Facilities',   'Recreation Area',   'Play area'),
        ('Facilities',   'Recreation Area',   'Playground'),
        ('Facilities',   'Recreation Area',   'Football / tennis court'),
        ('Facilities',   'Pest Control',      'Pest Control'),
        ('Facilities',   'Security',          'Security related issues');

    PRINT '✓ Seeded 49 service rows.';
END
ELSE
    PRINT 'ℹ services table already populated — skipping seed.';
GO

-- ── Seed master_units (from master_data.csv) ──────────────────────────────
IF NOT EXISTS (SELECT 1 FROM dbo.master_units)
BEGIN
    INSERT INTO dbo.master_units (phone_number, phone_display, telegram_user_id, owner_name, units)
    VALUES
        ('97455552345', '97455552345', '',          N'Ahmed Al-Thani',   N'["Villa 12","Villa 12A"]'),
        ('97455557890', '97455557890', '',          N'Fatima Hassan',    N'["Unit 301","Unit 302","Unit 303"]'),
        ('97455551111', '97455551111', '',          N'Khalid Mohammed',   N'["Flat 5B"]'),
        ('97455552222', '97455552222', '',          N'Sara Ibrahim',     N'["Office 7","Office 8","Warehouse C"]'),
        ('',            '',            '8580506857', N'Riazuddin M',      N'["Building A - Floor 1","Villa 20","Office Tower - Lobby"]'),
        ('',            '',            '8661070211', N'Khaja Bahauddin',  N'["Villa 50","Villa 51","Flat 101"]'),
        ('',            '',            '8767995042', N'Shahbaz Ahmed',    N'["Warehouse X","Office 12","Office 13"]');

    PRINT '✓ Seeded 7 master_unit rows.';
END
ELSE
    PRINT 'ℹ master_units table already populated — skipping seed.';
GO

PRINT '✓ Seed data complete.';
