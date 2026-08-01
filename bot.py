import telebot
from groq import Groq
import os

TOKEN = os.getenv('TELEGRAM_TOKEN')
GROQ_KEY = os.getenv('GROQ_KEY')

client = Groq(api_key=GROQ_KEY)
bot = telebot.TeleBot(TOKEN) 

FAST_REPLIES = {
    'start': "Welcome to EditableTemplateNG 👋\n\nGet 3 EDITABLE Canva Templates for ₦2,000\nCV + Food Flyer + Sales Flyer\nCommands: /PRICE /SAMPLE /BUY /HELP",
    'price': "PRICE: ₦2,000 for 3 templates ✅\n\nYou get:\n1. Editable Canva Links\n2. 2min Tutorial Video\n\nType /BUY for payment details",
    'sample': "Here are your 3 templates 👇\n\n1. CV Template\n2. Food Flyer\n3. Sales Flyer\nAll 100% editable on Canva App.\n\nWant them? Type /BUY",
    'buy': "Send ₦2,000 to:\nBank: [Opay]\nAcc No: [8100481004]\nAcc Name: [Daniel folorunso or coldfx34]\n\nSend payment proof here and I’ll send your links instantly ✅",
    'help': "FAQ:\nQ: Can I edit on phone? A: Yes, use Canva App\nQ: Delivery? A: Instant after payment\n\nJust ask me anything and I’ll reply"
}

@bot.message_handler(commands=['start', 'price', 'sample', 'buy', 'help'])
def fast_commands(message):
    cmd = message.text.replace('/', '').lower()
    bot.reply_to(message, FAST_REPLIES.get(cmd))

@bot.message_handler(func=lambda message: True)
def ai_reply(message):
    try:
        chat_completion = client.chat.completions.create(
            messages=[
                {"role": "system", "content": "You are a friendly sales rep for EditableTemplateNG. We sell 3 editable Canva templates for ₦2,000. Reply in 1-2 short sentences. Use Nigerian English. Be helpful. Always try to push them to /BUY."},
                {"role": "user", "content": message.text}
            ],
            model="llama-3.1-8b-instant",
        )
        bot.reply_to(message, chat_completion.choices[0].message.content)
    except:
        bot.reply_to(message, "Sorry, I’m having network issues. Please type /HELP or /BUY")

bot.polling(none_stop=True)
