"""Telegram bot entry point: python -m bot.main"""

from __future__ import annotations

import asyncio
import html
import logging
from dataclasses import dataclass, field, replace
from datetime import datetime

import gspread
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import NetworkError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import access as acl
from . import digest, pipeline
from .config import Config, load_config
from .llm import LLM, LLMUnavailable
from .media import Item, items_from_message
from .schema import Analysis, verify
from .sheets import Sheet, entry_to_row, rows_to_setting, setting_to_rows

log = logging.getLogger("onecargobot")

HELP = """<b>OneCargo notes bot</b>
Send me anything: text, voice notes, photos, PDFs, forwarded messages, or a mix. I read it, work out what you mean, and add rows to your Google Sheet. The newest rows are highlighted cyan.

If something is unclear I'll ask before saving.

<b>Commands</b>
/today: follow-up summary now
/reminders: show summary times
/reminders 9:00, 16:00: change summary times (US Eastern)
/colleagues: names I use for "Assigned To" (edit them in the Settings tab)
/undo: remove the last rows I added
/cancel: drop the question I'm waiting on

You can also just ask, e.g. "what's overdue for Bob?" or "how much do I owe Mike?\"
/whoami: your Telegram ID"""

ADMIN_HELP = """

<b>Admin</b>
/users: who has access
/adduser @username or ID: give someone access
/removeuser @username or ID: take access away
/setowner @username or ID: the owner gets the daily summaries"""


@dataclass
class Pending:
    """A batch waiting for the user to answer a clarifying question."""
    items: list[Item]
    analysis: Analysis
    warnings: list[str]
    provider: str


@dataclass
class LastSave:
    rows: list[int]
    originals: dict[int, str]
    closed: dict[int, str]  # row -> previous status


@dataclass
class ChatState:
    """Per-chat state, so the owner and the developer don't step on each other."""
    buffer: list[Item] = field(default_factory=list)
    pending: Pending | None = None
    last_save: LastSave | None = None


@dataclass
class State:
    access: acl.Access = field(default_factory=acl.Access)
    chats: dict[int, ChatState] = field(default_factory=dict)
    schedule_key: str = ""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)  # one sheet writer at a time

    def chat(self, chat_id: int) -> ChatState:
        return self.chats.setdefault(chat_id, ChatState())


# ---------------------------------------------------------------- helpers

def cfg_of(ctx: ContextTypes.DEFAULT_TYPE) -> Config:
    return ctx.application.bot_data["cfg"]


def sheet_of(ctx: ContextTypes.DEFAULT_TYPE) -> Sheet:
    return ctx.application.bot_data["sheet"]


def llm_of(ctx: ContextTypes.DEFAULT_TYPE) -> LLM:
    return ctx.application.bot_data["llm"]


def state_of(ctx: ContextTypes.DEFAULT_TYPE) -> State:
    return ctx.application.bot_data["state"]


def now_local(cfg: Config) -> datetime:
    """Naive datetime in the configured timezone (the sheet's dates are naive Eastern)."""
    return datetime.now(cfg.timezone).replace(tzinfo=None, second=0, microsecond=0)


async def authorized(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    state = state_of(ctx)
    user = update.effective_user
    if user is None:
        return False
    access = state.access
    if not access.admins:
        if update.effective_message:
            await update.effective_message.reply_text(
                f"Your Telegram user ID is {user.id}.\nPut it in ADMIN_USER_IDS in the .env file and restart me."
            )
        return False
    if access.allowed(user.id, user.username):
        await remember_user_id(ctx, user.id, user.username)
        return True

    log.warning("Unauthorized user %s (@%s)", user.id, user.username)
    if update.effective_message and update.effective_chat and update.effective_chat.type == "private":
        await update.effective_message.reply_text(
            f"This is a private bot. Ask the admin to add you.\nYour Telegram ID: {user.id}")
        # Tell the admins once per person, with ready-to-tap commands.
        notified = ctx.application.bot_data.setdefault("notified_unknown", set())
        if user.id not in notified:
            notified.add(user.id)
            who = html.escape(user.full_name) + (f" (@{html.escape(user.username)})" if user.username else "")
            for admin in access.admins:
                await send(ctx, admin, f"🔐 {who} tried to use the bot.\n"
                                       f"Give access: /adduser {user.id}\nMake owner: /setowner {user.id}")
    return False


async def remember_user_id(ctx: ContextTypes.DEFAULT_TYPE, user_id: int, username: str | None) -> None:
    """Someone added by @username just wrote: store their numeric ID too (needed for summaries)."""
    access, sheet = state_of(ctx).access, sheet_of(ctx)
    owner = acl.with_id(access.owner, user_id, username)
    users = acl.with_id(access.users, user_id, username)
    if owner != access.owner:
        access.owner = owner
        await asyncio.to_thread(sheet.write_setting, "owner", acl.format_people(owner))
        await sync_schedule(ctx, force=True)
    if users != access.users:
        access.users = users
        await asyncio.to_thread(sheet.write_setting, "users", acl.format_people(users))


async def load_access(ctx_or_app, settings=None) -> None:
    app = getattr(ctx_or_app, "application", ctx_or_app)
    cfg: Config = app.bot_data["cfg"]
    state: State = app.bot_data["state"]
    if settings is None:
        settings = await asyncio.to_thread(app.bot_data["sheet"].read_settings)
    state.access = acl.Access(admins=set(cfg.admin_user_ids),
                              owner=acl.parse_people(settings.get("owner"))[:1],
                              users=acl.parse_people(settings.get("users")))


def split_message(text: str, limit: int = 3900) -> list[str]:
    """Telegram caps messages at 4096 chars; split on line breaks so HTML tags stay intact."""
    chunks, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:  # a single huge line (rare): hard-split it
            chunks.append(line[:limit])
            line = line[limit:]
        if current and len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks or [""]


async def send(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, markup=None) -> None:
    chunks = split_message(text)
    for n, chunk in enumerate(chunks):
        await ctx.bot.send_message(chat_id, chunk, parse_mode=ParseMode.HTML,
                                   reply_markup=markup if n == len(chunks) - 1 else None,
                                   disable_web_page_preview=True)


UNDO_BUTTON = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Undo", callback_data="undo")]])
CLARIFY_BUTTONS = InlineKeyboardMarkup([[
    InlineKeyboardButton("💾 Save draft anyway", callback_data="save_draft"),
    InlineKeyboardButton("✖️ Cancel", callback_data="cancel"),
]])


# ---------------------------------------------------------------- message intake

async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, ctx):
        return
    cfg = cfg_of(ctx)
    msg = update.effective_message
    try:
        items, problems = await items_from_message(msg, cfg.timezone)
    except Exception:
        log.exception("Couldn't read message")
        await msg.reply_text("❌ I couldn't download that from Telegram. Please send it again.")
        return
    if problems:
        await msg.reply_text("\n".join(problems))
    if not items:
        return

    chat_id = msg.chat_id
    state_of(ctx).chat(chat_id).buffer.extend(items)
    # Wait a few seconds so albums, "text + photo", and multi-forwards arrive as one batch.
    for job in ctx.job_queue.get_jobs_by_name(f"flush-{chat_id}"):
        job.schedule_removal()
    ctx.job_queue.run_once(flush_job, cfg.batch_window_seconds, chat_id=chat_id, name=f"flush-{chat_id}")
    await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)


async def flush_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    state = state_of(ctx)
    chat_id = ctx.job.chat_id
    chat = state.chat(chat_id)
    items, chat.buffer = chat.buffer, []
    if not items:
        return
    async with state.lock:
        try:
            await process_batch(ctx, chat_id, items)
        except LLMUnavailable as exc:
            log.error("All AI providers unavailable: %s", exc)
            chat.buffer = items + chat.buffer  # keep it so the user can retry by sending anything
            await send(ctx, chat_id, "⏳ The AI is over its free limit or down right now. I kept your message; "
                                     "send me anything later (even a dot) and I'll retry.")
        except Exception:
            log.exception("Failed to process batch")
            await send(ctx, chat_id, "❌ Something went wrong. Please check the sheet and resend if the rows are missing.")


async def process_batch(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, items: list[Item]) -> None:
    cfg, sheet, llm, state = cfg_of(ctx), sheet_of(ctx), llm_of(ctx), state_of(ctx)
    await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)

    chat = state.chat(chat_id)
    question = None
    pending = chat.pending
    if pending:
        # This batch answers the question we asked: analyze original + answer together.
        question = pending.analysis.clarification_question
        items = pending.items + [replace(i, header="[ANSWER] " + i.header) for i in items]

    settings = await asyncio.to_thread(sheet.read_settings)
    rows = await asyncio.to_thread(sheet.read_rows)
    recent = rows[-cfg.recent_rows_context_limit:]
    context = pipeline.build_context(now_local(cfg), settings.colleagues, recent, question)

    analysis, provider = await llm.analyze(items, context)
    if pending is chat.pending:
        chat.pending = None
    analysis, warnings = verify(analysis, settings.colleagues, pipeline.open_row_numbers(rows))
    pipeline.apply_mentions(analysis, items, settings.colleagues)
    log.info("intent=%s entries=%d clarify=%s via %s", analysis.intent, len(analysis.entries),
             analysis.needs_clarification, provider)

    if analysis.intent == "question" and not analysis.entries:
        q = "\n".join(i.describe() for i in items)
        if analysis.transcript:
            q += f"\n(voice) {analysis.transcript}"
        prompt = (f"NOW: {now_local(cfg).strftime('%A, %m/%d/%Y %I:%M %p')}\n\nQUESTION:\n{q}\n\n"
                  f"LOG ROWS (CSV):\n{pipeline.rows_csv(rows[-400:])}")
        answer = await llm.answer(prompt)
        await send(ctx, chat_id, html.escape(answer or "I couldn't find that in the sheet."))
        return

    if analysis.needs_clarification:
        chat.pending = Pending(items=items, analysis=analysis, warnings=warnings, provider=provider)
        text = f"❓ {html.escape(analysis.clarification_question or '')}"
        if analysis.checks:
            text += "\n\n" + pipeline.checks_block(analysis)
        if analysis.entries:
            text += "\n\n<i>My draft so far:</i>\n" + pipeline.describe_entries(analysis.entries)
        text += "\n\nReply with the answer (text or voice), or use the buttons."
        await send(ctx, chat_id, text, CLARIFY_BUTTONS)
        return

    if not analysis.entries:
        await send(ctx, chat_id, html.escape(analysis.reply or "Nothing to log there. Send /help to see what I can do."))
        return

    await save(ctx, chat_id, items, analysis, warnings, provider, settings.auto_close)


async def save(ctx, chat_id: int, items: list[Item], analysis: Analysis, warnings: list[str],
               provider: str, auto_close: bool) -> None:
    cfg, sheet, state = cfg_of(ctx), sheet_of(ctx), state_of(ctx)
    original = pipeline.build_original(items, analysis)
    values = [entry_to_row(e, now_local(cfg), original) for e in analysis.entries]

    settings = await asyncio.to_thread(sheet.read_settings)
    previous = setting_to_rows(settings.get("last_added_rows"))
    new_rows = await asyncio.to_thread(sheet.append, values)
    await asyncio.to_thread(sheet.highlight, new_rows, previous)
    await asyncio.to_thread(sheet.write_setting, "last_added_rows", rows_to_setting(new_rows))

    closed: dict[int, str] = {}
    if auto_close and analysis.closes_rows:
        current = {r.row: r for r in await asyncio.to_thread(sheet.read_rows)}
        for row in analysis.closes_rows:
            if row in current and current[row]["Status"] != "Done":
                closed[row] = current[row]["Status"]
                await asyncio.to_thread(sheet.set_status, row, "Done")

    state.chat(chat_id).last_save = LastSave(rows=new_rows, originals={r: v[-1] for r, v in zip(new_rows, values)}, closed=closed)
    await send(ctx, chat_id, pipeline.saved_reply(analysis, new_rows, list(closed), warnings, provider),
               UNDO_BUTTON)


# ---------------------------------------------------------------- buttons & commands

async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not await authorized(update, ctx):
        await query.answer()
        return
    await query.answer()
    state = state_of(ctx)
    chat_id = query.message.chat_id
    async with state.lock:
        if query.data == "undo":
            await query.edit_message_reply_markup(None)
            await do_undo(ctx, chat_id)
        elif query.data == "save_draft" and state.chat(chat_id).pending:
            chat = state.chat(chat_id)
            p, chat.pending = chat.pending, None
            await query.edit_message_reply_markup(None)
            if not p.analysis.entries:
                await send(ctx, chat_id, "There's no draft to save. Please resend with more detail.")
                return
            settings = await asyncio.to_thread(sheet_of(ctx).read_settings)
            await save(ctx, chat_id, p.items, p.analysis, p.warnings, p.provider, settings.auto_close)
        elif query.data == "cancel":
            state.chat(chat_id).pending = None
            await query.edit_message_reply_markup(None)
            await send(ctx, chat_id, "Okay, dropped it. Nothing was saved.")


async def do_undo(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    state, sheet = state_of(ctx), sheet_of(ctx)
    chat = state.chat(chat_id)
    last = chat.last_save
    if not last:
        await send(ctx, chat_id, "Nothing to undo.")
        return
    for row, status in last.closed.items():
        await asyncio.to_thread(sheet.set_status, row, status)
    deleted = await asyncio.to_thread(sheet.delete_rows_if_match, last.rows, last.originals)
    await asyncio.to_thread(sheet.write_setting, "last_added_rows", "")
    chat.last_save = None
    skipped = len(last.rows) - len(deleted)
    text = f"↩️ Removed {len(deleted)} row(s)."
    if last.closed:
        text += f" Restored status on row(s) {', '.join(map(str, last.closed))}."
    if skipped:
        text += f" {skipped} row(s) had been edited or moved, so I left them; please check the sheet."
    await send(ctx, chat_id, text)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, ctx):
        return
    text = HELP + (ADMIN_HELP if state_of(ctx).access.is_admin(update.effective_user.id) else "")
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, ctx):
        return
    rows = await asyncio.to_thread(sheet_of(ctx).read_rows)
    await send(ctx, update.effective_chat.id, digest.build_digest(rows, now_local(cfg_of(ctx))))


async def cmd_undo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, ctx):
        return
    async with state_of(ctx).lock:
        await do_undo(ctx, update.effective_chat.id)


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, ctx):
        return
    chat = state_of(ctx).chat(update.effective_chat.id)
    had = chat.pending is not None
    chat.pending = None
    await update.effective_message.reply_text("Dropped it. Nothing was saved." if had else "Nothing pending.")


async def cmd_colleagues(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, ctx):
        return
    settings = await asyncio.to_thread(sheet_of(ctx).read_settings)
    names = "\n".join(f"• {html.escape(c.describe())}" for c in settings.colleagues) or "(none yet)"
    await send(ctx, update.effective_chat.id,
               f"<b>Colleagues</b>\n{names}\n\nEdit them in the <b>Settings</b> tab: "
               "column E = name, F = Telegram @username, G = role.")


async def cmd_reminders(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, ctx):
        return
    sheet = sheet_of(ctx)
    arg = " ".join(ctx.args or []).strip()
    if arg:
        times = digest.parse_times(arg)
        if not times:
            await update.effective_message.reply_text("I couldn't read those times. Example: /reminders 9:00, 16:00")
            return
        value = ", ".join(t.strftime("%H:%M") for t in times)
        await asyncio.to_thread(sheet.write_setting, "reminder_times", value)
    await sync_schedule(ctx, force=bool(arg))
    settings = await asyncio.to_thread(sheet.read_settings)
    times = digest.parse_times(settings.get("reminder_times"))
    shown = ", ".join(t.strftime("%I:%M %p").lstrip("0") for t in times) or "none"
    await update.effective_message.reply_text(
        f"Daily summaries: {shown} (US Eastern), on {settings.get('reminder_days') or 'every day'}.\n"
        "Change them with /reminders 8:30, 16:00 or in the Settings tab."
    )


async def cmd_whoami(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    access = state_of(ctx).access
    role = ("admin" if access.is_admin(user.id) else "owner" if access.is_owner(user.id, user.username)
            else "user" if access.allowed(user.id, user.username) else "no access")
    await update.effective_message.reply_text(f"Your Telegram ID: {user.id}\nRole: {role}")


async def require_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    if state_of(ctx).access.is_admin(update.effective_user.id):
        return True
    await update.effective_message.reply_text("Only the admin can do that.")
    return False


def describe_access(access: acl.Access) -> str:
    owner = acl.format_people(access.owner) or "not set (summaries go to the admin)"
    users = acl.format_people(access.users) or "none"
    admins = ", ".join(map(str, sorted(access.admins))) or "none"
    return f"<b>Access</b>\nAdmin: {admins}\nOwner: {html.escape(owner)}\nUsers: {html.escape(users)}"


async def cmd_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, ctx):
        return
    await send(ctx, update.effective_chat.id, describe_access(state_of(ctx).access))


async def _edit_people(update: Update, ctx: ContextTypes.DEFAULT_TYPE, key: str, add: bool) -> None:
    if not await require_admin(update, ctx):
        return
    tokens = [t for t in (acl.parse_token(a) for a in ctx.args or []) if t]
    if not tokens:
        await update.effective_message.reply_text(f"Usage: /{update.effective_message.text.split()[0][1:]} @username or numeric ID")
        return
    access, sheet = state_of(ctx).access, sheet_of(ctx)
    if key == "owner":
        new = [tokens]  # one owner (ID and/or @username); replaces the previous one
    elif add:
        new = acl.add(access.users, [[t] for t in tokens])
    else:
        new = acl.remove(access.users, tokens)
    await asyncio.to_thread(sheet.write_setting, key, acl.format_people(new))
    await load_access(ctx)
    await sync_schedule(ctx, force=True)
    note = ""
    if any(not t.isdigit() for t in tokens) and (add or key == "owner"):
        note = "\nThey need to send the bot any message once so I learn their ID."
    await send(ctx, update.effective_chat.id, "✅ Saved.\n" + describe_access(state_of(ctx).access) + note)


async def cmd_adduser(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _edit_people(update, ctx, "users", add=True)


async def cmd_removeuser(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _edit_people(update, ctx, "users", add=False)


async def cmd_setowner(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _edit_people(update, ctx, "owner", add=True)


# ---------------------------------------------------------------- scheduled summaries

async def digest_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    rows = await asyncio.to_thread(sheet_of(ctx).read_rows)
    await send(ctx, ctx.job.chat_id, digest.build_digest(rows, now_local(cfg_of(ctx))))


async def sync_schedule(ctx: ContextTypes.DEFAULT_TYPE, force: bool = False) -> None:
    """(Re)schedule summaries from the Settings tab. Runs every few minutes, so sheet edits apply too."""
    cfg, state = cfg_of(ctx), state_of(ctx)
    settings = await asyncio.to_thread(sheet_of(ctx).read_settings)
    await load_access(ctx, settings)  # picks up owner/users edited directly in the sheet
    targets = state.access.summary_chat_ids()
    key = f"{settings.get('reminder_times')}|{settings.get('reminder_days')}|{targets}"
    if key == state.schedule_key and not force:
        return
    state.schedule_key = key
    for job in ctx.job_queue.get_jobs_by_name("digest"):
        job.schedule_removal()
    days = digest.parse_days(settings.get("reminder_days"))
    times = digest.parse_times(settings.get("reminder_times"))
    for chat_id in targets:
        for t in times:
            ctx.job_queue.run_daily(digest_job, t.replace(tzinfo=cfg.timezone), days=days,
                                    chat_id=chat_id, name="digest")
    log.info("Summaries to %s at %s on days %s", targets, [t.strftime("%H:%M") for t in times], days)


async def sync_schedule_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await sync_schedule(ctx)
    except Exception:
        log.exception("Couldn't refresh schedule from Settings")


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(ctx.error, NetworkError):  # brief internet/Telegram hiccup; the bot retries on its own
        log.warning("Network problem, retrying: %s", ctx.error)
        return
    log.error("Unhandled error", exc_info=ctx.error)


# ---------------------------------------------------------------- startup

async def post_init(app: Application) -> None:
    sheet: Sheet = app.bot_data["sheet"]
    await asyncio.to_thread(sheet.ensure_layout)
    await load_access(app)
    app.job_queue.run_repeating(sync_schedule_job, interval=300, first=1, name="sync-schedule")
    log.info("Bot ready")


async def post_shutdown(app: Application) -> None:
    await app.bot_data["llm"].close()


def main() -> None:
    cfg = load_config()
    logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    app = (Application.builder().token(cfg.telegram_token)
           .post_init(post_init).post_shutdown(post_shutdown).build())
    try:
        sheet = Sheet(cfg.google_credentials_file, cfg.spreadsheet_id, cfg.timezone.key)
    except FileNotFoundError as exc:
        raise SystemExit(f"Google key file not found: {cfg.google_credentials_file}") from exc
    except (ValueError, KeyError) as exc:
        raise SystemExit(
            f"{cfg.google_credentials_file} isn't a valid service-account key ({exc.__class__.__name__}). "
            "Download a fresh JSON key: Google Cloud → IAM → Service accounts → Keys → Add key → JSON."
        ) from exc
    except (gspread.exceptions.SpreadsheetNotFound, gspread.exceptions.APIError) as exc:
        raise SystemExit(
            f"Can't open the Google Sheet ({exc}). Share it as Editor with the service account email "
            f"(client_email in {cfg.google_credentials_file}) and check SPREADSHEET_ID."
        ) from exc
    app.bot_data.update(cfg=cfg, state=State(), llm=LLM(cfg), sheet=sheet)

    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("undo", cmd_undo))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("colleagues", cmd_colleagues))
    app.add_handler(CommandHandler("reminders", cmd_reminders))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("adduser", cmd_adduser))
    app.add_handler(CommandHandler("removeuser", cmd_removeuser))
    app.add_handler(CommandHandler("setowner", cmd_setowner))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.UpdateType.MESSAGE & filters.ChatType.PRIVATE & ~filters.COMMAND, on_message))
    app.add_error_handler(on_error)

    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
