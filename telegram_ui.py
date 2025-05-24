# telegram_ui.py
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters
)
from telegram.error import TelegramError
from sniper_runner import start_sniping_for_user, stop_sniping_for_user
from session_manager import UserSession
from config import CONFIG
import base58

custom_keyboard = [["/setwallet", "/setamount"], ["/startsniping", "/stop"], ["/status"]]
reply_markup = ReplyKeyboardMarkup(custom_keyboard, resize_keyboard=True)

user_sessions = {}
WELCOME_TEXT = """
Welcome to *Targetfaze* — your Pump.fun sniper bot!

• /setwallet - Set your Phantom private key (burner only)
• /setamount - Set SOL amount per trade
• /startsniping - Begin sniping new Pump.fun tokens
• /stop - Halt all activity
• /status - View your current config
"""

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME_TEXT, reply_markup=reply_markup, parse_mode="Markdown")

async def set_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Send your **burner private key** (Phantom) now.", parse_mode="Markdown")

def is_valid_base58_key(msg):
    try:
        decoded = base58.b58decode(msg)
        return len(decoded) in [32, 64]
    except Exception:
        return False

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.chat_id
    msg = update.message.text.strip()
    if uid not in user_sessions:
        user_sessions[uid] = UserSession()

    if len(msg.split()) in [12, 24]:
        user_sessions[uid].private_key = msg
        await update.message.reply_text("✅ Mnemonic saved.")
    elif is_valid_base58_key(msg):
        user_sessions[uid].private_key = msg
        await update.message.reply_text("✅ Base58 private key saved.")
    else:
        try:
            amount = float(msg)
            user_sessions[uid].sol_amount = amount
            await update.message.reply_text(f"✅ Amount set: {amount} SOL per trade.")
        except:
            await update.message.reply_text("⚠️ Invalid input.")

async def set_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Enter SOL amount per trade (e.g., 0.01):")

async def start_sniping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.chat_id
    session = user_sessions[uid]
    if not session.private_key:
        await update.message.reply_text("⚠️ Please set your wallet first using /setwallet.")
        return
    await start_sniping_for_user(uid, session)
    await update.message.reply_text("🚀 Now sniping brand new Pump.fun tokens...")

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.chat_id
    stop_sniping_for_user(uid)
    if uid in user_sessions:
        user_sessions[uid].sniping = False
    await update.message.reply_text("🛑 Sniping stopped.")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = user_sessions.get(update.message.chat_id)
    if session:
        wallet = session.masked_wallet()
        await update.message.reply_text(
            f"🔐 Wallet: {wallet}\n💰 Amount: {session.sol_amount} SOL\n📡 Status: {'Sniping' if session.sniping else 'Idle'}"
        )
    else:
        await update.message.reply_text("No session info found. Use /setwallet and /setamount to configure.")

def start_bot():
    app = ApplicationBuilder().token(CONFIG["telegram_token"]).build()

    async def error_handler(update, context):
        print(f"Error while handling update: {context.error}")

    app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setwallet", set_wallet))
    app.add_handler(CommandHandler("setamount", set_amount))
    app.add_handler(CommandHandler("startsniping", start_sniping))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.run_polling()
