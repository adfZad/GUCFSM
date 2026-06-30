# GUC Field Service System — User Manual

**Version:** Phase 7 (Service Hierarchy)
**Last updated:** 2026-06-04

---

## Table of Contents

1. [Overview](#1-overview)
2. [Resident Bot — Submitting Requests](#2-resident-bot--submitting-requests)
   - 2.1 [Getting Started](#21-getting-started)
   - 2.2 [New Maintenance or Facilities Request](#22-new-maintenance-or-facilities-request)
   - 2.3 [Emergency Request](#23-emergency-request)
   - 2.4 [Follow Up on an Existing Request](#24-follow-up-on-an-existing-request)
3. [Agent Bot — Field Agents](#3-agent-bot--field-agents)
   - 3.1 [Getting Started](#31-getting-started)
   - 3.2 [Submitting a Ticket on Behalf of a Resident](#32-submitting-a-ticket-on-behalf-of-a-resident)
   - 3.3 [Viewing Your Assigned Tickets](#33-viewing-your-assigned-tickets)
   - 3.4 [Marking Work as Done](#34-marking-work-as-done)
4. [Agent Bot — Approvers](#4-agent-bot--approvers)
   - 4.1 [Getting Started](#41-getting-started)
   - 4.2 [Reviewing Pending Approvals](#42-reviewing-pending-approvals)
   - 4.3 [Approving a Ticket](#43-approving-a-ticket)
   - 4.4 [Rejecting a Ticket](#44-rejecting-a-ticket)
   - 4.5 [Viewing All Tickets](#45-viewing-all-tickets)
5. [Ticket Statuses Explained](#5-ticket-statuses-explained)
6. [Notifications — What to Expect](#6-notifications--what-to-expect)
7. [Master Data Management (Admin)](#7-master-data-management-admin)
   - 7.1 [Overview](#71-overview)
   - 7.2 [Adding a New Resident](#72-adding-a-new-resident)
   - 7.3 [Adding a New Unit or Compound](#73-adding-a-new-unit-or-compound)
   - 7.4 [Assigning a Field Agent to a Unit](#74-assigning-a-field-agent-to-a-unit)
   - 7.5 [Adding or Deactivating an Agent/Approver](#75-adding-or-deactivating-an-agentapprover)
   - 7.6 [Adding or Removing Services](#76-adding-or-removing-services)
   - 7.7 [Applying Master Data Changes](#77-applying-master-data-changes)

---

## 1. Overview

The GUC Field Service System consists of two Telegram bots:

| Bot | Who uses it | Purpose |
|-----|-------------|---------|
| **Resident Bot** | Residents / property owners | Submit service requests, report emergencies, follow up on tickets |
| **Agent Bot** | Field agents, Approver 1, Approver 2 | Create tickets, complete work, approve or reject requests |

Both bots connect to the same database so all parties see live data.

**Before you begin:** You must be registered in the system. Contact your system administrator if you cannot access the bot.

---

## 2. Resident Bot — Submitting Requests

### 2.1 Getting Started

1. Open Telegram and search for the resident bot (your administrator will provide the bot username).
2. Tap **Start** or type `/start`.
3. The bot asks for your **phone number**. Enter it exactly as registered (e.g. `+971501234567`). The system looks up your account by phone number.
4. If your number is found, the bot shows a list of **units linked to your account**. Tap the unit you are submitting the request for.
5. The **Main Menu** appears with three options:
   - **New Request** — for regular Maintenance or Facilities issues
   - **Follow Up** — to update an existing open ticket
   - **Emergency** — for urgent situations (no power, water overflow, etc.)

> **Tip:** If your phone number is not recognised, contact admin to verify your number is in the system.

---

### 2.2 New Maintenance or Facilities Request

**Step 1 — Choose request type:** Tap **New Request**.

**Step 2 — Choose main category:**
- **Maintenance** — issues inside your unit (AC, plumbing, electrical, carpentry, etc.)
- **Facilities** — shared areas (cleaning, landscaping, security, gym, pest control, etc.)

**Step 3 — Choose category:** A list of categories for your chosen type appears. Examples:
- Maintenance: Air Conditioning, Carpentry, Electrical, Plumbing, Painting, Appliances, Common Area
- Facilities: Cleaning, Garbage, Landscaping, Clubhouse / Gym, Recreation Area, Pest Control, Security

Tap the most relevant category.

**Step 4 — Choose specific service:** The sub-items for that category appear. Tap the one that best describes your issue. Examples for Air Conditioning: *AC Not Working*. Examples for Plumbing: *Leakage Issue*, *Broken Tap / Mixer*, *Drains Issues*.

**Step 5 — Describe the issue:** Type a short description of the problem (minimum 5 characters). Be as specific as possible — mention location, severity, how long the issue has been occurring.

**Step 6 — Attach a photo (optional):** Send a photo of the issue. This helps the field agent arrive prepared. Tap **Skip** if no photo is available.

**Step 7 — Review and confirm:** The bot shows a full summary:
```
Unit:        Diamond Villa 101
Request:     New Request
Category:    Maintenance
Service:     Air Conditioning — AC Not Working
Description: The living room AC has not cooled since yesterday evening.
Photo:       ✓ attached
Priority:    Normal
```
Tap **Confirm & Submit** to send the ticket, or **Cancel** to discard it.

**Step 8 — Confirmation:** The bot confirms with a ticket number (e.g. **Ticket #47**). The assigned field agent is notified automatically.

---

### 2.3 Emergency Request

Use this only for urgent situations that need immediate attention (no power, water overflow, fire alarm, etc.).

1. Tap **Emergency** from the Main Menu.
2. Describe the emergency briefly (e.g. "Complete power outage, all circuit breakers tripped").
3. Attach a photo if safe to do so, or tap **Skip**.
4. Confirm — the ticket is submitted with **high priority** and the emergency team is alerted immediately.

> **Important:** Do not use the Emergency option for routine issues. It triggers an urgent escalation.

---

### 2.4 Follow Up on an Existing Request

Use this to add notes to an existing open ticket — for example, to report that the issue has returned, that a previously scheduled visit was missed, or to request a progress update.

1. Tap **Follow Up** from the Main Menu.
2. Enter the **Ticket number** (e.g. `47`). You can find this in your original confirmation message.
3. Choose a **follow-up status**:
   - Issue persists
   - Work not completed
   - Awaiting parts / materials
   - Appointment not kept
   - Requesting update
   - Other
4. Type your follow-up note (minimum 5 characters).
5. Confirm — the follow-up is logged and the field agent is notified.

---

## 3. Agent Bot — Field Agents

### 3.1 Getting Started

1. Open Telegram and start the Agent Bot (your administrator will provide the bot username).
2. Tap **Start** or type `/start`.
3. The bot validates your Telegram account against the agents registry. If your account is not registered, contact admin.
4. The **Main Menu** appears with options based on your role:
   - **New Ticket** — create a ticket (field agents and above)
   - **My Tickets** — your open tickets where you are the assigned agent
   - **Pending Approvals** — tickets awaiting your approval (approvers only)
   - **All Tickets** — full ticket history across all statuses (approvers only)

---

### 3.2 Submitting a Ticket on Behalf of a Resident

Use this when a resident contacts you directly (by phone or in person) rather than using the Resident Bot.

**Step 1:** Tap **New Ticket**.

**Step 2 — Select compound:** Tap the compound where the unit is located (e.g. Diamond, Pearl, Sapphire).

**Step 3 — Select unit type:** Choose **Villa** or **Apartment**.

**Step 4 — Select unit:** Scroll and tap the specific unit (e.g. Villa 101, Flat 203).

**Step 5 — Request type:** Choose:
- **New Request** — standard service request
- **Follow Up** — updating an existing issue
- **Emergency** — urgent situation

**Step 6 — Category:** Choose **Maintenance** or **Facilities**.

**Step 7 — Specific service:** Select the category (e.g. Air Conditioning) then the sub-item (e.g. AC Not Working).

**Step 8 — Description:** Type the issue description. Include anything the resident told you verbally.

**Step 9 — Photo:** Attach a photo if you are at the location, or tap **Skip**.

**Step 10 — Confirm:** Review the summary and tap **Confirm & Submit**. The ticket is created and approvers at that compound are notified.

> **Note:** You will not receive your own notification for tickets you submit yourself.

---

### 3.3 Viewing Your Assigned Tickets

Tap **My Tickets** to see all tickets assigned to your units that are currently open or in progress.

- Each ticket shows: Ticket #, unit, service, status, and submission date.
- Tap a ticket to see full details including the description, photo, and approval history.

**Filtering:** Use the filter buttons at the top to narrow by compound or unit type.

---

### 3.4 Marking Work as Done

When you have completed the work on a ticket:

1. Open the ticket from **My Tickets**.
2. Tap **Mark as Done**.
3. Enter the **actual cost** of the work (materials + labour, or `0` if covered under contract).
4. Enter a **completion note** — what was done, parts replaced, etc.
5. Attach a **completion photo** (mandatory). This is the photo that approvers will review.
6. Confirm.

The ticket moves to **Work Done** status and the Approver 1 team is notified automatically.

---

## 4. Agent Bot — Approvers

There are two approval levels:
- **Approver 1** — first review, typically the supervisor or team lead
- **Approver 2** — final sign-off, typically management

Both levels use the same interface.

### 4.1 Getting Started

Start the Agent Bot as described in §3.1. If your account has an approver role, you will see **Pending Approvals** and **All Tickets** in your menu.

---

### 4.2 Reviewing Pending Approvals

Tap **Pending Approvals** to see all tickets currently awaiting your review.

Each entry shows: Ticket #, compound, unit, service, cost, and the date work was marked done.

Tap a ticket to see the full detail view:
```
Ticket #47
Unit:        Diamond Villa 101
Category:    Maintenance
Service:     Air Conditioning — AC Not Working
Description: Living room AC not cooling since yesterday.
Completed by: Riaz (2026-06-04 14:32)
Cost:        AED 350
Completion note: Replaced capacitor and re-gassed unit.
Completion photo: [photo attached]
```

---

### 4.3 Approving a Ticket

1. Open the ticket from **Pending Approvals**.
2. Tap **Approve**.
3. Add an optional note (e.g. "Approved — good turnaround time").
4. Confirm.

- If you are **Approver 1**: the ticket moves to `approved_1` and Approver 2 is notified.
- If you are **Approver 2**: the ticket moves to `closed`. The field agent who completed the work is notified.

---

### 4.4 Rejecting a Ticket

1. Open the ticket from **Pending Approvals**.
2. Tap **Reject**.
3. Enter a **rejection note** explaining why (mandatory — this is sent back to the field agent).
4. Confirm.

The ticket moves to `rejected` status and the field agent who completed the work is notified with your rejection note. They will need to re-visit and mark work done again after addressing the issue.

---

### 4.5 Viewing All Tickets

Tap **All Tickets** (Approver 2 only) to see the complete history across all statuses and compounds.

**Filtering options:**
- By compound
- By unit type (Villa / Apartment)
- By status (submitted, work_done, approved_1, closed, rejected)

Use this view for auditing, reporting, or checking the history of a specific unit.

---

## 5. Ticket Statuses Explained

| Status | Meaning |
|--------|---------|
| `submitted` | Ticket received, assigned agent notified |
| `work_done` | Field agent has completed the work and submitted a completion report |
| `approved_1` | Approved by Approver 1, awaiting Approver 2 sign-off |
| `closed` | Fully approved by both approvers — ticket complete |
| `rejected` | Rejected at any approval stage — field agent notified with reason |

---

## 6. Notifications — What to Expect

The system sends automatic Telegram messages at each stage of the lifecycle:

| Event | Who is notified |
|-------|----------------|
| New ticket submitted | All field agents assigned to that unit |
| Agent marks work done | All active Approver 1 for that compound |
| Approver 1 approves | All active Approver 2 for that compound |
| Approver 2 closes or rejects | Field agent who completed the work |
| Any approver rejects | Field agent who completed the work |

> **Note:** If you submit a ticket yourself (as a field agent), you will not receive a notification for your own submission.

Notifications are sent as plain text messages — no action is required from you in response to a notification; simply open the Agent Bot to take the next step.

---

## 7. Master Data Management (Admin)

This section is for administrators who maintain the system's reference data.

### 7.1 Overview

All master data is stored in the SQLite database at `/opt/field-service-bot/data/field_service.db`. Changes are made using SQL commands run inside the Docker container. No bot restart is required for most data changes — the bots query the database on every request.

**Connect to the database:**
```bash
docker exec -it field-service-bot sqlite3 /data/field_service.db
```

Once inside the SQLite shell, all commands below can be run directly. Type `.quit` to exit.

---

### 7.2 Adding a New Resident

Residents are looked up by phone number or Telegram user ID in the `master_units` table.

**Add a new resident:**
```sql
INSERT INTO master_units (phone_number, phone_display, owner_name, units)
VALUES ('971501234567', '+971 50 123 4567', 'Mohammed Al Hassan',
        '["Diamond Villa 205"]');
```

- `phone_number`: digits only, no spaces or dashes (the bot normalises input to this format)
- `phone_display`: the human-readable format shown in ticket details
- `units`: a JSON array of unit label strings — must exactly match the `full_label` values in `master_units_hierarchy`

**Link a resident to multiple units:**
```sql
INSERT INTO master_units (phone_number, phone_display, owner_name, units)
VALUES ('971501234567', '+971 50 123 4567', 'Mohammed Al Hassan',
        '["Diamond Villa 205", "Pearl Apartment B-301"]');
```

**Update an existing resident's units:**
```sql
UPDATE master_units
SET units = '["Diamond Villa 205", "Diamond Villa 206"]'
WHERE phone_number = '971501234567';
```

**Look up a resident:**
```sql
SELECT * FROM master_units WHERE phone_number = '971501234567';
```

---

### 7.3 Adding a New Unit or Compound

Units for the agent bot are stored in `master_units_hierarchy`.

**Add a villa:**
```sql
INSERT INTO master_units_hierarchy
    (compound, unit_type, villa_number, full_label, assigned_to)
VALUES ('Diamond', 'Villa', '210', 'Diamond Villa 210', NULL);
```

**Add an apartment:**
```sql
INSERT INTO master_units_hierarchy
    (compound, unit_type, building_number, flat_number, full_label, assigned_to)
VALUES ('Pearl', 'Apartment', 'B', '405', 'Pearl Apartment B-405', NULL);
```

- `compound`: must exactly match the compound name used throughout the system
- `full_label`: this is the display label shown in ticket details — use a consistent naming format
- `assigned_to`: leave `NULL` until a field agent is assigned (see §7.4)

**To add an entirely new compound**, simply insert units with the new compound name. The agent bot reads distinct compound values from this table dynamically — no code change needed.

---

### 7.4 Assigning a Field Agent to a Unit

The `assigned_to` column in `master_units_hierarchy` holds the Telegram user ID of the field agent responsible for that unit.

**Assign agent 8976446718 to Diamond Villa 210:**
```sql
UPDATE master_units_hierarchy
SET assigned_to = '8976446718'
WHERE full_label = 'Diamond Villa 210';
```

**Assign one agent to all units in a compound:**
```sql
UPDATE master_units_hierarchy
SET assigned_to = '8976446718'
WHERE compound = 'Diamond';
```

**Remove an assignment (unassign):**
```sql
UPDATE master_units_hierarchy
SET assigned_to = NULL
WHERE full_label = 'Diamond Villa 210';
```

> **Effect:** When a ticket is submitted for a unit, the system notifies the agent whose Telegram user ID is in `assigned_to`. Unassigned units generate no new-ticket notification.

---

### 7.5 Adding or Deactivating an Agent/Approver

People are managed in the `agents` table. Each person can have multiple rows (one per role).

**View all current agents:**
```sql
SELECT telegram_user_id, name, role, compound, active FROM agents ORDER BY name;
```

**Add a new field agent:**
```sql
INSERT INTO agents (telegram_user_id, name, role, compound, active)
VALUES ('9876543210', 'Ahmad Karimi', 'field_agent', NULL, 1);
```

**Add an approver (Approver 1 for Diamond compound):**
```sql
INSERT INTO agents (telegram_user_id, name, role, compound, active)
VALUES ('9876543210', 'Ahmad Karimi', 'approver_1', 'Diamond', 1);
```

- `role`: must be exactly `field_agent`, `approver_1`, or `approver_2`
- `compound`: use `NULL` for field agents; use the compound name for approvers
- To make someone an approver for multiple compounds, insert one row per compound

**Deactivate an agent (preferred over deleting):**
```sql
UPDATE agents SET active = 0 WHERE telegram_user_id = '9876543210' AND role = 'field_agent';
```

**Reactivate:**
```sql
UPDATE agents SET active = 1 WHERE telegram_user_id = '9876543210';
```

**Remove entirely (only if they have no ticket history):**
```sql
DELETE FROM agents WHERE telegram_user_id = '9876543210';
```

> **How to find a Telegram user ID:** Have the person send a message to the Agent Bot and search the bot logs: `tail -f /opt/field-service-bot/logs/agent_bot.log`. Their user ID is logged on first `/start`.

---

### 7.6 Adding or Removing Services

Services are stored in the `services` table with three levels: main_category → category → sub_category.

**View all current services:**
```sql
SELECT main_category, category, sub_category FROM services ORDER BY main_category, category, sub_category;
```

**Add a new sub-category to an existing category:**
```sql
INSERT INTO services (main_category, category, sub_category)
VALUES ('Maintenance', 'Electrical', 'Ceiling Fan');
```

**Add an entirely new category with its sub-items:**
```sql
INSERT INTO services (main_category, category, sub_category) VALUES
    ('Facilities', 'Swimming Pool', 'Pool Cleaning'),
    ('Facilities', 'Swimming Pool', 'Pool Equipment'),
    ('Facilities', 'Swimming Pool', 'Pump Issue');
```

**Remove a sub-category:**
```sql
DELETE FROM services WHERE main_category = 'Maintenance' AND category = 'Carpentry' AND sub_category = 'Curtain';
```

**Remove an entire category (all its sub-items):**
```sql
DELETE FROM services WHERE main_category = 'Facilities' AND category = 'Recreation Area';
```

> **Effect:** Changes to the `services` table take effect immediately for the next user interaction — no bot restart required. Existing tickets are not affected.

---

### 7.7 Applying Master Data Changes

Most data changes (agents, units, services) are read live from the database on every request, so **no restart is needed**.

The following operations DO require a bot restart because they change DB schema:

| Operation | Requires restart? |
|-----------|------------------|
| Add/update/delete rows in any table | No |
| Add a new DB column (schema change) | Yes — restart both bots |
| Reload `services.csv` from scratch | Run setup_db.py (see below) |

**Restart both bots after a schema change:**
```bash
docker exec field-service-bot supervisorctl restart all
```

**To fully reload services from the CSV** (only needed if you edit `services.csv` and want to replace all data):
```bash
# First, clear the existing data
docker exec -it field-service-bot sqlite3 /data/field_service.db "DELETE FROM services;"
# Then re-run the loader
docker exec field-service-bot python3 /app/setup_db.py
```

**Check bot status at any time:**
```bash
docker exec field-service-bot supervisorctl status
```

Expected output:
```
agent-bot    RUNNING   pid 12, uptime 0:05:33
resident-bot RUNNING   pid 13, uptime 0:05:33
```

**View live logs:**
```bash
# Resident bot
tail -f /opt/field-service-bot/logs/bot.log

# Agent bot
tail -f /opt/field-service-bot/logs/agent_bot.log
```

---

*For technical questions or system errors, refer to the Technical Manual (`TECHNICAL_MANUAL.md`) or contact the system administrator.*
