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
Welcome to *Targetfaze* — your moonshot sniper bot!

• /setwallet - Set your Phantom private key (burner only)
• /setamount - Set SOL amount per trade
• /startsniping - Begin sniping new tokens
• /stop - Halt all activity
• /status - View your current config
"""

# Globals for app and queued notifications
telegram_app = None
telegram_ready = False
notification_queue = []  # List of tuples: (chat_id, text, parse_mode)


async def send_notification(chat_id: int, text: str, parse_mode="Markdown"):
    """Send a message to the given Telegram user, or queue it if bot not ready."""
    global telegram_app, telegram_ready, notification_queue

    if not telegram_ready or telegram_app is None:
        print(f"[Warning] Telegram not ready — queuing message: {text}")
        notification_queue.append((chat_id, text, parse_mode))
        return

    try:
        await telegram_app.bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode)
    except TelegramError as e:
        print(f"[Telegram Error] Failed to send message: {e}")


async def flush_notifications():
    """Send all queued messages once bot is ready."""
    global notification_queue
    if not notification_queue:
        return
    print(f"[Info] Flushing {len(notification_queue)} queued Telegram messages...")
    for chat_id, text, parse_mode in notification_queue:
        try:
            await telegram_app.bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode)
        except TelegramError as e:
            print(f"[Telegram Error] Failed to send queued message: {e}")
    notification_queue.clear()


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
    await update.message.reply_text("🚀 Now sniping brand new Solana tokens...")


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.chat_id
    await stop_sniping_for_user(uid)
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
    global telegram_app, telegram_ready

    telegram_app = ApplicationBuilder().token(CONFIG["telegram_token"]).build()

    async def on_startup(app):
        global telegram_ready
        telegram_ready = True
        print("[Info] Telegram bot initialized — flushing queued messages...")
        await flush_notifications()

    async def error_handler(update, context):
        print(f"Error while handling update: {context.error}")

    telegram_app.add_error_handler(error_handler)
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("setwallet", set_wallet))
    telegram_app.add_handler(CommandHandler("setamount", set_amount))
    telegram_app.add_handler(CommandHandler("startsniping", start_sniping))
    telegram_app.add_handler(CommandHandler("stop", stop))
    telegram_app.add_handler(CommandHandler("status", status))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # ✅ Correct way to register startup hook
    telegram_app.post_init = on_startup

    telegram_app.run_polling()