"""
Azure Functions entry point for GUC Field Service Bot.
Two HTTP webhook endpoints — one per Telegram bot.

Local test:  func start
Deploy:      func azure functionapp publish <name>

Environment variables:
    BOT_TOKEN           — Resident bot Telegram token
    AGENT_BOT_TOKEN     — Agent bot Telegram token
    DB_CONNECTION_STRING— Azure SQL / local SQL Server connection
    WEBHOOK_SECRET      — Secret token for webhook verification
    WEBHOOK_BASE_URL    — Base URL of this Function App (for setWebhook)
"""

import azure.functions as func
import json
import logging
import os

from telegram import Update

# ── Module-level cache for Application instances ─────────────────────
# Avoids re-initializing on every invocation (cold start = once, then cached)
_apps = {}

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

app = func.FunctionApp()

logger = logging.getLogger(__name__)


async def _get_app(bot_type):
    """Create or return cached Application for the given bot type."""
    if bot_type not in _apps:
        if bot_type == "resident":
            from bot import create_application
            ptb_app = create_application()
        elif bot_type == "agent":
            from agent_bot import create_application
            ptb_app = create_application()
        else:
            raise ValueError(f"Unknown bot_type: {bot_type}")

        await ptb_app.initialize()
        _apps[bot_type] = ptb_app
        logger.info(f"Application initialized: {bot_type}")

    return _apps[bot_type]


def _verify_secret(req):
    """Check X-Telegram-Bot-Api-Secret-Token header. Returns True if valid or not configured."""
    if not WEBHOOK_SECRET:
        return True
    secret = req.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    return secret == WEBHOOK_SECRET


# ── Webhook: Resident Bot ────────────────────────────────────────────

@app.function_name(name="webhook_resident")
@app.route(route="webhook/resident", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
async def webhook_resident(req: func.HttpRequest) -> func.HttpResponse:
    """Handle incoming Telegram updates for the resident bot (@GUCMain1bot)."""
    if not _verify_secret(req):
        return func.HttpResponse("Unauthorized", status_code=401)

    try:
        ptb_app = await _get_app("resident")
        body = req.get_json()
        update = Update.de_json(body, ptb_app.bot)
        await ptb_app.process_update(update)
        await ptb_app.persistence.flush()
        return func.HttpResponse("OK", status_code=200)
    except Exception as e:
        import traceback
        err = traceback.format_exc()
        logger.exception("Resident webhook failed")
        return func.HttpResponse(f"Error: {err}", status_code=200)


# ── Webhook: Agent Bot ───────────────────────────────────────────────

@app.function_name(name="webhook_agent")
@app.route(route="webhook/agent", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
async def webhook_agent(req: func.HttpRequest) -> func.HttpResponse:
    """Handle incoming Telegram updates for the agent bot (@GUCMain2bot)."""
    if not _verify_secret(req):
        return func.HttpResponse("Unauthorized", status_code=401)

    try:
        ptb_app = await _get_app("agent")
        body = req.get_json()
        update = Update.de_json(body, ptb_app.bot)
        await ptb_app.process_update(update)
        await ptb_app.persistence.flush()
        return func.HttpResponse("OK", status_code=200)
    except Exception as e:
        import traceback
        err = traceback.format_exc()
        logger.exception("Agent webhook failed")
        return func.HttpResponse(f"Error: {err}", status_code=200)


# ── Register webhooks (run once after deploy) ────────────────────────

@app.function_name(name="set_webhooks")
@app.route(route="webhook/setup", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
async def set_webhooks(req: func.HttpRequest) -> func.HttpResponse:
    """
    POST to this endpoint once after deployment to register webhooks with Telegram.
    Requires WEBHOOK_BASE_URL env var (e.g., https://myapp.azurewebsites.net).
    """
    base = os.environ.get("WEBHOOK_BASE_URL", "").rstrip("/")
    if not base:
        return func.HttpResponse(
            "Missing WEBHOOK_BASE_URL environment variable", status_code=400
        )

    result = {}
    try:
        for bot_type in ("resident", "agent"):
            ptb_app = await _get_app(bot_type)
            url = f"{base}/api/webhook/{bot_type}"
            ok = await ptb_app.bot.set_webhook(
                url=url,
                secret_token=WEBHOOK_SECRET,
                allowed_updates=Update.ALL_TYPES,
            )
            result[bot_type] = {"url": url, "ok": ok}
            logger.info(f"setWebhook({bot_type}): {url} -> {ok}")
        return func.HttpResponse(json.dumps(result), status_code=200)
    except Exception as e:
        import traceback
        err_msg = "".join(traceback.format_exception(type(e), e, e.__traceback__))
        return func.HttpResponse(f"Error during initialization:\n{err_msg}", status_code=500)
