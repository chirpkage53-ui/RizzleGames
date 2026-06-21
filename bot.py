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
        amount INTEGER, status TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    
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

init_db()
print("✅ PostgreSQL Database locked and loaded!")

WAGER_MULTIPLIER = 10
TEMP_CAPTCHAS = {}

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
    markup.row(KeyboardButton("💰 Balance"), KeyboardButton("📜 History"))
    markup.row(KeyboardButton("📥 Deposit"), KeyboardButton("📤 Withdrawal"))
    markup.row(KeyboardButton("👤 Profile"), KeyboardButton("🏦 UPI / Banks"))
    return markup

# ==========================================
# SUPER ADMIN PANEL LOGIC
# ==========================================
@bot.message_handler(commands=['admin'])
def admin_panel(message):
    if message.chat.id != ADMIN_ID: return
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("📥 Pending Deposits", callback_data="admin_deposits"))
    markup.row(InlineKeyboardButton("📤 Pending Withdrawals", callback_data="admin_withdrawals"))
    markup.row(InlineKeyboardButton("👥 View All Users", callback_data="admin_users"))
    markup.row(InlineKeyboardButton("💰 Edit User Balance", callback_data="admin_edit_bal"))
    bot.reply_to(message, "👑 <b>SUPER ADMIN PANEL</b>", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_"))
def admin_actions(call):
    conn = get_db_connection()
    c = conn.cursor()
    
    if call.data == "admin_users":
        c.execute("SELECT chat_id, main_balance, bonus_balance FROM users")
        users = c.fetchall()
        msg = "📈 <b>User Balances:</b>\n" + "\n".join([f"ID: <code>{u[0]}</code> | Main: ₹{u[1]} | Bonus: ₹{u[2]}" for u in users])
        bot.send_message(call.message.chat.id, msg[:4000])
    
    elif call.data == "admin_deposits":
        c.execute("SELECT id, chat_id, amount FROM transactions WHERE type = 'DEPOSIT' AND status = 'PENDING'")
        pending = c.fetchall()
        if not pending:
            bot.send_message(call.message.chat.id, "✅ No pending deposits.")
        else:
            for p in pending:
                markup = InlineKeyboardMarkup()
                markup.row(
                    InlineKeyboardButton("✅ Approve", callback_data=f"tx_app_dep_{p[0]}_{p[1]}_{p[2]}"),
                    InlineKeyboardButton("❌ Reject", callback_data=f"tx_rej_dep_{p[0]}_{p[1]}_{p[2]}")
                )
                bot.send_message(call.message.chat.id, f"📥 <b>Deposit Request:</b>\nUser: <code>{p[1]}</code>\nAmount: <b>₹{p[2]}</b>", reply_markup=markup)

    elif call.data == "admin_withdrawals":
        c.execute("SELECT id, chat_id, amount FROM transactions WHERE type = 'WITHDRAWAL' AND status = 'PENDING'")
        pending = c.fetchall()
        if not pending:
            bot.send_message(call.message.chat.id, "✅ No pending withdrawals.")
        else:
            for p in pending:
                markup = InlineKeyboardMarkup()
                markup.row(
                    InlineKeyboardButton("✅ Approve", callback_data=f"tx_app_wit_{p[0]}_{p[1]}_{p[2]}"),
                    InlineKeyboardButton("❌ Reject", callback_data=f"tx_rej_wit_{p[0]}_{p[1]}_{p[2]}")
                )
                bot.send_message(call.message.chat.id, f"📤 <b>Withdrawal Request:</b>\nUser: <code>{p[1]}</code>\nAmount: <b>₹{p[2]}</b>", reply_markup=markup)

    elif call.data == "admin_edit_bal":
        msg = bot.send_message(call.message.chat.id, "✏️ <b>Enter the User's Chat ID to edit:</b>")
        bot.register_next_step_handler(msg, admin_process_edit_id)
        
    conn.close()

def admin_process_edit_id(message):
    if message.chat.id != ADMIN_ID: return
    try:
        target_id = int(message.text.strip())
        user = get_user(target_id)
        if not user:
            bot.reply_to(message, "❌ User not found in database.")
            return
        msg = bot.reply_to(message, f"👤 User: <code>{target_id}</code>\n💰 Current Main Balance: <b>₹{user['main_balance']}</b>\n\n✏️ Enter the <b>NEW Main Balance</b>:")
        bot.register_next_step_handler(msg, admin_process_new_balance, target_id)
    except ValueError:
        bot.reply_to(message, "❌ Invalid ID format. Please click 'Edit User Balance' again.")

def admin_process_new_balance(message, target_id):
    if message.chat.id != ADMIN_ID: return
    try:
        new_bal = int(message.text.strip())
        update_user(target_id, main_balance=new_bal)
        bot.reply_to(message, f"✅ Successfully updated user <code>{target_id}</code> balance to <b>₹{new_bal}</b>.")
        bot.send_message(target_id, f"🔔 <b>Admin Update:</b> Your main balance has been adjusted to <b>₹{new_bal}</b>.")
    except ValueError:
        bot.reply_to(message, "❌ Invalid amount. Action cancelled.")

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
            
            # Premium Deposit Success Message
            bot.send_message(user_id, (
                "<b>✅ DEPOSIT SUCCESSFUL</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"💳 <b>Amount Added:</b> <code>₹{amount}</code>\n\n"
                "<i>Your Main Wallet has been topped up. Good luck at the tables! 🎲</i>"
            ))
            
        elif action == "rej":
            c.execute("UPDATE transactions SET status = 'REJECTED' WHERE id = %s", (tx_id,))
            bot.edit_message_text(f"❌ Rejected Deposit of ₹{amount} for <code>{user_id}</code>", call.message.chat.id, call.message.message_id)
            bot.send_message(user_id, f"❌ <b>Deposit of <code>₹{amount}</code> was declined.</b>\nPlease contact support if you believe this is an error.")

    elif tx_type == "wit":
        if action == "app":
            c.execute("UPDATE transactions SET status = 'COMPLETED' WHERE id = %s", (tx_id,))
            bot.edit_message_text(f"✅ Approved Withdrawal of ₹{amount} for <code>{user_id}</code>", call.message.chat.id, call.message.message_id)
            
            # Premium Withdrawal Success Message
            bot.send_message(user_id, (
                "<b>💸 WITHDRAWAL PROCESSED</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"💳 <b>Amount Sent:</b> <code>₹{amount}</code>\n\n"
                "<i>Your winnings have been transferred to your linked payout method! 🎉</i>"
            ))
            
        elif action == "rej":
            update_user(user_id, main_balance=user['main_balance'] + amount)
            c.execute("UPDATE transactions SET status = 'REJECTED' WHERE id = %s", (tx_id,))
            bot.edit_message_text(f"❌ Rejected Withdrawal of ₹{amount} for <code>{user_id}</code>. Funds auto-refunded.", call.message.chat.id, call.message.message_id)
            bot.send_message(user_id, f"❌ <b>Withdrawal of <code>₹{amount}</code> was rejected.</b>\nThe funds have been safely returned to your Main Balance.")

    conn.commit()
    conn.close()

@bot.callback_query_handler(func=lambda call: call.data in ["edit_upi", "edit_bank"])
def edit_payment_methods(call):
    chat_id = call.message.chat.id
    if call.data == "edit_upi":
        update_user(chat_id, state='AWAITING_UPI')
        bot.edit_message_text(
            "<b>🔗 LINK YOUR UPI ID</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Please reply to this message with your exact UPI ID.\n\n"
            "<i>Example: <code>username@okicici</code></i>", 
            chat_id, call.message.message_id
        )
    elif call.data == "edit_bank":
        update_user(chat_id, state='AWAITING_BANK')
        bot.edit_message_text(
            "<b>🏦 LINK BANK ACCOUNT</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Please reply with your full bank details in a single message:\n\n"
            "<i>Format: Account Number, IFSC Code, Account Holder Name</i>\n"
            "<i>Example: <code>123456789, SBIN000123, John Doe</code></i>", 
            chat_id, call.message.message_id
        )

# ==========================================
# MAIN BOT LOGIC & PREMIUM UI TEXTS
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
            "<b>🎰 PREMIUM CASINO LOBBY</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Welcome back, VIP! Your table is ready.\n\n"
            "<i>Tap '🎮 Play Game' below to launch the Web App.</i>"
        ), reply_markup=get_main_menu(chat_id))
    else:
        num1, num2 = random.randint(1, 10), random.randint(1, 10)
        TEMP_CAPTCHAS[chat_id] = str(num1 + num2)
        update_user(chat_id, state='AWAITING_CAPTCHA')
        bot.send_message(chat_id, (
            "<b>🛡️ SECURITY VERIFICATION</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "To access the casino, please solve this quick math problem to prove you are human:\n\n"
            f"👉 <b>{num1} + {num2} = ?</b>\n\n"
            "<i>Reply to this message with the correct number.</i>"
        ))

@bot.message_handler(func=lambda message: True)
def handle_text(message):
    chat_id = message.chat.id
    text = message.text
    user = get_user(chat_id)
    if not user: return

    menu_items = ["🎮 Play Game", "💰 Balance", "📜 History", "📥 Deposit", "📤 Withdrawal", "👤 Profile", "🏦 UPI / Banks"]
    if text in menu_items:
        update_user(chat_id, state=None)
        user['state'] = None 

    # Handle Captcha Validation
    if user['state'] == 'AWAITING_CAPTCHA':
        if text.strip() == TEMP_CAPTCHAS.get(chat_id):
            update_user(chat_id, verified=1, bonus_balance=100, wager_remaining=(100 * WAGER_MULTIPLIER), state=None)
            bot.send_message(chat_id, (
                "<b>✅ VERIFICATION SUCCESSFUL</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "Welcome to the Premium Casino! 🎰\n\n"
                "🎁 <b>₹100 Bonus Cash</b> has been credited to your account.\n\n"
                "<i>Tap '🎮 Play Game' to enter the lobby!</i>"
            ), reply_markup=get_main_menu(chat_id))
        else:
            bot.reply_to(message, "❌ <b>Incorrect!</b> Please try again.")
        return

    # Menu Commands
    if text == "💰 Balance":
        wager_str = f"<code>₹{user['wager_remaining']}</code>" if user['wager_remaining'] > 0 else "<b>Complete ✅</b>"
        bot.reply_to(message, (
            "<b>💎 WALLET BALANCE</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"💳 <b>Main Cash:</b>  <code>₹{user['main_balance']}</code>\n"
            f"🎁 <b>Bonus Cash:</b> <code>₹{user['bonus_balance']}</code>\n\n"
            f"🔄 <b>Wager Left:</b> {wager_str}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "<i>Data is synced live with the secure server.</i>"
        ))
        bot.send_message(chat_id, "<i>Menu refreshed.</i>", reply_markup=get_main_menu(chat_id))
    
    elif text == "📜 History":
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT type, amount, status FROM transactions WHERE chat_id = %s ORDER BY id DESC LIMIT 6", (chat_id,))
        hist = c.fetchall()
        conn.close()
        
        hist_lines = []
        for h in hist:
            icon = "🟢" if h[0] == 'DEPOSIT' else "🔴"
            status = "✅" if h[2] == 'COMPLETED' else ("⏳" if h[2] == 'PENDING' else "❌")
            hist_lines.append(f"{icon} <b>{h[0]}</b>: <code>₹{h[1]}</code> {status}")
            
        hist_text = "\n\n".join(hist_lines) if hist else "<i>No recent transactions.</i>"
        
        bot.reply_to(message, (
            "<b>📜 TRANSACTION HISTORY</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"{hist_text}\n"
            "━━━━━━━━━━━━━━━━━━"
        ))
        
    elif text == "👤 Profile":
        bank_status = "Linked ✅" if user['bank_details'] else "Not Linked ❌"
        upi_status = f"<code>{user['upi_id']}</code>" if user['upi_id'] else "Not Linked ❌"
        bot.reply_to(message, (
            "<b>👤 PLAYER PROFILE</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"🆔 <b>Account ID:</b> <code>{chat_id}</code>\n"
            f"🛡️ <b>Status:</b> Verified VIP ✅\n\n"
            f"<b>💳 PAYOUT METHODS</b>\n"
            f"🔸 <b>UPI:</b> {upi_status}\n"
            f"🔸 <b>Bank:</b> {bank_status}\n"
            "━━━━━━━━━━━━━━━━━━"
        ))
        
    elif text == "📥 Deposit":
        update_user(chat_id, state='AWAITING_DEPOSIT_AMT')
        bot.reply_to(message, (
            "<b>📥 INITIATE DEPOSIT</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Reply to this message with the amount you wish to deposit.\n\n"
            "⚠️ <i>Minimum Deposit: <code>₹100</code></i>"
        ))

    elif text == "📤 Withdrawal":
        if not user['bank_details'] and not user['upi_id']:
            bot.reply_to(message, (
                "❌ <b>No Payout Method Found!</b>\n"
                "Please securely link a Bank or UPI first from the '🏦 UPI / Banks' menu."
            ))
            return
        update_user(chat_id, state='AWAITING_WITHDRAW_AMT')
        bot.reply_to(message, (
            "<b>📤 INITIATE WITHDRAWAL</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"💵 <b>Available to Withdraw:</b> <code>₹{user['main_balance']}</code>\n\n"
            "Reply to this message with the amount you wish to withdraw.\n"
            "⚠️ <i>Minimum Withdrawal: <code>₹100</code></i>"
        ))

    elif text == "🏦 UPI / Banks":
        markup = InlineKeyboardMarkup()
        markup.row(InlineKeyboardButton("🔗 Link/Edit UPI", callback_data="edit_upi"))
        markup.row(InlineKeyboardButton("🏦 Link/Edit Bank", callback_data="edit_bank"))
        
        upi = f"<code>{user['upi_id']}</code>" if user['upi_id'] else "<i>Not Linked ❌</i>"
        bank_info = "<i>Not Linked ❌</i>"
        if user['bank_details']:
            bank_info = f"<code>{user['bank_details'].get('info', 'Linked ✅')}</code>"

        bot.reply_to(message, (
            "<b>🏦 WITHDRAWAL METHODS</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"🔸 <b>UPI ID:</b> {upi}\n"
            f"🔸 <b>Bank:</b> {bank_info}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "<i>Select an option below to securely update your payout methods.</i>"
        ), reply_markup=markup)

    # --- INPUT STATES ---
    elif user['state'] == 'AWAITING_UPI':
        if "@" not in text:
            bot.reply_to(message, "❌ <b>Invalid Format!</b>\nA valid UPI must contain an '@' symbol. Please try again:")
            return
        update_user(chat_id, upi_id=text.strip(), state=None)
        bot.reply_to(message, f"✅ <b>UPI Successfully Linked!</b>\nAll future withdrawals will be routed to: <code>{text.strip()}</code>")

    elif user['state'] == 'AWAITING_BANK':
        update_user(chat_id, bank_details={"info": text.strip()}, state=None)
        bot.reply_to(message, f"✅ <b>Bank Details Secured!</b>\nInformation saved: <code>{text.strip()}</code>")

    elif user['state'] == 'AWAITING_DEPOSIT_AMT':
        if not text.isdigit() or int(text) < 100:
            bot.reply_to(message, "❌ <b>Error:</b> Minimum deposit is <code>₹100</code>.")
            return
        amt = int(text)
        update_user(chat_id, state=None)
        
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("INSERT INTO transactions (chat_id, type, amount, status) VALUES (%s, 'DEPOSIT', %s, 'PENDING')", (chat_id, amt))
        conn.commit()
        conn.close()
        
        bot.reply_to(message, (
            "<b>⏳ DEPOSIT PENDING</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"🔹 <b>Amount:</b> <code>₹{amt}</code>\n\n"
            "<b>Please send the exact amount to:</b>\n"
            "👉 <code>your-upi@bank</code>\n\n"
            "<i>Your balance will be updated instantly once approved by our team.</i>"
        ))

    elif user['state'] == 'AWAITING_WITHDRAW_AMT':
        if not text.isdigit() or int(text) < 100:
            bot.reply_to(message, "❌ <b>Error:</b> Minimum withdrawal is <code>₹100</code>.")
            return
        amt = int(text)
        if amt > user['main_balance']:
            bot.reply_to(message, f"❌ <b>Error:</b> Insufficient Main Balance (Available: <code>₹{user['main_balance']}</code>).")
            return
        
        update_user(chat_id, main_balance=user['main_balance'] - amt, state=None)
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("INSERT INTO transactions (chat_id, type, amount, status) VALUES (%s, 'WITHDRAWAL', %s, 'PENDING')", (chat_id, amt))
        conn.commit()
        conn.close()
        
        bot.reply_to(message, (
            "<b>⏳ WITHDRAWAL REQUESTED</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"🔹 <b>Amount:</b> <code>₹{amt}</code>\n"
            "🔹 <b>Status:</b> Processing ⏳\n\n"
            "<i>Your request has been forwarded to the payout team and will be credited to your linked method soon.</i>"
        ))

# ==========================================
# BOOT SEQUENCE
# ==========================================
def run_flask():
    print("✅ Local API Server is online!")
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    print("✅ Premium Bot is online and polling...")
    bot.infinity_polling()
