
import asyncio
import json
import logging
import os
import random
import tempfile
import threading
import urllib.parse
import requests
import time
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, HTTPServer

from datetime import time as dt_time

from huggingface_hub import InferenceClient
from openai import OpenAI
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup

try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

# ============================================================
# CONFIG
# ============================================================

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_ADMIN_ID = os.environ.get("TELEGRAM_ADMIN_ID")
HF_TOKEN = os.environ.get("HF_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

BANK_NAME = os.environ.get("BANK_NAME", "")
BANK_ACCOUNT = os.environ.get("BANK_ACCOUNT", "")
BANK_OWNER = os.environ.get("BANK_OWNER", "")

BOT_NAME = "Editable Template Bot"

FREE_IMAGE_LIMIT = 5
PREMIUM_PRICE = 1500
REFERRALS_REQUIRED = 2
REFERRAL_POINTS = 500
PREMIUM_POINTS_REQUIRED = 10000

# Hugging Face text-to-image model.
# If this model/provider becomes unavailable, change it here.
HF_IMAGE_MODEL = "black-forest-labs/FLUX.1-schnell"

# Optional: set this to a channel ID (e.g. -100123456789) to enable the
# public gallery feature. Bot must be an admin of that channel.
GALLERY_CHANNEL_ID = os.environ.get("GALLERY_CHANNEL_ID")

GROUP_PREMIUM_PRICE = 5000

TOPUP_PACKAGES = {
    "topup5": {"images": 5, "price": 500, "label": "5 images — ₦500"},
    "topup15": {"images": 15, "price": 1200, "label": "15 images — ₦1,200"},
}

STYLE_PRESETS = {
    "wedding": "elegant wedding invitation design, floral accents, soft colors",
    "business": "professional business flyer, clean corporate look",
    "meme": "funny meme style image, bold text overlay, internet humor",
    "event": "vibrant event poster, bold typography, eye-catching colors",
    "product": "premium product advertisement, studio lighting, minimal background",
}

if not TELEGRAM_TOKEN or not TELEGRAM_ADMIN_ID or not HF_TOKEN or not GROQ_API_KEY:
    missing = []
    if not TELEGRAM_TOKEN:
        missing.append("TELEGRAM_TOKEN")
    if not TELEGRAM_ADMIN_ID:
        missing.append("TELEGRAM_ADMIN_ID")
    if not HF_TOKEN:
        missing.append("HF_TOKEN")
    if not GROQ_API_KEY:
        missing.append("GROQ_API_KEY")
    raise RuntimeError("Missing environment variable(s): " + ", ".join(missing))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

hf_client = InferenceClient(api_key=HF_TOKEN)

groq_client = OpenAI(
    api_key=GROQ_API_KEY,
    base_url="https://api.groq.com/openai/v1",
)

# Keeps a short rolling conversation per user so replies have context.
# Capped so memory/token usage doesn't grow forever.
CHAT_HISTORY_LIMIT = 10
chat_history = defaultdict(list)

# ============================================================
# REDIS STORAGE (Upstash) — persists across restarts/redeploys
# ============================================================

UPSTASH_REDIS_REST_URL = os.environ.get("UPSTASH_REDIS_REST_URL")
UPSTASH_REDIS_REST_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN")

REDIS_DATA_KEY = "bot_data"
data_lock = asyncio.Lock()

DEFAULT_DATA = {
    "users": {},
    "referrals": {},
    "groups": {},
}


def load_data():
    if not (UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN):
        logger.warning(
            "UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN not set — "
            "data will NOT be saved between restarts."
        )
        return DEFAULT_DATA.copy()

    try:
        response = requests.get(
            f"{UPSTASH_REDIS_REST_URL}/get/{REDIS_DATA_KEY}",
            headers={"Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}"},
            timeout=10,
        )
        response.raise_for_status()
        result = response.json().get("result")

        if result:
            value = json.loads(result)
            if isinstance(value, dict):
                value.setdefault("users", {})
                value.setdefault("referrals", {})
                value.setdefault("groups", {})
                return value
    except Exception as error:
        logger.exception("Could not load data from Upstash: %s", error)

    return DEFAULT_DATA.copy()


data = load_data()


def save_data_sync():
    if not (UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN):
        return

    try:
        payload = json.dumps(data)
        response = requests.post(
            f"{UPSTASH_REDIS_REST_URL}/set/{REDIS_DATA_KEY}",
            headers={"Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}"},
            data=payload.encode("utf-8"),
            timeout=10,
        )
        response.raise_for_status()
    except Exception as error:
        logger.exception("Could not save data to Upstash: %s", error)


def ensure_user(user):
    uid = str(user.id)

    if uid not in data["users"]:
        data["users"][uid] = {
            "first_name": user.first_name or "User",
            "username": user.username or "",
            "free_images_used": 0,
            "premium": False,
            "points": 0,
            "referrals": 0,
            "referrals_month": 0,
            "referred_by": None,
            "daily_claim": "",
            "daily_streak": 0,
            "quiz_claimed": [],
            "bonus_images": 0,
            "image_history": [],
            "last_prompt": None,
            "gallery_opt_in": False,
            "share_claimed": "",
        }

    profile = data["users"][uid]
    profile.setdefault("first_name", user.first_name or "User")
    profile.setdefault("username", user.username or "")
    profile.setdefault("free_images_used", 0)
    profile.setdefault("premium", False)
    profile.setdefault("points", 0)
    profile.setdefault("referrals", 0)
    profile.setdefault("referrals_month", 0)
    profile.setdefault("referred_by", None)
    profile.setdefault("daily_claim", "")
    profile.setdefault("daily_streak", 0)
    profile.setdefault("quiz_claimed", [])
    profile.setdefault("bonus_images", 0)
    profile.setdefault("image_history", [])
    profile.setdefault("last_prompt", None)
    profile.setdefault("gallery_opt_in", False)
    profile.setdefault("share_claimed", "")

    return profile


def ensure_group(chat):
    cid = str(chat.id)

    if cid not in data["groups"]:
        data["groups"][cid] = {
            "title": chat.title or "Group",
            "premium": False,
        }

    group = data["groups"][cid]
    group.setdefault("title", chat.title or "Group")
    group.setdefault("premium", False)

    return group


async def save():
    async with data_lock:
        await asyncio.to_thread(save_data_sync)


# ============================================================
# CUSTOMER / REFERRAL SYSTEM
# ============================================================

def referral_link(user_id, bot_username):
    return f"https://t.me/{bot_username}?start=ref_{user_id}"


async def register_user(user, context, referral_code=None):
    is_new = str(user.id) not in data["users"]
    profile = ensure_user(user)

    if is_new and referral_code:
        referrer_id = str(referral_code)

        if referrer_id != str(user.id) and referrer_id in data["users"]:
            # Only one referrer can ever be attached to an account.
            if not profile["referred_by"]:
                profile["referred_by"] = referrer_id
                data["referrals"][str(user.id)] = referrer_id

                referrer = ensure_user(
                    await context.bot.get_chat(int(referrer_id))
                )
                referrer["referrals"] += 1
                referrer["referrals_month"] = referrer.get("referrals_month", 0) + 1
                referrer["points"] += REFERRAL_POINTS

                await context.bot.send_message(
                    chat_id=int(referrer_id),
                    text=(
                        "🎉 NEW REFERRAL!\n\n"
                        f"{user.first_name or 'Someone'} joined using your link.\n"
                        f"+{REFERRAL_POINTS} points!\n\n"
                        f"Your referrals: {referrer['referrals']}\n"
                        f"Your points: {referrer['points']:,}\n\n"
                        f"Need {REFERRALS_REQUIRED} referrals and "
                        f"{PREMIUM_POINTS_REQUIRED:,} points to unlock "
                        "Premium for free."
                    ),
                )
                await save()

    return is_new, profile


# ============================================================
# ACCESS / POINTS
# ============================================================

def is_admin(update):
    user = update.effective_user
    return bool(user and str(user.id) == str(TELEGRAM_ADMIN_ID))


def premium_eligible(profile):
    return (
        profile["referrals"] >= REFERRALS_REQUIRED
        and profile["points"] >= PREMIUM_POINTS_REQUIRED
    )


def access_text(profile):
    if profile["premium"]:
        return "💎 Premium: ACTIVE"

    remaining = max(0, FREE_IMAGE_LIMIT - profile["free_images_used"])
    bonus = profile.get("bonus_images", 0)
    extra = f"\n🎟️ Bonus images: {bonus}" if bonus else ""
    return (
        f"🆓 Free images left: {remaining}\n"
        f"💎 Premium: LOCKED"
        f"{extra}"
    )


# ============================================================
# ADMIN
# ============================================================

async def notify_admin(context, text):
    try:
        await context.bot.send_message(
            chat_id=TELEGRAM_ADMIN_ID,
            text=text,
        )
    except Exception as error:
        logger.warning("Admin notification failed: %s", error)


# ============================================================
# RATE LIMIT
# ============================================================

RATE_LIMIT_SECONDS = 15
last_call = defaultdict(float)


async def enforce_rate_limit(update, key):
    user = update.effective_user
    message = update.effective_message

    if not user:
        return False

    if is_admin(update):
        return True

    now = time.monotonic()
    previous = last_call[(user.id, key)]

    if now - previous < RATE_LIMIT_SECONDS:
        wait = RATE_LIMIT_SECONDS - (now - previous)
        await message.reply_text(
            f"Please wait {wait:.0f} seconds before trying again."
        )
        return False

    last_call[(user.id, key)] = now
    return True


# ============================================================
# IMAGE GENERATION
# ============================================================

def generate_image_sync(prompt):
    full_prompt = f"{prompt}, realistic photo, high quality"

    encoded_prompt = urllib.parse.quote(full_prompt)
    url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=1024&model=flux&nologo=true"

    response = requests.get(url, timeout=60)
    response.raise_for_status()

    path = tempfile.NamedTemporaryFile(
        delete=False,
        suffix=".png",
    ).name

    image_data = response.content
    with open(path, "wb") as f:
        f.write(image_data)

    return path


def add_watermark(path):
    """Adds a subtle watermark to free-tier images. No-op if Pillow isn't installed."""
    if not PIL_AVAILABLE:
        return

    try:
        img = Image.open(path).convert("RGBA")
        overlay = Image.new("RGBA", img.size, (255, 255, 255, 0))
        draw = ImageDraw.Draw(overlay)
        text = f"{BOT_NAME} • FREE"

        try:
            font = ImageFont.truetype(
                "DejaVuSans-Bold.ttf", size=max(18, img.width // 22)
            )
        except Exception:
            font = ImageFont.load_default()

        bbox = draw.textbbox((0, 0), text, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
        x = max(0, img.width - text_w - 20)
        y = max(0, img.height - text_h - 20)

        draw.text((x, y), text, font=font, fill=(255, 255, 255, 140))
        combined = Image.alpha_composite(img, overlay).convert("RGB")
        combined.save(path)
    except Exception as error:
        logger.warning("Watermark failed: %s", error)


async def generate_image(update, context, prompt):
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    profile = ensure_user(user)

    group_premium = False
    if chat and chat.type != "private":
        group_premium = ensure_group(chat)["premium"]

    unlimited = profile["premium"] or group_premium
    using_bonus = False

    if not unlimited:
        remaining_free = max(0, FREE_IMAGE_LIMIT - profile["free_images_used"])
        if remaining_free <= 0:
            if profile["bonus_images"] > 0:
                using_bonus = True
            else:
                await message.reply_text(
                    "🛑 You have used all 5 free image generations.\n\n"
                    "💎 Premium costs ₦1,500 — or\n"
                    "🎟️ Top up extra images with /topup\n\n"
                    "Use /premium to see all options."
                )
                return

    status = await message.reply_text(
        "🎨 Creating your image...\n\n"
        "This can take a little while."
    )

    path = None
    try:
        path = await asyncio.to_thread(generate_image_sync, prompt)

        if not unlimited:
            add_watermark(path)

        profile["last_prompt"] = prompt
        history_list = profile["image_history"]
        history_list.append(prompt)
        del history_list[:-10]

        keyboard = [
            [
                InlineKeyboardButton("🔄 Regenerate", callback_data="regen"),
                InlineKeyboardButton("📤 Share bot +50 pts", callback_data="share_earn"),
            ]
        ]

        with open(path, "rb") as image_file:
            await message.reply_photo(
                photo=image_file,
                caption=(
                    "🖼️ AI TEMPLATE\n\n"
                    f"Prompt: {prompt[:700]}\n\n"
                    f"{access_text(profile)}"
                ),
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

        if not unlimited:
            if using_bonus:
                profile["bonus_images"] -= 1
            else:
                profile["free_images_used"] += 1
        await save()

        if profile["gallery_opt_in"] and GALLERY_CHANNEL_ID:
            try:
                with open(path, "rb") as gallery_file:
                    await context.bot.send_photo(
                        chat_id=GALLERY_CHANNEL_ID,
                        photo=gallery_file,
                        caption=f"✨ New template created with {BOT_NAME}",
                    )
            except Exception as error:
                logger.warning("Gallery post failed: %s", error)

        try:
            await status.delete()
        except Exception:
            pass

    except Exception as error:
        logger.exception("Image generation failed")
        await status.edit_text(
            "❌ I couldn't create the image right now.\n\n"
            "Your free generation was NOT used.\n\n"
            "Please try again later."
        )
    finally:
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


# ============================================================
# COMMANDS
# ============================================================

async def start(update, context):
    user = update.effective_user
    if not user:
        return

    referral_code = None
    if context.args:
        arg = context.args[0]
        if arg.startswith("ref_"):
            referral_code = arg[4:]

    is_new, profile = await register_user(
        user,
        context,
        referral_code,
    )

    if is_new:
        await notify_admin(
            context,
            (
                "🆕 NEW CUSTOMER\n\n"
                f"Name: {user.full_name}\n"
                f"Username: @{user.username or 'No username'}\n"
                f"User ID: {user.id}\n\n"
                f"Total users: {len(data['users'])}"
            ),
        )
        await save()

    keyboard = [
        [InlineKeyboardButton("🎨 Create AI Template", callback_data="image_help")],
        [InlineKeyboardButton("🖌️ Style Presets", callback_data="open_styles")],
        [InlineKeyboardButton("💎 Premium ₦1,000", callback_data="premium")],
        [InlineKeyboardButton("👥 My Referrals", callback_data="referrals")],
        [InlineKeyboardButton("🏆 My Points", callback_data="points")],
    ]

    await update.effective_message.reply_text(
        f"👋 Welcome to {BOT_NAME}!\n\n"
        "Create AI-generated image templates from your ideas.\n\n"
        "🆓 You get 5 free image generations.\n"
        "💎 Premium unlocks more access.\n"
        "👥 Invite people and earn points.\n\n"
        "Use /image followed by what you want.\n\n"
        "Example:\n"
        "/image A professional blue and black gaming tournament flyer",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def help_command(update, context):
    await update.effective_message.reply_text(
        "📚 COMMANDS\n\n"
        "🎨 AI\n"
        "/image <prompt>\n"
        "/design <idea>\n"
        "/ask <question>\n"
        "/styles — pick a style preset\n"
        "/history — redo a past prompt\n\n"
        "💎 PREMIUM\n"
        "/premium\n"
        "/confirm\n"
        "/topup — buy extra images\n"
        "/groupplan — unlimited for your group\n\n"
        "👥 REWARDS\n"
        "/refer\n"
        "/points\n"
        "/daily\n"
        "/quiz\n"
        "/leaderboard\n"
        "/claimpremium\n\n"
        "🌐 COMMUNITY\n"
        "/togglegallery — opt in/out of public gallery\n\n"
        "👤 ACCOUNT\n"
        "/myid\n\n"
        "🛠 ADMIN\n"
        "/customers\n"
        "/seen <user_id>\n"
        "/addpoints <user_id> <points>\n"
        "/addimages <user_id> <count>\n"
        "/seengroup <chat_id>\n"
        "/broadcast <message>"
    )


async def myid_command(update, context):
    await update.effective_message.reply_text(
        f"Your Telegram user ID is:\n{update.effective_user.id}"
    )


async def image_command(update, context):
    if not context.args:
        await update.effective_message.reply_text(
            "Tell me what image you want.\n\n"
            "Example:\n"
            "/image luxury perfume advertisement, black and gold\n\n"
            "Tip: use /styles to pick a ready-made style first."
        )
        return

    if not await enforce_rate_limit(update, "image"):
        return

    prompt = " ".join(context.args)

    style_key = context.user_data.pop("style_preset", None)
    if style_key and style_key in STYLE_PRESETS:
        prompt = f"{prompt}, {STYLE_PRESETS[style_key]}"

    await generate_image(update, context, prompt)


async def styles_command(update, context):
    keyboard = [
        [InlineKeyboardButton(key.title(), callback_data=f"style:{key}")]
        for key in STYLE_PRESETS
    ]

    await update.effective_message.reply_text(
        "🎨 PICK A STYLE\n\n"
        "Choose a style, then send /image <what you want> "
        "and I'll apply it automatically to your next image.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def history_command(update, context):
    profile = ensure_user(update.effective_user)
    history_list = profile["image_history"]

    if not history_list:
        await update.effective_message.reply_text(
            "You haven't created any images yet.\n\n"
            "Use /image <your idea> to get started."
        )
        return

    recent = history_list[-5:]
    context.user_data["history_recent"] = recent

    keyboard = [
        [InlineKeyboardButton(f"🔄 {p[:30]}", callback_data=f"history:{i}")]
        for i, p in enumerate(recent)
    ]

    await update.effective_message.reply_text(
        "🕘 YOUR RECENT PROMPTS\n\n"
        "Tap one to regenerate it.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def design_command(update, context):
    if not context.args:
        await update.effective_message.reply_text(
            "Example:\n/design Gaming tournament flyer"
        )
        return

    await update.effective_message.reply_text(
        "💡 For an actual image, use /image instead.\n\n"
        "Example:\n"
        "/image Gaming tournament flyer, blue and black, premium style"
    )


async def ask_ai(update, question):
    message = update.effective_message
    user = update.effective_user

    history = chat_history[user.id]
    history.append({"role": "user", "content": question})
    # Keep only the last N messages so the payload doesn't grow unbounded.
    del history[:-CHAT_HISTORY_LIMIT]

    thinking = await message.reply_text("💭 Thinking...")

    try:
        response = await asyncio.to_thread(
            groq_client.chat.completions.create,
            model="llama-3.3-70b-versatile",
            messages=history,
        )
        reply = response.choices[0].message.content
        history.append({"role": "assistant", "content": reply})

        try:
            await thinking.delete()
        except Exception:
            pass

        await message.reply_text(reply)

    except Exception as error:
        logger.exception("Groq request failed")
        # Don't keep a broken turn in history.
        history.pop()
        try:
            await thinking.edit_text(
                "❌ I couldn't get an answer right now. Please try again."
            )
        except Exception:
            pass


async def ask_command(update, context):
    if not context.args:
        await update.effective_message.reply_text(
            "Ask me anything.\n\n"
            "Example:\n"
            "/ask What's the capital of France?"
        )
        return

    if not await enforce_rate_limit(update, "ask"):
        return

    question = " ".join(context.args)
    await ask_ai(update, question)


# ============================================================
# PREMIUM / PAYMENT
# ============================================================

def payment_info():
    return (
        f"Bank: {BANK_NAME or 'Not configured'}\n"
        f"Account number: {BANK_ACCOUNT or 'Not configured'}\n"
        f"Account name: {BANK_OWNER or 'Not configured'}"
    )


async def premium_command(update, context):
    profile = ensure_user(update.effective_user)

    if profile["premium"]:
        await update.effective_message.reply_text(
            "💎 Premium is already ACTIVE on your account."
        )
        return

    await update.effective_message.reply_text(
        "💎 PREMIUM\n\n"
        f"Price: ₦{PREMIUM_PRICE:,}\n\n"
        "Premium gives you access beyond the 5 free generations.\n\n"
        "💳 PAY BY TRANSFER\n\n"
        f"{payment_info()}\n\n"
        "After paying, send your payment screenshot here "
        "and use /confirm.\n\n"
        "You can also work toward free Premium with:\n"
        f"• At least {REFERRALS_REQUIRED} successful referrals\n"
        f"• {PREMIUM_POINTS_REQUIRED:,} points\n\n"
        "Use /refer, /points and /claimpremium.\n\n"
        "Not ready for full Premium? Use /topup to buy a small batch "
        "of extra images instead.\n"
        "Running a group or business? Check /groupplan."
    )


async def confirm_command(update, context):
    user = update.effective_user

    await notify_admin(
        context,
        "💳 PAYMENT CONFIRMATION\n\n"
        f"Name: {user.full_name}\n"
        f"Username: @{user.username or 'No username'}\n"
        f"User ID: {user.id}\n\n"
        "User says they have paid. Check the screenshot/payment "
        "before activating Premium."
    )

    await update.effective_message.reply_text(
        "✅ Confirmation received.\n\n"
        "Your payment will be checked manually. "
        "Premium will only be activated after verification."
    )


async def handle_payment_screenshot(update, context):
    user = update.effective_user
    photo = update.effective_message.photo

    if not user or not photo:
        return

    file_id = photo[-1].file_id

    try:
        await context.bot.send_photo(
            chat_id=TELEGRAM_ADMIN_ID,
            photo=file_id,
            caption=(
                "💳 PAYMENT SCREENSHOT\n\n"
                f"Name: {user.full_name}\n"
                f"Username: @{user.username or 'No username'}\n"
                f"User ID: {user.id}\n\n"
                "Verify the payment manually, then use ONE of:\n"
                f"/seen {user.id}  (premium)\n"
                f"/addimages {user.id} <count>  (top-up)\n"
                "/seengroup <chat_id>  (group plan)"
            ),
        )
    except Exception as error:
        logger.warning("Could not forward payment screenshot: %s", error)

    await update.effective_message.reply_text(
        "📸 Screenshot received.\n\n"
        "I've sent it to the bot owner for verification."
    )


async def topup_command(update, context):
    lines = ["🎟️ IMAGE TOP-UPS", "", "Buy extra image generations:"]
    for key, pkg in TOPUP_PACKAGES.items():
        lines.append(f"• {pkg['label']}  →  /buytopup {key}")

    lines.append("")
    lines.append(
        "After choosing, pay by transfer and send your screenshot + /confirm."
    )
    lines.append("")
    lines.append(payment_info())

    await update.effective_message.reply_text("\n".join(lines))


async def buytopup_command(update, context):
    if not context.args or context.args[0] not in TOPUP_PACKAGES:
        await update.effective_message.reply_text(
            "Usage:\n/buytopup <package>\n\n"
            "See /topup for available packages."
        )
        return

    pkg = TOPUP_PACKAGES[context.args[0]]

    await update.effective_message.reply_text(
        f"🎟️ {pkg['label']}\n\n"
        f"{payment_info()}\n\n"
        "After paying, send your payment screenshot here and use /confirm.\n"
        "Once verified, the bot owner will add your images."
    )


async def groupplan_command(update, context):
    chat = update.effective_chat

    if chat.type == "private":
        await update.effective_message.reply_text(
            "This command is for group chats. Add me to a group and run "
            "/groupplan there."
        )
        return

    group = ensure_group(chat)

    if group["premium"]:
        await update.effective_message.reply_text(
            "💎 This group already has unlimited Premium access."
        )
        return

    await update.effective_message.reply_text(
        "💎 GROUP / BUSINESS PLAN\n\n"
        f"Price: ₦{GROUP_PREMIUM_PRICE:,}/month\n\n"
        "Unlimited image generations for everyone in this group.\n\n"
        f"{payment_info()}\n\n"
        "After paying, send the payment screenshot to the bot owner "
        "directly and use /confirm here.\n\n"
        f"This group's ID: {chat.id}"
    )


async def togglegallery_command(update, context):
    profile = ensure_user(update.effective_user)
    profile["gallery_opt_in"] = not profile["gallery_opt_in"]
    await save()

    if profile["gallery_opt_in"]:
        await update.effective_message.reply_text(
            "✅ Your future images may now be featured in the public gallery."
        )
    else:
        await update.effective_message.reply_text(
            "🚫 Your images will no longer be shared to the public gallery."
        )


async def seen_command(update, context):
    if not is_admin(update):
        await update.effective_message.reply_text(
            "You are not authorized to use this command."
        )
        return

    if not context.args:
        await update.effective_message.reply_text(
            "Usage:\n/seen <user_id>\n\n"
            "Example:\n/seen 123456789"
        )
        return

    try:
        user_id = str(int(context.args[0]))
    except ValueError:
        await update.effective_message.reply_text(
            "User ID must be a number."
        )
        return

    if user_id not in data["users"]:
        await update.effective_message.reply_text(
            "That user is not registered with the bot."
        )
        return

    data["users"][user_id]["premium"] = True
    await save()

    await update.effective_message.reply_text(
        f"✅ Premium activated for {user_id}."
    )

    try:
        await context.bot.send_message(
            chat_id=int(user_id),
            text=(
                "🎉 PREMIUM ACTIVATED!\n\n"
                "Your Premium access has been activated.\n"
                "Enjoy your templates! 💎"
            ),
        )
    except Exception:
        pass


# ============================================================
# REFERRALS / POINTS
# ============================================================

async def refer_command(update, context):
    user = update.effective_user
    profile = ensure_user(user)

    try:
        me = await context.bot.get_me()
        link = referral_link(user.id, me.username)
    except Exception:
        link = f"https://t.me/YOUR_BOT_USERNAME?start=ref_{user.id}"

    await update.effective_message.reply_text(
        "👥 YOUR REFERRAL LINK\n\n"
        f"{link}\n\n"
        f"Successful referrals: {profile['referrals']}\n"
        f"Points: {profile['points']:,}\n\n"
        f"Goal for free Premium:\n"
        f"• {REFERRALS_REQUIRED} referrals\n"
        f"• {PREMIUM_POINTS_REQUIRED:,} points"
    )


async def points_command(update, context):
    profile = ensure_user(update.effective_user)

    await update.effective_message.reply_text(
        "🏆 YOUR REWARDS\n\n"
        f"Points: {profile['points']:,}\n"
        f"Referrals: {profile['referrals']}\n\n"
        f"Free Premium requires:\n"
        f"• {REFERRALS_REQUIRED} referrals\n"
        f"• {PREMIUM_POINTS_REQUIRED:,} points\n\n"
        f"Progress: "
        f"{min(profile['points'], PREMIUM_POINTS_REQUIRED):,}/"
        f"{PREMIUM_POINTS_REQUIRED:,} points"
    )


async def daily_command(update, context):
    profile = ensure_user(update.effective_user)
    today = time.strftime("%Y-%m-%d")
    yesterday = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))

    if profile["daily_claim"] == today:
        await update.effective_message.reply_text(
            "⏰ You already claimed today's reward.\n"
            "Come back tomorrow."
        )
        return

    if profile["daily_claim"] == yesterday:
        profile["daily_streak"] = min(profile["daily_streak"] + 1, 7)
    else:
        profile["daily_streak"] = 1

    reward = 100 + (profile["daily_streak"] - 1) * 20
    profile["daily_claim"] = today
    profile["points"] += reward
    await save()

    await update.effective_message.reply_text(
        f"🎉 DAILY REWARD\n\n"
        f"🔥 Streak: {profile['daily_streak']} day(s)\n"
        f"+{reward} points!\n"
        f"Total: {profile['points']:,}"
    )


QUIZES = [
    ("What is the capital of Nigeria?", ["Lagos", "Abuja", "Kano"], 1),
    ("Which planet is known as the Red Planet?", ["Earth", "Mars", "Venus"], 1),
    ("How many days are in a week?", ["5", "7", "10"], 1),
    ("Which language is this bot written in?", ["Python", "HTML", "CSS"], 0),
]


async def quiz_command(update, context):
    profile = ensure_user(update.effective_user)
    today = time.strftime("%Y-%m-%d")

    if today in profile["quiz_claimed"]:
        await update.effective_message.reply_text(
            "🧠 You already completed today's quiz."
        )
        return

    question, options, correct = random.choice(QUIZES)

    keyboard = [
        [
            InlineKeyboardButton(
                options[0],
                callback_data=f"quiz:{correct}:{options[0]}",
            ),
            InlineKeyboardButton(
                options[1],
                callback_data=f"quiz:{correct}:{options[1]}",
            ),
            InlineKeyboardButton(
                options[2],
                callback_data=f"quiz:{correct}:{options[2]}",
            ),
        ]
    ]

    context.user_data["quiz"] = {
        "date": today,
        "correct": options[correct],
    }

    await update.effective_message.reply_text(
        "🧠 DAILY QUIZ\n\n" + question,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def claimpremium_command(update, context):
    user = update.effective_user
    profile = ensure_user(user)

    if profile["premium"]:
        await update.effective_message.reply_text(
            "💎 Premium is already active."
        )
        return

    if not premium_eligible(profile):
        await update.effective_message.reply_text(
            "🔒 Not unlocked yet.\n\n"
            f"You need at least {REFERRALS_REQUIRED} referrals "
            f"and {PREMIUM_POINTS_REQUIRED:,} points.\n\n"
            f"Current referrals: {profile['referrals']}\n"
            f"Current points: {profile['points']:,}"
        )
        return

    # Do not automatically grant this without a server-side check
    # that the referrals are genuine. Alert the admin first.
    await notify_admin(
        context,
        "🏆 FREE PREMIUM CLAIM\n\n"
        f"User: {user.full_name}\n"
        f"User ID: {user.id}\n"
        f"Referrals: {profile['referrals']}\n"
        f"Points: {profile['points']:,}\n\n"
        f"Use /seen {user.id} after checking the account."
    )

    await update.effective_message.reply_text(
        "🎉 You meet the requirements!\n\n"
        "Your claim has been sent to the bot owner for verification."
    )


async def leaderboard_command(update, context):
    users = data["users"]

    if not users:
        await update.effective_message.reply_text(
            "No points yet."
        )
        return

    ranked = sorted(
        users.items(),
        key=lambda item: item[1].get("points", 0),
        reverse=True,
    )[:10]

    lines = ["🏆 TOP 10", ""]

    for i, (_, profile) in enumerate(ranked, start=1):
        name = profile.get("first_name", "User")
        total = profile.get("points", 0)
        lines.append(f"{i}. {name} — {total:,} points")

    await update.effective_message.reply_text("\n".join(lines))


# ============================================================
# ADMIN COMMANDS
# ============================================================

async def customers_command(update, context):
    if not is_admin(update):
        await update.effective_message.reply_text(
            "You are not authorized to use this command."
        )
        return

    premium_count = sum(
        1 for p in data["users"].values()
        if p.get("premium")
    )

    await update.effective_message.reply_text(
        "📊 CUSTOMER STATS\n\n"
        f"Total users: {len(data['users'])}\n"
        f"Premium users: {premium_count}"
    )


async def addpoints_command(update, context):
    if not is_admin(update):
        await update.effective_message.reply_text(
            "You are not authorized to use this command."
        )
        return

    if len(context.args) != 2:
        await update.effective_message.reply_text(
            "Usage:\n/addpoints <user_id> <points>"
        )
        return

    try:
        user_id = str(int(context.args[0]))
        amount = int(context.args[1])
    except ValueError:
        await update.effective_message.reply_text(
            "User ID and points must be numbers."
        )
        return

    if user_id not in data["users"]:
        await update.effective_message.reply_text(
            "User not found."
        )
        return

    if amount <= 0 or amount > 100000:
        await update.effective_message.reply_text(
            "Choose between 1 and 100,000 points."
        )
        return

    data["users"][user_id]["points"] += amount
    await save()

    await update.effective_message.reply_text(
        f"✅ Added {amount:,} points.\n"
        f"New total: {data['users'][user_id]['points']:,}"
    )


async def addimages_command(update, context):
    if not is_admin(update):
        await update.effective_message.reply_text(
            "You are not authorized to use this command."
        )
        return

    if len(context.args) != 2:
        await update.effective_message.reply_text(
            "Usage:\n/addimages <user_id> <count>"
        )
        return

    try:
        user_id = str(int(context.args[0]))
        amount = int(context.args[1])
    except ValueError:
        await update.effective_message.reply_text(
            "User ID and count must be numbers."
        )
        return

    if user_id not in data["users"]:
        await update.effective_message.reply_text("User not found.")
        return

    if amount <= 0 or amount > 1000:
        await update.effective_message.reply_text(
            "Choose between 1 and 1,000 images."
        )
        return

    data["users"][user_id]["bonus_images"] = (
        data["users"][user_id].get("bonus_images", 0) + amount
    )
    await save()

    await update.effective_message.reply_text(
        f"✅ Added {amount} bonus images.\n"
        f"New balance: {data['users'][user_id]['bonus_images']}"
    )

    try:
        await context.bot.send_message(
            chat_id=int(user_id),
            text=f"🎟️ {amount} extra images have been added to your account!",
        )
    except Exception:
        pass


async def seengroup_command(update, context):
    if not is_admin(update):
        await update.effective_message.reply_text(
            "You are not authorized to use this command."
        )
        return

    if not context.args:
        await update.effective_message.reply_text(
            "Usage:\n/seengroup <chat_id>"
        )
        return

    chat_id = context.args[0]
    data["groups"].setdefault(chat_id, {"title": "Group", "premium": False})
    data["groups"][chat_id]["premium"] = True
    await save()

    await update.effective_message.reply_text(
        f"✅ Group premium activated for {chat_id}."
    )

    try:
        await context.bot.send_message(
            chat_id=int(chat_id),
            text="🎉 This group now has unlimited Premium image generation!",
        )
    except Exception:
        pass


async def monthly_referral_reset(context):
    users = data["users"]
    ranked = sorted(
        users.items(),
        key=lambda item: item[1].get("referrals_month", 0),
        reverse=True,
    )

    if ranked and ranked[0][1].get("referrals_month", 0) > 0:
        winner_id, winner_profile = ranked[0]
        bonus = 1000
        winner_profile["points"] += bonus

        try:
            await context.bot.send_message(
                chat_id=int(winner_id),
                text=(
                    "🏆 YOU WON THIS MONTH'S REFERRAL LEADERBOARD!\n\n"
                    f"+{bonus:,} bonus points!"
                ),
            )
        except Exception:
            pass

        await notify_admin(
            context,
            "🏆 Monthly referral winner: "
            f"{winner_profile.get('first_name')} ({winner_id}) with "
            f"{winner_profile.get('referrals_month', 0)} referrals.",
        )

    for profile in users.values():
        profile["referrals_month"] = 0

    await save()


async def broadcast_command(update, context):
    if not is_admin(update):
        await update.effective_message.reply_text(
            "You are not authorized to use this command."
        )
        return

    if not context.args:
        await update.effective_message.reply_text(
            "Usage:\n/broadcast Your message"
        )
        return

    text = " ".join(context.args)
    sent = 0
    failed = 0

    for user_id in list(data["users"]):
        try:
            await context.bot.send_message(
                chat_id=int(user_id),
                text=text,
            )
            sent += 1
            await asyncio.sleep(0.05)
        except Exception as error:
            failed += 1
            logger.warning("Broadcast failed for %s: %s", user_id, error)

    await update.effective_message.reply_text(
        f"📢 BROADCAST COMPLETE\n\n"
        f"Sent: {sent}\n"
        f"Failed: {failed}"
    )


# ============================================================
# FUN COMMANDS
# ============================================================

JOKES = [
    "Why did the computer go to the doctor? It had a virus. 😂",
    "Why was the keyboard tired? It had too many shifts. 😭",
    "I told my computer I needed a break... now it won't stop sending vacation ads. 😂",
]

FACTS = [
    "Octopuses have three hearts. 🐙",
    "A day on Venus is longer than its year. 🪐",
    "Honey can stay edible for a very long time when stored properly. 🍯",
]

async def joke_command(update, context):
    await update.effective_message.reply_text(random.choice(JOKES))


async def fact_command(update, context):
    await update.effective_message.reply_text(
        "🧠 " + random.choice(FACTS)
    )


async def coinflip_command(update, context):
    await update.effective_message.reply_text(
        "🪙 Heads!" if random.choice([True, False]) else "🪙 Tails!"
    )


async def dice_command(update, context):
    await update.effective_message.reply_text(
        f"🎲 You rolled: {random.randint(1, 6)}"
    )


# ============================================================
# BUTTONS
# ============================================================

async def button(update, context):
    query = update.callback_query
    await query.answer()

    if query.data == "image_help":
        await query.message.reply_text(
            "🎨 CREATE A TEMPLATE\n\n"
            "Use:\n"
            "/image <what you want>\n\n"
            "Example:\n"
            "/image Luxury perfume advert, black and gold"
        )

    elif query.data == "premium":
        await premium_command(update, context)

    elif query.data == "referrals":
        await refer_command(update, context)

    elif query.data == "points":
        await points_command(update, context)

    elif query.data == "open_styles":
        await styles_command(update, context)

    elif query.data.startswith("style:"):
        key = query.data.split(":", 1)[1]
        if key not in STYLE_PRESETS:
            return
        context.user_data["style_preset"] = key
        await query.message.reply_text(
            f"✅ Style set: {key.title()}\n\n"
            "Now send:\n/image <what you want>\n\n"
            "I'll apply the style automatically to your next image."
        )

    elif query.data == "regen":
        if not await enforce_rate_limit(update, "image"):
            return

        profile = ensure_user(query.from_user)
        prompt = profile.get("last_prompt")

        if not prompt:
            await query.message.reply_text(
                "No previous prompt found yet. Use /image first."
            )
            return

        await generate_image(update, context, prompt)

    elif query.data == "share_earn":
        profile = ensure_user(query.from_user)
        today = time.strftime("%Y-%m-%d")

        if profile["share_claimed"] == today:
            await query.message.reply_text(
                "You already claimed today's share bonus. Come back tomorrow!"
            )
            return

        profile["share_claimed"] = today
        profile["points"] += 50
        await save()

        try:
            me = await context.bot.get_me()
            link = f"https://t.me/{me.username}"
        except Exception:
            link = "this bot"

        await query.message.reply_text(
            f"🎉 +50 points! Thanks for sharing {link} with friends.\n"
            f"Total: {profile['points']:,}"
        )

    elif query.data.startswith("history:"):
        try:
            idx = int(query.data.split(":", 1)[1])
        except ValueError:
            return

        recent = context.user_data.get("history_recent", [])
        if idx >= len(recent):
            await query.message.reply_text("That prompt is no longer available.")
            return

        if not await enforce_rate_limit(update, "image"):
            return

        await generate_image(update, context, recent[idx])

    elif query.data.startswith("quiz:"):
        profile = ensure_user(query.from_user)
        today = time.strftime("%Y-%m-%d")

        if today in profile["quiz_claimed"]:
            await query.message.reply_text(
                "You already completed today's quiz."
            )
            return

        parts = query.data.split(":", 2)
        chosen = parts[2]
        correct = context.user_data.get("quiz", {}).get("correct")

        if chosen == correct:
            profile["points"] += 150
            profile["quiz_claimed"].append(today)
            await save()

            await query.message.reply_text(
                f"✅ Correct!\n\n"
                f"+150 points\n"
                f"Total: {profile['points']:,}"
            )
        else:
            profile["quiz_claimed"].append(today)
            await save()

            await query.message.reply_text(
                f"❌ Not quite.\n\n"
                f"The correct answer was: {correct}"
            )


# ============================================================
# NORMAL TEXT
# ============================================================

async def handle_message(update, context):
    if not await enforce_rate_limit(update, "ask"):
        return

    question = update.effective_message.text
    await ask_ai(update, question)


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(update, context):
    logger.error(
        "Unhandled exception",
        exc_info=context.error,
    )

    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                "Something went wrong. Please try again."
            )
    except Exception:
        pass


# ============================================================
# MAIN
# ============================================================

class HealthCheckHandler(BaseHTTPRequestHandler):
    """Minimal HTTP responder so Render treats this as a Web Service
    (free tier) instead of a Background Worker (paid only)."""

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running.")

    def log_message(self, format, *args):
        pass  # keep the console clean


def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()


def main():
    threading.Thread(target=run_health_server, daemon=True).start()

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("myid", myid_command))

    app.add_handler(CommandHandler("image", image_command))
    app.add_handler(CommandHandler("design", design_command))
    app.add_handler(CommandHandler("ask", ask_command))
    app.add_handler(CommandHandler("styles", styles_command))
    app.add_handler(CommandHandler("history", history_command))

    app.add_handler(CommandHandler("premium", premium_command))
    app.add_handler(CommandHandler("confirm", confirm_command))
    app.add_handler(CommandHandler("topup", topup_command))
    app.add_handler(CommandHandler("buytopup", buytopup_command))
    app.add_handler(CommandHandler("groupplan", groupplan_command))
    app.add_handler(CommandHandler("togglegallery", togglegallery_command))

    app.add_handler(CommandHandler("refer", refer_command))
    app.add_handler(CommandHandler("points", points_command))
    app.add_handler(CommandHandler("daily", daily_command))
    app.add_handler(CommandHandler("quiz", quiz_command))
    app.add_handler(CommandHandler("claimpremium", claimpremium_command))
    app.add_handler(CommandHandler("leaderboard", leaderboard_command))

    app.add_handler(CommandHandler("seen", seen_command))
    app.add_handler(CommandHandler("customers", customers_command))
    app.add_handler(CommandHandler("addpoints", addpoints_command))
    app.add_handler(CommandHandler("addimages", addimages_command))
    app.add_handler(CommandHandler("seengroup", seengroup_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))

    app.add_handler(CommandHandler("joke", joke_command))
    app.add_handler(CommandHandler("fact", fact_command))
    app.add_handler(CommandHandler("coinflip", coinflip_command))
    app.add_handler(CommandHandler("dice", dice_command))

    app.add_handler(CallbackQueryHandler(button))

    app.add_handler(
        MessageHandler(
            filters.PHOTO,
            handle_payment_screenshot,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_message,
        )
    )

    app.add_error_handler(error_handler)

    if app.job_queue is not None:
        app.job_queue.run_monthly(
            monthly_referral_reset,
            when=dt_time(hour=0, minute=5),
            day=1,
        )
    else:
        logger.warning(
            "JobQueue not available — monthly referral leaderboard reset "
            "will not run. Install with: "
            "pip install \"python-telegram-bot[job-queue]\""
        )

    logger.info("%s starting...", BOT_NAME)
    app.run_polling()


if __name__ == "__main__":
    main()
