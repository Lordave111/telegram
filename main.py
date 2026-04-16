import os
import sqlite3
import logging
import random
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
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TIMEZONE = ZoneInfo(os.environ.get("TIMEZONE", "Africa/Lagos")) # Adjusted for your local time
DB_PATH = "assistant_master.db"

logging.basicConfig(level=logging.INFO)

# --- Database Initialization ---
def db_init():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY, chat_id INTEGER, title TEXT, status TEXT DEFAULT 'pending')")
        conn.execute("CREATE TABLE IF NOT EXISTS finance (id INTEGER PRIMARY KEY, chat_id INTEGER, amount REAL, category TEXT, date TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, chat_id INTEGER, content TEXT, date TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS habits (id INTEGER PRIMARY KEY, chat_id INTEGER, name TEXT, streak INTEGER DEFAULT 0)")
        conn.execute("CREATE TABLE IF NOT EXISTS goals (id INTEGER PRIMARY KEY, chat_id INTEGER, target TEXT)")
        conn.commit()

# --- Keyboard UI ---
def get_main_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton("📋 View My Data"), KeyboardButton("📈 Habit Tracker")],
        [KeyboardButton("📝 Quick Memo"), KeyboardButton("💰 Finance Status")],
        [KeyboardButton("🕒 System Time"), KeyboardButton("❓ Help")]
    ], resize_keyboard=True)

def get_view_inline_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Tasks", callback_data="v_tasks"), InlineKeyboardButton("📜 Notes", callback_data="v_notes")],
        [InlineKeyboardButton("💵 Money", callback_data="v_fin"), InlineKeyboardButton("🔥 Habits", callback_data="v_habits")],
        [InlineKeyboardButton("🎯 My Goals", callback_data="v_goals")]
    ])

# --- Expanded Dictionary Brain ---
def get_chat_response(text):
    text = text.lower().strip()
    
    responses = {
        "hello": ["Hello! Ready to smash some goals today?", "Hi there! How can I assist your productivity?", "Hey! I'm online and ready."],
        "morning": ["Good morning! Remember: A productive day starts with a clear plan.", "Morning! Have you checked your /tasks yet?"],
        "night": ["Good night! Logging off? I'll be here in the morning.", "Rest well. You've earned it."],
        "who are you": ["I am Dave, your advanced command-based personal assistant. I don't need AI to keep you organized!", "I'm your digital manager."],
        "how are you": ["Processing at 100% efficiency. Thanks for asking!", "Systems optimal. Ready for your next command."],
        "javascript": ["JavaScript is a high-level language used for the web. Need me to save a JS note for you?"],
        "python": ["Python is the king of automation! I was actually built using it."],
        "thank": ["Happy to be of service!", "No problem at all.", "Anytime, boss!"],
        "weather": ["I can't check live weather yet, but if it's raining, don't forget your umbrella!"],
        "joke": ["Why did the developer go broke? Because he used up all his cache!", "I'd tell you a chemistry joke but I know I wouldn't get a reaction."],
        "boring": ["Productivity isn't always fun, but the results are! Let's get back to work."],
        "smart": ["I'm only as smart as the person who programmed me—and the person using me!"]
    }

    for key, replies in responses.items():
        if key in text:
            return random.choice(replies)

    return "💬 I'm listening. Use /help if you need a specific action, or type /note to save this thought."

# --- Core Logic Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db_init()
    chat_id = update.effective_chat.id
    
    # Schedule Briefings (8am, 1pm, 8pm)
    times = [time(8, 0), time(13, 0), time(20, 0)]
    for t, label in zip(times, ["Morning", "Afternoon", "Night"]):
        context.job_queue.run_daily(send_briefing, t.replace(tzinfo=TIMEZONE), chat_id=chat_id, data=label)

    await update.message.reply_text(
        "✨ **Dave Master Assistant Online**\n\nYour menu is ready below. I am now your personal manager for tasks, money, notes, habits, and goals.",
        reply_markup=get_main_keyboard()
    )

async def send_briefing(context: ContextTypes.JOB):
    chat_id = context.job.chat_id
    with sqlite3.connect(DB_PATH) as conn:
        tasks = conn.execute("SELECT title FROM tasks WHERE chat_id=? AND status='pending'", (chat_id,)).fetchall()
    task_str = "\n".join([f"• {t[0]}" for t in tasks]) if tasks else "No tasks!"
    await context.bot.send_message(chat_id=chat_id, text=f"🔔 **{context.job.data} Update**\n\nYour Tasks:\n{task_str}")

# --- Functional Commands ---

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 **Command Manual**\n\n"
        "✅ `/add [text]` - Create task\n"
        "✅ `/done [id]` - Complete task\n"
        "💰 `/spend [amt] [cat]` - Log money\n"
        "📜 `/note [text]` - Save note\n"
        "🔥 `/habit [name]` - Start habit\n"
        "🎯 `/goal [text]` - Record goal\n"
        "🔢 `/calc [math]` - Calculator\n"
        "🔍 `/view` - Open Data Center",
        parse_mode="Markdown"
    )

async def add_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmd = update.message.text.split()[0][1:] # get 'add', 'note', 'spend', etc.
    content = " ".join(context.args)
    chat_id = update.effective_chat.id

    with sqlite3.connect(DB_PATH) as conn:
        if cmd == "add":
            conn.execute("INSERT INTO tasks (chat_id, title) VALUES (?,?)", (chat_id, content))
            msg = "✅ Task Saved"
        elif cmd == "note":
            conn.execute("INSERT INTO notes (chat_id, content, date) VALUES (?,?,?)", (chat_id, content, datetime.now().strftime("%Y-%m-%d")))
            msg = "📝 Note Saved"
        elif cmd == "habit":
            conn.execute("INSERT INTO habits (chat_id, name) VALUES (?,?)", (chat_id, content))
            msg = f"🔥 Habit '{content}' started!"
        elif cmd == "goal":
            conn.execute("INSERT INTO goals (chat_id, target) VALUES (?,?)", (chat_id, content))
            msg = "🎯 Goal Recorded"
        else: msg = "❌ Error"
    await update.message.reply_text(msg)

# --- View Center (Callbacks) ---

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    
    with sqlite3.connect(DB_PATH) as conn:
        if query.data == "v_tasks":
            data = conn.execute("SELECT id, title FROM tasks WHERE chat_id=? AND status='pending'", (chat_id,)).fetchall()
            text = "📋 **Tasks:**\n" + "\n".join([f"{r[0]}. {r[1]}" for r in data])
        elif query.data == "v_notes":
            data = conn.execute("SELECT content FROM notes WHERE chat_id=?", (chat_id,)).fetchall()
            text = "📜 **Notes:**\n" + "\n".join([f"• {r[0]}" for r in data])
        elif query.data == "v_habits":
            data = conn.execute("SELECT name, streak FROM habits WHERE chat_id=?", (chat_id,)).fetchall()
            text = "🔥 **Habits:**\n" + "\n".join([f"{r[0]}: {r[1]} days" for r in data])
        elif query.data == "v_goals":
            data = conn.execute("SELECT target FROM goals WHERE chat_id=?", (chat_id,)).fetchall()
            text = "🎯 **Goals:**\n" + "\n".join([f"• {r[0]}" for r in data])
        elif query.data == "v_fin":
            data = conn.execute("SELECT SUM(amount) FROM finance WHERE chat_id=?", (chat_id,)).fetchone()[0]
            text = f"💰 **Total Expenses Recorded:** ${data if data else 0}"

    await query.edit_message_text(text if text else "Nothing found.", parse_mode="Markdown")

# --- Message Router ---

async def handle_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "📋 View My Data":
        await update.message.reply_text("🔍 **Data Center**", reply_markup=get_view_inline_keyboard())
    elif text == "❓ Help":
        await help_cmd(update, context)
    elif text == "🕒 System Time":
        await update.message.reply_text(f"🕒 Time: {datetime.now(TIMEZONE).strftime('%I:%M %p')}")
    elif text == "💰 Finance Status":
        await update.message.reply_text("Check your spending via 'View My Data' or use `/spend [amt] [cat]`")
    else:
        await update.message.reply_text(get_chat_response(text))

# --- Server & Run ---
server = Flask('')
@server.route('/')
def home(): return "Online"

def main():
    db_init()
    Thread(target=lambda: server.run(host='0.0.0.0', port=8080), daemon=True).start()
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler(["add", "note", "habit", "goal"], add_data))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_msg))

    print("Master Assistant Polling...")
    app.run_polling()

if __name__ == "__main__":
    main()
