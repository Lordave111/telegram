import os
import sqlite3
import logging
import random
import re
from datetime import datetime, time
from zoneinfo import ZoneInfo
from threading import Thread

from flask import Flask
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# --- Configuration ---
# Ensure these environment variables are set in your hosting platform (Render/Railway)
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TIMEZONE = ZoneInfo(os.environ.get("TIMEZONE", "Africa/Lagos")) 
DB_PATH = "assistant_master.db"

logging.basicConfig(level=logging.INFO)

# --- Database Initialization ---
def db_init():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT, 
                chat_id INTEGER, 
                title TEXT, 
                due_time TEXT, 
                notified INTEGER DEFAULT 0,
                status TEXT DEFAULT 'pending'
            )""")
        conn.execute("CREATE TABLE IF NOT EXISTS finance (id INTEGER PRIMARY KEY, chat_id INTEGER, amount REAL, category TEXT, date TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, chat_id INTEGER, content TEXT, date TEXT)")
        conn.commit()

# --- Keyboard UI ---
def get_main_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton("📋 View My Data"), KeyboardButton("❓ Help")],
        [KeyboardButton("🕒 System Time"), KeyboardButton("💰 Finance Status")]
    ], resize_keyboard=True)

# --- The "Watchdog" (Background Reminders) ---
async def check_reminders(context: ContextTypes.DEFAULT_TYPE):
    """Scans every minute for tasks due RIGHT NOW."""
    now = datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M")
    
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        due_tasks = conn.execute(
            "SELECT * FROM tasks WHERE due_time <= ? AND notified = 0 AND status = 'pending'", 
            (now,)
        ).fetchall()
        
        for task in due_tasks:
            try:
                await context.bot.send_message(
                    chat_id=task['chat_id'],
                    text=f"⏰ **TIME'S UP!**\n\n📌 *{task['title']}*\nStatus: Due now!",
                    parse_mode="Markdown"
                )
                conn.execute("UPDATE tasks SET notified = 1 WHERE id = ?", (task['id'],))
            except Exception as e:
                logging.error(f"Reminder failed: {e}")
        conn.commit()

# --- Expanded Dictionary Brain ---
def get_chat_response(text):
    text = text.lower().strip()
    
    brain = {
        "hello": ["Hello! Ready to manage your day?", "Hi! What's our first mission?", "Greetings! I'm online."],
        "who are you": ["I am Dave, your private digital assistant. I don't use AI, so I'm fast and private!", "I'm your command-based manager."],
        "how are you": ["Processing at 100% efficiency!", "I'm doing great. Ready for a task?"],
        "javascript": ["JavaScript is great for web dev! I'm built on Python, though."],
        "python": ["Python is my native language! It's the best for automation."],
        "morning": ["Good morning! Have you planned your day yet?", "Morning! Time to be productive."],
        "night": ["Good night! Don't forget to set your alarm.", "Sleep well. I'll be here tomorrow."],
        "thank": ["Happy to help!", "No problem!", "You're welcome!"],
        "boring": ["Productivity can be tough, but the results are worth it!"],
        "smart": ["I'm as smart as my code allows me to be!"]
    }

    for key, replies in brain.items():
        if key in text:
            return random.choice(replies)
    return None

# --- Main Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db_init()
    await update.message.reply_text(
        "🚀 **Dave Master Assistant Online**\n\n"
        "I'll remind you of tasks automatically. To add one, just type:\n"
        "`Gym at 18:30` or `Meeting at 09:00`.",
        reply_markup=get_main_keyboard()
    )

async def handle_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    chat_id = update.effective_chat.id

    # 1. Check Dictionary
    response = get_chat_response(text)
    if response:
        return await update.message.reply_text(response)

    # 2. Smart Task Grabber (Auto-save if time is found)
    time_match = re.search(r'(\d{1,2}:\d{2})', text)
    if time_match:
        due_time = time_match.group(1)
        title = text.replace(due_time, "").replace("at", "").strip()
        full_due = f"{datetime.now(TIMEZONE).strftime('%Y-%m-%d')} {due_time}"

        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("INSERT INTO tasks (chat_id, title, due_time) VALUES (?,?,?)",
                         (chat_id, title, full_due))
        return await update.message.reply_text(f"✅ **Task Saved!**\n📌 {title}\n⏰ Reminder set for {due_time}")

    # 3. Default to Note
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT INTO notes (chat_id, content, date) VALUES (?,?,?)",
                     (chat_id, text, datetime.now(TIMEZONE).strftime("%Y-%m-%d")))
    await update.message.reply_text("📝 No time detected, so I saved this as a **Note**.")

async def view_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    with sqlite3.connect(DB_PATH) as conn:
        tasks = conn.execute("SELECT id, title, due_time FROM tasks WHERE chat_id=? AND status='pending'", (chat_id,)).fetchall()
        notes = conn.execute("SELECT content FROM notes WHERE chat_id=?", (chat_id,)).fetchall()
    
    msg = "📋 **Your Data:**\n\n**Tasks:**\n"
    msg += "\n".join([f"{t[0]}. {t[1]} ({t[2][11:]})" for t in tasks]) if tasks else "No tasks."
    msg += "\n\n**Notes:**\n"
    msg += "\n".join([f"• {n[0]}" for n in notes]) if notes else "No notes."
    
    await update.message.reply_text(msg, parse_mode="Markdown")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 **User Guide:**\n\n"
        "• **Auto Task:** Just type `[Activity] at [Time]`\n"
        "• **Finance:** `/spend [amt] [cat]`\n"
        "• **Manual Task:** `/add [name]`\n"
        "• **Finish Task:** `/done [ID]`\n"
        "• **View All:** Click the '📋 View My Data' button."
    )

# --- Flask server for uptime ---
server = Flask('')
@server.route('/')
def home(): return "Assistant Active"

def main():
    db_init()
    Thread(target=lambda: server.run(host='0.0.0.0', port=8080), daemon=True).start()
    
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # The Watchdog: Runs every 60 seconds
    app.job_queue.run_repeating(check_reminders, interval=60, first=10)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.Regex('^📋 View My Data$'), view_data))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_msg))

    print("Master Assistant Polling...")
    app.run_polling()

if __name__ == "__main__":
    main()
        
