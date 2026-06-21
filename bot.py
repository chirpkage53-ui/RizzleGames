import os
import json
import threading
import telebot
import random
import psycopg2
from psycopg2 import sql
from flask import Flask, request, jsonify
from flask_cors import CORS
from telebot.types import ReplyKeyboardMarkup, KeyboardButton, WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv
from datetime import datetime, timedelta

# --- CONFIGURATION ---
load_dotenv()
TOKEN = os.getenv('BOT_TOKEN')
DB_URL = os.getenv('DATABASE_URL')

if not TOKEN or not DB_URL:
    print("❌ Error: BOT_TOKEN or DATABASE_URL is missing from environment variables!")
    exit()

bot = telebot.TeleBot(TOKEN, parse_mode="HTML")
app = Flask(__name__)
CORS(app)

ADMIN_ID = 5339772189 # Your Admin ID is set

# 👇👇 REPLACE THIS WITH YOUR REAL PAYMENT UPI ID 👇👇
ADMIN_UPI = "your-upi@bank" 

# ==========================================
# DATABASE LOGIC (POSTGRESQL)
# ==========================================
def get_db_connection():
    return psycopg2.connect(DB_URL)

def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        chat_id BIGINT PRIMARY KEY, verified INTEGER DEFAULT 0, 
        main_balance INTEGER DEFAULT 0, bonus_balance INTEGER DEFAULT 0, 
        wager_remaining INTEGER DEFAULT 0, upi_id TEXT, bank_details TEXT, state TEXT)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS transactions (
        id SERIAL PRIMARY KEY, chat_id BIGINT, type TEXT, 
        amount INTEGER, status TEXT, utr TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    
    # Safely upgrade the database to include the UTR column if it doesn't exist
    try:
        c.execute("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS utr TEXT")
    except Exception:
        pass
        
    c.execute('''CREATE TABLE IF NOT EXISTS game_history (
        id SERIAL PRIMARY KEY, chat_id BIGINT, 
        wager INTEGER, won INTEGER, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    conn.close()

def get_user(chat_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE chat_id = %s", (chat_id,))
    row = c.fetchone()
    conn.close()
    if row: 
        return {
            'chat_id': row[0], 'verified': bool(row[1]), 'main_balance': row[2], 
            'bonus_balance': row[3], 'wager_remaining': row[4], 'upi_id': row[5], 
            'bank_details': json.loads(row[6]) if row[6] else None, 'state': row[7]
        }
    return None

def update_user(chat_id, **kwargs):
    conn = get_db_connection()
    c = conn.cursor()
    for key, value in kwargs.items():
        if key == 'bank_details' and value is not None: value = json.dumps(value)
        c.execute(sql.SQL("UPDATE users SET {} = %s WHERE chat_id = %s").format(sql.Identifier(key)), (value, chat_id))
    conn.commit()
    conn.close()

def get_total_deposits(chat_id):
    """Calculates the total completed deposits for a user."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT SUM(amount) FROM transactions WHERE chat_id = %s AND type = 'DEPOSIT' AND status = 'COMPLETED'", (chat_id,))
    total = c.fetchone()[0]
    conn.close()
    return total if total else 0

init_db()
print("✅ PostgreSQL Database locked and loaded!")

WAGER_MULTIPLIER = 10
TEMP_CAPTCHAS = {}
TEMP_DEPOSITS = {} # Temporarily holds the deposit amount between steps

# ==========================================
# WEB APP RECEIVER (FLASK API)
# ==========================================
@app.route('/sync', methods=['POST'])
def handle_background_sync():
    data = request.json
    chat_id = data.get('chat_id')
    if not chat_id: return jsonify({"status": "error"}), 400

    update_user(chat_id, main_balance=data.get('main', 0), 
                bonus_balance=data.get('bonus', 0), wager_remaining=data.get('wager', 0))
    
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO game_history (chat_id, wager, won) VALUES (%s, %s, %s)", 
                 (chat_id, data.get('total_bet', 0), data.get('total_won', 0)))
    conn.commit()
    conn.close()
    return jsonify({"status": "success"}), 200

@app.route('/get_balance', methods=['POST'])
def serve_live_balance():
    data = request.json
    chat_id = data.get('chat_id')
    if not chat_id: return jsonify({"status": "error"}), 400

    user = get_user(chat_id)
    if user:
        return jsonify({
            "status": "success",
            "main": user['main_balance'],
            "bonus": user['bonus_balance'],
            "wager": user['wager_remaining']
        }), 200
    else:
        return jsonify({"status": "error"}), 404

# ==========================================
# TELEGRAM UI HELPER
# ==========================================
def get_main_menu(chat_id):
    user = get_user(chat_id)
    base_url = "https://adorable-llama-015fe9.netlify.app"
    dynamic_url = f"{base_url}/?chat_id={chat_id}&main={user['main_balance']}&bonus={user['bonus_balance']}&wager={user['wager_remaining']}"
    
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row(KeyboardButton("🎮 Play Game", web_app=WebAppInfo(url=dynamic_url)))
    markup.row(KeyboardButton("💳 Balance"), KeyboardButton("📊 History"))
    markup.row(KeyboardButton("⚡ Deposit"), KeyboardButton("🏛️ Withdrawal"))
    markup.row(KeyboardButton("👤 Profile"), KeyboardButton("🏦 UPI / Banks"))
    return markup

# ==========================================
# ADMIN PANEL LOGIC
# ==========================================
@bot.message_handler(commands=['admin'])
def admin_panel(message):
    if message.chat.id != ADMIN_ID: return
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("⚡ Pending Deposits", callback_data="admin_deposits"))
    markup.row(InlineKeyboardButton("🏛️ Pending Withdrawals", callback_data="admin_withdrawals"))
    markup.row(InlineKeyboardButton("👥 View All Users", callback_data="admin_users"))
    markup.row(InlineKeyboardButton("💳 Edit User Balance", callback_data="admin_edit_bal"))
    bot.reply_to(message, "⚙️ <b>RIZZLE GAMES | SYSTEM ADMIN</b>", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_"))
def admin_actions(call):
    conn = get_db_connection()
    c = conn.cursor()
    
    if call.data == "admin_users":
        c.execute("SELECT chat_id, main_balance, bonus_balance FROM users")
        users = c.fetchall()
        msg = "📊 <b>USER DIRECTORY</b>\n" + "\n".join([f"ID: <code>{u[0]}</code> | Main: ₹{u[1]} | Bonus: ₹{u[2]}" for u in users])
        bot.send_message(call.message.chat.id, msg[:4000])
    
    elif call.data == "admin_deposits":
        c.execute("SELECT id, chat_id, amount, utr FROM transactions WHERE type = 'DEPOSIT' AND status = 'PENDING'")
        pending = c.fetchall()
        if not pending:
            bot.send_message(call.message.chat.id, "✅ No pending deposits.")
        else:
            for p in pending:
                tx_id, uid, amt, utr = p[0], p[1], p[2], p[3]
                markup = InlineKeyboardMarkup()
                markup.row(
                    InlineKeyboardButton("✅ Approve", callback_data=f"tx_app_dep_{tx_id}_{uid}_{amt}"),
                    InlineKeyboardButton("❌ Reject", callback_data=f"tx_rej_dep_{tx_id}_{uid}_{amt}")
                )
                bot.send_message(call.message.chat.id, (
                    "⚡ <b>DEPOSIT REVIEW</b>\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    f"👤 <b>User:</b> <code>{uid}</code>\n"
                    f"💵 <b>Amount Claimed:</b> <code>₹{amt}</code>\n"
                    f"🧾 <b>UTR / Ref:</b> <code>{utr}</code>"
                ), reply_markup=markup)

    elif call.data == "admin_withdrawals":
        c.execute("SELECT id, chat_id, amount FROM transactions WHERE type = 'WITHDRAWAL' AND status = 'PENDING'")
        pending = c.fetchall()
        if not pending:
            bot.send_message(call.message.chat.id, "✅ No pending withdrawals.")
        else:
            for p in pending:
                tx_id, uid, amt = p[0], p[1], p[2]
                
                # Retrieve User's Payout Details dynamically
                u_data = get_user(uid)
                upi = u_data['upi_id'] if u_data and u_data['upi_id'] else "UNBOUND ❌"
                bank = "UNBOUND ❌"
                if u_data and u_data['bank_details']:
                    bank = u_data['bank_details'].get('info', 'BOUND ✅')
                
                markup = InlineKeyboardMarkup()
                markup.row(
                    InlineKeyboardButton("✅ Approve (Paid)", callback_data=f"tx_app_wit_{tx_id}_{uid}_{amt}"),
                    InlineKeyboardButton("❌ Reject (Refund)", callback_data=f"tx_rej_wit_{tx_id}_{uid}_{amt}")
                )
                
                bot.send_message(call.message.chat.id, (
                    "🏛️ <b>WITHDRAWAL REVIEW</b>\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    f"👤 <b>User:</b> <code>{uid}</code>\n"
                    f"💵 <b>Amount to Pay:</b> <code>₹{amt}</code>\n\n"
                    "<b>PAYMENT DESTINATION:</b>\n"
                    f"🔸 <b>UPI:</b> <code>{upi}</code>\n"
                    f"🔸 <b>Bank:</b> <code>{bank}</code>"
                ), reply_markup=markup)

    elif call.data == "admin_edit_bal":
        msg = bot.send_message(call.message.chat.id, "✏️ <b>Enter the User's Chat ID:</b>")
        bot.register_next_step_handler(msg, admin_process_edit_id)
        
    conn.close()

def admin_process_edit_id(message):
    if message.chat.id != ADMIN_ID: return
    try:
        target_id = int(message.text.strip())
        user = get_user(target_id)
        if not user:
            bot.reply_to(message, "❌ User not found.")
            return
        msg = bot.reply_to(message, f"👤 User: <code>{target_id}</code>\n💳 Current Balance: <b>₹{user['main_balance']}</b>\n\n✏️ Enter the <b>NEW Main Balance</b>:")
        bot.register_next_step_handler(msg, admin_process_new_balance, target_id)
    except ValueError:
        bot.reply_to(message, "❌ Invalid ID format.")

def admin_process_new_balance(message, target_id):
    if message.chat.id != ADMIN_ID: return
    try:
        new_bal = int(message.text.strip())
        update_user(target_id, main_balance=new_bal)
        bot.reply_to(message, f"✅ Successfully updated user <code>{target_id}</code> to <b>₹{new_bal}</b>.")
        bot.send_message(target_id, f"🔔 <b>SYSTEM UPDATE</b>\nYour main balance has been adjusted to <code>₹{new_bal}</code>.")
    except ValueError:
        bot.reply_to(message, "❌ Invalid amount.")

@bot.callback_query_handler(func=lambda call: call.data.startswith("tx_"))
def handle_transactions(call):
    parts = call.data.split("_")
    action, tx_type, tx_id, user_id, amount = parts[1], parts[2], int(parts[3]), int(parts[4]), int(parts[5])

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT status FROM transactions WHERE id = %s", (tx_id,))
    status = c.fetchone()
    if not status or status[0] != 'PENDING':
        bot.answer_callback_query(call.id, "❌ Transaction already processed!", show_alert=True)
        bot.delete_message(call.message.chat.id, call.message.message_id)
        conn.close()
        return

    user = get_user(user_id)
    if not user:
        bot.answer_callback_query(call.id, "❌ User not found.")
        conn.close()
        return

    if tx_type == "dep":
        if action == "app":
            update_user(user_id, main_balance=user['main_balance'] + amount)
            c.execute("UPDATE transactions SET status = 'COMPLETED' WHERE id = %s", (tx_id,))
            bot.edit_message_text(f"✅ Approved Deposit of ₹{amount} for <code>{user_id}</code>", call.message.chat.id, call.message.message_id)
            
            bot.send_message(user_id, (
                "<b>🟢 DEPOSIT CONFIRMED</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"💳 <b>Credited:</b> <code>₹{amount}</code>\n\n"
                "<i>Your funds are now available. Best of luck at Rizzle Games!</i>"
            ))
            
        elif action == "rej":
            c.execute("UPDATE transactions SET status = 'REJECTED' WHERE id = %s", (tx_id,))
            bot.edit_message_text(f"❌ Rejected Deposit of ₹{amount} for <code>{user_id}</code>", call.message.chat.id, call.message.message_id)
            bot.send_message(user_id, f"🔴 <b>DEPOSIT DECLINED</b>\nYour transaction of <code>₹{amount}</code> could not be verified. Please contact support.")

    elif tx_type == "wit":
        if action == "app":
            c.execute("UPDATE transactions SET status = 'COMPLETED' WHERE id = %s", (tx_id,))
            bot.edit_message_text(f"✅ Approved Withdrawal of ₹{amount} for <code>{user_id}</code>", call.message.chat.id, call.message.message_id)
            
            bot.send_message(user_id, (
                "<b>🟢 WITHDRAWAL PROCESSED</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"🏛️ <b>Amount Sent:</b> <code>₹{amount}</code>\n\n"
                "<i>Your funds have been dispatched to your linked payout method.</i>"
            ))
            
        elif action == "rej":
            update_user(user_id, main_balance=user['main_balance'] + amount)
            c.execute("UPDATE transactions SET status = 'REJECTED' WHERE id = %s", (tx_id,))
            bot.edit_message_text(f"❌ Rejected Withdrawal of ₹{amount} for <code>{user_id}</code>. Funds auto-refunded.", call.message.chat.id, call.message.message_id)
            bot.send_message(user_id, f"🔴 <b>WITHDRAWAL DECLINED</b>\nYour withdrawal of <code>₹{amount}</code> was rejected. Funds have been safely returned to your wallet.")

    conn.commit()
    conn.close()

@bot.callback_query_handler(func=lambda call: call.data in ["edit_upi", "edit_bank"])
def edit_payment_methods(call):
    chat_id = call.message.chat.id
    if call.data == "edit_upi":
        update_user(chat_id, state='AWAITING_UPI')
        bot.edit_message_text(
            "<b>🔗 BIND UPI ADDRESS</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Reply to this message with your precise UPI ID.\n\n"
            "<i>Format:</i> <code>username@okicici</code>", 
            chat_id, call.message.message_id
        )
    elif call.data == "edit_bank":
        update_user(chat_id, state='AWAITING_BANK')
        bot.edit_message_text(
            "<b>🏦 BIND BANK ACCOUNT</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Reply with your bank details in a single message.\n\n"
            "<i>Format:</i> <code>Account Number, IFSC Code, Holder Name</code>", 
            chat_id, call.message.message_id
        )

# ==========================================
# MAIN BOT LOGIC & RIZZLE GAMES TEXTS
# ==========================================
@bot.message_handler(commands=['start'])
def start(message):
    chat_id = message.chat.id
    if not get_user(chat_id): 
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("INSERT INTO users (chat_id) VALUES (%s) ON CONFLICT DO NOTHING", (chat_id,))
        conn.commit()
        conn.close()
    
    user = get_user(chat_id)
    if user['verified']:
        bot.send_message(chat_id, (
            "<b>🎮 RIZZLE GAMES LOBBY</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Welcome back. Your session is active.\n\n"
            "<i>Select '🎮 Play Game' to enter the casino floor.</i>"
        ), reply_markup=get_main_menu(chat_id))
    else:
        num1, num2 = random.randint(1, 10), random.randint(1, 10)
        TEMP_CAPTCHAS[chat_id] = str(num1 + num2)
        update_user(chat_id, state='AWAITING_CAPTCHA')
        bot.send_message(chat_id, (
            "<b>🛡️ SECURITY PROTOCOL</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "To access Rizzle Games, please verify your session:\n\n"
            f"👉 <code>{num1} + {num2} = ?</code>\n\n"
            "<i>Reply directly with the correct numerical answer.</i>"
        ))

@bot.message_handler(func=lambda message: True)
def handle_text(message):
    chat_id = message.chat.id
    text = message.text
    user = get_user(chat_id)
    if not user: return

    menu_items = ["🎮 Play Game", "💳 Balance", "📊 History", "⚡ Deposit", "🏛️ Withdrawal", "👤 Profile", "🏦 UPI / Banks"]
    if text in menu_items:
        update_user(chat_id, state=None)
        user['state'] = None 

    # Handle Captcha Validation
    if user['state'] == 'AWAITING_CAPTCHA':
        if text.strip() == TEMP_CAPTCHAS.get(chat_id):
            update_user(chat_id, verified=1, bonus_balance=100, wager_remaining=(100 * WAGER_MULTIPLIER), state=None)
            bot.send_message(chat_id, (
                "<b>🟢 VERIFICATION COMPLETE</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "Authorization granted. Welcome to Rizzle Games.\n\n"
                "🎁 <b>Sign-Up Bonus:</b> <code>₹100</code>\n\n"
                "<i>Select '🎮 Play Game' to deploy the Web App.</i>"
            ), reply_markup=get_main_menu(chat_id))
        else:
            bot.reply_to(message, "🔴 <b>ERROR:</b> Verification failed. Try again.")
        return

    # Menu Commands
    if text == "💳 Balance":
        wager_str = f"<code>₹{user['wager_remaining']}</code>" if user['wager_remaining'] > 0 else "<code>WAGER COMPLETE</code>"
        bot.reply_to(message, (
            "<b>💳 PORTFOLIO BALANCE</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"💵 <b>Main Wallet:</b>  <code>₹{user['main_balance']}</code>\n"
            f"🎁 <b>Bonus Funds:</b> <code>₹{user['bonus_balance']}</code>\n\n"
            f"🔄 <b>Wager Target:</b> {wager_str}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "<i>Data synchronized securely.</i>"
        ))
        bot.send_message(chat_id, "<i>System refreshed.</i>", reply_markup=get_main_menu(chat_id))
    
    elif text == "📊 History":
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT id, type, amount, status, timestamp FROM transactions WHERE chat_id = %s ORDER BY id DESC LIMIT 5", (chat_id,))
        hist = c.fetchall()
        conn.close()
        
        hist_lines = []
        for h in hist:
            icon = "🟢" if h[1] == 'DEPOSIT' else "🔴"
            status_map = {'COMPLETED': 'SUCCESS', 'PENDING': 'PROCESSING', 'REJECTED': 'FAILED'}
            status_text = status_map.get(h[3], h[3])
            
            # Convert UTC server time to Indian Standard Time (IST)
            if h[4]:
                ist_time = h[4] + timedelta(hours=5, minutes=30)
                date_str = ist_time.strftime('%d-%b-%Y %I:%M %p') # e.g., 21-Jun-2026 10:41 AM
            else:
                date_str = "N/A"
            
            block = (
                f"{icon} <b>{h[1]}</b> | <code>#{h[0]}</code>\n"
                f"├ <b>Date:</b> <code>{date_str}</code>\n"
                f"├ <b>Amt:</b>  <code>₹{h[2]}</code>\n"
                f"└ <b>Stat:</b> <code>{status_text}</code>"
            )
            hist_lines.append(block)
            
        hist_text = "\n\n".join(hist_lines) if hist else "<i>No transactional data found.</i>"
        
        bot.reply_to(message, (
            "<b>📊 DETAILED LEDGER</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"{hist_text}\n"
            "━━━━━━━━━━━━━━━━━━"
        ))
        
    elif text == "👤 Profile":
        bank_status = "<code>BOUND ✅</code>" if user['bank_details'] else "<code>UNBOUND ❌</code>"
        upi_status = f"<code>{user['upi_id']}</code>" if user['upi_id'] else "<code>UNBOUND ❌</code>"
        
        total_dep = get_total_deposits(chat_id)
        
        bot.reply_to(message, (
            "<b>👤 ACCOUNT METRICS</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"🆔 <b>Rizzle ID:</b> <code>{chat_id}</code>\n"
            f"📈 <b>Total Deposits:</b> <code>₹{total_dep}</code>\n\n"
            f"<b>🏦 WITHDRAWAL BINDINGS</b>\n"
            f"🔸 <b>UPI:</b>  {upi_status}\n"
            f"🔸 <b>Bank:</b> {bank_status}\n"
            "━━━━━━━━━━━━━━━━━━"
        ))
        
    elif text == "⚡ Deposit":
        update_user(chat_id, state='AWAITING_DEPOSIT_AMT')
        bot.reply_to(message, (
            "<b>⚡ INITIATE DEPOSIT</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Input the desired deposit volume.\n\n"
            "⚠️ <i>Minimum Volume:</i> <code>₹100</code>"
        ))

    elif text == "🏛️ Withdrawal":
        # RIZZLE GAMES RULE: 200RS MINIMUM DEPOSIT TO UNLOCK
        total_dep = get_total_deposits(chat_id)
        if total_dep < 200:
            bot.reply_to(message, (
                "<b>🔒 WITHDRAWAL LOCKED</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "Rizzle Games requires a minimum total deposit of <code>₹200</code> to unlock network withdrawals.\n\n"
                f"📊 <b>Your Total Deposits:</b> <code>₹{total_dep}</code>\n"
                f"🎯 <b>Required:</b> <code>₹200</code>\n\n"
                "<i>Please use the '⚡ Deposit' menu to unlock your account.</i>"
            ))
            return

        if not user['bank_details'] and not user['upi_id']:
            bot.reply_to(message, (
                "🔴 <b>ROUTING ERROR</b>\n"
                "No payout destination found. Bind a UPI or Bank Account via the '🏦 UPI / Banks' menu."
            ))
            return
            
        update_user(chat_id, state='AWAITING_WITHDRAW_AMT')
        bot.reply_to(message, (
            "<b>🏛️ INITIATE WITHDRAWAL</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"💵 <b>Liquid Balance:</b> <code>₹{user['main_balance']}</code>\n\n"
            "Input the withdrawal volume.\n"
            "⚠️ <i>Minimum Volume:</i> <code>₹100</code>"
        ))

    elif text == "🏦 UPI / Banks":
        markup = InlineKeyboardMarkup()
        markup.row(InlineKeyboardButton("🔗 Bind UPI", callback_data="edit_upi"))
        markup.row(InlineKeyboardButton("🏦 Bind Bank", callback_data="edit_bank"))
        
        upi = f"<code>{user['upi_id']}</code>" if user['upi_id'] else "<code>UNBOUND ❌</code>"
        bank_info = "<code>UNBOUND ❌</code>"
        if user['bank_details']:
            bank_info = f"<code>{user['bank_details'].get('info', 'BOUND ✅')}</code>"

        bot.reply_to(message, (
            "<b>🏦 DESTINATION BINDING</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"🔸 <b>UPI:</b>  {upi}\n"
            f"🔸 <b>Bank:</b> {bank_info}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "<i>Select a parameter below to securely map your payout destination.</i>"
        ), reply_markup=markup)

    # --- INPUT STATES ---
    elif user['state'] == 'AWAITING_UPI':
        if "@" not in text:
            bot.reply_to(message, "🔴 <b>SYNTAX ERROR:</b> Missing '@' parameter. Re-enter your UPI:")
            return
        update_user(chat_id, upi_id=text.strip(), state=None)
        bot.reply_to(message, f"🟢 <b>UPI BOUND SUCCESSFULLY</b>\nTarget: <code>{text.strip()}</code>")

    elif user['state'] == 'AWAITING_BANK':
        update_user(chat_id, bank_details={"info": text.strip()}, state=None)
        bot.reply_to(message, f"🟢 <b>BANK BOUND SUCCESSFULLY</b>\nTarget: <code>{text.strip()}</code>")

    # --- 2-STEP DEPOSIT FLOW (Amount -> UTR) ---
    elif user['state'] == 'AWAITING_DEPOSIT_AMT':
        if not text.isdigit() or int(text) < 100:
            bot.reply_to(message, "🔴 <b>ERROR:</b> Volume must be <code>≥ ₹100</code>.")
            return
            
        amt = int(text)
        TEMP_DEPOSITS[chat_id] = amt # Save amount temporarily
        update_user(chat_id, state='AWAITING_UTR')
        
        bot.reply_to(message, (
            "<b>💸 PAYMENT REQUIRED</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"🔹 <b>Volume:</b> <code>₹{amt}</code>\n\n"
            "<b>1. Transfer the exact amount to this UPI ID:</b>\n"
            f"👉 <code>{ADMIN_UPI}</code>\n\n"
            "<b>2. Reply to this message with your 12-digit UTR / Ref No:</b>\n"
            "<i>(The Transaction ID from PhonePe/GPay/Paytm)</i>"
        ))

    elif user['state'] == 'AWAITING_UTR':
        utr = text.strip()
        amt = TEMP_DEPOSITS.get(chat_id)
        
        if not amt:
            # Fallback if bot restarted during payment
            update_user(chat_id, state=None)
            bot.reply_to(message, "🔴 <b>SESSION EXPIRED:</b> Please click '⚡ Deposit' and initiate the transaction again.")
            return
            
        update_user(chat_id, state=None)
        
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("INSERT INTO transactions (chat_id, type, amount, status, utr) VALUES (%s, 'DEPOSIT', %s, 'PENDING', %s)", (chat_id, amt, utr))
        conn.commit()
        conn.close()
        
        del TEMP_DEPOSITS[chat_id] # Clear temp storage
        
        bot.reply_to(message, (
            "<b>⏳ DEPOSIT PROCESSING</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"🔹 <b>Volume:</b> <code>₹{amt}</code>\n"
            f"🔹 <b>UTR:</b> <code>{utr}</code>\n\n"
            "<i>Your transaction is under review. Funds will automatically reflect post-verification.</i>"
        ))

    elif user['state'] == 'AWAITING_WITHDRAW_AMT':
        if not text.isdigit() or int(text) < 100:
            bot.reply_to(message, "🔴 <b>ERROR:</b> Volume must be <code>≥ ₹100</code>.")
            return
        amt = int(text)
        if amt > user['main_balance']:
            bot.reply_to(message, f"🔴 <b>ERROR:</b> Insufficient Liquidity (Available: <code>₹{user['main_balance']}</code>).")
            return
        
        update_user(chat_id, main_balance=user['main_balance'] - amt, state=None)
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("INSERT INTO transactions (chat_id, type, amount, status) VALUES (%s, 'WITHDRAWAL', %s, 'PENDING')", (chat_id, amt))
        conn.commit()
        conn.close()
        
        bot.reply_to(message, (
            "<b>⏳ WITHDRAWAL QUEUED</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"🔹 <b>Volume:</b> <code>₹{amt}</code>\n"
            "🔹 <b>State:</b> <code>PROCESSING</code>\n\n"
            "<i>Assets are being routed to your bound destination.</i>"
        ))

# ==========================================
# BOOT SEQUENCE
# ==========================================
def run_flask():
    print("✅ Local API Server is online!")
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    print("✅ Rizzle Games Server is online and polling...")
    bot.infinity_polling()
