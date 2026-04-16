import os
import sqlite3
import logging
import json
import re
import google.generativeai as genai
from datetime import datetime, time
from zoneinfo import ZoneInfo
from threading import Thread

from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# --- Configuration & Security ---
# Note: Use environment variables for production!
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = "AIzaSyDcB9f_ItpfWuhJy0YCb6kzaMPPKfpnuVE"
TIMEZONE_STR = os.environ.get("TIMEZONE", "Africa/Lagos")
TIMEZONE = ZoneInfo(TIMEZONE_STR)
DB_PATH = "assistant.db"

# Configure Gemini
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Database Management ---

def db_init():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                title TEXT,
                start TEXT,
                done INTEGER DEFAULT 0
            )
        """)
        conn.commit()

def get_todays_tasks(chat_id):
    today = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT title, start FROM tasks WHERE chat_id = ? AND start LIKE ? AND done = 0",
            (chat_id, f"{today}%")
        ).fetchall()
    return [f"• {r['title']} at {r['start'][11:16]}" for r in rows]

# --- AI Integration Logic ---

async def get_ai_chat_response(prompt: str):
    """Answers general user questions."""
    try:
        response = model.generate_content(f"You are a witty, helpful AI personal assistant. User says: {prompt}")
        return response.text
    except Exception as e:
        logger.error(f"Gemini Chat Error: {e}")
        return "I'm a bit overwhelmed right now. Can we try that again?"

async def ai_parse_task(text: str):
    """Extracts task info using AI."""
    now = datetime.now(TIMEZONE)
    prompt = (
        f"Today's date/time is {now.strftime('%Y-%m-%d %H:%M')}. "
        f"Extract the task title and time from: '{text}'. "
        "Return ONLY a JSON object: {\"title\": \"...\", \"start_iso\": \"YYYY-MM-DDTHH:MM:SS\"}. "
        "If no time is provided, assume today at 12:00:00."
    )
    try:
        response = model.generate_content(prompt)
        match = re.search(r'\{.*\}', response.text, re.DOTALL)
        return json.loads(match.group()) if match else None
    except Exception as e:
        logger.error(f"AI Task Parsing Error: {e}")
        return None

# --- Scheduled Briefings ---

async def daily_briefing(context: ContextTypes.JOB):
    """Sends morning, afternoon, and night updates."""
    chat_id = context.job.chat_id
    label = context.job.data  # "Morning", "Afternoon", or "Night"
    tasks = get_todays_tasks(chat_id)
    task_list = "\n".join(tasks) if tasks else "No tasks scheduled yet."

    prompt = (
        f"It's {label} briefing time. Here is the user's schedule:\n{task_list}\n"
        "Give a warm, concise update. If it's night, reflect on the day. "
        "If it's morning, be motivating."
    )
    
    try:
        response = model.generate_content(prompt)
        await context.bot.send_message(chat_id=chat_id, text=response.text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Briefing Error: {e}")

# --- Telegram Command & Message Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    
    # Schedule the 3 daily messages (Morning 8am, Afternoon 1pm, Night 9pm)
    times = [time(8, 0), time(13, 0), time(21, 0)]
    labels = ["Morning", "Afternoon", "Night"]
    
    # Clean existing jobs to avoid duplicates
    current_jobs = context.job_queue.get_jobs_by_name(f"user_{chat_id}")
    for job in current_jobs: job.schedule_removal()

    for t, label in zip(times, labels):
        context.job_queue.run_daily(
            daily_briefing, t.replace(tzinfo=TIMEZONE), 
            chat_id=chat_id, data=label, name=f"user_{chat_id}"
        )

    await update.message.reply_text(
        "👋 **Assistant Online!**\n\nI will now message you every morning, afternoon, and night.\n"
        "• **Chat:** Ask me anything.\n"
        "• **Tasks:** Say 'Remind me to...' or 'Add task...'",
        parse_mode="Markdown"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    chat_id = update.effective_chat.id

    # Smart Task Detection
    triggers = ["remind", "task", "todo", "schedule", "add"]
    if any(word in user_text.lower() for word in triggers):
        await update.message.reply_chat_action("typing")
        data = await ai_parse_task(user_text)
        if data:
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute("INSERT INTO tasks (chat_id, title, start) VALUES (?,?,?)",
                             (chat_id, data['title'], data['start_iso']))
            await update.message.reply_text(f"✅ **Task Saved**\n📌 {data['title']}\n⏰ {data['start_iso']}", parse_mode="Markdown")
            return

    # General AI Chat
    await update.message.reply_chat_action("typing")
    response = await get_ai_chat_response(user_text)
    await update.message.reply_text(response)

# --- Flask Heartbeat (for Render/Deployment) ---
server = Flask('')
@server.route('/')
def home(): return "Assistant is running."

def run_server():
    server.run(host='0.0.0.0', port=int(os.environ.get("PORT", 8080)))

# --- Main Initialization ---

def main():
    db_init()
    Thread(target=run_server, daemon=True).start()
    
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Bot is polling...")
    app.run_polling()

if __name__ == "__main__":
    main()
