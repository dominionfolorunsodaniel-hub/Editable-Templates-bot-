import os
import logging
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

TELEGRAM_TOKEN = os.getenv("8662696847:AAE7M964TpSsq0U6dUcNtDSica6u75PGecI")
PAYSTACK_SECRET_KEY = os.getenv("sk_test_c36d724d380c57883ffd7391ae1aeb1ba6c4140e")
PAYSTACK_PUBLIC_KEY = os.getenv("pk_test_7e70a878505d7b2eaef506298887b3d9c7e2c34e")
os.getenv("sk_test_c36d724d380c57883ffd7391ae1aeb1ba6c4140e")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
ADMIN_USERNAME = "t.me/coldfx34"

PAID_USERS = set()
COMPLAINTS = []

logging.basicConfig(level=logging.INFO)

def create_paystack_payment(email, amount_kobo, user_id):
    url = "https://api.paystack.co/transaction/initialize"
    headers = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}","Content-Type": "application/json"}
    data = {"email": email,"amount": amount_kobo,"callback_url": f"https://t.me/{8662696847:AAE7M964TpSsq0U6dUcNtDSica6u75PGecI.split(':')[0]}","metadata": {"user_id": user_id}}
    r = requests.post(url, headers=headers, json=data)
    if r.status_code == 200:
        return r.json()["data"]["authorization_url"]
    else:
        logging.error(f"Paystack Error: {r.text}")
        return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (f"Welcome to EditableTemplateNG 😍\n\nGet 3 EDITABLE TEMPLATES\n💰 Price: ₦2,000\nFollow us on IG: @editabletemplateng\nhttps://instagram.com/editabletemplateng?igsh=MXdjMTI5NHUwZGh4bg==\n\nCommands:\n/PRICE /SAMPLE /BUY")
    await update.message.reply_text(msg)

async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("💰 PRICE: ₦2,000 only\n\nYou get: 3 Editable Canva Templates\nType /BUY to get started")

async def sample(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # NEW: Instead of sending photos, tell them to text admin
    keyboard = [[InlineKeyboardButton("📩 Text @{ADMIN_USERNAME} for Samples", url=f"https://t.me/{ADMIN_USERNAME}")]]
    await update.message.reply_text(
        "🔥 Want to see samples?\n\nClick the button below to chat with Admin directly on Telegram.\nI'll send you sample pictures + answer any questions 😊",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    email = f"{user.id}@telegram.user"
    amount = 200000
    if user.id in PAID_USERS:
        await send_templates(update, user.id)
        return
    payment_url = create_paystack_payment(email, amount, user.id)
    if payment_url:
        keyboard = [[InlineKeyboardButton("Pay ₦2,000 Now", url=payment_url)]]
        await update.message.reply_text("Click below to pay. After payment type /VERIFY", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text("Error creating payment. Contact admin.")

async def verify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    PAID_USERS.add(update.effective_user.id)
    await update.message.reply_text("Payment Verified! ✅")
    await send_templates(update, update.effective_user.id)

async def send_templates(update: Update, user_id):
    links_text = "\n".join([f"{i+1}. {link}" for i, link in enumerate(TEMPLATE_LINKS) if link])
    await update.message.reply_text(f"Thank you! 🎉\n\nYour 3 Template Links:\n\n{links_text}\n\nTag us @{ADMIN_USERNAME}")

async def complain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    complaint_text = " ".join(context.args)
    if not complaint_text:
        await update.message.reply_text("Usage: /COMPLAIN your issue")
        return
    user = update.effective_user
    COMPLAINTS.append({"user": user.full_name, "id": user.id, "text": complaint_text})
    if ADMIN_ID!= 0:
        await context.bot.send_message(chat_id=ADMIN_ID, text=f"🚨 NEW COMPLAINT 🚨\nFrom: {user.full_name}\nID: {user.id}\nIssue: {complaint_text}")
    await update.message.reply_text("Complaint received. Admin will reach out 🙏")

async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Not authorized")
        return
    
    paid_count = len(PAID_USERS)
    complaint_count = len(COMPLAINTS)
    
    await update.message.reply_text(
        f"📊 ADMIN DASHBOARD 📊\n\n"
        f"Paid Users: {paid_count}\n"
        f"Complaints: {complaint_count}\n\n"
        f"Tag: @{ADMIN_USERNAME}"
    )

def main():
    TOKEN = os.getenv("TELEGRAM_TOKEN")
    print("TOKEN loaded successfully.")

    app = ApplicationBuilder().token(TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("price", price))
    app.add_handler(CommandHandler("sample", sample))
    app.add_handler(CommandHandler("buy", buy))
    app.add_handler(CommandHandler("verify", verify))
    app.add_handler(CommandHandler("complain", complain))
    app.add_handler(CommandHandler("admin", admin))
    
    print("Bot is running...")
app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
