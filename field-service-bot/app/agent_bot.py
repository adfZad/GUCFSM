#!/usr/bin/env python3
"""
Zad Field Service — Agent Bot (hierarchical unit selection + approver workflow).
Compound → Unit Type → Villa# or Building# → Flat# → Request form.
Same DB as end-user bot. Separate token.
"""
import html, json, logging, os, re, secrets, sys, time, traceback

import pyodbc
from db import get_db, validate_schema, insert_and_get_id
from persistence import AzureSqlPersistence
from blob_storage import upload_photo, delete_photo
from logging.handlers import RotatingFileHandler
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ConversationHandler, MessageHandler, filters, ContextTypes, TypeHandler
)

# ── Config ──────────────────────────────────────────────────────────
TOKEN = os.environ.get("AGENT_BOT_TOKEN", "")
if not TOKEN:
    _tf = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".agent_bot_token")
    if os.path.exists(_tf):
        with open(_tf) as f:
            TOKEN = f.read().strip()

DB_PATH   = os.environ.get("DB_PATH",    "/data/field_service.db")
LOG_DIR   = os.environ.get("LOG_DIR",    "/tmp")
os.makedirs(LOG_DIR, exist_ok=True)

# ── Logging ─────────────────────────────────────────────────────────
logger = logging.getLogger("agent-bot")
logger.setLevel(logging.INFO)
fh = RotatingFileHandler(os.path.join(LOG_DIR, "agent_bot.log"), maxBytes=5_000_000, backupCount=5)
fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(fh)
logger.addHandler(logging.StreamHandler(sys.stdout))

# ── Conversation states ─────────────────────────────────────────────
(COMPOUND, UNIT_TYPE, VILLA_NUMBER, BUILDING, FLAT,
 REQUEST_TYPE, CATEGORY, SERVICE, FACILITY_SERVICE,
 DESCRIPTION, PHOTO, CONFIRM,
 FOLLOWUP_ID, FOLLOWUP_STATUS, FOLLOWUP_NOTE,
 EMERGENCY_DESC, EMERGENCY_PHOTO, EMERGENCY_CONFIRM,
 # Existing-tickets flow
 MAIN_MENU, EX_FILTER, EX_COMPOUND, EX_UNIT_TYPE,
 EX_VILLA, EX_BUILDING, EX_FLAT,
 TICKET_LIST, TICKET_DETAIL,
 COMPLETE_COST, COMPLETE_CONFIRM,           # work completion: actual cost entry, confirm
 # Approver workflow
 APPROVAL_LIST, APPROVAL_DETAIL,
 APPROVAL_NOTE, _UNUSED_31,                 # _UNUSED_31 keeps indices stable
 COMPLETE_PHOTO,
 SUB_SERVICE,                              # 3rd level of service hierarchy
 # New workflow states
 ASSIGN_LIST, ASSIGN_DETAIL, ASSIGN_TECH, ASSIGN_PRIORITY,
 MY_JOBS_LIST, MY_JOB_DETAIL, TECH_INSPECT, TECH_INSPECT_TYPE, BOQ_PHOTO,
 QUALITY_LIST, QUALITY_DETAIL, QUALITY_NOTE,
 QUOTATION_LIST, QUOTATION_DETAIL, QUOTATION_NOTE) = range(48)

# ── Status display ───────────────────────────────────────────────────
STATUS_EMOJI = {
    "submitted":  "🆕",
    "in_progress":"🔄",
    "approved_1": "1️⃣",
    "approved":   "✅",
    "closed":     "🔒",
    "rejected":   "❌",
}
STATUS_LABEL = {
    "submitted":  "Submitted",
    "in_progress":"In Progress",
    "approved_1": "Approved (L1)",
    "approved":   "Approved — Ready for Work",
    "closed":     "Closed",
    "rejected":   "Rejected",
}
TICKETS_PER_PAGE = 8
COMPOUND_EMOJI = {"Diamond": "💎", "Pearl": "🦪", "Sapphire": "💠"}

FOLLOWUP_STATUSES = [
    "In Progress", "Completed", "Delayed", "Needs Parts",
    "Needs Supervisor", "Cannot Resolve"
]

# ── DB helpers ───────────────────────────────────────────────────────
def db():
    return get_db()

def sanitize(text: str) -> str:
    text = html.escape(text or "", quote=False)
    return text[:500]

def mk_buttons(items: list, prefix: str, cols: int = 1, cancel: bool = True,
               back_cb: str = None) -> InlineKeyboardMarkup:
    buttons = []
    row_list = []
    for i, item in enumerate(items):
        row_list.append(InlineKeyboardButton(str(item), callback_data=f"{prefix}:{i}"))
        if len(row_list) >= cols:
            buttons.append(row_list); row_list = []
    if row_list:
        buttons.append(row_list)
    nav = []
    if back_cb:
        nav.append(InlineKeyboardButton("🔙 Back", callback_data=back_cb))
    if cancel:
        nav.append(InlineKeyboardButton("❌ Cancel", callback_data="cancel"))
    if nav:
        buttons.append(nav)
    return InlineKeyboardMarkup(buttons)


# ── Role helpers ─────────────────────────────────────────────────────
def _get_user_roles(uid: str, username: str = None) -> dict:
    """Return dict of booleans for different roles for a given TG user ID or username."""
    conn = db()
    
    # Auto-link telegram_user_id if missing but username matches
    if username:
        # Telegram usernames are typically without '@' in the update object, but handle just in case
        username = username.lstrip('@')
        conn.execute(
            "UPDATE agents SET telegram_user_id=? WHERE telegram_user_id='' AND telegram_username=? COLLATE SQL_Latin1_General_CP1_CI_AS", 
            (uid, username)
        )
        conn.commit()

    rows = conn.execute(
        "SELECT DISTINCT role FROM agents WHERE telegram_user_id=? AND active=1", (uid,)
    ).fetchall()
    roles = {r["role"] for r in rows}

    is_field = bool(roles & {"field_agent", "technician"})
    is_supervisor = "supervisor" in roles
    is_management = bool(roles & {"senior_engineer", "facility_manager"})
    approver_roles = [r for r in ["approver_1", "approver_2"] if r in roles]
    is_approver = bool(approver_roles)
    
    # Backward-compat: agents in unit_agents but not agents table
    if not is_field and not (roles & {"approver_1", "approver_2", "supervisor", "senior_engineer", "facility_manager"}):
        count = conn.execute(
            "SELECT COUNT(*) FROM unit_agents WHERE telegram_user_id=?", (uid,)
        ).fetchone()[0]
        if count > 0:
            is_field = True
    conn.close()

    return {
        "is_field_agent": is_field,
        "is_supervisor": is_supervisor,
        "is_management": is_management,
        "is_approver": is_approver,
        "approver_roles": approver_roles,
        "has_access": is_field or is_supervisor or is_management or is_approver
    }


def _dynamic_main_menu_keyboard(context) -> InlineKeyboardMarkup:
    """Build main menu based on user roles stored in context.user_data."""
    buttons = []
    
    if context.user_data.get("is_supervisor"):
        buttons.append([InlineKeyboardButton("👨‍🔧 Assign Tickets", callback_data="main:assign")])
        buttons.append([InlineKeyboardButton("🔎 Quality Inspections", callback_data="main:quality_inspections")])
        buttons.append([InlineKeyboardButton("📋 All Tickets", callback_data="main:all_tickets")])
    
    if context.user_data.get("is_field_agent"):
        buttons.append([InlineKeyboardButton("🆕 New Ticket (Legacy)", callback_data="main:new")])
        if context.user_data.get("is_supervisor"):
            buttons.append([InlineKeyboardButton("📋 Existing Tickets (Legacy)", callback_data="main:existing")])
        else:
            buttons.append([InlineKeyboardButton("🛠️ My Assigned Jobs", callback_data="main:my_jobs")])

    if context.user_data.get("is_management"):
        buttons.append([InlineKeyboardButton("📝 Quotation Approvals", callback_data="main:quotations")])
        buttons.append([InlineKeyboardButton("🗂️ All Tickets", callback_data="main:all_tickets")])
        
    if context.user_data.get("is_approver"):
        buttons.append([InlineKeyboardButton("✅ Pending Approvals", callback_data="main:approvals")])
        if not context.user_data.get("is_management") and not context.user_data.get("is_supervisor"):
            buttons.append([InlineKeyboardButton("🗂️ All Tickets", callback_data="main:all_tickets")])
            
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(buttons)


# ── Notification helpers ─────────────────────────────────────────────
NOTIFICATIONS_ENABLED = os.environ.get("NOTIFICATIONS_ENABLED", "true").lower() != "false"

async def _notify(app, recipient_uid: str, message: str, photo_file_id: str = None):
    """Send a Markdown notification. Optionally attaches a photo via file_id."""
    if not NOTIFICATIONS_ENABLED:
        return
    try:
        if photo_file_id:
            await app.bot.send_photo(chat_id=recipient_uid, photo=photo_file_id,
                                     caption=message, parse_mode="Markdown")
        else:
            await app.bot.send_message(chat_id=recipient_uid, text=message, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Notification failed uid={recipient_uid}: {e}")

def _get_role_uids_for_compound(compound: str, role: str) -> list:
    conn = db()
    rows = conn.execute(
        "SELECT DISTINCT telegram_user_id FROM agents WHERE role=? AND compound=? AND active=1",
        (role, compound)
    ).fetchall()
    conn.close()
    return [r["telegram_user_id"] for r in rows]

def _get_agent_compounds(uid: str) -> list:
    conn = db()
    rows = conn.execute(
        "SELECT DISTINCT muh.compound FROM master_units_hierarchy muh "
        "JOIN unit_agents ua ON muh.full_label = ua.full_label "
        "WHERE ua.telegram_user_id=? AND muh.compound IS NOT NULL ORDER BY muh.compound",
        (uid,)
    ).fetchall()
    conn.close()
    return [r["compound"] for r in rows]

def _get_categories(main_category: str) -> list:
    conn = db()
    rows = conn.execute(
        "SELECT DISTINCT category FROM services WHERE main_category=? ORDER BY category",
        (main_category,)
    ).fetchall()
    conn.close()
    return [r["category"] for r in rows]

def _get_sub_categories(main_category: str, category: str) -> list:
    conn = db()
    rows = conn.execute(
        "SELECT sub_category FROM services WHERE main_category=? AND category=? ORDER BY sub_category",
        (main_category, category)
    ).fetchall()
    conn.close()
    return [r["sub_category"] for r in rows]


# ── Start ────────────────────────────────────────────────────────────
# ── Navigation helper ─────────────────────────────────────────────
async def _nav(query, confirmed: str, next_text: str, reply_markup=None):
    """Show confirmation as edit, then send next step as new message below."""
    try:
        await query.edit_message_text(confirmed, parse_mode="Markdown")
    except Exception:
        pass
    await query.message.reply_text(next_text, reply_markup=reply_markup, parse_mode="Markdown")


def _fmt_date(dt, chars=10):
    """Format a datetime or string to YYYY-MM-DD (chars=10) or YYYY-MM-DD HH:MM (chars=16)."""
    if dt is None:
        return ""
    if hasattr(dt, 'strftime'):
        s = dt.strftime("%Y-%m-%d %H:%M")
    else:
        s = str(dt)
    return s[:chars] if chars else s


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = str(user.id)
    username = user.username
    logger.info(f"Start: uid={uid} username={username} name={user.first_name}")

    roles = _get_user_roles(uid, username)
    if not roles["has_access"]:
        await update.message.reply_text(
            f"⛔ You are not authorized.\n\nYour Telegram ID is: `{uid}`\nYour username is: `@{username}`\nGive this info to your administrator to be added.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data["agent_uid"] = uid
    context.user_data.update(roles)

    await update.message.reply_text(
        f"👋 Welcome, {user.first_name}! What would you like to do?",
        reply_markup=_dynamic_main_menu_keyboard(context)
    )
    return MAIN_MENU


# ── Main menu ────────────────────────────────────────────────────────
def _compound_keyboard(uid: str, back_cb: str = "main:back") -> InlineKeyboardMarkup:
    compounds = _get_agent_compounds(uid)
    buttons = [
        [InlineKeyboardButton(f"{COMPOUND_EMOJI.get(c, '🏘️')} {c}", callback_data=f"compound:{c}")]
        for c in compounds
    ]
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data=back_cb)])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(buttons)

async def main_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    action = query.data.split(":", 1)[1]

    if action == "new":
        uid = context.user_data.get("agent_uid", str(update.effective_user.id))
        await query.edit_message_text("Select *Compound*:", reply_markup=_compound_keyboard(uid), parse_mode="Markdown")
        return COMPOUND

    if action == "existing":
        return await _show_compound_screen(query, context)

    if action == "approvals":
        context.user_data["tkt_page"] = 0
        context.user_data["approver_view"] = "pending"
        return await _render_approval_list(query, context)

    if action == "all_tickets":
        context.user_data["tkt_page"] = 0
        context.user_data["approver_view"] = "all"
        return await _render_approver_all_tickets(query, context)

    if action == "assign":
        context.user_data["tkt_page"] = 0
        return await _render_assign_list(query, context)

    if action == "quality_inspections":
        context.user_data["tkt_page"] = 0
        return await _render_quality_list(query, context)
        
    if action == "my_jobs":
        context.user_data["tkt_page"] = 0
        return await _render_my_jobs_list(query, context)
        
    if action == "quotations":
        context.user_data["tkt_page"] = 0
        return await _render_quotation_list(query, context)

    # main:back
    await query.edit_message_text(
        "👋 What would you like to do?",
        reply_markup=_dynamic_main_menu_keyboard(context)
    )
    return MAIN_MENU


# ── Existing tickets — filter helpers ───────────────────────────────
def _clear_filter(context):
    for k in ("f_compound", "f_unit"):
        context.user_data.pop(k, None)

def _filter_label(context) -> str:
    unit = context.user_data.get("f_unit")
    comp = context.user_data.get("f_compound")
    if unit: return unit
    if comp: return f"All in {comp}"
    return "All Units"

def _build_ticket_query(uid: str, context) -> tuple:
    unit = context.user_data.get("f_unit")
    comp = context.user_data.get("f_compound")

    if unit:
        return (
            "SELECT id, unit, service, status, submitted_at FROM submissions "
            "WHERE unit=? ORDER BY submitted_at DESC",
            [unit]
        )
    subq, params = "SELECT full_label FROM unit_agents WHERE telegram_user_id=?", [uid]
    if comp:
        subq = (
            "SELECT ua.full_label FROM unit_agents ua "
            "JOIN master_units_hierarchy muh ON ua.full_label=muh.full_label "
            "WHERE ua.telegram_user_id=? AND muh.compound=?"
        )
        params.append(comp)
    return (
        f"SELECT id, unit, service, status, submitted_at FROM submissions "
        f"WHERE unit IN ({subq}) ORDER BY submitted_at DESC",
        params
    )

def _unit_list_keyboard(uid: str, compound: str, prefix: str, back_cb: str) -> InlineKeyboardMarkup:
    """Build a unit selection keyboard for a compound (flat list of full_label values)."""
    conn = db()
    rows = conn.execute(
        "SELECT DISTINCT muh.full_label FROM master_units_hierarchy muh "
        "JOIN unit_agents ua ON muh.full_label = ua.full_label "
        "WHERE ua.telegram_user_id=? AND muh.compound=? ORDER BY muh.full_label",
        (uid, compound)
    ).fetchall()
    conn.close()
    buttons = [[InlineKeyboardButton(r["full_label"], callback_data=f"{prefix}:{r['full_label']}")] for r in rows]
    buttons.append([InlineKeyboardButton(f"📋 All in {compound}", callback_data="ex_show:now")])
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data=back_cb),
                    InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(buttons)

async def _show_compound_screen(query, context) -> int:
    uid = context.user_data.get("agent_uid", str(query.from_user.id))
    _clear_filter(context)
    context.user_data["tkt_page"] = 0
    compounds = _get_agent_compounds(uid)
    buttons = [
        [InlineKeyboardButton(f"{COMPOUND_EMOJI.get(c, '🏘️')} {c}", callback_data=f"compound:{c}")]
        for c in compounds
    ]
    buttons.append([InlineKeyboardButton("📋 All Units", callback_data="ex_show:now")])
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="main:back"),
                    InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
    await query.edit_message_text(
        "📋 **Existing Tickets** — select compound:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown"
    )
    return EX_COMPOUND


async def ex_filter_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    return await _show_compound_screen(query, context)


async def ex_compound_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    compound = query.data.split(":", 1)[1]
    context.user_data["f_compound"] = compound
    context.user_data.pop("f_unit", None)
    uid = context.user_data.get("agent_uid", str(update.effective_user.id))
    await query.edit_message_text(
        f"**{compound}** — select unit:",
        reply_markup=_unit_list_keyboard(uid, compound, "ex_unit", "ex_utype:back"),
        parse_mode="Markdown"
    )
    return EX_UNIT_TYPE


async def ex_unit_type_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    action = query.data.split(":", 1)[1]

    if action == "back":
        return await _show_compound_screen(query, context)

    # action is the full_label of the selected unit
    context.user_data["f_unit"] = action
    context.user_data["tkt_page"] = 0
    return await _render_ticket_list(query, context)


async def ex_show_now_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    context.user_data["tkt_page"] = 0
    return await _render_ticket_list(query, context)


# ── Agent ticket list ────────────────────────────────────────────────
async def _render_ticket_list(query, context: ContextTypes.DEFAULT_TYPE):
    uid = context.user_data.get("agent_uid", str(query.from_user.id))
    page = context.user_data.get("tkt_page", 0)

    sql, params = _build_ticket_query(uid, context)
    conn = db()
    rows = conn.execute(sql, params).fetchall()
    conn.close()

    total = len(rows)
    total_pages = max(1, (total + TICKETS_PER_PAGE - 1) // TICKETS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    context.user_data["tkt_page"] = page

    if not total:
        await query.edit_message_text(
            "📋 No tickets found for this filter.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data="tkt_nav:back")]
            ])
        )
        return TICKET_LIST

    page_rows = rows[page * TICKETS_PER_PAGE:(page + 1) * TICKETS_PER_PAGE]
    buttons = []
    for r in page_rows:
        date = _fmt_date(r["submitted_at"], 10)
        emoji = STATUS_EMOJI.get(r["status"], "❓")
        svc = (r["service"] or "?")[:18]
        status_text = (r["status"] or "unknown").replace("_", " ").title()
        buttons.append([InlineKeyboardButton(f"{emoji} #{r['id']} | {svc} | {status_text} | {date}", callback_data=f"tkt:{r['id']}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data="tkt_nav:prev"))
    nav.append(InlineKeyboardButton(f"📄 {page + 1}/{total_pages}", callback_data="tkt_nav:info"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data="tkt_nav:next"))
    buttons.append(nav)
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="tkt_nav:back")])

    scope_label = _filter_label(context)
    await query.edit_message_text(
        f"📋 **Tickets — {scope_label}**\n{total} ticket(s), page {page + 1}/{total_pages}:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown"
    )
    return TICKET_LIST


async def ticket_list_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    action = query.data.split(":", 1)[1]

    if action == "prev":
        context.user_data["tkt_page"] = max(0, context.user_data.get("tkt_page", 0) - 1)
        return await _render_ticket_list(query, context)
    if action == "next":
        context.user_data["tkt_page"] = context.user_data.get("tkt_page", 0) + 1
        return await _render_ticket_list(query, context)
    if action == "info":
        return TICKET_LIST
    if action == "back":
        return await _show_compound_screen(query, context)
    return TICKET_LIST


async def ticket_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    tid = int(query.data.split(":", 1)[1])
    context.user_data["view_ticket_id"] = tid
    return await _render_agent_ticket_detail(query, context, tid)


async def _render_agent_ticket_detail(query, context, tid: int):
    conn = db()
    row = conn.execute("SELECT * FROM submissions WHERE id=?", (tid,)).fetchone()
    conn.close()

    if not row:
        await query.edit_message_text("❌ Ticket not found.")
        return TICKET_LIST

    status = row["status"] or "submitted"
    emoji = STATUS_EMOJI.get(status, "❓")
    label = STATUS_LABEL.get(status, status)
    photo = "📎 Attached" if row["photo_file_id"] else "None"
    date = _fmt_date(row["submitted_at"], 16)

    sub_svc = row["sub_service"] or ""
    svc_line = (row["service"] or "N/A") + (f" — {sub_svc}" if sub_svc else "")
    text = (
        f"🎫 **Ticket #{row['id']}**\n\n"
        f"🏠 Unit: {row['unit']}\n"
        f"📂 Type: {row['request_type']}\n"
        f"🏗️ Category: {row['category'] or 'N/A'}\n"
        f"🔧 Service: {svc_line}\n"
        f"📝 Issue: {row['issue_description'] or 'N/A'}\n"
        f"📸 Photo: {photo}\n"
        f"⚡ Status: {emoji} {label}\n"
        f"🔢 Priority: {row['priority'] or 'normal'}\n"
        f"📅 Submitted: {date}"
    )
    if row["work_done_note"]:
        text += f"\n✅ Completion note: {row['work_done_note']}"
    if row["actual_cost"]:
        text += f"\n💰 Actual cost: {row['actual_cost']}"
    if status == "rejected":
        text += "\n\n⚠️ Rejected — you can re-submit for approval."

    buttons = []
    if status == "approved":
        buttons.append([InlineKeyboardButton("✅ Complete Work", callback_data="tkt_action:complete")])
    elif status == "rejected":
        buttons.append([InlineKeyboardButton("🔄 Re-submit for Approval", callback_data="tkt_action:resubmit")])
    buttons.append([InlineKeyboardButton("🔙 Back to List", callback_data="tkt_action:back")])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
    return TICKET_DETAIL


async def ticket_detail_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    action = query.data.split(":", 1)[1]

    if action == "back":
        return await _render_ticket_list(query, context)

    if action == "complete":
        tid = context.user_data.get("view_ticket_id", "?")
        await query.edit_message_text(
            f"✅ **Complete Ticket #{tid}**\n\nEnter the actual cost (required):",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data="complete:back"),
                 InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
            ]),
            parse_mode="Markdown"
        )
        return COMPLETE_COST

    if action == "resubmit":
        tid = context.user_data.get("view_ticket_id", "?")
        await query.edit_message_text(
            f"🔄 **Re-submit Ticket #{tid} for Approval?**\n\nThis will send it back to Approver 1.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Re-submit", callback_data="tkt_action:resubmit_confirm"),
                 InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
            ]),
            parse_mode="Markdown"
        )
        return TICKET_DETAIL

    if action == "resubmit_confirm":
        tid = context.user_data["view_ticket_id"]
        uid = context.user_data.get("agent_uid", str(update.effective_user.id))
        try:
            conn = db()
            row = conn.execute("SELECT compound, unit, service FROM submissions WHERE id=?", (tid,)).fetchone()
            conn.execute("UPDATE submissions SET status='submitted' WHERE id=?", (tid,))
            conn.commit()
            conn.close()
            logger.info(f"Ticket #{tid} re-submitted by uid={uid}")
            if row and row["compound"]:
                for auid in _get_role_uids_for_compound(row["compound"], "approver_1"):
                    await _notify(context.application, auid,
                        f"🔄 *Ticket #{tid} re-submitted for review*\n🏠 {row['unit']} / {row['service'] or 'N/A'}")
        except Exception as e:
            logger.error(f"re-submit failed: {e}")
        await query.edit_message_text(
            f"✅ **Ticket #{tid} re-submitted for approval.**\n\nSay **hi** to continue.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    return TICKET_DETAIL


# ── Complete Work flow (approved tickets: actual cost + mandatory photo) ──────
async def complete_cost_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """COMPLETE_COST: text input for actual cost, or back callback."""
    if update.callback_query:
        query = update.callback_query; await query.answer()
        # back → return to ticket detail
        tid = context.user_data.get("view_ticket_id")
        if tid:
            return await _render_agent_ticket_detail(query, context, tid)
        await query.edit_message_text("👋 What would you like to do?", reply_markup=_dynamic_main_menu_keyboard(context))
        return MAIN_MENU
    cost = sanitize(update.message.text.strip())
    if not cost:
        await update.message.reply_text("⚠️ Cost is required. Enter the actual cost (e.g. 350 AED):")
        return COMPLETE_COST
    context.user_data["complete_cost"] = cost
    tid = context.user_data.get("view_ticket_id", "?")
    await update.message.reply_text(
        f"📸 **Ticket #{tid} — Upload Completion Photo**\n\nA photo is required to close this ticket:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back", callback_data="complete:back_photo"),
             InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
        ]),
        parse_mode="Markdown"
    )
    return COMPLETE_PHOTO


async def complete_photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """COMPLETE_PHOTO: mandatory photo upload, or back callback."""
    if update.callback_query:
        query = update.callback_query; await query.answer()
        action = query.data.split(":", 1)[1]
        if action == "back_photo":
            tid = context.user_data.get("view_ticket_id", "?")
            await query.edit_message_text(
                f"✅ **Complete Ticket #{tid}**\n\nEnter the actual cost (required):",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Back", callback_data="complete:back"),
                     InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
                ]),
                parse_mode="Markdown"
            )
            return COMPLETE_COST
        return COMPLETE_PHOTO

    # Photo received
    photo = update.message.photo[-1]
    ok, url = await _save_photo(photo, "COMPLETE", context,
                                key_path="complete_photo_path",
                                key_fid="complete_photo_file_id")
    if not ok:
        await update.message.reply_text("❌ Photo upload failed. Please try again:")
        return COMPLETE_PHOTO
    await update.message.reply_text("📸 Photo received!")

    tid = context.user_data.get("view_ticket_id", "?")
    cost = context.user_data.get("complete_cost", "")
    await update.message.reply_text(
        f"✅ **Confirm Closure — Ticket #{tid}**\n\n💰 Actual cost: _{sanitize(cost)}_\n📸 Photo: Attached\n\nClose this ticket?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirm & Close", callback_data="complete_confirm:yes"),
             InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
        ]),
        parse_mode="Markdown"
    )
    return COMPLETE_CONFIRM


async def complete_confirm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """COMPLETE_CONFIRM: final confirmation to close the ticket."""
    query = update.callback_query; await query.answer()
    uid = context.user_data.get("agent_uid", str(update.effective_user.id))
    tid = context.user_data["view_ticket_id"]
    cost = context.user_data.get("complete_cost", "")
    photo_path = context.user_data.get("complete_photo_path")
    photo_file_id = context.user_data.get("complete_photo_file_id")

    try:
        conn = db()
        conn.execute(
            "UPDATE submissions SET status='closed', work_done_by=?, work_done_at=SYSUTCDATETIME(), "
            "actual_cost=?, completion_photo_path=?, completion_photo_file_id=? WHERE id=?",
            (uid, cost, photo_path, photo_file_id, tid)
        )
        conn.commit()
        conn.close()
        logger.info(f"Ticket #{tid} closed by uid={uid}, cost={cost}")
    except Exception as e:
        logger.error(f"close failed: {e}\n{traceback.format_exc()}")
        await query.edit_message_text("❌ Update failed. Try again.")
        return COMPLETE_CONFIRM

    # Notify approver_1 and approver_2 with full summary
    try:
        n_conn = db()
        tkt = n_conn.execute("SELECT compound, unit, service FROM submissions WHERE id=?", (tid,)).fetchone()
        n_conn.close()
        if tkt and tkt["compound"]:
            compound = tkt["compound"]
            unit = tkt["unit"] or "?"
            service = tkt["service"] or "N/A"
            summary = (
                f"🔒 *Ticket #{tid} Closed*\n"
                f"🏠 {unit} / {service}\n"
                f"💰 Actual cost: {sanitize(cost)}"
            )
            notified = set()
            for role in ("approver_1", "approver_2"):
                for auid in _get_role_uids_for_compound(compound, role):
                    if auid not in notified:
                        await _notify(context.application, auid, summary, photo_file_id=photo_file_id)
                        notified.add(auid)
    except Exception as e:
        logger.error(f"close notify failed: {e}")

    await query.edit_message_text(
        f"🔒 **Ticket #{tid} closed successfully.**\n\nSay **hi** to continue.",
        parse_mode="Markdown"
    )
    for k in ("complete_cost", "complete_photo_path", "complete_photo_file_id", "view_ticket_id"):
        context.user_data.pop(k, None)
    return ConversationHandler.END


# ── Approver workflow ────────────────────────────────────────────────
def _build_pending_approvals_query(uid: str, context) -> tuple:
    """Build SQL for tickets pending this approver's action (combines L1 and L2)."""
    approver_roles = context.user_data.get("approver_roles", [])
    clauses, params = [], []
    if "approver_1" in approver_roles:
        clauses.append(
            "(s.status='submitted' AND s.compound IN "
            "(SELECT compound FROM agents WHERE telegram_user_id=? AND role='approver_1' AND active=1))"
        )
        params.append(uid)
    if "approver_2" in approver_roles:
        clauses.append(
            "(s.status='approved_1' AND s.compound IN "
            "(SELECT compound FROM agents WHERE telegram_user_id=? AND role='approver_2' AND active=1))"
        )
        params.append(uid)
    if not clauses:
        return ("SELECT id, unit, service, status, submitted_at FROM submissions WHERE 1=0", [])
    return (
        f"SELECT DISTINCT s.id, s.unit, s.service, s.status, s.submitted_at "
        f"FROM submissions s WHERE {' OR '.join(clauses)} ORDER BY s.submitted_at DESC",
        params
    )


async def _render_approval_list(query, context):
    uid = context.user_data.get("agent_uid", str(query.from_user.id))
    page = context.user_data.get("tkt_page", 0)

    sql, params = _build_pending_approvals_query(uid, context)
    conn = db()
    rows = conn.execute(sql, params).fetchall()
    conn.close()

    total = len(rows)
    total_pages = max(1, (total + TICKETS_PER_PAGE - 1) // TICKETS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    context.user_data["tkt_page"] = page

    if not total:
        await query.edit_message_text(
            "✅ No tickets pending your approval.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data="approval_nav:back")]
            ])
        )
        return APPROVAL_LIST

    page_rows = rows[page * TICKETS_PER_PAGE:(page + 1) * TICKETS_PER_PAGE]
    buttons = []
    for r in page_rows:
        date = _fmt_date(r["submitted_at"], 10)
        emoji = STATUS_EMOJI.get(r["status"], "❓")
        svc = (r["service"] or "?")[:18]
        status_text = (r["status"] or "unknown").replace("_", " ").title()
        buttons.append([InlineKeyboardButton(
            f"{emoji} #{r['id']} | {svc} | {status_text} | {date}",
            callback_data=f"appr_tkt:{r['id']}"
        )])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data="approval_nav:prev"))
    nav.append(InlineKeyboardButton(f"📄 {page+1}/{total_pages}", callback_data="approval_nav:info"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data="approval_nav:next"))
    buttons.append(nav)
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="approval_nav:back")])

    await query.edit_message_text(
        f"✅ **Pending Approvals**\n{total} ticket(s) awaiting your action:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown"
    )
    return APPROVAL_LIST


async def approval_list_nav_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    action = query.data.split(":", 1)[1]

    if action == "prev":
        context.user_data["tkt_page"] = max(0, context.user_data.get("tkt_page", 0) - 1)
        return await _render_approval_list(query, context)
    if action == "next":
        context.user_data["tkt_page"] = context.user_data.get("tkt_page", 0) + 1
        return await _render_approval_list(query, context)
    if action == "info":
        return APPROVAL_LIST
    if action == "back":
        await query.edit_message_text(
            "👋 What would you like to do?",
            reply_markup=_dynamic_main_menu_keyboard(context)
        )
        return MAIN_MENU
    return APPROVAL_LIST


async def approval_ticket_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """appr_tkt:{id} — open a pending ticket for approval."""
    query = update.callback_query; await query.answer()
    tid = int(query.data.split(":", 1)[1])
    return await _render_approval_detail(query, context, tid)


async def _render_approval_detail(query, context, tid: int):
    conn = db()
    row = conn.execute("SELECT * FROM submissions WHERE id=?", (tid,)).fetchone()
    conn.close()

    if not row:
        await query.edit_message_text("❌ Ticket not found.")
        return APPROVAL_LIST

    status = row["status"]
    approver_roles = context.user_data.get("approver_roles", [])
    is_l1 = (status == "submitted"  and "approver_1" in approver_roles)
    is_l2 = (status == "approved_1" and "approver_2" in approver_roles)

    if not is_l1 and not is_l2:
        await query.edit_message_text(
            "❌ This ticket is not currently pending your approval.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data="approval_nav:back")]
            ])
        )
        return APPROVAL_LIST

    level_label = "Level 1 Review" if is_l1 else "Level 2 Review"
    photo = "📎 Attached" if row["photo_file_id"] else "None"

    sub_svc = row["sub_service"] or ""
    svc_line = (row["service"] or "N/A") + (f" — {sub_svc}" if sub_svc else "")
    text = (
        f"🎫 **Ticket #{row['id']}** — {level_label}\n\n"
        f"🏠 Unit: {row['unit']}\n"
        f"📂 Type: {row['request_type']}\n"
        f"🔧 Service: {svc_line}\n"
        f"📝 Issue: {row['issue_description'] or 'N/A'}\n"
        f"📸 Photo: {photo}\n"
        f"📅 Submitted: {_fmt_date(row['submitted_at'], 16)}"
    )

    context.user_data["approval_tid"] = tid
    context.user_data["approval_level"] = 1 if is_l1 else 2

    buttons = [
        [InlineKeyboardButton("✅ Approve", callback_data="approval:approve"),
         InlineKeyboardButton("❌ Reject",  callback_data="approval:reject")],
        [InlineKeyboardButton("🔙 Back", callback_data="approval:back")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
    return APPROVAL_DETAIL


async def approval_detail_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    action = query.data.split(":", 1)[1]

    if action == "back":
        context.user_data["tkt_page"] = 0
        return await _render_approval_list(query, context)

    tid = context.user_data["approval_tid"]
    level = context.user_data["approval_level"]
    context.user_data["approval_action"] = action

    if action == "approve" and level == 1:
        await query.edit_message_text(
            f"✅ **Approve Ticket #{tid}** (Level 1)\n\nAdd approval note (optional):",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⏭️ Skip", callback_data="approval_note:skip")],
                [InlineKeyboardButton("🔙 Back", callback_data="approval_note:back"),
                 InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
            ]),
            parse_mode="Markdown"
        )
        return APPROVAL_NOTE

    elif action == "approve":
        # Level 2: no cost step
        await query.edit_message_text(
            f"✅ **Approve Ticket #{tid}** (Level 2 — Final)\n\nAdd approval note (optional):",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⏭️ Skip", callback_data="approval_note:skip")],
                [InlineKeyboardButton("🔙 Back", callback_data="approval_note:back"),
                 InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
            ]),
            parse_mode="Markdown"
        )
        return APPROVAL_NOTE

    else:  # reject
        await query.edit_message_text(
            f"❌ **Reject Ticket #{tid}**\n\nEnter rejection reason (required, min 5 chars):",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data="approval_note:back"),
                 InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
            ]),
            parse_mode="Markdown"
        )
        return APPROVAL_NOTE


async def approval_note_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """APPROVAL_NOTE: text note or skip/back callback."""
    is_reject = (context.user_data.get("approval_action") == "reject")
    tid = context.user_data["approval_tid"]

    uid = context.user_data.get("agent_uid", str(update.effective_user.id))

    if update.callback_query:
        query = update.callback_query; await query.answer()
        action = query.data.split(":", 1)[1]
        if action == "back":
            return await _render_approval_detail(query, context, tid)
        # skip (only valid for approve)
        if not is_reject:
            context.user_data["approval_note"] = ""
            return await _do_write_approval(uid, context, query.edit_message_text)
        return APPROVAL_NOTE
    else:
        note = sanitize(update.message.text.strip())
        if is_reject and len(note) < 5:
            await update.message.reply_text("⚠️ Rejection reason must be at least 5 characters:")
            return APPROVAL_NOTE
        context.user_data["approval_note"] = note
        return await _do_write_approval(uid, context, update.message.reply_text)


async def _do_write_approval(uid, context, reply_fn):
    """Write approval/rejection to DB and send confirmation."""
    tid = context.user_data["approval_tid"]
    level = context.user_data["approval_level"]
    action = context.user_data["approval_action"]
    note = context.user_data.get("approval_note", "")

    try:
        conn = db()
        conn.execute(
            "INSERT INTO approvals (submission_id, level, action, actor_id, actor_note) VALUES (?,?,?,?,?)",
            (tid, level, action, uid, note)
        )
        if action == "approve":
            if level == 1:
                conn.execute("UPDATE submissions SET status='approved_1' WHERE id=?", (tid,))
            else:
                conn.execute("UPDATE submissions SET status='approved' WHERE id=?", (tid,))
        else:
            conn.execute("UPDATE submissions SET status='rejected' WHERE id=?", (tid,))
        conn.commit()
        conn.close()
        logger.info(f"Ticket #{tid} {action}d at level {level} by uid={uid}")
    except Exception as e:
        logger.error(f"Approval write failed: {e}\n{traceback.format_exc()}")
        await reply_fn("❌ Update failed. Try again.")
        return APPROVAL_DETAIL

    try:
        n_conn = db()
        tkt = n_conn.execute(
            "SELECT compound, unit, service, telegram_user_id FROM submissions WHERE id=?", (tid,)
        ).fetchone()
        n_conn.close()
        if tkt:
            compound = tkt["compound"] or ""
            unit = tkt["unit"] or "?"
            service = tkt["service"] or "N/A"
            submitter = tkt["telegram_user_id"]
            if action == "approve" and level == 1 and compound:
                for auid in _get_role_uids_for_compound(compound, "approver_2"):
                    await _notify(context.application, auid,
                        f"1️⃣ *Ticket #{tid} approved (L1), needs your review*\n🏠 {unit} / {service}")
            elif action == "approve" and level == 2:
                # Notify submitter/field agent (they are the same person)
                if submitter:
                    await _notify(context.application, submitter,
                        f"✅ *Ticket #{tid} approved — ready for work*\n🏠 {unit} / {service}")
                # Notify Approver_1
                if compound:
                    for auid in _get_role_uids_for_compound(compound, "approver_1"):
                        if auid != submitter:
                            await _notify(context.application, auid,
                                f"✅ *Ticket #{tid} fully approved (L2)*\n🏠 {unit} / {service}")
            elif action == "reject":
                if level == 2 and compound:
                    for auid in _get_role_uids_for_compound(compound, "approver_1"):
                        await _notify(context.application, auid,
                            f"❌ *Ticket #{tid} rejected at Level 2*\n📝 Reason: {note}")
                if submitter:
                    await _notify(context.application, submitter,
                        f"❌ *Ticket #{tid} rejected at Level {level}*\n📝 Reason: {note}")
    except Exception as e:
        logger.error(f"approval notify failed: {e}")

    a_emoji = "✅" if action == "approve" else "❌"
    a_label = "approved" if action == "approve" else "rejected"
    suffix = " Work can now begin." if (action == "approve" and level == 2) else ""
    await reply_fn(
        f"{a_emoji} **Ticket #{tid} {a_label}.**{suffix}\n\nSay **hi** to continue.",
        parse_mode="Markdown"
    )
    for k in ("approval_tid", "approval_level", "approval_action", "approval_note"):
        context.user_data.pop(k, None)
    return ConversationHandler.END


# ── Approver: All Tickets view ───────────────────────────────────────
async def _render_approver_all_tickets(query, context):
    uid = context.user_data.get("agent_uid", str(query.from_user.id))
    page = context.user_data.get("tkt_page", 0)

    conn = db()
    compounds = [r["compound"] for r in conn.execute(
        "SELECT DISTINCT compound FROM agents WHERE telegram_user_id=? AND compound IS NOT NULL AND active=1",
        (uid,)
    ).fetchall()]

    if not compounds:
        await query.edit_message_text(
            "❌ No compounds assigned to your account.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data="all_tkt_nav:back")]
            ])
        )
        conn.close()
        return APPROVAL_LIST

    placeholders = ",".join("?" * len(compounds))
    rows = conn.execute(
        f"SELECT id, unit, service, status, submitted_at FROM submissions "
        f"WHERE compound IN ({placeholders}) ORDER BY submitted_at DESC",
        compounds
    ).fetchall()
    conn.close()

    total = len(rows)
    total_pages = max(1, (total + TICKETS_PER_PAGE - 1) // TICKETS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    context.user_data["tkt_page"] = page

    if not total:
        await query.edit_message_text(
            "🗂️ No tickets found in your compounds.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data="all_tkt_nav:back")]
            ])
        )
        return APPROVAL_LIST

    page_rows = rows[page * TICKETS_PER_PAGE:(page + 1) * TICKETS_PER_PAGE]
    buttons = []
    for r in page_rows:
        date = _fmt_date(r["submitted_at"], 10)
        emoji = STATUS_EMOJI.get(r["status"], "❓")
        svc = (r["service"] or "?")[:18]
        status_text = (r["status"] or "unknown").replace("_", " ").title()
        buttons.append([InlineKeyboardButton(
            f"{emoji} #{r['id']} | {svc} | {status_text} | {date}",
            callback_data=f"all_tkt:{r['id']}"
        )])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data="all_tkt_nav:prev"))
    nav.append(InlineKeyboardButton(f"📄 {page+1}/{total_pages}", callback_data="all_tkt_nav:info"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data="all_tkt_nav:next"))
    buttons.append(nav)
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="all_tkt_nav:back")])

    comp_label = ", ".join(compounds)
    await query.edit_message_text(
        f"🗂️ **All Tickets** ({comp_label})\n{total} ticket(s), page {page+1}/{total_pages}:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown"
    )
    return APPROVAL_LIST


async def all_tickets_nav_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    action = query.data.split(":", 1)[1]

    if action == "prev":
        context.user_data["tkt_page"] = max(0, context.user_data.get("tkt_page", 0) - 1)
        return await _render_approver_all_tickets(query, context)
    if action == "next":
        context.user_data["tkt_page"] = context.user_data.get("tkt_page", 0) + 1
        return await _render_approver_all_tickets(query, context)
    if action == "info":
        return APPROVAL_LIST
    if action == "back":
        await query.edit_message_text(
            "👋 What would you like to do?",
            reply_markup=_dynamic_main_menu_keyboard(context)
        )
        return MAIN_MENU
    return APPROVAL_LIST


async def all_ticket_view_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """all_tkt:{id} — read-only ticket detail from the all-tickets view."""
    query = update.callback_query; await query.answer()
    tid = int(query.data.split(":", 1)[1])

    conn = db()
    row = conn.execute("SELECT * FROM submissions WHERE id=?", (tid,)).fetchone()
    conn.close()

    if not row:
        await query.edit_message_text("❌ Ticket not found.")
        return APPROVAL_LIST

    status = row["status"] or "submitted"
    emoji = STATUS_EMOJI.get(status, "❓")
    label = STATUS_LABEL.get(status, status)
    photo = "📎 Attached" if row["photo_file_id"] else "None"

    sub_svc = row["sub_service"] or ""
    svc_line = (row["service"] or "N/A") + (f" — {sub_svc}" if sub_svc else "")
    text = (
        f"🎫 **Ticket #{row['id']}**\n\n"
        f"🏠 Unit: {row['unit']}\n"
        f"📂 Type: {row['request_type']}\n"
        f"🔧 Service: {svc_line}\n"
        f"📝 Issue: {row['issue_description'] or 'N/A'}\n"
        f"📸 Photo: {photo}\n"
        f"⚡ Status: {emoji} {label}\n"
        f"📅 Submitted: {_fmt_date(row['submitted_at'], 16)}"
    )
    if row["work_done_note"]:
        text += f"\n✅ Work done: {row['work_done_note']}"
    if row["actual_cost"]:
        text += f"\n💰 Actual cost: {row['actual_cost']}"

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back", callback_data="all_tkt_nav:page")]
        ]),
        parse_mode="Markdown"
    )
    return APPROVAL_LIST


async def all_tickets_page_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """all_tkt_nav:page — return to all-tickets list at current page."""
    query = update.callback_query; await query.answer()
    return await _render_approver_all_tickets(query, context)


# ── New ticket: Compound → Unit ──────────────────────────────────────
async def compound_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    compound = query.data.split(":")[1]
    context.user_data["compound"] = compound
    uid = context.user_data.get("agent_uid", str(update.effective_user.id))

    conn = db()
    rows = conn.execute(
        "SELECT full_label FROM master_units_hierarchy "
        "WHERE assigned_to=? AND compound=? ORDER BY full_label",
        (uid, compound)
    ).fetchall()
    conn.close()

    if not rows:
        await query.edit_message_text(
            f"❌ No units assigned to you in {compound}.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="unit:back")]]))
        return UNIT_TYPE

    emoji = COMPOUND_EMOJI.get(compound, "🏘️")
    buttons = [[InlineKeyboardButton(r["full_label"], callback_data=f"unit:{r['full_label']}")] for r in rows]
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="unit:back")])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])

    await _nav(query, f"{emoji} **{compound}**", "Select unit:",
        reply_markup=InlineKeyboardMarkup(buttons))
    return UNIT_TYPE


async def unit_type_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    data = query.data.split(":", 1)
    action = data[1]

    if action == "back":
        uid = context.user_data.get("agent_uid", str(update.effective_user.id))
        await query.edit_message_text("Select **Compound**:",
            reply_markup=_compound_keyboard(uid), parse_mode="Markdown")
        return COMPOUND

    # action is the full_label of the selected unit
    context.user_data["unit"] = action
    await query.edit_message_text(f"🏠 *{action}* selected.", parse_mode="Markdown")
    return await show_request_type_callback(query, context)




# ── Request type ─────────────────────────────────────────────────────
async def show_request_type_callback(query, context: ContextTypes.DEFAULT_TYPE):
    await query.message.reply_text("Select request type:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🆕 New Request", callback_data="type:0")],
            [InlineKeyboardButton("🔄 Follow Up", callback_data="type:1")],
            [InlineKeyboardButton("🚨 Emergency", callback_data="type:2")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
        ]))
    return REQUEST_TYPE

async def request_type_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    idx = int(query.data.split(":")[1])

    if idx == 0:
        context.user_data["request_type"] = "New Request"
        await query.edit_message_text("Select category:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔧 Maintenance", callback_data="cat:0")],
                [InlineKeyboardButton("🏢 Facilities", callback_data="cat:1")],
                [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
            ]))
        return CATEGORY
    elif idx == 1:
        context.user_data["request_type"] = "Follow Up"
        await query.edit_message_text("Enter the Request ID to follow up on:")
        return FOLLOWUP_ID
    else:
        context.user_data["request_type"] = "Emergency"
        await query.edit_message_text("🚨 *EMERGENCY* — describe the issue (min 5 chars):", parse_mode="Markdown")
        return EMERGENCY_DESC


async def category_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if query.data == "back:category":
        return await show_request_type_back(query, context)
    idx = int(query.data.split(":")[1])
    main_cat = "Maintenance" if idx == 0 else "Facilities"
    context.user_data["category"] = main_cat
    context.user_data["main_category"] = main_cat
    cats = _get_categories(main_cat)
    if not cats:
        await query.edit_message_text(f"❌ No categories found for {main_cat}. Contact admin.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back:category")]]))
        return CATEGORY
    emoji = "🔧" if main_cat == "Maintenance" else "🏢"
    buttons = [[InlineKeyboardButton(c, callback_data=f"svc:{c}")] for c in cats]
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="back:category")])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
    await _nav(query, f"{emoji} **{main_cat}**", "Select category:",
        reply_markup=InlineKeyboardMarkup(buttons))
    return SERVICE


async def service_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if query.data == "back:category":
        return await back_to_category(query, context)
    cat = query.data.split(":", 1)[1]
    context.user_data["service"] = cat
    main_cat = context.user_data.get("main_category", "Maintenance")
    subs = _get_sub_categories(main_cat, cat)
    if not subs:
        await query.edit_message_text(f"❌ No sub-categories for {cat}. Contact admin.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back:category")]]))
        return SERVICE
    emoji = "🔧" if main_cat == "Maintenance" else "🏢"
    buttons = [[InlineKeyboardButton(s, callback_data=f"subsvc:{s}")] for s in subs]
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="back:category")])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
    await _nav(query, f"{emoji} **{cat}**", "Select issue type:",
        reply_markup=InlineKeyboardMarkup(buttons))
    return SUB_SERVICE


async def sub_service_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """SUB_SERVICE: user picks a sub-category, or hits Back to return to category list."""
    query = update.callback_query; await query.answer()
    if query.data == "back:service":
        main_cat = context.user_data.get("main_category", "Maintenance")
        cats = _get_categories(main_cat)
        emoji = "🔧" if main_cat == "Maintenance" else "🏢"
        buttons = [[InlineKeyboardButton(c, callback_data=f"svc:{c}")] for c in cats]
        buttons.append([InlineKeyboardButton("🔙 Back", callback_data="back:category")])
        buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
        await query.edit_message_text(
            f"{emoji} **{main_cat}** — select category:",
            reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
        return SERVICE
    sub = query.data.split(":", 1)[1]
    context.user_data["sub_service"] = sub
    cat = context.user_data.get("service", "")
    await _nav(query, f"🔧 **{cat} — {sub}**", "Describe the issue (min 5 chars):")
    return DESCRIPTION


async def facility_service_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Unused — Facilities now routes through service_handler. Kept for index stability."""
    pass


async def description_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = sanitize(update.message.text.strip())
    if len(text) < 5:
        await update.message.reply_text("⚠️ At least 5 characters:")
        return DESCRIPTION
    context.user_data["description"] = text
    await update.message.reply_text("📸 Attach photo (optional):",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⏭️ Skip", callback_data="photo:skip")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
        ]))
    return PHOTO


async def _save_photo(photo, prefix, context, key_path="photo_path", key_fid="photo_file_id"):
    """Download photo from Telegram, upload to blob (or local), store in user_data."""
    uid = context.user_data.get("agent_uid", "?")
    ts = int(time.time())
    blob_name = f"{prefix}_{uid}_{ts}_{secrets.token_hex(4)}.jpg"
    try:
        photo_file = await photo.get_file()
        file_bytes = await photo_file.download_as_bytearray()
        url = upload_photo(bytes(file_bytes), blob_name)
        context.user_data[key_path] = url
        context.user_data[key_fid] = photo.file_id
        logger.info(f"Photo saved: {blob_name}")
        return True, url
    except Exception as e:
        logger.error(f"Photo save failed: {e}")
        context.user_data[key_path] = None
        context.user_data[key_fid] = None
        return False, None


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo = update.message.photo[-1]
    ok, url = await _save_photo(photo, "AGENT", context)
    if ok:
        await update.message.reply_text("✅ Photo received!")
    else:
        await update.message.reply_text("⚠️ Photo upload failed — continuing without photo.")
    return await show_summary(update, context)

async def photo_skip_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    context.user_data["photo_path"] = None
    context.user_data["photo_file_id"] = None
    await query.edit_message_text("📸 Photo: skipped")
    return await show_summary_callback(query, context)


def build_summary(data: dict) -> str:
    photo = "📎 Attached" if data.get("photo_file_id") else "None"
    sub = data.get("sub_service", "")
    svc_line = data.get("service", "N/A") + (f" — {sub}" if sub else "")
    return (
        f"📋 **Request Summary**\n\n"
        f"🏠 Unit: {data.get('unit', 'N/A')}\n"
        f"📂 Type: {data.get('request_type', 'N/A')}\n"
        f"🏗️ Category: {data.get('category', 'N/A')}\n"
        f"🔧 Service: {svc_line}\n"
        f"📝 Issue: {data.get('description', 'N/A')}\n"
        f"📸 Photo: {photo}"
    )

async def show_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(build_summary(context.user_data),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Submit", callback_data="confirm:yes"),
             InlineKeyboardButton("❌ Cancel", callback_data="confirm:no")],
        ]), parse_mode="Markdown")
    return CONFIRM

async def show_summary_callback(query, context: ContextTypes.DEFAULT_TYPE):
    await query.message.reply_text(build_summary(context.user_data),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Submit", callback_data="confirm:yes"),
             InlineKeyboardButton("❌ Cancel", callback_data="confirm:no")],
        ]), parse_mode="Markdown")
    return CONFIRM


async def confirm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if query.data.split(":")[1] == "no":
        context.user_data.clear()
        await query.edit_message_text("❌ Cancelled. Say *hi* to start over.", parse_mode="Markdown")
        return ConversationHandler.END
    return await write_submission(update, context, is_emergency=False)

async def write_submission(update, context, is_emergency=False):
    query = update.callback_query if update.callback_query else None
    uid = str(update.effective_user.id)
    data = context.user_data
    try:
        conn = db()
        sid = insert_and_get_id(conn,
            """INSERT INTO submissions
               (telegram_user_id, phone_number, unit, compound, request_type, category,
                service, sub_service, issue_description, photo_path, photo_file_id, priority, required_approvals)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,2)""",
            (uid, data.get("phone", ""), data["unit"], data.get("compound"),
             data["request_type"], data.get("category"), data.get("service"),
             data.get("sub_service"),
             data["description"], data.get("photo_path"), data.get("photo_file_id"),
             "high" if is_emergency else "normal")
        )
        conn.close()
    except Exception as e:
        logger.error(f"DB write failed: {e}\n{traceback.format_exc()}")
        msg = "❌ Submission failed. Please try again."
        if query: await query.edit_message_text(msg)
        else: await update.message.reply_text(msg)
        context.user_data.clear()
        return ConversationHandler.END

    logger.info(f"Agent submission #{sid}: uid={uid} unit={data.get('unit')} compound={data.get('compound')}")
    sub_compound = data.get("compound", "")
    sub_unit = data.get("unit", "")
    sub_service = data.get("service") or data.get("request_type", "")
    context.user_data.clear()
    try:
        if sub_compound:
            for auid in _get_role_uids_for_compound(sub_compound, "approver_1"):
                await _notify(context.application, auid,
                    f"🆕 *New Request #{sid} submitted*\n🏠 {sub_unit} / {sub_service}")
    except Exception as e:
        logger.error(f"submission notify failed: {e}")
    emoji = "🚨" if is_emergency else "✅"
    msg = f"{emoji} **Request #{sid} submitted!**\n\nSay **hi** for another."
    if query: await query.edit_message_text(msg, parse_mode="Markdown")
    else: await update.message.reply_text(msg, parse_mode="Markdown")
    return ConversationHandler.END


# ── Follow Up ────────────────────────────────────────────────────────
async def followup_id_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rid = update.message.text.strip()
    if not rid.isdigit():
        await update.message.reply_text("⚠️ Enter a numeric Request ID:")
        return FOLLOWUP_ID
    conn = db()
    row = conn.execute("SELECT id, unit, service, issue_description FROM submissions WHERE id=?", (int(rid),)).fetchone()
    conn.close()
    if not row:
        await update.message.reply_text("❌ Request ID not found. Try again:")
        return FOLLOWUP_ID

    uid = context.user_data.get("agent_uid", str(update.effective_user.id))
    assigned_conn = db()
    assigned = {r["full_label"] for r in assigned_conn.execute(
        "SELECT full_label FROM unit_agents WHERE telegram_user_id=?", (uid,)
    ).fetchall()}
    assigned_conn.close()
    if row["unit"] not in assigned:
        await update.message.reply_text("❌ Request ID not found for your assigned units. Try again:")
        return FOLLOWUP_ID

    context.user_data["followup_id"] = int(rid)
    context.user_data["request_type"] = "Follow Up"
    context.user_data["unit"] = row["unit"]
    context.user_data["service"] = row["service"]
    context.user_data["category"] = "Follow Up"
    await update.message.reply_text(
        f"🔄 Following up on **#{rid}** — {row['service']}\n\"{row['issue_description'][:80]}...\"\n\nSelect status:",
        reply_markup=mk_buttons(FOLLOWUP_STATUSES, "fstatus", back_cb="back:request_type"), parse_mode="Markdown")
    return FOLLOWUP_STATUS

async def followup_status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if query.data == "back:request_type":
        return await show_request_type_back(query, context)
    idx = int(query.data.split(":")[1])
    context.user_data["followup_status"] = FOLLOWUP_STATUSES[idx]
    await query.edit_message_text(f"Status: *{FOLLOWUP_STATUSES[idx]}*\n\nAdd a note:", parse_mode="Markdown")
    return FOLLOWUP_NOTE

async def followup_note_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = sanitize(update.message.text.strip())
    if len(text) < 5:
        await update.message.reply_text("⚠️ At least 5 characters:")
        return FOLLOWUP_NOTE
    context.user_data["description"] = (
        f"[Follow-up #{context.user_data['followup_id']}] "
        f"Status: {context.user_data['followup_status']} — {text}"
    )
    await update.message.reply_text("📸 Attach photo (optional):",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⏭️ Skip", callback_data="photo:skip")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
        ]))
    return PHOTO


# ── Emergency ────────────────────────────────────────────────────────
async def emergency_desc_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = sanitize(update.message.text.strip())
    if len(text) < 5:
        await update.message.reply_text("⚠️ At least 5 characters:")
        return EMERGENCY_DESC
    context.user_data["description"] = text
    context.user_data["category"] = "Emergency"
    context.user_data["service"] = "Emergency"
    await update.message.reply_text("📸 Attach photo (strongly recommended):",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⏭️ Skip", callback_data="photo:skip")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
        ]))
    return EMERGENCY_PHOTO

async def emergency_photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo = update.message.photo[-1]
    ok, url = await _save_photo(photo, "EMERG_AGENT", context)
    if ok:
        await update.message.reply_text("✅ Photo received!")
    else:
        await update.message.reply_text("⚠️ Photo upload failed — continuing without photo.")
    return await show_emergency_confirm(update, context)

async def emergency_photo_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    context.user_data["photo_path"] = None
    context.user_data["photo_file_id"] = None
    await query.edit_message_text("📸 Photo: skipped")
    return await show_emergency_confirm_callback(query, context)

async def show_emergency_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚨 **EMERGENCY — Confirm**\n\n" + build_summary(context.user_data),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🚨 Submit Emergency", callback_data="emergency_confirm:yes")],
            [InlineKeyboardButton("❌ Cancel", callback_data="emergency_confirm:no")],
        ]), parse_mode="Markdown")
    return EMERGENCY_CONFIRM

async def show_emergency_confirm_callback(query, context: ContextTypes.DEFAULT_TYPE):
    await query.message.reply_text("🚨 **EMERGENCY — Confirm**\n\n" + build_summary(context.user_data),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🚨 Submit Emergency", callback_data="emergency_confirm:yes")],
            [InlineKeyboardButton("❌ Cancel", callback_data="emergency_confirm:no")],
        ]), parse_mode="Markdown")
    return EMERGENCY_CONFIRM

async def emergency_confirm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if query.data.split(":")[1] == "no":
        context.user_data.clear()
        await query.edit_message_text("❌ Cancelled. Say *hi* to start over.", parse_mode="Markdown")
        return ConversationHandler.END
    return await write_submission(update, context, is_emergency=True)


# ── Back navigation helpers ─────────────────────────────────────────
async def show_request_type_back(query, context):
    await query.edit_message_text("Select request type:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🆕 New Request", callback_data="type:0")],
            [InlineKeyboardButton("🔄 Follow Up", callback_data="type:1")],
            [InlineKeyboardButton("🚨 Emergency", callback_data="type:2")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
        ]))
    return REQUEST_TYPE

async def back_to_category(query, context):
    await query.edit_message_text("Select category:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔧 Maintenance", callback_data="cat:0")],
            [InlineKeyboardButton("🏢 Facilities", callback_data="cat:1")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
        ]))
    return CATEGORY


# ── Cancel ───────────────────────────────────────────────────────────
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    msg = "❌ Cancelled. Say **hi** to start over."
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(msg, parse_mode="Markdown")
    else:
        await update.message.reply_text(msg, parse_mode="Markdown")
    return ConversationHandler.END


# ── Error handler ────────────────────────────────────────────────────
async def timeout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    try:
        await update.effective_message.reply_text(
            "⏰ Session timed out. Say *hi* to continue.", parse_mode="Markdown"
        )
    except Exception:
        pass
    return ConversationHandler.END


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Exception: {context.error}\n{traceback.format_exc()}")
    if update and hasattr(update, 'effective_message'):
        try:
            await update.effective_message.reply_text("⚠️ Something went wrong. Say *hi* to start over.", parse_mode="Markdown")
        except Exception:
            pass


# ── DB schema validation ─────────────────────────────────────────────
def validate_db_schema_local():
    ok = validate_schema()
    if not ok:
        return False, (
            "❌ DB schema mismatch! Missing columns in 'submissions'.\n"
            "   Run: python migration/validate.py\n"
            "   Then restart this bot."
        )
    return True, None


# ── Main ─────────────────────────────────────────────────────────────
async def load_state_from_db(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Load latest state from DB before processing update in a serverless environment."""
    if update.effective_user:
        await context.application.persistence.refresh_user_data(update.effective_user.id, context.user_data)
    if update.effective_chat:
        await context.application.persistence.refresh_chat_data(update.effective_chat.id, context.chat_data)




# ── 1. Supervisor Assignment Workflow ────────────────────────────────
async def _render_assign_list(query, context):
    uid = context.user_data.get("agent_uid", str(query.from_user.id))
    page = context.user_data.get("tkt_page", 0)
    
    # Unassigned tickets in supervisor's compounds
    conn = db()
    sql = """
        SELECT id, unit, service, status, submitted_at FROM submissions 
        WHERE status='submitted' AND assigned_technician_id IS NULL
        AND compound IN (SELECT compound FROM agents WHERE telegram_user_id=? AND role='supervisor' AND active=1)
        ORDER BY submitted_at ASC
    """
    rows = conn.execute(sql, (uid,)).fetchall()
    conn.close()
    
    total = len(rows)
    total_pages = max(1, (total + TICKETS_PER_PAGE - 1) // TICKETS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    context.user_data["tkt_page"] = page

    if not total:
        await query.edit_message_text(
            "✅ No unassigned tickets in your compounds.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="main:back")]])
        )
        return ASSIGN_LIST

    page_rows = rows[page * TICKETS_PER_PAGE:(page + 1) * TICKETS_PER_PAGE]
    buttons = []
    for r in page_rows:
        date = _fmt_date(r["submitted_at"], 10)
        svc = (r["service"] or "?")[:18]
        buttons.append([InlineKeyboardButton(f"🆕 #{r['id']} | {svc} | {date}", callback_data=f"asgn_tkt:{r['id']}")])

    nav = []
    if page > 0: nav.append(InlineKeyboardButton("◀️", callback_data="asgn_nav:prev"))
    nav.append(InlineKeyboardButton(f"📄 {page+1}/{total_pages}", callback_data="asgn_nav:info"))
    if page < total_pages - 1: nav.append(InlineKeyboardButton("▶️", callback_data="asgn_nav:next"))
    buttons.append(nav)
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="main:back")])

    await query.edit_message_text(f"👨‍🔧 **Unassigned Tickets**\n{total} ticket(s) waiting:", reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
    return ASSIGN_LIST

async def assign_list_nav_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    action = query.data.split(":", 1)[1]
    if action == "prev":
        context.user_data["tkt_page"] = max(0, context.user_data.get("tkt_page", 0) - 1)
    elif action == "next":
        context.user_data["tkt_page"] = context.user_data.get("tkt_page", 0) + 1
    elif action == "back":
        return await main_menu_handler(update, context)
    return await _render_assign_list(query, context)

async def assign_ticket_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    tid = int(query.data.split(":", 1)[1])
    context.user_data["view_ticket_id"] = tid
    return await _render_assign_detail(query, context, tid)

async def _render_assign_detail(query, context, tid):
    conn = db()
    row = conn.execute("SELECT * FROM submissions WHERE id=?", (tid,)).fetchone()
    conn.close()
    if not row:
        await query.edit_message_text("❌ Ticket not found.")
        return ASSIGN_LIST
    
    text = (
        f"🎫 **Ticket #{row['id']}**\n\n"
        f"🏠 Unit: {row['unit']}\n"
        f"🔧 Service: {row['service']}\n"
        f"📝 Issue: {row['issue_description'] or 'N/A'}\n"
        f"📅 Submitted: {_fmt_date(row['submitted_at'], 16)}"
    )
    buttons = [
        [InlineKeyboardButton("👨‍🔧 Assign to Technician", callback_data="asgn_action:assign")],
        [InlineKeyboardButton("🔙 Back to List", callback_data="asgn_action:back")]
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
    return ASSIGN_DETAIL

async def assign_detail_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    action = query.data.split(":", 1)[1]
    if action == "back":
        return await _render_assign_list(query, context)
    
    if action == "assign":
        tid = context.user_data["view_ticket_id"]
        # Get technicians for this compound
        conn = db()
        comp = conn.execute("SELECT compound FROM submissions WHERE id=?", (tid,)).fetchone()["compound"]
        techs = conn.execute("SELECT telegram_user_id, name FROM agents WHERE role='technician' AND compound=? AND active=1", (comp,)).fetchall()
        conn.close()
        
        if not techs:
            await query.edit_message_text(
                "❌ No active technicians found for this compound.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="asgn_action:back")]])
            )
            return ASSIGN_DETAIL
            
        buttons = [[InlineKeyboardButton(t["name"], callback_data=f"sel_tech:{t['telegram_user_id']}")] for t in techs]
        buttons.append([InlineKeyboardButton("🔙 Cancel", callback_data="asgn_action:back")])
        await query.edit_message_text(f"👨‍🔧 Select technician for Ticket #{tid}:", reply_markup=InlineKeyboardMarkup(buttons))
        return ASSIGN_TECH

async def assign_tech_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    tech_id = query.data.split(":", 1)[1]
    context.user_data["sel_tech_id"] = tech_id
    
    # Priority
    buttons = [
        [InlineKeyboardButton("🚨 Emergency (Dispatch immediately)", callback_data="sel_prio:emergency")],
        [InlineKeyboardButton("📅 Normal (Schedule)", callback_data="sel_prio:normal")],
        [InlineKeyboardButton("🔙 Cancel", callback_data="asgn_action:back")]
    ]
    await query.edit_message_text("Select Priority:", reply_markup=InlineKeyboardMarkup(buttons))
    return ASSIGN_PRIORITY

async def assign_priority_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    prio = query.data.split(":", 1)[1]
    tid = context.user_data["view_ticket_id"]
    tech_id = context.user_data["sel_tech_id"]
    
    conn = db()
    conn.execute("UPDATE submissions SET status='assigned', priority=?, assigned_technician_id=? WHERE id=?", (prio, tech_id, tid))
    conn.commit()
    conn.close()
    
    # Notify tech
    try:
        await _notify(context.application, tech_id, f"🆕 *New Job Assigned: #{tid}*\nPriority: {prio.upper()}")
    except Exception as e:
        logger.error(f"Notify failed: {e}")
        
    await query.edit_message_text(f"✅ Ticket #{tid} assigned successfully.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Unassigned", callback_data="main:assign")]]))
    return ASSIGN_LIST

# ── 2. Technician My Jobs Workflow ────────────────────────────────
async def _render_my_jobs_list(query, context):
    uid = context.user_data.get("agent_uid", str(query.from_user.id))
    page = context.user_data.get("tkt_page", 0)
    
    conn = db()
    sql = """
        SELECT id, unit, service, status, submitted_at, priority FROM submissions 
        WHERE assigned_technician_id=? AND status IN ('assigned', 'approved')
        ORDER BY priority ASC, submitted_at ASC
    """
    rows = conn.execute(sql, (uid,)).fetchall()
    conn.close()
    
    total = len(rows)
    total_pages = max(1, (total + TICKETS_PER_PAGE - 1) // TICKETS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    context.user_data["tkt_page"] = page

    if not total:
        await query.edit_message_text(
            "✅ No active assigned jobs.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="main:back")]])
        )
        return MY_JOBS_LIST

    page_rows = rows[page * TICKETS_PER_PAGE:(page + 1) * TICKETS_PER_PAGE]
    buttons = []
    for r in page_rows:
        date = _fmt_date(r["submitted_at"], 10)
        svc = (r["service"] or "?")[:18]
        emoji = "🚨" if r["priority"] == "emergency" else "🔧"
        buttons.append([InlineKeyboardButton(f"{emoji} #{r['id']} | {svc} | {date}", callback_data=f"job_tkt:{r['id']}")])

    nav = []
    if page > 0: nav.append(InlineKeyboardButton("◀️", callback_data="job_nav:prev"))
    nav.append(InlineKeyboardButton(f"📄 {page+1}/{total_pages}", callback_data="job_nav:info"))
    if page < total_pages - 1: nav.append(InlineKeyboardButton("▶️", callback_data="job_nav:next"))
    buttons.append(nav)
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="main:back")])

    await query.edit_message_text(f"🛠️ **My Assigned Jobs**\n{total} job(s) pending:", reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
    return MY_JOBS_LIST

async def my_jobs_list_nav_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    action = query.data.split(":", 1)[1]
    if action == "prev":
        context.user_data["tkt_page"] = max(0, context.user_data.get("tkt_page", 0) - 1)
    elif action == "next":
        context.user_data["tkt_page"] = context.user_data.get("tkt_page", 0) + 1
    elif action == "back":
        return await main_menu_handler(update, context)
    return await _render_my_jobs_list(query, context)

async def my_job_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    tid = int(query.data.split(":", 1)[1])
    context.user_data["view_ticket_id"] = tid
    return await _render_my_job_detail(query, context, tid)

async def _render_my_job_detail(query, context, tid):
    conn = db()
    row = conn.execute("SELECT * FROM submissions WHERE id=?", (tid,)).fetchone()
    conn.close()
    if not row:
        return await _render_my_jobs_list(query, context)
    
    status = row["status"]
    text = (
        f"🎫 **Job #{row['id']}**\n\n"
        f"🏠 Unit: {row['unit']}\n"
        f"🔧 Service: {row['service']}\n"
        f"📝 Issue: {row['issue_description'] or 'N/A'}\n"
        f"🚨 Priority: {row['priority']}\n"
    )
    buttons = []
    if status == "assigned":
        buttons.append([InlineKeyboardButton("🔎 Perform Technical Inspection", callback_data="job_action:inspect")])
    elif status == "approved":
        buttons.append([InlineKeyboardButton("✅ Execute Work", callback_data="tkt_action:complete")]) # Reuse complete flow
        
    buttons.append([InlineKeyboardButton("🔙 Back to Jobs", callback_data="job_action:back")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
    return MY_JOB_DETAIL

async def my_job_detail_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    action = query.data.split(":", 1)[1]
    if action == "back":
        return await _render_my_jobs_list(query, context)
    
    if action == "inspect":
        await query.edit_message_text("📝 Enter diagnosis/inspection notes (min 5 chars):", 
                                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]]))
        return TECH_INSPECT

async def tech_inspect_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = sanitize(update.message.text.strip())
    if len(text) < 5:
        await update.message.reply_text("⚠️ At least 5 characters required.")
        return TECH_INSPECT
    context.user_data["inspection_diagnosis"] = text
    
    buttons = [
        [InlineKeyboardButton("🔧 Minor Repair (No quote needed)", callback_data="insp_type:minor")],
        [InlineKeyboardButton("🏗️ Major Repair (Needs BOQ/Quote)", callback_data="insp_type:major")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]
    ]
    await update.message.reply_text("Select repair complexity:", reply_markup=InlineKeyboardMarkup(buttons))
    return TECH_INSPECT_TYPE

async def tech_inspect_type_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    comp = query.data.split(":", 1)[1]
    context.user_data["repair_complexity"] = comp
    tid = context.user_data["view_ticket_id"]
    
    if comp == "minor":
        conn = db()
        conn.execute("UPDATE submissions SET inspection_diagnosis=?, repair_complexity=?, status='approved' WHERE id=?", 
                     (context.user_data["inspection_diagnosis"], comp, tid))
        conn.commit()
        conn.close()
        await query.edit_message_text(f"✅ Inspection saved as Minor Repair. You can now execute the work.", 
                                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Job", callback_data=f"job_tkt:{tid}")]]))
        return MY_JOBS_LIST
    else:
        await query.edit_message_text("📸 Please upload a photo of the BOQ/Quotation document:")
        return BOQ_PHOTO

async def boq_photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo = update.message.photo[-1]
    ok, url = await _save_photo(photo, "BOQ", context, key_path="boq_path", key_fid="boq_file_id")
    if not ok:
        await update.message.reply_text("❌ Upload failed. Try again.")
        return BOQ_PHOTO
    
    tid = context.user_data["view_ticket_id"]
    conn = db()
    conn.execute("UPDATE submissions SET inspection_diagnosis=?, repair_complexity=?, boq_path=?, boq_file_id=?, status='pending_quotation_approval' WHERE id=?", 
                 (context.user_data["inspection_diagnosis"], "major", context.user_data["boq_path"], context.user_data["boq_file_id"], tid))
    
    # Notify management
    tkt = conn.execute("SELECT compound, unit FROM submissions WHERE id=?", (tid,)).fetchone()
    conn.commit()
    conn.close()
    
    if tkt and tkt["compound"]:
        for role in ["senior_engineer", "facility_manager"]:
            for auid in _get_role_uids_for_compound(tkt["compound"], role):
                try:
                    await _notify(context.application, auid, f"📝 *New Quotation for Approval: #{tid}*\n🏠 {tkt['unit']}")
                except:
                    pass
                
    await update.message.reply_text("✅ Major Repair quotation submitted for management approval.",
                                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Jobs", callback_data="main:my_jobs")]]))
    return MY_JOBS_LIST

# ── 3. Quality Inspection (Supervisor) ────────────────────────────────
async def _render_quality_list(query, context):
    uid = context.user_data.get("agent_uid", str(query.from_user.id))
    page = context.user_data.get("tkt_page", 0)
    
    conn = db()
    sql = """
        SELECT id, unit, service, status, submitted_at FROM submissions 
        WHERE status='closed' AND resident_confirmed=0
        AND compound IN (SELECT compound FROM agents WHERE telegram_user_id=? AND role='supervisor' AND active=1)
        ORDER BY work_done_at DESC
    """
    rows = conn.execute(sql, (uid,)).fetchall()
    conn.close()
    
    total = len(rows)
    total_pages = max(1, (total + TICKETS_PER_PAGE - 1) // TICKETS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    context.user_data["tkt_page"] = page

    if not total:
        await query.edit_message_text(
            "✅ No completed jobs pending quality inspection.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="main:back")]])
        )
        return QUALITY_LIST

    page_rows = rows[page * TICKETS_PER_PAGE:(page + 1) * TICKETS_PER_PAGE]
    buttons = []
    for r in page_rows:
        date = _fmt_date(r["submitted_at"], 10)
        svc = (r["service"] or "?")[:18]
        buttons.append([InlineKeyboardButton(f"🔎 #{r['id']} | {svc} | {date}", callback_data=f"qual_tkt:{r['id']}")])

    nav = []
    if page > 0: nav.append(InlineKeyboardButton("◀️", callback_data="qual_nav:prev"))
    nav.append(InlineKeyboardButton(f"📄 {page+1}/{total_pages}", callback_data="qual_nav:info"))
    if page < total_pages - 1: nav.append(InlineKeyboardButton("▶️", callback_data="qual_nav:next"))
    buttons.append(nav)
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="main:back")])

    await query.edit_message_text(f"🔎 **Quality Inspections**\n{total} job(s) pending review:", reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
    return QUALITY_LIST

async def quality_list_nav_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    action = query.data.split(":", 1)[1]
    if action == "prev":
        context.user_data["tkt_page"] = max(0, context.user_data.get("tkt_page", 0) - 1)
    elif action == "next":
        context.user_data["tkt_page"] = context.user_data.get("tkt_page", 0) + 1
    elif action == "back":
        return await main_menu_handler(update, context)
    return await _render_quality_list(query, context)

async def quality_ticket_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    tid = int(query.data.split(":", 1)[1])
    context.user_data["view_ticket_id"] = tid
    return await _render_quality_detail(query, context, tid)

async def _render_quality_detail(query, context, tid):
    conn = db()
    row = conn.execute("SELECT * FROM submissions WHERE id=?", (tid,)).fetchone()
    conn.close()
    if not row:
        return await _render_quality_list(query, context)
    
    photo = "📎 Attached" if row["completion_photo_file_id"] else "None"
    text = (
        f"🎫 **Ticket #{row['id']} Completed Work**\n\n"
        f"🏠 Unit: {row['unit']}\n"
        f"🔧 Service: {row['service']}\n"
        f"💰 Cost: {row['actual_cost']}\n"
        f"📸 Photo: {photo}\n"
    )
    buttons = [
        [InlineKeyboardButton("✅ Approve Quality", callback_data="qual_action:approve")],
        [InlineKeyboardButton("❌ Reject (Needs Rework)", callback_data="qual_action:reject")],
        [InlineKeyboardButton("🔙 Back to List", callback_data="qual_action:back")]
    ]
    if row["completion_photo_file_id"]:
        try:
            await query.message.reply_photo(photo=row["completion_photo_file_id"], caption=text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
        except:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
    else:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
    return QUALITY_DETAIL

async def quality_detail_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    action = query.data.split(":", 1)[1]
    if action == "back":
        return await _render_quality_list(query, context)
    
    context.user_data["qual_action"] = action
    await query.edit_message_text("📝 Enter inspection note (optional):", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⏭️ Skip", callback_data="qual_note:skip"), InlineKeyboardButton("❌ Cancel", callback_data="cancel")]]))
    return QUALITY_NOTE

async def quality_note_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = ""
    if update.message:
        text = sanitize(update.message.text.strip())
    
    action = context.user_data["qual_action"]
    tid = context.user_data["view_ticket_id"]
    uid = context.user_data.get("agent_uid", "?")
    
    conn = db()
    tkt = conn.execute("SELECT telegram_user_id, assigned_technician_id, unit, service FROM submissions WHERE id=?", (tid,)).fetchone()
    if action == "approve":
        conn.execute("UPDATE submissions SET quality_inspector_id=?, status='quality_approved' WHERE id=?", (uid, tid))
        # Notify resident
        if tkt and tkt["telegram_user_id"]:
            resident_msg = f"✅ *Work Completed: #{tid}*\nYour maintenance request for {tkt['service']} is done.\nPlease confirm satisfactory completion by replying to this bot or using the menu."
            try:
                await _notify(context.application, tkt["telegram_user_id"], resident_msg)
            except: pass
    else:
        # Reject - goes back to assigned
        conn.execute("UPDATE submissions SET status='assigned' WHERE id=?", (tid,))
        if tkt and tkt["assigned_technician_id"]:
            try:
                await _notify(context.application, tkt["assigned_technician_id"], f"❌ *Work Rejected: #{tid}*\nNotes: {text}\nPlease rework and resubmit.")
            except: pass
            
    conn.commit()
    conn.close()
    
    if update.message:
        await update.message.reply_text(f"✅ Quality inspection saved.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to List", callback_data="main:quality_inspections")]]))
    else:
        await update.callback_query.edit_message_text(f"✅ Quality inspection saved.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to List", callback_data="main:quality_inspections")]]))
    return QUALITY_LIST

# ── 4. Quotation Approval Workflow (Management) ──────────────────────
async def _render_quotation_list(query, context):
    uid = context.user_data.get("agent_uid", str(query.from_user.id))
    page = context.user_data.get("tkt_page", 0)
    
    conn = db()
    sql = """
        SELECT id, unit, service, status, submitted_at FROM submissions 
        WHERE status='pending_quotation_approval'
        AND compound IN (SELECT compound FROM agents WHERE telegram_user_id=? AND role IN ('senior_engineer', 'facility_manager') AND active=1)
        ORDER BY submitted_at ASC
    """
    rows = conn.execute(sql, (uid,)).fetchall()
    conn.close()
    
    total = len(rows)
    total_pages = max(1, (total + TICKETS_PER_PAGE - 1) // TICKETS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    context.user_data["tkt_page"] = page

    if not total:
        await query.edit_message_text("✅ No pending quotations.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="main:back")]]))
        return QUOTATION_LIST

    page_rows = rows[page * TICKETS_PER_PAGE:(page + 1) * TICKETS_PER_PAGE]
    buttons = []
    for r in page_rows:
        date = _fmt_date(r["submitted_at"], 10)
        svc = (r["service"] or "?")[:18]
        buttons.append([InlineKeyboardButton(f"📝 #{r['id']} | {svc} | {date}", callback_data=f"quot_tkt:{r['id']}")])

    nav = []
    if page > 0: nav.append(InlineKeyboardButton("◀️", callback_data="quot_nav:prev"))
    nav.append(InlineKeyboardButton(f"📄 {page+1}/{total_pages}", callback_data="quot_nav:info"))
    if page < total_pages - 1: nav.append(InlineKeyboardButton("▶️", callback_data="quot_nav:next"))
    buttons.append(nav)
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="main:back")])

    await query.edit_message_text(f"📝 **Quotation Approvals**\n{total} pending:", reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
    return QUOTATION_LIST

async def quotation_list_nav_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    action = query.data.split(":", 1)[1]
    if action == "prev": context.user_data["tkt_page"] = max(0, context.user_data.get("tkt_page", 0) - 1)
    elif action == "next": context.user_data["tkt_page"] = context.user_data.get("tkt_page", 0) + 1
    elif action == "back": return await main_menu_handler(update, context)
    return await _render_quotation_list(query, context)

async def quotation_ticket_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    tid = int(query.data.split(":", 1)[1])
    context.user_data["view_ticket_id"] = tid
    return await _render_quotation_detail(query, context, tid)

async def _render_quotation_detail(query, context, tid):
    conn = db()
    row = conn.execute("SELECT * FROM submissions WHERE id=?", (tid,)).fetchone()
    conn.close()
    if not row: return await _render_quotation_list(query, context)
    
    photo = "📎 Attached" if row["boq_file_id"] else "None"
    text = (
        f"🎫 **Ticket #{row['id']} BOQ Quotation**\n\n"
        f"🏠 Unit: {row['unit']}\n"
        f"🔧 Service: {row['service']}\n"
        f"📝 Diagnosis: {row['inspection_diagnosis']}\n"
        f"📸 BOQ: {photo}\n"
    )
    buttons = [
        [InlineKeyboardButton("✅ Approve BOQ", callback_data="quot_action:approve")],
        [InlineKeyboardButton("❌ Reject BOQ", callback_data="quot_action:reject")],
        [InlineKeyboardButton("🔙 Back to List", callback_data="quot_action:back")]
    ]
    if row["boq_file_id"]:
        try:
            await query.message.reply_photo(photo=row["boq_file_id"], caption=text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
        except:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
    else:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
    return QUOTATION_DETAIL

async def quotation_detail_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    action = query.data.split(":", 1)[1]
    if action == "back": return await _render_quotation_list(query, context)
    
    context.user_data["quot_action"] = action
    await query.edit_message_text("📝 Enter approval/rejection note (optional):", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⏭️ Skip", callback_data="quot_note:skip"), InlineKeyboardButton("❌ Cancel", callback_data="cancel")]]))
    return QUOTATION_NOTE

async def quotation_note_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = ""
    if update.message: text = sanitize(update.message.text.strip())
    
    action = context.user_data["quot_action"]
    tid = context.user_data["view_ticket_id"]
    
    conn = db()
    tkt = conn.execute("SELECT assigned_technician_id FROM submissions WHERE id=?", (tid,)).fetchone()
    if action == "approve":
        conn.execute("UPDATE submissions SET status='approved' WHERE id=?", (tid,))
        msg = f"✅ *Quotation Approved: #{tid}*\nYou can now execute the work."
    else:
        conn.execute("UPDATE submissions SET status='assigned', repair_complexity=NULL, boq_file_id=NULL WHERE id=?", (tid,))
        msg = f"❌ *Quotation Rejected: #{tid}*\nNotes: {text}\nPlease submit a new quotation."
    
    if tkt and tkt["assigned_technician_id"]:
        try: await _notify(context.application, tkt["assigned_technician_id"], msg)
        except: pass
            
    conn.commit()
    conn.close()
    
    if update.message:
        await update.message.reply_text("✅ BOQ Decision saved.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to List", callback_data="main:quotations")]]))
    else:
        await update.callback_query.edit_message_text("✅ BOQ Decision saved.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to List", callback_data="main:quotations")]]))
    return QUOTATION_LIST


def create_application():
    """Create and configure the PTB Application. Called by both polling and webhook modes."""
    if not TOKEN:
        raise RuntimeError("Agent bot token not found!")

    ok, err = validate_db_schema_local()
    if not ok:
        raise RuntimeError(err)

    logger.info("[OK] DB schema validated")

    from persistence import AzureSqlPersistence
    persistence = AzureSqlPersistence()

    app = Application.builder().token(TOKEN).persistence(persistence).build()
    app.add_error_handler(error_handler)
    app.add_handler(TypeHandler(Update, load_state_from_db), group=-1)

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(filters.TEXT & ~filters.COMMAND, start),
        ],
        conversation_timeout=300,
        persistent=True,
        name="agent_main",
        states={
            MAIN_MENU: [CallbackQueryHandler(main_menu_handler, pattern=r"^main:")],

            # ── New ticket flow ─────────────────────────────────────
            COMPOUND: [
                CallbackQueryHandler(compound_handler, pattern=r"^compound:"),
                CallbackQueryHandler(main_menu_handler, pattern=r"^main:back$"),
            ],
            UNIT_TYPE: [CallbackQueryHandler(unit_type_handler, pattern=r"^unit:")],
            REQUEST_TYPE: [CallbackQueryHandler(request_type_handler, pattern=r"^type:")],
            CATEGORY:     [CallbackQueryHandler(category_handler,      pattern=r"^cat:|^back:category$")],
            SERVICE:      [CallbackQueryHandler(service_handler,       pattern=r"^svc:|^back:category$")],
            SUB_SERVICE:  [CallbackQueryHandler(sub_service_handler,   pattern=r"^subsvc:|^back:service$")],
            DESCRIPTION:  [MessageHandler(filters.TEXT & ~filters.COMMAND, description_handler)],
            PHOTO: [
                MessageHandler(filters.PHOTO, photo_handler),
                CallbackQueryHandler(photo_skip_handler, pattern=r"^photo:skip"),
            ],
            CONFIRM: [CallbackQueryHandler(confirm_handler, pattern=r"^confirm:")],
            FOLLOWUP_ID:     [MessageHandler(filters.TEXT & ~filters.COMMAND, followup_id_handler)],
            FOLLOWUP_STATUS: [CallbackQueryHandler(followup_status_handler, pattern=r"^fstatus:|^back:request_type$")],
            FOLLOWUP_NOTE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, followup_note_handler)],
            EMERGENCY_DESC:  [MessageHandler(filters.TEXT & ~filters.COMMAND, emergency_desc_handler)],
            EMERGENCY_PHOTO: [
                MessageHandler(filters.PHOTO, emergency_photo_handler),
                CallbackQueryHandler(emergency_photo_skip, pattern=r"^photo:skip"),
            ],
            EMERGENCY_CONFIRM: [CallbackQueryHandler(emergency_confirm_handler, pattern=r"^emergency_confirm:")],

            # ── Existing tickets flow ───────────────────────────────
            EX_FILTER: [
                CallbackQueryHandler(ex_filter_handler,  pattern=r"^ex_filter:"),
                CallbackQueryHandler(main_menu_handler,  pattern=r"^main:"),
            ],
            EX_COMPOUND: [
                CallbackQueryHandler(ex_compound_handler,  pattern=r"^compound:"),
                CallbackQueryHandler(ex_show_now_handler,  pattern=r"^ex_show:now$"),
                CallbackQueryHandler(main_menu_handler,    pattern=r"^main:"),
            ],
            EX_UNIT_TYPE: [
                CallbackQueryHandler(ex_unit_type_handler, pattern=r"^ex_unit:|^ex_utype:"),
                CallbackQueryHandler(ex_show_now_handler,  pattern=r"^ex_show:now$"),
            ],
            TICKET_LIST: [
                CallbackQueryHandler(ticket_handler,      pattern=r"^tkt:\d+$"),
                CallbackQueryHandler(ticket_list_handler, pattern=r"^tkt_nav:"),
            ],
            TICKET_DETAIL: [
                CallbackQueryHandler(ticket_detail_handler, pattern=r"^tkt_action:"),
            ],
            COMPLETE_COST: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, complete_cost_handler),
                CallbackQueryHandler(complete_cost_handler, pattern=r"^complete:back$"),
            ],
            COMPLETE_PHOTO: [
                MessageHandler(filters.PHOTO, complete_photo_handler),
                CallbackQueryHandler(complete_photo_handler, pattern=r"^complete:back_photo$"),
            ],
            COMPLETE_CONFIRM: [CallbackQueryHandler(complete_confirm_handler, pattern=r"^complete_confirm:")],

            # ── Approver workflow ───────────────────────────────────
            APPROVAL_LIST: [
                CallbackQueryHandler(approval_ticket_handler,    pattern=r"^appr_tkt:\d+$"),
                CallbackQueryHandler(approval_list_nav_handler,  pattern=r"^approval_nav:"),
                CallbackQueryHandler(all_ticket_view_handler,    pattern=r"^all_tkt:\d+$"),
                CallbackQueryHandler(all_tickets_nav_handler,    pattern=r"^all_tkt_nav:(?!page$)"),
                CallbackQueryHandler(all_tickets_page_handler,   pattern=r"^all_tkt_nav:page$"),
            ],
            APPROVAL_DETAIL: [
                CallbackQueryHandler(approval_detail_handler, pattern=r"^approval:"),
            ],
            APPROVAL_NOTE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, approval_note_handler),
                CallbackQueryHandler(approval_note_handler, pattern=r"^approval_note:"),
            ],
            
            # ── Supervisor Assignment Workflow ──────────────────────
            ASSIGN_LIST: [
                CallbackQueryHandler(assign_ticket_handler, pattern=r"^asgn_tkt:\d+$"),
                CallbackQueryHandler(assign_list_nav_handler, pattern=r"^asgn_nav:"),
            ],
            ASSIGN_DETAIL: [
                CallbackQueryHandler(assign_detail_handler, pattern=r"^asgn_action:"),
            ],
            ASSIGN_TECH: [
                CallbackQueryHandler(assign_tech_handler, pattern=r"^sel_tech:"),
                CallbackQueryHandler(assign_detail_handler, pattern=r"^asgn_action:"),
            ],
            ASSIGN_PRIORITY: [
                CallbackQueryHandler(assign_priority_handler, pattern=r"^sel_prio:"),
                CallbackQueryHandler(assign_detail_handler, pattern=r"^asgn_action:"),
            ],
            # ── Technician My Jobs Workflow ────────────────────────
            MY_JOBS_LIST: [
                CallbackQueryHandler(my_job_handler, pattern=r"^job_tkt:\d+$"),
                CallbackQueryHandler(my_jobs_list_nav_handler, pattern=r"^job_nav:"),
            ],
            MY_JOB_DETAIL: [
                CallbackQueryHandler(my_job_detail_handler, pattern=r"^job_action:"),
                CallbackQueryHandler(ticket_detail_handler, pattern=r"^tkt_action:"),
            ],
            TECH_INSPECT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, tech_inspect_handler),
                CallbackQueryHandler(cancel, pattern=r"^cancel$"),
            ],
            TECH_INSPECT_TYPE: [
                CallbackQueryHandler(tech_inspect_type_handler, pattern=r"^insp_type:"),
                CallbackQueryHandler(cancel, pattern=r"^cancel$"),
            ],
            BOQ_PHOTO: [
                MessageHandler(filters.PHOTO, boq_photo_handler),
            ],
            # ── Supervisor Quality Workflow ────────────────────────
            QUALITY_LIST: [
                CallbackQueryHandler(quality_ticket_handler, pattern=r"^qual_tkt:\d+$"),
                CallbackQueryHandler(quality_list_nav_handler, pattern=r"^qual_nav:"),
            ],
            QUALITY_DETAIL: [
                CallbackQueryHandler(quality_detail_handler, pattern=r"^qual_action:"),
            ],
            QUALITY_NOTE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, quality_note_handler),
                CallbackQueryHandler(quality_note_handler, pattern=r"^qual_note:"),
            ],
            # ── Management Quotation Workflow ──────────────────────
            QUOTATION_LIST: [
                CallbackQueryHandler(quotation_ticket_handler, pattern=r"^quot_tkt:\d+$"),
                CallbackQueryHandler(quotation_list_nav_handler, pattern=r"^quot_nav:"),
            ],
            QUOTATION_DETAIL: [
                CallbackQueryHandler(quotation_detail_handler, pattern=r"^quot_action:"),
            ],
            QUOTATION_NOTE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, quotation_note_handler),
                CallbackQueryHandler(quotation_note_handler, pattern=r"^quot_note:"),
            ],

            ConversationHandler.TIMEOUT: [
                MessageHandler(filters.ALL, timeout_handler),
                CallbackQueryHandler(timeout_handler),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(cancel, pattern=r"^cancel$"),
            CommandHandler("cancel", cancel),
        ],
    )
    app.add_handler(conv_handler)

    logger.info("Agent Bot — ready")
    return app


def main():
    try:
        app = create_application()
    except RuntimeError as e:
        logger.error(str(e))
        print(str(e), file=sys.stderr)
        sys.exit(1)

    logger.info("=" * 50)
    logger.info("Agent Bot starting — Phase 7 (Service Hierarchy)")
    logger.info("Compounds: loaded dynamically from master_units_hierarchy")
    logger.info("Services: loaded dynamically from DB (services table)")
    logger.info(f"States: {35} total")
    logger.info("=" * 50)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

# Trigger deployment
