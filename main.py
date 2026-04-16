import os
import sqlite3
import logging
import random
from datetime import datetime, time
from zoneinfo import ZoneInfo
from threading import Thread

from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# --- Config ---
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TIMEZONE = ZoneInfo(os.environ.get("TIMEZONE", "America/New_York"))
DB_PATH = "assistant_pro.db"

logging.basicConfig(level=logging.INFO)

# --- Database Setup ---
def db_init():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY, chat_id INTEGER, title TEXT, status TEXT DEFAULT 'pending')")
        conn.execute("CREATE TABLE IF NOT EXISTS finance (id INTEGER PRIMARY KEY, chat_id INTEGER, amount REAL, category TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, chat_id INTEGER, content TEXT)")
        conn.commit()

# --- Rules-Based Chat Engine ---
def get_chat_response(text):
    text = text.lower()
    
    responses = {
        "greetings": ["Hello! How can I help you today?", "Hi there! Ready to get organized?", "Hey! What's on the agenda?"],
        "identity": ["I am Dave, your personal command-based assistant.", "I'm your assistant! I don't use AI, so I'm fast and always reliable."],
        "status": ["I'm running perfectly!", "All systems go. Ready for your commands."],
        "thanks": ["You're welcome!", "No problem, happy to help!", "Anytime!"]
    }

    if any(word in text for word in ["hi", "hello", "hey", "good morning", "good evening"]):
        return random.choice(responses["greetings"])
    
    if any(word in text for word in ["who are you", "your name", "what are you"]):
        return random.choice(responses["identity"])
    
    if "how are you" in text:
        return random.choice(responses["status"])

    if any(word in text for word in ["thank", "thanks"]):
        return random.choice(responses["thanks"])

    return "💬 I'm not sure how to respond to that, but I can manage your tasks! Type /help to see what I can do."

# --- Scheduled Briefings ---
async def send_briefing(context: ContextTypes.JOB):
    chat_id = context.job.chat_id
    period = context.job.data # Morning, Afternoon, or Night
    
    with sqlite3.connect(DB_PATH) as conn:
        tasks = conn.execute("SELECT title FROM tasks WHERE chat_id=? AND status='pending'", (chat_id,)).fetchall()
    
    task_list = "\n".join([f"• {t[0]}" for t in tasks]) if tasks else "No pending tasks."
    
    messages = {
        "Morning": f"☀️ **Good Morning!**\nHere is your plan for today:\n\n{task_list}",
        "Afternoon": f"🌤 **Good Afternoon!**\nQuick check-in. Remaining tasks:\n\n{task_list}",
        "Night": f"🌙 **Good Night!**\nRest well. Here is what's left for tomorrow:\n\n{task_list}"
    }
    
    await context.bot.send_message(chat_id=chat_id, text=messages[period], parse_mode="Markdown")

# --- Command Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    
    # Schedule Morning (8am), Afternoon (1pm), and Night (9pm)
    times = [time(8, 0), time(13, 0), time(21, 0)]
    labels = ["Morning", "Afternoon", "Night"]
    
    for t, label in zip(times, labels):
        context.job_queue.run_daily(send_briefing, t.replace(tzinfo=TIMEZONE), chat_id=chat_id, data=label)

    await update.message.reply_text(
        "🚀 **Dave Assistant Activated!**\n"
        "I will message you 3 times a day.\n\n"
        "**Commands:**\n"
        "/add [task] - Add a todo\n"
        "/list - Show tasks\n"
        "/spend [amt] [cat] - Track money\n"
        "/memo [text] - Save a note\n"
        "/calc [math] - Calculator\n"
        "/help - See all commands\n\n"
        "Or just say 'Hello' to me!",
        parse_mode="Markdown"
    )

async def add_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    title = " ".join(context.args)
    if not title: return await update.message.reply_text("❌ Usage: /add Buy Milk")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT INTO tasks (chat_id, title) VALUES (?,?)", (update.effective_chat.id, title))
    await update.message.reply_text(f"✅ Added: {title}")

async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT id, title FROM tasks WHERE chat_id=? AND status='pending'", (update.effective_chat.id,)).fetchall()
    text = "📋 **Your Tasks:**\n" + "\n".join([f"{r[0]}. {r[1]}" for r in rows]) if rows else "📭 No tasks."
    await update.message.reply_text(text, parse_mode="Markdown")

async def chat_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    response = get_chat_response(update.message.text)
    await update.message.reply_text(response)

async def calculate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        res = eval(" ".join(context.args))
        await update.message.reply_text(f"🔢 Result: {res}")
    except: await update.message.reply_text("❌ Error in math.")

# --- Flask Server ---
server = Flask('')
@server.route('/')
def home(): return "Bot Running"

def main():
    db_init()
    Thread(target=lambda: server.run(host='0.0.0.0', port=8080), daemon=True).start()
    
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add_task))
    app.add_handler(CommandHandler("list", list_tasks))
    app.add_handler(CommandHandler("calc", calculate))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat_handler))

    print("Assistant is online.")
    app.run_polling()

if __name__ == "__main__":
    main()
    
