#!/usr/bin/env python3
"""
Zad Field Service Bot — deterministic Telegram bot with inline keyboards.
python-telegram-bot 22.x. Zero AI, zero hallucination, prompt-injection-proof.
"""

import html
import json
import logging
import os
import re
import secrets
import sys
import time
import traceback

import pyodbc
from db import get_db, validate_schema, insert_and_get_id
from persistence import AzureSqlPersistence
from blob_storage import upload_photo, delete_photo
from datetime import datetime
from logging.handlers import RotatingFileHandler

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ConversationHandler, MessageHandler, filters, ContextTypes, TypeHandler
)

# Pre-compiled back-text pattern for case-insensitive matching
BACK_TEXT_RE = re.compile(r'^back$', re.IGNORECASE)

# ── Config ──────────────────────────────────────────────────────────
TOKEN = os.environ.get("BOT_TOKEN", "")
if not TOKEN:
    _tf = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".bot_token")
    if os.path.exists(_tf):
        with open(_tf) as f:
            TOKEN = f.read().strip()

DB_PATH   = os.environ.get("DB_PATH",    "/data/field_service.db")
LOG_DIR   = os.environ.get("LOG_DIR",    "/tmp")
os.makedirs(LOG_DIR, exist_ok=True)

# ── Logging ─────────────────────────────────────────────────────────
logger = logging.getLogger("field-service-bot")
logger.setLevel(logging.INFO)
fh = RotatingFileHandler(
    os.path.join(LOG_DIR, "bot.log"), maxBytes=5_000_000, backupCount=5
)
fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(fh)
logger.addHandler(logging.StreamHandler(sys.stdout))

# ── Conversation states ─────────────────────────────────────────────
(PHONE, UNITS, REQUEST_TYPE,
 CATEGORY, SERVICE, FACILITY_SERVICE,
 DESCRIPTION, PHOTO, CONFIRM,
 FOLLOWUP_ID, FOLLOWUP_STATUS, FOLLOWUP_NOTE,
 EMERGENCY_DESC, EMERGENCY_PHOTO, EMERGENCY_CONFIRM,
 SUB_SERVICE) = range(16)

FOLLOWUP_STATUSES = [
    "In Progress", "Completed", "Delayed", "Needs Parts",
    "Needs Supervisor", "Cannot Resolve"
]

# ── DB helpers ───────────────────────────────────────────────────────
def db():
    return get_db()

def norm_phone(p):
    return re.sub(r'\D', '', p) if p else ""

def sanitize(text: str) -> str:
    """Strip HTML/markdown, limit length."""
    text = html.escape(text or "", quote=False)
    return text[:500]


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

def _get_field_agents_for_unit(unit: str) -> list:
    conn = db()
    rows = conn.execute(
        "SELECT DISTINCT assigned_to FROM master_units_hierarchy "
        "WHERE full_label=? AND assigned_to IS NOT NULL",
        (unit,)
    ).fetchall()
    conn.close()
    return [r["assigned_to"] for r in rows]


def _get_compound_for_unit(unit: str) -> str:
    """Find the compound for a given unit label."""
    conn = db()
    row = conn.execute(
        "SELECT compound FROM master_units_hierarchy WHERE full_label=?",
        (unit,)
    ).fetchone()
    conn.close()
    return row["compound"] if row else None


def _get_role_uids_for_compound(compound: str, role: str) -> list:
    """Get TG IDs for all active users with a given role in a compound."""
    conn = db()
    rows = conn.execute(
        "SELECT DISTINCT telegram_user_id FROM agents WHERE role=? AND compound=? AND active=1",
        (role, compound)
    ).fetchall()
    conn.close()
    return [r["telegram_user_id"] for r in rows]

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


# ── Keyboard builders ────────────────────────────────────────────────
def mk_buttons(items: list, prefix: str, cols: int = 1,
               cancel: bool = True, back_cb: str = None) -> InlineKeyboardMarkup:
    """Generic keyboard builder."""
    buttons = []
    row = []
    for i, item in enumerate(items):
        row.append(InlineKeyboardButton(str(item), callback_data=f"{prefix}:{i}"))
        if len(row) >= cols:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    nav = []
    if back_cb:
        nav.append(InlineKeyboardButton("🔙 Back", callback_data=back_cb))
    if cancel:
        nav.append(InlineKeyboardButton("❌ Cancel", callback_data="cancel"))
    if nav:
        buttons.append(nav)
    return InlineKeyboardMarkup(buttons)


# ── Start / Entry ───────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = str(user.id)
    logger.info(f"Start: uid={uid} name={user.first_name}")

    context.user_data.clear()

    conn = db()
    conn.execute("DELETE FROM form_state WHERE telegram_user_id=?", (uid,))
    conn.commit()

    # TG ID lookup
    rows = conn.execute(
        "SELECT owner_name, units FROM master_units WHERE telegram_user_id=? AND telegram_user_id != ''",
        (uid,)
    ).fetchall()
    conn.close()

    if rows:
        all_units = list(dict.fromkeys(
            unit for r in rows for unit in json.loads(r["units"])
        ))
        context.user_data.update({
            "owner": rows[0]["owner_name"],
            "units": all_units,
            "phone": "",
            "tg_known": True,
        })
        await update.message.reply_text(
            f"👋 Welcome, {rows[0]['owner_name']}!\n\n🏠 Select unit:",
            reply_markup=mk_buttons(all_units, "unit")
        )
        return UNITS
    else:
        context.user_data["tg_known"] = False
        await update.message.reply_text(
            "📱 Enter phone number linked to premises\n(e.g. +974XXXXXXXX or digits):",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]
            ])
        )
        return PHONE


# ── Phone validation ─────────────────────────────────────────────────
async def phone_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    digits = norm_phone(update.message.text.strip())
    if len(digits) < 8 or len(digits) > 15:
        await update.message.reply_text("⚠️ Phone must be 8–15 digits. Try again:")
        return PHONE

    conn = db()
    row = conn.execute(
        "SELECT phone_number, owner_name, units FROM master_units WHERE phone_number=?",
        (digits,)
    ).fetchone()
    conn.close()

    if not row:
        await update.message.reply_text("❌ Phone not found. Try again or contact supervisor.")
        return PHONE

    units = json.loads(row["units"])
    context.user_data.update({
        "phone": row["phone_number"],
        "owner": row["owner_name"],
        "units": units,
    })
    await update.message.reply_text(
        "🏠 Select unit:", reply_markup=mk_buttons(units, "unit")
    )
    return UNITS


# ── Navigation helpers ─────────────────────────────────────────────
def _cq(uq):
    """Normalize: PTB passes Update to callbacks, but internal calls pass CallbackQuery directly."""
    return uq.callback_query if hasattr(uq, 'callback_query') and uq.callback_query else uq


async def _nav(query, confirmed: str, next_text: str, reply_markup=None):
    """Show confirmation as edit, then send next step as new message below."""
    try:
        await query.edit_message_text(confirmed, parse_mode="Markdown")
    except Exception:
        pass
    await query.message.reply_text(next_text, reply_markup=reply_markup, parse_mode="Markdown")


# ── Unit selection ───────────────────────────────────────────────────
async def unit_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    idx = int(query.data.split(":")[1])
    context.user_data["unit"] = context.user_data["units"][idx]
    await query.edit_message_text(
        f"✅ Unit: {context.user_data['unit']}\n\n📋 Request type:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🆕 New Request", callback_data="type:new")],
            [InlineKeyboardButton("🔄 Follow Up", callback_data="type:followup")],
            [InlineKeyboardButton("🚨 Emergency", callback_data="type:emergency")],
            [InlineKeyboardButton("🔙 Back", callback_data="back:units"),
             InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
        ])
    )
    return REQUEST_TYPE


# ── Request type ─────────────────────────────────────────────────────
async def request_type_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if query.data == "back:units":
        return await back_to_units(query, context)
    rtype = query.data.split(":")[1]

    if rtype == "followup":
        context.user_data["_back_target"] = "followup_id"
        await query.edit_message_text(
            "🔄 **Follow Up**\n\nEnter the **Request ID** you want to follow up on:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data="back:type"),
                 InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
            ]),
            parse_mode="Markdown"
        )
        return FOLLOWUP_ID

    elif rtype == "emergency":
        context.user_data["request_type"] = "Report Emergency"
        context.user_data["_back_target"] = "emergency_desc"
        await query.edit_message_text(
            "🚨 **EMERGENCY**\n\nDescribe the emergency (min 5 chars):",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data="back:type"),
                 InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
            ]),
            parse_mode="Markdown"
        )
        return EMERGENCY_DESC

    else:  # new
        context.user_data["request_type"] = "New Request"
        context.user_data["_back_target"] = "description"
        await query.edit_message_text(
            "🏗️ Category:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🛠️ Facilities", callback_data="cat:facilities")],
                [InlineKeyboardButton("🔧 Maintenance", callback_data="cat:maintenance")],
                [InlineKeyboardButton("🔙 Back", callback_data="back:type"),
                 InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
            ])
        )
        return CATEGORY


# ── Category → service list ─────────────────────────────────────────
async def category_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if query.data == "back:type":
        return await back_to_request_type(query, context)
    cat = query.data.split(":")[1]
    main_cat = "Maintenance" if cat == "maintenance" else "Facilities"
    context.user_data["category"] = main_cat
    context.user_data["main_category"] = main_cat
    context.user_data["_back_target"] = "description"
    cats = _get_categories(main_cat)
    if not cats:
        await query.edit_message_text(f"❌ No categories found for {main_cat}. Contact admin.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back", callback_data="back:type"),
                InlineKeyboardButton("❌ Cancel", callback_data="cancel"),
            ]]))
        return CATEGORY
    emoji = "🔧" if main_cat == "Maintenance" else "🛠️"
    buttons = [[InlineKeyboardButton(c, callback_data=f"svc:{c}")] for c in cats]
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="back:cat"),
                    InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
    await query.edit_message_text(
        f"{emoji} **{main_cat}** — select category:",
        reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
    return SERVICE


# ── Category selection (level 2) ────────────────────────────────────
async def service_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if query.data == "back:cat":
        return await back_to_category(query, context)
    cat = query.data.split(":", 1)[1]
    context.user_data["service"] = cat
    main_cat = context.user_data.get("main_category", "Maintenance")
    subs = _get_sub_categories(main_cat, cat)
    if not subs:
        await query.edit_message_text(f"❌ No sub-categories for {cat}. Contact admin.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back", callback_data="back:cat"),
                InlineKeyboardButton("❌ Cancel", callback_data="cancel"),
            ]]))
        return SERVICE
    emoji = "🔧" if main_cat == "Maintenance" else "🛠️"
    buttons = [[InlineKeyboardButton(s, callback_data=f"subsvc:{s}")] for s in subs]
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="back:service"),
                    InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
    await query.edit_message_text(
        f"{emoji} **{cat}** — select issue type:",
        reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
    return SUB_SERVICE


# ── Sub-category selection (level 3) ────────────────────────────────
async def sub_service_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if query.data == "back:service":
        main_cat = context.user_data.get("main_category", "Maintenance")
        cats = _get_categories(main_cat)
        emoji = "🔧" if main_cat == "Maintenance" else "🛠️"
        buttons = [[InlineKeyboardButton(c, callback_data=f"svc:{c}")] for c in cats]
        buttons.append([InlineKeyboardButton("🔙 Back", callback_data="back:cat"),
                        InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
        await query.edit_message_text(
            f"{emoji} **{main_cat}** — select category:",
            reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
        return SERVICE
    sub = query.data.split(":", 1)[1]
    context.user_data["sub_service"] = sub
    context.user_data["_back_target"] = "sub_service"
    cat = context.user_data.get("service", "")
    await query.edit_message_text(
        f"✅ {cat} — {sub}\n\n📝 Describe the issue (min 5 chars):",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back", callback_data="back:desc_to_subsvc"),
             InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
        ]))
    return DESCRIPTION


async def facility_service_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Unused — Facilities now routes through service_handler. Kept for index stability."""
    pass


# ── Description ──────────────────────────────────────────────────────
async def description_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = sanitize(update.message.text.strip())
    if len(text) < 5:
        await update.message.reply_text("⚠️ At least 5 characters. Try again:")
        return DESCRIPTION
    context.user_data["description"] = text
    context.user_data["_back_target"] = "description"
    await update.message.reply_text(
        "📸 Attach photo (optional):",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⏭️ Skip", callback_data="photo:skip")],
            [InlineKeyboardButton("🔙 Back", callback_data="photo:back_desc"),
             InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
        ])
    )
    return PHOTO


# ── Photo helpers ────────────────────────────────────────────────────
async def _save_photo(photo, prefix, context, key_path="photo_path", key_fid="photo_file_id"):
    """Download photo from Telegram, upload to blob (or local), store in user_data."""
    uid = str(context._user_id if hasattr(context, '_user_id') else "?")
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


# ── Photo ────────────────────────────────────────────────────────────
async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo = update.message.photo[-1]
    ok, url = await _save_photo(photo, "RES", context)
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


# ── Summary ──────────────────────────────────────────────────────────
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
    await update.message.reply_text(
        build_summary(context.user_data),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Submit", callback_data="confirm:yes"),
             InlineKeyboardButton("🔙 Back", callback_data="confirm:back_photo"),
             InlineKeyboardButton("❌ Cancel", callback_data="confirm:no")],
        ]),
        parse_mode="Markdown"
    )
    return CONFIRM


async def show_summary_callback(query, context: ContextTypes.DEFAULT_TYPE):
    await query.message.reply_text(
        build_summary(context.user_data),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Submit", callback_data="confirm:yes"),
             InlineKeyboardButton("🔙 Back", callback_data="confirm:back_photo"),
             InlineKeyboardButton("❌ Cancel", callback_data="confirm:no")],
        ]),
        parse_mode="Markdown"
    )
    return CONFIRM


# ── Confirm & Submit ─────────────────────────────────────────────────
async def confirm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()

    data = query.data.split(":")[1]
    if data == "no":
        context.user_data.clear()
        await query.edit_message_text("❌ Cancelled. Say **hi** to start over.", parse_mode="Markdown")
        return ConversationHandler.END
    elif data == "back_photo":
        return await back_to_description(query, context)

    return await write_submission(update, context, is_emergency=False)


async def write_submission(update, context, is_emergency=False):
    query = update.callback_query if update.callback_query else None
    uid = str(update.effective_user.id)
    data = context.user_data
    compound = _get_compound_for_unit(data.get("unit", ""))

    try:
        conn = db()
        sid = insert_and_get_id(conn,
            """INSERT INTO submissions
               (telegram_user_id, phone_number, unit, compound, request_type, category,
                service, sub_service, issue_description, photo_path, photo_file_id, priority, required_approvals)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,2)""",
            (uid, data.get("phone", ""), data["unit"], compound, data["request_type"],
             data.get("category"), data.get("service"), data.get("sub_service"),
             data["description"], data.get("photo_path"), data.get("photo_file_id"),
             "high" if is_emergency else "normal")
        )
        conn.close()
    except Exception as e:
        logger.error(f"DB write failed: {e}\n{traceback.format_exc()}")
        msg = "❌ Submission failed. Please try again."
        if query:
            await query.edit_message_text(msg)
        else:
            await update.message.reply_text(msg)
        context.user_data.clear()
        return ConversationHandler.END

    logger.info(f"Submission #{sid}: uid={uid} unit={data.get('unit')} compound={compound}")
    sub_unit = data.get("unit", "")
    sub_service = data.get("service") or data.get("request_type", "")
    context.user_data.clear()
    try:
        # Notify field agents
        for auid in _get_field_agents_for_unit(sub_unit):
            await _notify(context.application, auid,
                f"🆕 *New Ticket #{sid}*\n🏠 {sub_unit} / {sub_service}")
        # Notify approver_1 for this compound
        if compound:
            for auid in _get_role_uids_for_compound(compound, "approver_1"):
                await _notify(context.application, auid,
                    f"🆕 *New Request #{sid} submitted*\n🏠 {sub_unit} / {sub_service}")
    except Exception as e:
        logger.error(f"submission notify failed: {e}")
    emoji = "🚨" if is_emergency else "✅"
    msg = f"{emoji} **Request #{sid} submitted!**\n\nSay **hi** for another."

    if query:
        await query.edit_message_text(msg, parse_mode="Markdown")
    else:
        await update.message.reply_text(msg, parse_mode="Markdown")

    return ConversationHandler.END


# ── Follow Up path ───────────────────────────────────────────────────
async def followup_id_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rid = update.message.text.strip()
    if not rid.isdigit():
        await update.message.reply_text("⚠️ Enter a numeric Request ID:")
        return FOLLOWUP_ID

    conn = db()
    row = conn.execute("SELECT id, unit, service, issue_description, status, resident_confirmed FROM submissions WHERE id=?", (int(rid),)).fetchone()
    conn.close()

    if not row:
        await update.message.reply_text("❌ Request ID not found. Try again:")
        return FOLLOWUP_ID

    if row["unit"] not in context.user_data.get("units", []):
        await update.message.reply_text("❌ Request ID not found for your unit. Try again:")
        return FOLLOWUP_ID

    context.user_data["followup_id"] = int(rid)
    context.user_data["request_type"] = "Follow Up"
    context.user_data["unit"] = row["unit"]
    context.user_data["service"] = row["service"]
    context.user_data["category"] = "Follow Up"
    context.user_data["_back_target"] = "followup_note"

    if row["status"] == "quality_approved" and row.get("resident_confirmed", 0) == 0:
        await update.message.reply_text(
            f"✅ **Ticket #{rid} is marked as completed by quality inspection.**\n"
            f"Service: {row['service']}\n\n"
            "Please confirm if the work was completed satisfactorily:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Confirm Completion", callback_data=f"res_conf:{rid}")],
                [InlineKeyboardButton("🔙 Back", callback_data="back:fuid")]
            ]),
            parse_mode="Markdown"
        )
        return FOLLOWUP_STATUS

    await update.message.reply_text(
        f"🔄 Following up on **#{rid}** — {row['service']}\n"
        f"Status: **{row['status']}**\n"
        f"_\"{row['issue_description'][:80] if row['issue_description'] else 'N/A'}...\"_\n\n"
        "Select what you want to report:",
        reply_markup=mk_buttons(FOLLOWUP_STATUSES, "fstatus", back_cb="back:fuid"),
        parse_mode="Markdown"
    )
    return FOLLOWUP_STATUS


async def followup_status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    
    if query.data.startswith("res_conf:"):
        rid = int(query.data.split(":")[1])
        conn = db()
        conn.execute("UPDATE submissions SET resident_confirmed=1, status='closed' WHERE id=?", (rid,))
        conn.commit()
        conn.close()
        await query.edit_message_text(f"✅ **Thank you!** Ticket #{rid} has been successfully closed.", parse_mode="Markdown")
        return ConversationHandler.END

    idx = int(query.data.split(":")[1])
    context.user_data["followup_status"] = FOLLOWUP_STATUSES[idx]
    context.user_data["_back_target"] = "followup_note"
    await query.edit_message_text(
        f"Status: **{FOLLOWUP_STATUSES[idx]}**\n\nAdd a note (min 5 chars):",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back", callback_data="back:note_to_status"),
             InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
        ]),
        parse_mode="Markdown"
    )
    return FOLLOWUP_NOTE


async def followup_note_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = sanitize(update.message.text.strip())
    if len(text) < 5:
        await update.message.reply_text("⚠️ At least 5 characters:")
        return FOLLOWUP_NOTE
    context.user_data["description"] = f"[Follow-up #{context.user_data['followup_id']}] Status: {context.user_data['followup_status']} — {text}"
    context.user_data["_back_target"] = "followup_note"

    await update.message.reply_text(
        "📸 Attach photo (optional):",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⏭️ Skip", callback_data="photo:skip")],
            [InlineKeyboardButton("🔙 Back", callback_data="photo:back_fnote"),
             InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
        ])
    )
    return PHOTO


# ── Emergency path ───────────────────────────────────────────────────
async def emergency_desc_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = sanitize(update.message.text.strip())
    if len(text) < 5:
        await update.message.reply_text("⚠️ At least 5 characters:")
        return EMERGENCY_DESC

    context.user_data["description"] = text
    context.user_data["category"] = "Emergency"
    context.user_data["service"] = "Emergency"
    context.user_data["_back_target"] = "emergency_desc"

    await update.message.reply_text(
        "📸 Attach photo (strongly recommended for emergencies):",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⏭️ Skip", callback_data="photo:skip")],
            [InlineKeyboardButton("🔙 Back", callback_data="photo:back_emdesc"),
             InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
        ])
    )
    return EMERGENCY_PHOTO


async def emergency_photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Emergency photo — save then go to emergency confirm."""
    photo = update.message.photo[-1]
    ok, url = await _save_photo(photo, "EMERG", context)
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
    await update.message.reply_text(
        "🚨 **EMERGENCY — Confirm Submission**\n\n"
        + build_summary(context.user_data),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🚨 Submit Emergency", callback_data="emergency_confirm:yes")],
            [InlineKeyboardButton("🔙 Back", callback_data="emergency_confirm:back_emphoto"),
             InlineKeyboardButton("❌ Cancel", callback_data="emergency_confirm:no")],
        ]),
        parse_mode="Markdown"
    )
    return EMERGENCY_CONFIRM


async def show_emergency_confirm_callback(query, context: ContextTypes.DEFAULT_TYPE):
    await query.message.reply_text(
        "🚨 **EMERGENCY — Confirm Submission**\n\n"
        + build_summary(context.user_data),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🚨 Submit Emergency", callback_data="emergency_confirm:yes")],
            [InlineKeyboardButton("🔙 Back", callback_data="emergency_confirm:back_emphoto"),
             InlineKeyboardButton("❌ Cancel", callback_data="emergency_confirm:no")],
        ]),
        parse_mode="Markdown"
    )
    return EMERGENCY_CONFIRM


async def emergency_confirm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    data = query.data.split(":")[1]
    if data == "no":
        context.user_data.clear()
        await query.edit_message_text("❌ Cancelled. Say **hi** to start over.", parse_mode="Markdown")
        return ConversationHandler.END
    elif data == "back_emphoto":
        return await back_to_emergency_desc(query, context)
    return await write_submission(update, context, is_emergency=True)


# ── Back navigation helpers ─────────────────────────────────────────
async def back_to_units(query, context):
    """Back from request type to unit selection."""
    units = context.user_data.get("units", [])
    await query.edit_message_text("🏠 Select unit:",
        reply_markup=mk_buttons(units, "unit"))
    return UNITS

async def back_to_request_type(uq, context):
    query = _cq(uq)
    """Back from category to request type."""
    await query.edit_message_text(
        f"✅ Unit: {context.user_data.get('unit', '')}\n\n📋 Request type:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🆕 New Request", callback_data="type:new")],
            [InlineKeyboardButton("🔄 Follow Up", callback_data="type:followup")],
            [InlineKeyboardButton("🚨 Emergency", callback_data="type:emergency")],
            [InlineKeyboardButton("🔙 Back", callback_data="back:units"),
             InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
        ]))
    return REQUEST_TYPE

async def back_to_category(uq, context):
    """Back from service to category."""
    query = _cq(uq)
    await query.edit_message_text("🏗️ Category:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🛠️ Facilities", callback_data="cat:facilities")],
            [InlineKeyboardButton("🔧 Maintenance", callback_data="cat:maintenance")],
            [InlineKeyboardButton("🔙 Back", callback_data="back:type"),
             InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
        ]))
    return CATEGORY


async def back_to_sub_service(update, context):
    """Back from DESCRIPTION to sub-service selection."""
    query = _cq(update); await query.answer()
    main_cat = context.user_data.get("main_category", "Maintenance")
    cat = context.user_data.get("service", "")
    subs = _get_sub_categories(main_cat, cat)
    emoji = "🔧" if main_cat == "Maintenance" else "🛠️"
    buttons = [[InlineKeyboardButton(s, callback_data=f"subsvc:{s}")] for s in subs]
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="back:service"),
                    InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
    await query.edit_message_text(
        f"{emoji} **{cat}** — select issue type:",
        reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
    return SUB_SERVICE


async def back_to_sub_service_via_message(update: Update, context):
    """Back to sub-service selection via text message."""
    main_cat = context.user_data.get("main_category", "Maintenance")
    cat = context.user_data.get("service", "")
    subs = _get_sub_categories(main_cat, cat)
    emoji = "🔧" if main_cat == "Maintenance" else "🛠️"
    buttons = [[InlineKeyboardButton(s, callback_data=f"subsvc:{s}")] for s in subs]
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="back:service"),
                    InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
    await update.message.reply_text(
        f"{emoji} **{cat}** — select issue type:",
        reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
    return SUB_SERVICE


async def back_to_description(uq, context):
    """Back from PHOTO to description prompt (re-ask)."""
    query = _cq(uq)
    svc = context.user_data.get("service", "")
    await query.edit_message_text(
        f"✅ {svc}\n\n📝 Describe the issue (min 5 chars):",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back", callback_data="back:desc_to_cat"),
             InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
        ])
    )
    return DESCRIPTION


async def back_to_followup_id(uq, context):
    query = _cq(uq)
    """Back from followup status to followup ID prompt."""
    await query.edit_message_text(
        "🔄 **Follow Up**\n\nEnter the **Request ID** you want to follow up on:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back", callback_data="back:type"),
             InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
        ]),
        parse_mode="Markdown"
    )
    return FOLLOWUP_ID


async def back_to_followup_note(uq, context):
    query = _cq(uq)
    """Back from PHOTO to followup note prompt (re-ask)."""
    status = context.user_data.get("followup_status", "")
    await query.edit_message_text(
        f"Status: **{status}**\n\nAdd a note (min 5 chars):",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back", callback_data="back:note_to_status"),
             InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
        ]),
        parse_mode="Markdown"
    )
    return FOLLOWUP_NOTE


async def back_to_emergency_desc(uq, context):
    query = _cq(uq)
    """Back from EMERGENCY_PHOTO to emergency description prompt."""
    await query.edit_message_text(
        "🚨 **EMERGENCY**\n\nDescribe the emergency (min 5 chars):",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back", callback_data="back:type"),
             InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
        ]),
        parse_mode="Markdown"
    )
    return EMERGENCY_DESC


async def text_back_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle 'back' or '/back' text in text-input states."""
    current_state = context.user_data.get("_back_target", "")
    if current_state == "sub_service":
        return await back_to_sub_service_via_message(update, context)
    elif current_state == "description":
        return await back_to_category_via_message(update, context)
    elif current_state == "followup_id":
        return await back_to_request_type_via_message(update, context)
    elif current_state == "followup_note":
        return await back_to_followup_id_via_message(update, context)
    elif current_state == "emergency_desc":
        return await back_to_request_type_via_message(update, context)
    # Fallback: go to request type
    return await back_to_request_type_via_message(update, context)


async def back_to_category_via_message(update: Update, context):
    """Back from description text to category selection."""
    await update.message.reply_text("🏗️ Category:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🛠️ Facilities", callback_data="cat:facilities")],
            [InlineKeyboardButton("🔧 Maintenance", callback_data="cat:maintenance")],
            [InlineKeyboardButton("🔙 Back", callback_data="back:type"),
             InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
        ]))
    return CATEGORY


async def back_to_request_type_via_message(update: Update, context):
    """Back to request type selection via message (for text-input states)."""
    await update.message.reply_text(
        f"✅ Unit: {context.user_data.get('unit', '')}\n\n📋 Request type:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🆕 New Request", callback_data="type:new")],
            [InlineKeyboardButton("🔄 Follow Up", callback_data="type:followup")],
            [InlineKeyboardButton("🚨 Emergency", callback_data="type:emergency")],
            [InlineKeyboardButton("🔙 Back", callback_data="back:units"),
             InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
        ]))
    return REQUEST_TYPE


async def back_to_followup_id_via_message(update: Update, context):
    """Back to followup ID prompt via message."""
    await update.message.reply_text(
        "🔄 **Follow Up**\n\nEnter the **Request ID** you want to follow up on:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back", callback_data="back:type"),
             InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
        ]),
        parse_mode="Markdown"
    )
    return FOLLOWUP_ID


async def back_to_followup_status_from_note(uq, context):
    query = _cq(uq)
    """Back from FOLLOWUP_NOTE to status selection."""
    fid = context.user_data.get("followup_id", "?")
    await query.edit_message_text(
        f"🔄 Following up on **#{fid}**\n\nSelect status:",
        reply_markup=mk_buttons(FOLLOWUP_STATUSES, "fstatus", back_cb="back:fuid"),
        parse_mode="Markdown"
    )
    return FOLLOWUP_STATUS


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


# ── Global error handler ─────────────────────────────────────────────
async def timeout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    try:
        await update.effective_message.reply_text(
            "⏰ Your session has expired. Send us a message to start a new request."
        )
    except Exception:
        pass
    return ConversationHandler.END


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Exception: {context.error}\n{traceback.format_exc()}")
    if update and hasattr(update, 'effective_message'):
        try:
            await update.effective_message.reply_text(
                f"⚠️ Error: {str(context.error)}"
            )
        except Exception:
            pass


# ── Main ─────────────────────────────────────────────────────────────
def validate_db_schema_local():
    """
    Verify all expected columns exist in the submissions table.
    Returns (True, None) if OK, (False, error_message) if not.
    """
    try:
        validate_schema()
    except Exception as err:
        raise err
    return True, None


async def load_state_from_db(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Load latest state from DB before processing update in a serverless environment."""
    if update.effective_user:
        await context.application.persistence.refresh_user_data(update.effective_user.id, context.user_data)
    if update.effective_chat:
        await context.application.persistence.refresh_chat_data(update.effective_chat.id, context.chat_data)


def create_application():
    """Create and configure the PTB Application. Called by both polling and webhook modes."""
    if not TOKEN:
        raise RuntimeError("Bot token not found!")

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
        name="resident_main",
        states={
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, phone_handler)],
            UNITS: [CallbackQueryHandler(unit_handler, pattern=r"^unit:")],
            REQUEST_TYPE: [CallbackQueryHandler(request_type_handler, pattern=r"^(type:|back:)")],
            CATEGORY: [CallbackQueryHandler(category_handler, pattern=r"^(cat:|back:)")],
            SERVICE: [CallbackQueryHandler(service_handler, pattern=r"^(svc:|back:cat$)")],
            SUB_SERVICE: [CallbackQueryHandler(sub_service_handler, pattern=r"^(subsvc:|back:service$)")],
            DESCRIPTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.Regex(BACK_TEXT_RE), description_handler),
                MessageHandler(filters.Regex(BACK_TEXT_RE), text_back_handler),
                CallbackQueryHandler(back_to_category, pattern=r"^back:desc_to_cat"),
                CallbackQueryHandler(back_to_sub_service, pattern=r"^back:desc_to_subsvc"),
            ],
            PHOTO: [
                MessageHandler(filters.PHOTO, photo_handler),
                CallbackQueryHandler(photo_skip_handler, pattern=r"^photo:skip"),
                CallbackQueryHandler(back_to_description, pattern=r"^photo:back_desc"),
                CallbackQueryHandler(back_to_followup_note, pattern=r"^photo:back_fnote"),
            ],
            CONFIRM: [CallbackQueryHandler(confirm_handler, pattern=r"^confirm:")],
            FOLLOWUP_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.Regex(BACK_TEXT_RE), followup_id_handler),
                MessageHandler(filters.Regex(BACK_TEXT_RE), text_back_handler),
                CallbackQueryHandler(back_to_request_type, pattern=r"^back:type$"),
            ],
            FOLLOWUP_STATUS: [
                CallbackQueryHandler(followup_status_handler, pattern=r"^fstatus:"),
                CallbackQueryHandler(back_to_followup_id, pattern=r"^back:fuid"),
            ],
            FOLLOWUP_NOTE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.Regex(BACK_TEXT_RE), followup_note_handler),
                MessageHandler(filters.Regex(BACK_TEXT_RE), text_back_handler),
                CallbackQueryHandler(back_to_followup_status_from_note, pattern=r"^back:note_to_status"),
            ],
            EMERGENCY_DESC: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.Regex(BACK_TEXT_RE), emergency_desc_handler),
                MessageHandler(filters.Regex(BACK_TEXT_RE), text_back_handler),
                CallbackQueryHandler(back_to_request_type, pattern=r"^back:type$"),
            ],
            EMERGENCY_PHOTO: [
                MessageHandler(filters.PHOTO, emergency_photo_handler),
                CallbackQueryHandler(emergency_photo_skip, pattern=r"^photo:skip"),
                CallbackQueryHandler(back_to_emergency_desc, pattern=r"^photo:back_emdesc"),
            ],
            EMERGENCY_CONFIRM: [CallbackQueryHandler(emergency_confirm_handler, pattern=r"^emergency_confirm:")],
            ConversationHandler.TIMEOUT: [
                MessageHandler(filters.ALL, timeout_handler),
                CallbackQueryHandler(timeout_handler),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(cancel, pattern=r"^cancel$"),
        ],
    )

    app.add_handler(conv_handler)
    logger.info("Field Service Bot — ready (Resident)")
    return app


def main():
    try:
        app = create_application()
    except RuntimeError as e:
        logger.error(str(e))
        print(str(e), file=sys.stderr)
        sys.exit(1)

    logger.info("=" * 50)
    logger.info("Field Service Bot starting — Phase 7 (Service Hierarchy)")
    logger.info("Services: loaded dynamically from DB (services table)")
    logger.info(f"Follow-up statuses: {len(FOLLOWUP_STATUSES)}")
    logger.info("=" * 50)
    app.run_polling()


if __name__ == "__main__":
    main()
