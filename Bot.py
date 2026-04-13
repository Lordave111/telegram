import os
import sqlite3
import calendar
import logging
import re
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

# ── Config ────────────────────────────────────────────────────────────────────
# Only the Telegram token is required now!
TELEGRAM_TOKEN      = os.environ["TELEGRAM_BOT_TOKEN"]
TIMEZONE            = os.environ.get("TIMEZONE", "UTC")
REMINDER_MINUTES    = int(os.environ.get("REMINDER_MINUTES", "10"))
DB_PATH             = os.environ.get("DB_PATH", "scheduler.db")

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Conversation states ───────────────────────────────────────────────────────
PICK_DATE, PICK_HOUR, PICK_MINUTE, PICK_END_HOUR, PICK_END_MINUTE, TYPE_TITLE = range(6)

# ── SQLite persistence ────────────────────────────────────────────────────────

def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def db_init():
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

def get_tasks(chat_id: int) -> list:
    with db_connect() as conn:
        rows = conn.execute("SELECT * FROM tasks WHERE chat_id = ? ORDER BY start", (chat_id,)).fetchall()
    return [dict(r) | {"done": bool(r["done"])} for r in rows]

def save_tasks(chat_id: int, tasks: list):
    with db_connect() as conn:
        conn.execute("DELETE FROM tasks WHERE chat_id = ?", (chat_id,))
        conn.executemany(
            "INSERT INTO tasks (chat_id, title, start, end, priority, done) VALUES (?,?,?,?,?,?)",
            [(chat_id, t["title"], t["start"], t["end"], t.get("priority", "medium"), int(t.get("done", False))) for t in tasks],
        )
        conn.commit()

def append_task(chat_id: int, task: dict):
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO tasks (chat_id, title, start, end, priority, done) VALUES (?,?,?,?,?,?)",
            (chat_id, task["title"], task["start"], task["end"], task.get("priority", "medium"), int(task.get("done", False))),
        )
        conn.commit()

# ── Helpers ───────────────────────────────────────────────────────────────────

def now_local() -> datetime:
    return datetime.now(ZoneInfo(TIMEZONE))

def fmt_dt(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%b %d, %I:%M %p")

def fmt_time(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%I:%M %p")

# ── Rule-Based Parser (The "Non-AI" Plan Logic) ───────────────────────────────

def simple_plan_parser(text: str):
    """
    Very basic parser: Looks for 'HH:MM Title' patterns.
    Example: '09:00 Gym, 11:00 Work'
    """
    tasks = []
    today = now_local().date()
    # Matches patterns like 9:00, 14:30, 09:00
    pattern = re.compile(r"(\d{1,2}:\d{2})\s+([^,]+)")
    matches = pattern.findall(text)
    
    for time_str, title in matches:
        hour, minute = map(int, time_str.split(":"))
        start_dt = datetime(today.year, today.month, today.day, hour, minute)
        end_dt = start_dt + timedelta(hours=1) # Default 1 hour
        tasks.append({
            "title": title.strip(),
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat(),
            "priority": "medium",
            "done": False
        })
    return tasks

# ── Keyboards ─────────────────────────────────────────────────────────────────

def build_calendar(year: int, month: int) -> InlineKeyboardMarkup:
    today = now_local().date()
    kb = [[InlineKeyboardButton("◀", callback_data=f"cal_prev_{year}_{month}"),
           InlineKeyboardButton(f"{calendar.month_name[month]} {year}", callback_data="ignore"),
           InlineKeyboardButton("▶", callback_data=f"cal_next_{year}_{month}")]]
    kb.append([InlineKeyboardButton(d, callback_data="ignore") for d in ["Mo","Tu","We","Th","Fr","Sa","Su"]])
    for week in calendar.monthcalendar(year, month):
        row = []
        for day in week:
            if day == 0: row.append(InlineKeyboardButton(" ", callback_data="ignore"))
            else:
                d = date(year, month, day)
                row.append(InlineKeyboardButton(str(day) if d != today else f"·{day}·", 
                           callback_data=f"cal_day_{year}_{month}_{day}" if d >= today else "ignore"))
        kb.append(row)
    kb.append([InlineKeyboardButton("❌ Cancel", callback_data="cal_cancel")])
    return InlineKeyboardMarkup(kb)

def build_hour_keyboard(prefix: str) -> InlineKeyboardMarkup:
    rows = []
    hours = list(range(6, 24))
    for i in range(0, len(hours), 4):
        rows.append([InlineKeyboardButton(f"{h}:00", callback_data=f"{prefix}_{h}") for h in hours[i:i+4]])
    return InlineKeyboardMarkup(rows)

# ── Handlers ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 *Scheduler Bot*\n/add - Use Calendar\n/plan - Text input\n/schedule - View list", parse_mode="Markdown")

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    now = now_local()
    context.user_data["add"] = {}
    await update.message.reply_text("Pick date:", reply_markup=build_calendar(now.year, now.month))
    return PICK_DATE

async def cal_pick_day(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _, _, y, m, d = query.data.split("_")
    context.user_data["add"]["date"] = date(int(y), int(m), int(d))
    await query.edit_message_text("Pick start hour:", reply_markup=build_hour_keyboard("h"))
    return PICK_HOUR

async def pick_hour(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["add"]["hour"] = int(query.data.split("_")[1])
    await query.edit_message_text("Type task title:")
    return TYPE_TITLE

async def receive_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    title = update.message.text
    d = context.user_data["add"]
    start = datetime(d["date"].year, d["date"].month, d["date"].day, d["hour"], 0)
    task = {"title": title, "start": start.isoformat(), "end": (start + timedelta(hours=1)).isoformat()}
    append_task(update.effective_chat.id, task)
    await update.message.reply_text("✅ Added!")
    return ConversationHandler.END

async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("Format: `/plan 09:00 Gym, 12:00 Lunch`", parse_mode="Markdown")
        return
    tasks = simple_plan_parser(text)
    if tasks:
        save_tasks(update.effective_chat.id, tasks)
        await update.message.reply_text(f"📅 Planned {len(tasks)} tasks! View with /schedule.")
    else:
        await update.message.reply_text("Could not parse. Use `HH:MM Title` format.")

async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tasks = get_tasks(update.effective_chat.id)
    if not tasks:
        await update.message.reply_text("Empty!")
        return
    res = "\n".join([f"• {fmt_time(t['start'])}: {t['title']}" for t in tasks])
    await update.message.reply_text(f"📅 *Today's Schedule*\n{res}", parse_mode="Markdown")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    db_init()
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    add_conv = ConversationHandler(
        entry_points=[CommandHandler("add", cmd_add)],
        states={
            PICK_DATE: [CallbackQueryHandler(cal_pick_day, pattern=r"^cal_day_")],
            PICK_HOUR: [CallbackQueryHandler(pick_hour, pattern=r"^h_")],
            TYPE_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_title)],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)],
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(add_conv)
    app.add_handler(CommandHandler("plan", cmd_plan))
    app.add_handler(CommandHandler("schedule", cmd_schedule))
    app.run_polling()

if __name__ == "__main__":
    main()

# 
# ── /plan ─────────────────────────────────────────────────────────────────────

