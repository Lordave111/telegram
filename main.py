import os
import json
import sqlite3
import calendar
import logging
import re
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from threading import Thread

from flask import Flask
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

# ── RENDER HEARTBEAT (Flask) ──────────────────────────────────────────────────
server = Flask('')

@server.route('/')
def home():
    return "Bot is running and healthy!"

def run_server():
    port = int(os.environ.get("PORT", 8080))
    server.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_server)
    t.daemon = True
    t.start()

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_BOT_TOKEN"]
TIMEZONE         = os.environ.get("TIMEZONE", "America/New_York")
REMINDER_MINS    = int(os.environ.get("REMINDER_MINUTES", "10"))
DB_PATH          = os.environ.get("DB_PATH", "scheduler.db")

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Conversation States ───────────────────────────────────────────────────────
PICK_DATE, PICK_TIME, PICK_DURATION, TYPE_TITLE = range(4)

# ── SQLite Database ───────────────────────────────────────────────────────────

def db_init():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                title TEXT,
                start TEXT,
                end TEXT,
                priority TEXT DEFAULT 'medium',
                done INTEGER DEFAULT 0
            )
        """)
        conn.commit()

def get_tasks(chat_id: int, day: date = None) -> list:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        if day:
            day_str = day.isoformat()
            rows = conn.execute("SELECT * FROM tasks WHERE chat_id = ? AND start LIKE ? ORDER BY start", 
                                (chat_id, f"{day_str}%")).fetchall()
        else:
            rows = conn.execute("SELECT * FROM tasks WHERE chat_id = ? ORDER BY start", (chat_id,)).fetchall()
    return [dict(r) for r in rows]

# ── Helpers ───────────────────────────────────────────────────────────────────

def now_local() -> datetime:
    return datetime.now(ZoneInfo(TIMEZONE))

def fmt_time(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%I:%M %p")

# ── Keyboards ─────────────────────────────────────────────────────────────────

def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Task", callback_data="btn_add"), 
         InlineKeyboardButton("📅 Today's List", callback_data="view_today")],
        [InlineKeyboardButton("⏭ Tomorrow", callback_data="view_tomorrow"),
         InlineKeyboardButton("🗑 Clear All", callback_data="btn_clear")]
    ])

def build_calendar_kb(year, month):
    kb = [[InlineKeyboardButton(f"{calendar.month_name[month]} {year}", callback_data="ignore")]]
    kb.append([InlineKeyboardButton(d, callback_data="ignore") for d in ["Mo","Tu","We","Th","Fr","Sa","Su"]])
    for week in calendar.monthcalendar(year, month):
        row = []
        for d in week:
            if d == 0: row.append(InlineKeyboardButton(" ", callback_data="ignore"))
            else:
                row.append(InlineKeyboardButton(str(d), callback_data=f"day_{year}_{month}_{d}"))
        kb.append(row)
    return InlineKeyboardMarkup(kb)

def build_duration_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("30 Mins", callback_data="dur_30"), InlineKeyboardButton("1 Hour", callback_data="dur_60")],
        [InlineKeyboardButton("2 Hours", callback_data="dur_120"), InlineKeyboardButton("3 Hours", callback_data="dur_180")]
    ])

# ── Command Handlers ──────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚡ *AI-Ready Scheduler Bot*\nUse the buttons below to manage your day.",
        parse_mode="Markdown",
        reply_markup=main_menu_kb()
    )

async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = now_local()
    msg = update.callback_query.message if update.callback_query else update.message
    await msg.reply_text("📅 *Pick a Date:*", parse_mode="Markdown", 
                         reply_markup=build_calendar_kb(now.year, now.month))
    return PICK_DATE

async def date_picked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, y, m, d = query.data.split("_")
    context.user_data["temp_task"] = {"date": f"{y}-{int(m):02d}-{int(d):02d}"}
    
    # Hour picker
    hours = [[InlineKeyboardButton(f"{h}:00", callback_data=f"h_{h}") for h in range(8, 12)],
             [InlineKeyboardButton(f"{h}:00", callback_data=f"h_{h}") for h in range(12, 16)],
             [InlineKeyboardButton(f"{h}:00", callback_data=f"h_{h}") for h in range(16, 21)]]
    await query.edit_message_text("🕒 *Pick Start Hour:*", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(hours))
    return PICK_TIME

async def time_picked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["temp_task"]["hour"] = int(query.data.split("_")[1])
    await query.edit_message_text("⏳ *How long will this take?*", parse_mode="Markdown", reply_markup=build_duration_kb())
    return PICK_DURATION

async def duration_picked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["temp_task"]["dur"] = int(query.data.split("_")[1])
    await query.edit_message_text("✍️ Type the *Task Title*: ", parse_mode="Markdown")
    return TYPE_TITLE

async def save_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    title = update.message.text
    data = context.user_data["temp_task"]
    
    start_dt = datetime.fromisoformat(f"{data['date']}T{data['hour']:02d}:00:00")
    end_dt = start_dt + timedelta(minutes=data['dur'])
    
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT INTO tasks (chat_id, title, start, end) VALUES (?,?,?,?)",
                     (update.effective_chat.id, title, start_dt.isoformat(), end_dt.isoformat()))
    
    await update.message.reply_text(f"✅ *Saved: {title}*\n{start_dt.strftime('%b %d at %I:%M %p')}", 
                                    parse_mode="Markdown", reply_markup=main_menu_kb())
    return ConversationHandler.END

# ── View & Actions ────────────────────────────────────────────────────────────

async def show_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE, day_offset=0):
    query = update.callback_query
    target_date = now_local().date() + timedelta(days=day_offset)
    tasks = get_tasks(update.effective_chat.id, target_date)
    
    if not tasks:
        text = f"📭 No tasks for {target_date.strftime('%A')}."
        kb = [[InlineKeyboardButton("➕ Add One", callback_data="btn_add")]]
    else:
        text = f"📅 *Schedule for {target_date.strftime('%A, %b %d')}*\n\n"
        kb = []
        for t in tasks:
            status = "✅" if t['done'] else "🔲"
            text += f"{status} `{fmt_time(t['start'])}` - *{t['title']}*\n"
            kb.append([InlineKeyboardButton(f"✅ Done: {t['title'][:15]}", callback_data=f"done_{t['id']}"),
                       InlineKeyboardButton("🗑 Delete", callback_data=f"del_{t['id']}")])
    
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="menu")])
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    chat_id = update.effective_chat.id

    if data == "view_today": await show_schedule(update, context, 0)
    elif data == "view_tomorrow": await show_schedule(update, context, 1)
    elif data == "menu": await query.edit_message_text("Main Menu:", reply_markup=main_menu_kb())
    elif data == "btn_clear":
        with sqlite3.connect(DB_PATH) as conn: conn.execute("DELETE FROM tasks WHERE chat_id=?", (chat_id,))
        await query.answer("Cleared!")
        await query.edit_message_text("🗑 All tasks deleted.", reply_markup=main_menu_kb())
    elif data.startswith("done_"):
        tid = data.split("_")[1]
        with sqlite3.connect(DB_PATH) as conn: conn.execute("UPDATE tasks SET done=1 WHERE id=?", (tid,))
        await query.answer("Marked as done!")
        await show_schedule(update, context, 0)
    elif data.startswith("del_"):
        tid = data.split("_")[1]
        with sqlite3.connect(DB_PATH) as conn: conn.execute("DELETE FROM tasks WHERE id=?", (tid,))
        await query.answer("Deleted!")
        await show_schedule(update, context, 0)

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    db_init()
    keep_alive() # Starts Flask server for Render
    
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_start, pattern="^btn_add$")],
        states={
            PICK_DATE: [CallbackQueryHandler(date_picked, pattern="^day_")],
            PICK_TIME: [CallbackQueryHandler(time_picked, pattern="^h_")],
            PICK_DURATION: [CallbackQueryHandler(duration_picked, pattern="^dur_")],
            TYPE_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_task)],
        },
        fallbacks=[CommandHandler("cancel", start)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv_handler)
    app.add_handler(CallbackQueryHandler(handle_callback))

    print("Bot is starting...")
    app.run_polling()

if __name__ == "__main__":
    main()
    
