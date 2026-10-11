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
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

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

You can also just ask, e.g. "what's overdue for Bob?" or "how much do I owe Mike?\""""


@dataclass
class Pending:
    """A batch waiting for the owner to answer a clarifying question."""
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
class State:
    buffer: list[Item] = field(default_factory=list)
    pending: Pending | None = None
    last_save: LastSave | None = None
    schedule_key: str = ""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


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
    cfg = cfg_of(ctx)
    user = update.effective_user
    if user is None:
        return False
    if cfg.allowed_user_id is None:
        if update.effective_message:
            await update.effective_message.reply_text(
                f"Your Telegram user ID is {user.id}.\nPut it in ALLOWED_USER_ID in the .env file and restart me."
            )
        return False
    if user.id != cfg.allowed_user_id:
        log.warning("Ignoring message from unauthorized user %s", user.id)
        return False
    return True


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

    state = state_of(ctx)
    state.buffer.extend(items)
    # Wait a few seconds so albums, "text + photo", and multi-forwards arrive as one batch.
    chat_id = msg.chat_id
    for job in ctx.job_queue.get_jobs_by_name(f"flush-{chat_id}"):
        job.schedule_removal()
    ctx.job_queue.run_once(flush_job, cfg.batch_window_seconds, chat_id=chat_id, name=f"flush-{chat_id}")
    await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)


async def flush_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    state = state_of(ctx)
    chat_id = ctx.job.chat_id
    items, state.buffer = state.buffer, []
    if not items:
        return
    async with state.lock:
        try:
            await process_batch(ctx, chat_id, items)
        except LLMUnavailable as exc:
            log.error("All AI providers unavailable: %s", exc)
            state.buffer = items + state.buffer  # keep it so the owner can retry by sending anything
            await send(ctx, chat_id, "⏳ The AI is over its free limit or down right now. I kept your message; "
                                     "send me anything later (even a dot) and I'll retry.")
        except Exception:
            log.exception("Failed to process batch")
            await send(ctx, chat_id, "❌ Something went wrong. Please check the sheet and resend if the rows are missing.")


async def process_batch(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, items: list[Item]) -> None:
    cfg, sheet, llm, state = cfg_of(ctx), sheet_of(ctx), llm_of(ctx), state_of(ctx)
    await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)

    question = None
    pending = state.pending
    if pending:
        # This batch answers the question we asked: analyze original + answer together.
        question = pending.analysis.clarification_question
        items = pending.items + [replace(i, header="[ANSWER] " + i.header) for i in items]

    settings = await asyncio.to_thread(sheet.read_settings)
    rows = await asyncio.to_thread(sheet.read_rows)
    recent = rows[-cfg.recent_rows_context_limit:]
    context = pipeline.build_context(now_local(cfg), settings.colleagues, recent, question)

    analysis, provider = await llm.analyze(items, context)
    if pending is state.pending:
        state.pending = None
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
        state.pending = Pending(items=items, analysis=analysis, warnings=warnings, provider=provider)
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

    state.last_save = LastSave(rows=new_rows, originals={r: v[-1] for r, v in zip(new_rows, values)}, closed=closed)
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
        elif query.data == "save_draft" and state.pending:
            p, state.pending = state.pending, None
            await query.edit_message_reply_markup(None)
            if not p.analysis.entries:
                await send(ctx, chat_id, "There's no draft to save. Please resend with more detail.")
                return
            settings = await asyncio.to_thread(sheet_of(ctx).read_settings)
            await save(ctx, chat_id, p.items, p.analysis, p.warnings, p.provider, settings.auto_close)
        elif query.data == "cancel":
            state.pending = None
            await query.edit_message_reply_markup(None)
            await send(ctx, chat_id, "Okay, dropped it. Nothing was saved.")


async def do_undo(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    state, sheet = state_of(ctx), sheet_of(ctx)
    last = state.last_save
    if not last:
        await send(ctx, chat_id, "Nothing to undo.")
        return
    for row, status in last.closed.items():
        await asyncio.to_thread(sheet.set_status, row, status)
    deleted = await asyncio.to_thread(sheet.delete_rows_if_match, last.rows, last.originals)
    await asyncio.to_thread(sheet.write_setting, "last_added_rows", "")
    state.last_save = None
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
    await update.effective_message.reply_text(HELP, parse_mode=ParseMode.HTML)


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
    state = state_of(ctx)
    had = state.pending is not None
    state.pending = None
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


# ---------------------------------------------------------------- scheduled summaries

async def digest_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    rows = await asyncio.to_thread(sheet_of(ctx).read_rows)
    await send(ctx, ctx.job.chat_id, digest.build_digest(rows, now_local(cfg_of(ctx))))


async def sync_schedule(ctx: ContextTypes.DEFAULT_TYPE, force: bool = False) -> None:
    """(Re)schedule summaries from the Settings tab. Runs every few minutes, so sheet edits apply too."""
    cfg, state = cfg_of(ctx), state_of(ctx)
    if cfg.allowed_user_id is None:
        return
    settings = await asyncio.to_thread(sheet_of(ctx).read_settings)
    key = f"{settings.get('reminder_times')}|{settings.get('reminder_days')}"
    if key == state.schedule_key and not force:
        return
    state.schedule_key = key
    for job in ctx.job_queue.get_jobs_by_name("digest"):
        job.schedule_removal()
    days = digest.parse_days(settings.get("reminder_days"))
    times = digest.parse_times(settings.get("reminder_times"))
    for t in times:
        ctx.job_queue.run_daily(digest_job, t.replace(tzinfo=cfg.timezone), days=days,
                                chat_id=cfg.allowed_user_id, name="digest")
    log.info("Summaries scheduled at %s on days %s", [t.strftime("%H:%M") for t in times], days)


async def sync_schedule_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await sync_schedule(ctx)
    except Exception:
        log.exception("Couldn't refresh schedule from Settings")


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled error", exc_info=ctx.error)


# ---------------------------------------------------------------- startup

async def post_init(app: Application) -> None:
    sheet: Sheet = app.bot_data["sheet"]
    await asyncio.to_thread(sheet.ensure_layout)
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
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.UpdateType.MESSAGE & filters.ChatType.PRIVATE & ~filters.COMMAND, on_message))
    app.add_error_handler(on_error)

    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
