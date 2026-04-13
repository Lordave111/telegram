"""
AI-Powered Day Scheduler Telegram Bot — with Calendar UI
---------------------------------------------------------
/add      → opens interactive calendar → pick date → pick time → type title → saved
/schedule → shows all tasks with ✅ tick buttons inline
/plan     → AI builds full schedule from free-text description
/clear    → wipe schedule
/help     → help
"""

import os
import json
import sqlite3
import calendar
import logging
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from anthropic import Anthropic

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN      = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY   = os.environ["ANTHROPIC_API_KEY"]
TIMEZONE            = os.environ.get("TIMEZONE", "UTC")
REMINDER_MINUTES    = int(os.environ.get("REMINDER_MINUTES", "10"))
DB_PATH             = os.environ.get("DB_PATH", "scheduler.db")

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

client = Anthropic(api_key=ANTHROPIC_API_KEY)

# ── Conversation states ───────────────────────────────────────────────────────
PICK_DATE, PICK_HOUR, PICK_MINUTE, PICK_END_HOUR, PICK_END_MINUTE, TYPE_TITLE = range(6)

# ── SQLite persistence ────────────────────────────────────────────────────────

def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def db_init():
    """Create tables if they don't exist yet."""
    with db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id   INTEGER NOT NULL,
                title     TEXT    NOT NULL,
                start     TEXT    NOT NULL,
                end       TEXT    NOT NULL,
                priority  TEXT    NOT NULL DEFAULT 'medium',
                done      INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.commit()
    logger.info("Database ready at %s", DB_PATH)

def get_tasks(chat_id: int) -> list:
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE chat_id = ? ORDER BY start", (chat_id,)
        ).fetchall()
    return [dict(r) | {"done": bool(r["done"])} for r in rows]

def save_tasks(chat_id: int, tasks: list):
    """Replace all tasks for a chat with the provided list."""
    with db_connect() as conn:
        conn.execute("DELETE FROM tasks WHERE chat_id = ?", (chat_id,))
        conn.executemany(
            "INSERT INTO tasks (chat_id, title, start, end, priority, done) VALUES (?,?,?,?,?,?)",
            [(chat_id, t["title"], t["start"], t["end"],
              t.get("priority", "medium"), int(t.get("done", False))) for t in tasks],
        )
        conn.commit()

def append_task(chat_id: int, task: dict):
    """Insert a single new task."""
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO tasks (chat_id, title, start, end, priority, done) VALUES (?,?,?,?,?,?)",
            (chat_id, task["title"], task["start"], task["end"],
             task.get("priority", "medium"), int(task.get("done", False))),
        )
        conn.commit()

def mark_task_done(chat_id: int, task_index: int):
    """Mark the Nth task (sorted by start) as done."""
    tasks = get_tasks(chat_id)
    if 0 <= task_index < len(tasks):
        task_id = tasks[task_index]["id"]
        with db_connect() as conn:
            conn.execute("UPDATE tasks SET done = 1 WHERE id = ?", (task_id,))
            conn.commit()

def clear_tasks(chat_id: int):
    with db_connect() as conn:
        conn.execute("DELETE FROM tasks WHERE chat_id = ?", (chat_id,))
        conn.commit()

# ── Active reminder job names (in-memory is fine — rebuilt on restart) ────────
reminder_jobs: dict[int, list[str]] = {}

# ── Helpers ───────────────────────────────────────────────────────────────────

def now_local() -> datetime:
    return datetime.now(ZoneInfo(TIMEZONE))

def fmt_dt(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%b %d, %I:%M %p")
    except Exception:
        return iso

def fmt_time(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%I:%M %p")
    except Exception:
        return iso

# ── Calendar keyboard builder ─────────────────────────────────────────────────

def build_calendar(year: int, month: int) -> InlineKeyboardMarkup:
    today = now_local().date()
    kb = []

    kb.append([
        InlineKeyboardButton("◀", callback_data=f"cal_prev_{year}_{month}"),
        InlineKeyboardButton(f"{calendar.month_name[month]} {year}", callback_data="cal_ignore"),
        InlineKeyboardButton("▶", callback_data=f"cal_next_{year}_{month}"),
    ])

    kb.append([InlineKeyboardButton(d, callback_data="cal_ignore") for d in ["Mo","Tu","We","Th","Fr","Sa","Su"]])

    month_cal = calendar.monthcalendar(year, month)
    for week in month_cal:
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(" ", callback_data="cal_ignore"))
            else:
                d = date(year, month, day)
                label = f"·{day}·" if d == today else str(day)
                if d < today:
                    row.append(InlineKeyboardButton(str(day), callback_data="cal_ignore"))
                else:
                    row.append(InlineKeyboardButton(label, callback_data=f"cal_day_{year}_{month}_{day}"))
        kb.append(row)

    kb.append([InlineKeyboardButton("❌ Cancel", callback_data="cal_cancel")])
    return InlineKeyboardMarkup(kb)


def build_hour_keyboard(prefix: str) -> InlineKeyboardMarkup:
    hours = list(range(6, 24))
    rows = []
    for i in range(0, len(hours), 4):
        row = []
        for h in hours[i:i+4]:
            label = datetime(2000, 1, 1, h).strftime("%-I %p")
            row.append(InlineKeyboardButton(label, callback_data=f"{prefix}_{h}"))
        rows.append(row)
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="cal_cancel")])
    return InlineKeyboardMarkup(rows)


def build_minute_keyboard(prefix: str) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton(":00", callback_data=f"{prefix}_0"),
        InlineKeyboardButton(":15", callback_data=f"{prefix}_15"),
        InlineKeyboardButton(":30", callback_data=f"{prefix}_30"),
        InlineKeyboardButton(":45", callback_data=f"{prefix}_45"),
    ]]
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="cal_cancel")])
    return InlineKeyboardMarkup(rows)


# ── Schedule display with tick buttons ───────────────────────────────────────

def build_schedule_message(tasks: list) -> tuple[str, InlineKeyboardMarkup]:
    if not tasks:
        return "📭 No tasks yet. Use /add to add one.", InlineKeyboardMarkup([])

    sorted_tasks = sorted(tasks, key=lambda x: x["start"])
    lines = ["📅 *Your Schedule*\n"]
    buttons = []

    for i, t in enumerate(sorted_tasks):
        done = t.get("done", False)
        icon = "✅" if done else "🔲"
        lines.append(f"{icon} *{i+1}.* {fmt_dt(t['start'])} → {fmt_time(t['end'])}\n   {t['title']}")
        if not done:
            buttons.append([InlineKeyboardButton(
                f"✅ Done: {t['title'][:28]}",
                callback_data=f"done_{i}"
            )])

    if not buttons:
        buttons.append([InlineKeyboardButton("🎉 All done!", callback_data="cal_ignore")])

    return "\n".join(lines), InlineKeyboardMarkup(buttons)


# ── Reminders ─────────────────────────────────────────────────────────────────

async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    task = job.data
    await context.bot.send_message(
        chat_id=job.chat_id,
        text=(
            f"⏰ *Reminder — {REMINDER_MINUTES} min to go!*\n\n"
            f"📌 *{task['title']}*\n"
            f"🕐 {fmt_time(task['start'])} – {fmt_time(task['end'])}"
        ),
        parse_mode="Markdown",
    )

def schedule_reminders(chat_id: int, tasks: list, job_queue):
    # Cancel any existing reminder jobs for this chat
    for name in reminder_jobs.get(chat_id, []):
        for job in job_queue.get_jobs_by_name(name):
            job.schedule_removal()
    reminder_jobs[chat_id] = []

    tz = ZoneInfo(TIMEZONE)
    for task in tasks:
        try:
            start_dt = datetime.fromisoformat(task["start"])
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=tz)
            remind_at = start_dt - timedelta(minutes=REMINDER_MINUTES)
            if remind_at > now_local():
                name = f"reminder_{chat_id}_{task['start']}"
                job_queue.run_once(send_reminder, when=remind_at, chat_id=chat_id, name=name, data=task)
                reminder_jobs[chat_id].append(name)
        except Exception as e:
            logger.warning("Reminder scheduling failed: %s", e)


# ── /add conversation ─────────────────────────────────────────────────────────

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    now = now_local()
    context.user_data["add"] = {}
    await update.message.reply_text(
        "📅 *Pick a date for your task:*",
        parse_mode="Markdown",
        reply_markup=build_calendar(now.year, now.month),
    )
    return PICK_DATE


async def cal_navigate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _, direction, year, month = query.data.split("_")
    year, month = int(year), int(month)
    if direction == "next":
        month += 1
        if month > 12:
            month, year = 1, year + 1
    else:
        month -= 1
        if month < 1:
            month, year = 12, year - 1
    await query.edit_message_reply_markup(reply_markup=build_calendar(year, month))
    return PICK_DATE


async def cal_pick_day(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _, _, year, month, day = query.data.split("_")
    picked = date(int(year), int(month), int(day))
    context.user_data["add"]["date"] = picked
    await query.edit_message_text(
        f"📅 *{picked.strftime('%A, %B %d')}*\n\nPick the *start hour:*",
        parse_mode="Markdown",
        reply_markup=build_hour_keyboard("start_h"),
    )
    return PICK_HOUR


async def pick_start_hour(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    hour = int(query.data.split("_")[-1])
    context.user_data["add"]["start_hour"] = hour
    await query.edit_message_text(
        "🕐 Pick the *start minute:*",
        parse_mode="Markdown",
        reply_markup=build_minute_keyboard("start_m"),
    )
    return PICK_MINUTE


async def pick_start_minute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    minute = int(query.data.split("_")[-1])
    context.user_data["add"]["start_minute"] = minute
    await query.edit_message_text(
        "🕐 Pick the *end hour:*",
        parse_mode="Markdown",
        reply_markup=build_hour_keyboard("end_h"),
    )
    return PICK_END_HOUR


async def pick_end_hour(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    hour = int(query.data.split("_")[-1])
    context.user_data["add"]["end_hour"] = hour
    await query.edit_message_text(
        "🕐 Pick the *end minute:*",
        parse_mode="Markdown",
        reply_markup=build_minute_keyboard("end_m"),
    )
    return PICK_END_MINUTE


async def pick_end_minute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    minute = int(query.data.split("_")[-1])
    context.user_data["add"]["end_minute"] = minute
    d = context.user_data["add"]
    sh, sm = d["start_hour"], d["start_minute"]
    eh, em = d["end_hour"], d["end_minute"]
    picked = d["date"]
    await query.edit_message_text(
        f"📅 *{picked.strftime('%A, %B %d')}*\n"
        f"🕐 {datetime(2000,1,1,sh,sm).strftime('%I:%M %p')} → {datetime(2000,1,1,eh,em).strftime('%I:%M %p')}\n\n"
        "✏️ Now type the *task title:*",
        parse_mode="Markdown",
    )
    return TYPE_TITLE


async def receive_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    title = update.message.text.strip()
    d = context.user_data["add"]
    picked = d["date"]

    start_dt = datetime(picked.year, picked.month, picked.day, d["start_hour"], d["start_minute"])
    end_dt   = datetime(picked.year, picked.month, picked.day, d["end_hour"],   d["end_minute"])
    if end_dt <= start_dt:
        end_dt = start_dt + timedelta(hours=1)

    task = {"title": title, "start": start_dt.isoformat(), "end": end_dt.isoformat(),
            "priority": "medium", "done": False}

    chat_id = update.effective_chat.id
    append_task(chat_id, task)
    tasks = get_tasks(chat_id)
    schedule_reminders(chat_id, tasks, context.job_queue)

    await update.message.reply_text(
        f"✅ *Task added!*\n\n"
        f"📌 *{title}*\n"
        f"📅 {start_dt.strftime('%A, %B %d')}\n"
        f"🕐 {start_dt.strftime('%I:%M %p')} → {end_dt.strftime('%I:%M %p')}\n\n"
        f"⏰ Reminder set {REMINDER_MINUTES} min before.\n"
        "Use /schedule to see your full day.",
        parse_mode="Markdown",
    )
    context.user_data.pop("add", None)
    return ConversationHandler.END


async def cal_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("❌ Cancelled.")
    context.user_data.pop("add", None)
    return ConversationHandler.END


async def cal_ignore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()


# ── /schedule + tick buttons ──────────────────────────────────────────────────

async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    tasks = get_tasks(chat_id)
    text, markup = build_schedule_message(tasks)
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=markup)


async def cb_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Marked as done! ✅")
    chat_id = update.effective_chat.id
    idx = int(query.data.split("_")[1])
    mark_task_done(chat_id, idx)
    tasks = get_tasks(chat_id)
    text, markup = build_schedule_message(tasks)
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)


# ── /plan ─────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a personal day-scheduling assistant.
Always respond with valid JSON only — no prose, no markdown fences.
Task objects must have: title (str), start (ISO 8601), end (ISO 8601), priority ("high"|"medium"|"low").
Fit tasks into waking hours 7 AM – 10 PM unless specified. Leave short buffers between tasks."""

async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    description = " ".join(context.args).strip()
    if not description:
        await update.message.reply_text(
            "Tell me about your day, e.g.:\n\n"
            "`/plan team standup 9am, deep work until noon, gym at 6pm`",
            parse_mode="Markdown",
        )
        return

    msg = await update.message.reply_text("🧠 Building your schedule…")
    try:
        now = now_local()
        resp = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": (
                f"Current time: {now.isoformat()}\nTimezone: {TIMEZONE}\n\n"
                f"User: \"{description}\"\n\nReturn JSON: {{\"tasks\": [...]}}. Schedule for today."
            )}],
        )
        result = json.loads(resp.content[0].text.strip())
        tasks = result.get("tasks", [])
        if not tasks:
            await msg.edit_text("😕 Couldn't generate a schedule. Try describing your day differently.")
            return
        for t in tasks:
            t.setdefault("done", False)
        save_tasks(chat_id, tasks)
        schedule_reminders(chat_id, tasks, context.job_queue)
        text, markup = build_schedule_message(tasks)
        await msg.edit_text(
            text + f"\n\n⏰ Reminders {REMINDER_MINUTES} min before each task!",
            parse_mode="Markdown",
            reply_markup=markup,
        )
    except Exception as e:
        logger.exception("Plan error")
        await msg.edit_text(f"⚠️ Error: {e}")


# ── Other commands ────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Welcome to your AI Day Scheduler!*\n\n"
        "• /add — pick date & time on a calendar picker\n"
        "• /plan — describe your day, AI builds the schedule\n"
        "• /schedule — view tasks & tap ✅ to tick them off\n"
        "• /clear — start fresh\n"
        "• /help — all commands",
        parse_mode="Markdown",
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Commands*\n\n"
        "/add — interactive calendar → pick date & time → type title\n"
        "/plan <description> — AI builds your full day schedule\n"
        "/schedule — view tasks, tap ✅ to mark done\n"
        "/clear — delete all tasks\n"
        "/help — this message\n\n"
        f"⏰ Reminders fire {REMINDER_MINUTES} min before each task.",
        parse_mode="Markdown",
    )

async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    for name in reminder_jobs.get(chat_id, []):
        for job in context.job_queue.get_jobs_by_name(name):
            job.schedule_removal()
    reminder_jobs[chat_id] = []
    clear_tasks(chat_id)
    await update.message.reply_text("🗑 Schedule cleared!")

async def fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Use /add to add a task with the calendar picker. /help for all commands."
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    db_init()

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    async def on_startup(app: Application):
        """Re-register reminders for all future tasks after a restart."""
        with db_connect() as conn:
            chat_ids = [r[0] for r in conn.execute("SELECT DISTINCT chat_id FROM tasks").fetchall()]
        restored = 0
        for chat_id in chat_ids:
            tasks = get_tasks(chat_id)
            future = [t for t in tasks if not t["done"]]
            if future:
                schedule_reminders(chat_id, future, app.job_queue)
                restored += len(future)
        logger.info("Restored %d pending reminders for %d chats.", restored, len(chat_ids))

    app.post_init = on_startup

    add_conv = ConversationHandler(
        entry_points=[CommandHandler("add", cmd_add)],
        states={
            PICK_DATE: [
                CallbackQueryHandler(cal_navigate, pattern=r"^cal_(prev|next)_"),
                CallbackQueryHandler(cal_pick_day,  pattern=r"^cal_day_"),
                CallbackQueryHandler(cal_cancel,    pattern=r"^cal_cancel$"),
                Callb
