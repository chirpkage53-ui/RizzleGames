import os
import json
import sqlite3
import threading
import telebot
import random
from flask import Flask, request, jsonify
from flask_cors import CORS
from telebot.types import ReplyKeyboardMarkup, KeyboardButton, WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv

# --- CONFIGURATION ---
load_dotenv()
TOKEN = os.getenv('BOT_TOKEN')
if not TOKEN:
    print("❌ Error: BOT_TOKEN is missing from .env file!")
    exit()

bot = telebot.TeleBot(TOKEN, parse_mode="HTML")
app = Flask(__name__)
CORS(app)

DB_FILE = "casino.db"
ADMIN_ID = 5339772189 # <--- IMPORTANT: PUT YOUR TELEGRAM ID HERE

# ==========================================
# DATABASE LOGIC
# ==========================================
def init_db():
    """Initializes all necessary tables for the casino."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        chat_id INTEGER PRIMARY KEY, verified INTEGER DEFAULT 0, 
        main_balance INTEGER DEFAULT 0, bonus_balance INTEGER DEFAULT 0, 
        wager_remaining INTEGER DEFAULT 0, upi_id TEXT, bank_details TEXT, state TEXT)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, type TEXT, 
        amount INTEGER, status TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS game_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, 
        wager INTEGER, won INTEGER, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    conn.close()

def get_user(chat_id):
    """Fetches user data from the database."""
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute("SELECT * FROM users WHERE chat_id = ?", (chat_id,)).fetchone()
    conn.close()
    if row: 
        return {
            'chat_id': row[0], 'verified': bool(row[1]), 'main_balance': row[2], 
            'bonus_balance': row[3], 'wager_remaining': row[4], 'upi_id': row[5], 
            'bank_details': json.loads(row[6]) if row[6] else None, 'state': row[7]
        }
    return None

def update_user(chat_id, **kwargs):
    """Updates specific fields for a user."""
    conn = sqlite3.connect(DB_FILE)
    for key, value in kwargs.items():
        if key == 'bank_details' and value is not None: value = json.dumps(value)
        conn.execute(f"UPDATE users SET {key} = ? WHERE chat_id = ?", (value, chat_id))
    conn.commit()
    conn.close()

init_db()
print("✅ Database locked and loaded!")

WAGER_MULTIPLIER = 10
TEMP_CAPTCHAS = {}

# ==========================================
# WEB APP RECEIVER (FLASK API)
# ==========================================
@app.route('/sync', methods=['POST'])
def handle_background_sync():
    """Receives silent background requests from Netlify and saves to DB."""
    data = request.json
    chat_id = data.get('chat_id')
    
    if not chat_id: return jsonify({"status": "error"}), 400

    # 1. Update user balances
    update_user(chat_id, main_balance=data.get('main', 0), 
                bonus_balance=data.get('bonus', 0), wager_remaining=data.get('wager', 0))
    
    # 2. Log exactly what was wagered and won in this session/roll
    conn = sqlite3.connect(DB_FILE)
    conn.execute("INSERT INTO game_history (chat_id, wager, won) VALUES (?, ?, ?)", 
                 (chat_id, data.get('total_bet', 0), data.get('total_won', 0)))
    conn.commit()
    conn.close()
    
    return jsonify({"status": "success"}), 200

# ==========================================
# TELEGRAM UI HELPER
# ==========================================
def get_main_menu(chat_id):
    """Builds the dynamic keyboard injecting live balances into the URL."""
    user = get_user(chat_id)
    base_url = "https://comfy-lily-05a411.netlify.app/"
    dynamic_url = f"{base_url}/?main={user['main_balance']}&bonus={user['bonus_balance']}&wager={user['wager_remaining']}"
    
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row(KeyboardButton("🎮 Play Game", web_app=WebAppInfo(url=dynamic_url)))
    markup.row(KeyboardButton("💰 Balance"), KeyboardButton("📜 History"))
    markup.row(KeyboardButton("📥 Deposit"), KeyboardButton("📤 Withdrawal"))
    markup.row(KeyboardButton("👤 Profile"), KeyboardButton("🏦 UPI / Banks"))
    return markup

# ==========================================
# ADMIN PANEL LOGIC
# ==========================================
@bot.message_handler(commands=['admin'])
def admin_panel(message):
    if message.chat.id != ADMIN_ID: return
    
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("✅ Pending Deposits", callback_data="admin_deposits"))
    markup.row(InlineKeyboardButton("📈 View All Users", callback_data="admin_users"))
    bot.reply_to(message, "👑 <b>ADMIN PANEL</b>", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_"))
def admin_actions(call):
    if call.data == "admin_users":
        conn = sqlite3.connect(DB_FILE)
        users = conn.execute("SELECT chat_id, main_balance, bonus_balance FROM users").fetchall()
        conn.close()
        msg = "📈 <b>User Balances:</b>\n" + "\n".join([f"ID: <code>{u[0]}</code> | Main: ₹{u[1]} | Bonus: ₹{u[2]}" for u in users])
        bot.send_message(call.message.chat.id, msg)
    
    elif call.data == "admin_deposits":
        conn = sqlite3.connect(DB_FILE)
        pending = conn.execute("SELECT id, chat_id, amount FROM transactions WHERE status = 'PENDING'").fetchall()
        conn.close()
        
        if not pending:
            bot.send_message(call.message.chat.id, "✅ No pending deposits.")
            return
            
        for p in pending:
            markup = InlineKeyboardMarkup()
            markup.row(InlineKeyboardButton("✅ Approve", callback_data=f"approve_{p[0]}_{p[1]}_{p[2]}"))
            bot.send_message(call.message.chat.id, f"📥 <b>Pending Deposit:</b>\nID: <code>{p[1]}</code>\nAmount: <b>₹{p[2]}</b>", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("approve_"))
def approve_deposit(call):
    _, tx_id, user_chat_id, amount = call.data.split("_")
    user_chat_id, amount = int(user_chat_id), int(amount)
    
    user = get_user(user_chat_id)
    if user:
        # Update User Balance
        new_main = user['main_balance'] + amount
        update_user(user_chat_id, main_balance=new_main)
        
        # Update Transaction Status
        conn = sqlite3.connect(DB_FILE)
        conn.execute("UPDATE transactions SET status = 'COMPLETED' WHERE id = ?", (tx_id,))
        conn.commit()
        conn.close()
        
        bot.edit_message_text(f"✅ Approved ₹{amount} for user {user_chat_id}", call.message.chat.id, call.message.message_id)
        bot.send_message(user_chat_id, f"🎉 <b>DEPOSIT APPROVED!</b>\n₹{amount} has been added to your Main Wallet.")

# ==========================================
# MAIN BOT LOGIC (START, MENUS, TRANSACTIONS)
# ==========================================
@bot.message_handler(commands=['start'])
def start(message):
    chat_id = message.chat.id
    if not get_user(chat_id): 
        sqlite3.connect(DB_FILE).execute("INSERT INTO users (chat_id) VALUES (?)", (chat_id,)).connection.commit()
    
    user = get_user(chat_id)
    if user['verified']:
        bot.send_message(chat_id, "Welcome back! 🎲", reply_markup=get_main_menu(chat_id))
    else:
        num1, num2 = random.randint(1, 10), random.randint(1, 10)
        TEMP_CAPTCHAS[chat_id] = str(num1 + num2)
        update_user(chat_id, state='AWAITING_CAPTCHA')
        bot.send_message(chat_id, f"🛡️ <b>SECURITY VERIFICATION</b>\nSolve: <b>{num1} + {num2}?</b>")

@bot.message_handler(func=lambda message: True)
def handle_text(message):
    chat_id = message.chat.id
    text = message.text
    user = get_user(chat_id)
    
    if not user: return

    # --- CAPTCHA STATE ---
    if user['state'] == 'AWAITING_CAPTCHA':
        if text.strip() == TEMP_CAPTCHAS.get(chat_id):
            update_user(chat_id, verified=1, bonus_balance=100, wager_remaining=(100 * WAGER_MULTIPLIER), state=None)
            msg = "🎉 <b>Identity Verified!</b>\n🎁 ₹100 Bonus Cash credited. Tap Play Game to wager it!"
            bot.send_message(chat_id, msg, reply_markup=get_main_menu(chat_id))
        else:
            bot.reply_to(message, "❌ Wrong! Try again.")
        return

    # Cancel state if menu clicked
    menu_items = ["🎮 Play Game", "💰 Balance", "📜 History", "📥 Deposit", "📤 Withdrawal", "👤 Profile", "🏦 UPI / Banks"]
    if text in menu_items:
        update_user(chat_id, state=None)

    # --- MENU COMMANDS ---
    if text == "💰 Balance":
        bot.reply_to(message, f"💰 <b>Main:</b> ₹{user['main_balance']} | 🎁 <b>Bonus:</b> ₹{user['bonus_balance']}\n🔄 <b>Wager Left:</b> ₹{user['wager_remaining']}")
        bot.send_message(chat_id, "Menu refreshed.", reply_markup=get_main_menu(chat_id))
    
    elif text == "📜 History":
        conn = sqlite3.connect(DB_FILE)
        hist = conn.execute("SELECT type, amount, status, timestamp FROM transactions WHERE chat_id = ? ORDER BY id DESC LIMIT 5", (chat_id,)).fetchall()
        conn.close()
        msg = "📜 <b>Recent Transactions:</b>\n\n" + ("\n".join([f"🔸 {h[0]} - ₹{h[1]} ({h[2]})" for h in hist]) if hist else "<i>No transactions yet.</i>")
        bot.reply_to(message, msg)
        
    elif text == "👤 Profile":
        bot.reply_to(message, f"👤 <b>PROFILE</b>\n🆔 ID: <code>{chat_id}</code>\n🏦 Linked Bank: {'Yes ✅' if user['bank_details'] else 'No ❌'}")
        
    elif text == "📥 Deposit":
        update_user(chat_id, state='AWAITING_DEPOSIT_AMT')
        bot.reply_to(message, "📥 Enter the amount you wish to deposit (Min ₹100):")

    elif text == "📤 Withdrawal":
        if not user['bank_details'] and not user['upi_id']:
            bot.reply_to(message, "❌ Link a Bank or UPI first from the menu!")
            return
        update_user(chat_id, state='AWAITING_WITHDRAW_AMT')
        bot.reply_to(message, f"📤 Available to withdraw: <b>₹{user['main_balance']}</b>\nEnter amount:")

    # --- INPUT STATES ---
    elif user['state'] == 'AWAITING_DEPOSIT_AMT':
        if not text.isdigit() or int(text) < 100:
            bot.reply_to(message, "❌ Minimum deposit is ₹100.")
            return
        amt = int(text)
        update_user(chat_id, state=None)
        
        # Log Transaction
        conn = sqlite3.connect(DB_FILE)
        conn.execute("INSERT INTO transactions (chat_id, type, amount, status) VALUES (?, 'DEPOSIT', ?, 'PENDING')", (chat_id, amt))
        conn.commit()
        conn.close()
        
        payment_url = f"https://example.com/pay?amount={amt}&user={chat_id}"
        bot.reply_to(message, f"📥 <b>Deposit requested: ₹{amt}</b>\n🔗 <a href='{payment_url}'>Pay Now</a>\n<i>Your balance will update once approved by admin.</i>")

    elif user['state'] == 'AWAITING_WITHDRAW_AMT':
        if not text.isdigit() or int(text) < 100:
            bot.reply_to(message, "❌ Minimum withdrawal is ₹100.")
            return
        amt = int(text)
        if amt > user['main_balance']:
            bot.reply_to(message, f"❌ Insufficient Main Balance (₹{user['main_balance']}).")
            return
        
        # Deduct Balance & Log Transaction
        update_user(chat_id, main_balance=user['main_balance'] - amt, state=None)
        conn = sqlite3.connect(DB_FILE)
        conn.execute("INSERT INTO transactions (chat_id, type, amount, status) VALUES (?, 'WITHDRAWAL', ?, 'PENDING')", (chat_id, amt))
        conn.commit()
        conn.close()
        
        bot.reply_to(message, f"📤 <b>Withdrawal requested: ₹{amt}</b>\nSent to payout team.")

# ==========================================
# BOOT SEQUENCE
# ==========================================
def run_flask():
    print("✅ Local API Server is online!")
    app.run(port=5000)

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    print("✅ Premium Bot is online and polling...")
    bot.infinity_polling()