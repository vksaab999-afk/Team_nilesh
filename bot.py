import logging
import os
import asyncio
from threading import Thread

from flask import Flask
from telegram import Update, MessageEntity
from telegram.error import RetryAfter, Forbidden, BadRequest
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    CommandHandler,
    ChatMemberHandler,
    filters,
)
from pymongo import MongoClient

# ------------------------------------------------------------------
# Logging Setup
# ------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Config
# ------------------------------------------------------------------
TOKEN = os.environ["BOT_TOKEN"]
MONGO_URI = os.environ["MONGO_URI"]

# Admin User IDs List
ADMIN_USER_IDS = [5785924075]

# Admin Modes: 'channel' (for /prediction) or 'user' (for /broadcast)
admin_modes = {}

# ------------------------------------------------------------------
# Flask keep-alive
# ------------------------------------------------------------------
app = Flask("")

@app.route("/")
def home():
    return "VIP Prediction & Broadcast Bot is active 24x7!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

def keep_alive():
    Thread(target=run_flask, daemon=True).start()

# ------------------------------------------------------------------
# Mongo Setup
# ------------------------------------------------------------------
client = MongoClient(MONGO_URI)
db = client["telegram_broadcast_bot"]
channels_collection = db["active_channels"]
mappings_collection = db["broadcast_mappings"]
users_collection = db["bot_users"]

# ------------------------------------------------------------------
# Premium-emoji capability check
# ------------------------------------------------------------------
_PREMIUM_OK = None

async def check_premium_capability(bot) -> bool:
    global _PREMIUM_OK
    if _PREMIUM_OK is not None:
        return _PREMIUM_OK

    try:
        me = await bot.get_me()
        test = await bot.send_message(
            chat_id=me.id,
            text="⭐",
            entities=[
                MessageEntity(
                    type=MessageEntity.CUSTOM_EMOJI,
                    offset=0,
                    length=1,
                    custom_emoji_id="5368324170671202286",
                )
            ],
        )
        kept = any(e.type == MessageEntity.CUSTOM_EMOJI for e in (test.entities or []))
        _PREMIUM_OK = kept
        try:
            await test.delete()
        except Exception:
            pass
    except Exception as e:
        logger.warning(f"Premium capability check failed: {e}")
        _PREMIUM_OK = False

    return _PREMIUM_OK

# ------------------------------------------------------------------
# Entity sanitization & Safe Call (With Failed Broadcast Retry)
# ------------------------------------------------------------------
_ALLOWED_ENTITY_TYPES = {
    MessageEntity.MENTION, MessageEntity.HASHTAG, MessageEntity.CASHTAG,
    MessageEntity.BOT_COMMAND, MessageEntity.URL, MessageEntity.EMAIL,
    MessageEntity.PHONE_NUMBER, MessageEntity.BOLD, MessageEntity.ITALIC,
    MessageEntity.UNDERLINE, MessageEntity.STRIKETHROUGH, MessageEntity.SPOILER,
    MessageEntity.CODE, MessageEntity.PRE, MessageEntity.TEXT_LINK,
    MessageEntity.TEXT_MENTION, MessageEntity.CUSTOM_EMOJI,
    MessageEntity.BLOCKQUOTE, MessageEntity.EXPANDABLE_BLOCKQUOTE,
}

def sanitize_entities(entities):
    if not entities:
        return None
    cleaned = [e for e in entities if e.type in _ALLOWED_ENTITY_TYPES]
    return cleaned or None

# Feature: Failed Broadcast Retrier (Safe Call with Backoff)
async def safe_call(coro_factory, retries: int = 3):
    for attempt in range(retries):
        try:
            return await coro_factory()
        except RetryAfter as e:
            wait = int(e.retry_after) + 1
            logger.warning(f"FloodWait encountered. Retrying in {wait}s... (Attempt {attempt+1}/{retries})")
            await asyncio.sleep(wait)
        except (Forbidden, BadRequest) as e:
            logger.error(f"Failed call permanently: {e}")
            raise
        except Exception as e:
            logger.warning(f"Transient error: {e}. Retrying... (Attempt {attempt+1}/{retries})")
            await asyncio.sleep(2)
    return await coro_factory()

async def send_clean_with_entities(bot, chat_id, message, reply_to_channel_msg_id=None):
    text_entities = sanitize_entities(message.entities)
    caption_entities = sanitize_entities(message.caption_entities)

    if message.text:
        async def _send():
            return await bot.send_message(
                chat_id=chat_id, text=message.text, entities=text_entities,
                reply_to_message_id=reply_to_channel_msg_id, reply_markup=message.reply_markup,
                disable_web_page_preview=False,
            )
        return await safe_call(_send)

    if message.photo:
        async def _send():
            return await bot.send_photo(
                chat_id=chat_id, photo=message.photo[-1].file_id, caption=message.caption,
                caption_entities=caption_entities, reply_to_message_id=reply_to_channel_msg_id,
                reply_markup=message.reply_markup,
            )
        return await safe_call(_send)

    if message.video:
        async def _send():
            return await bot.send_video(
                chat_id=chat_id, video=message.video.file_id, caption=message.caption,
                caption_entities=caption_entities, reply_to_message_id=reply_to_channel_msg_id,
                reply_markup=message.reply_markup,
            )
        return await safe_call(_send)

    if message.audio:
        async def _send():
            return await bot.send_audio(
                chat_id=chat_id, audio=message.audio.file_id, caption=message.caption,
                caption_entities=caption_entities, reply_to_message_id=reply_to_channel_msg_id,
                reply_markup=message.reply_markup,
            )
        return await safe_call(_send)

    if message.voice:
        async def _send():
            return await bot.send_voice(
                chat_id=chat_id, voice=message.voice.file_id, caption=message.caption,
                caption_entities=caption_entities, reply_to_message_id=reply_to_channel_msg_id,
                reply_markup=message.reply_markup,
            )
        return await safe_call(_send)

    if message.document:
        async def _send():
            return await bot.send_document(
                chat_id=chat_id, document=message.document.file_id, caption=message.caption,
                caption_entities=caption_entities, reply_to_message_id=reply_to_channel_msg_id,
                reply_markup=message.reply_markup,
            )
        return await safe_call(_send)

    async def _copy():
        return await bot.copy_message(
            chat_id=chat_id, from_chat_id=message.chat_id, message_id=message.message_id,
            reply_to_message_id=reply_to_channel_msg_id, reply_markup=message.reply_markup,
        )
    return await safe_call(_copy)

# ------------------------------------------------------------------
# Track Channel Members
# ------------------------------------------------------------------
async def track_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = update.my_chat_member
    if not result or result.chat.type != "channel":
        return

    chat = result.chat
    new_status = result.new_chat_member.status
    if new_status in ("administrator", "creator"):
        channels_collection.update_one(
            {"chat_id": chat.id},
            {"$set": {"title": chat.title, "username": chat.username}},
            upsert=True,
        )
    elif new_status in ("left", "kicked", "member"):
        channels_collection.delete_one({"chat_id": chat.id})

# ------------------------------------------------------------------
# Start Command for Normal Users
# ------------------------------------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    users_collection.update_one(
        {"user_id": user.id},
        {"$set": {"user_id": user.id, "first_name": user.first_name, "username": user.username}},
        upsert=True,
    )
    await update.message.reply_text(
        f"👋 Welcome {user.first_name}!\nAapka swagat hai. Aap jo bhi message bhejenge, hamari team tak pahunch jayega."
    )

# ------------------------------------------------------------------
# Mode Switching Commands for Admin
# ------------------------------------------------------------------
async def set_prediction_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_USER_IDS:
        return
    admin_modes[user.id] = "channel"
    await update.message.reply_text(
        "📢 **Prediction Mode Active!**\n\nAb aap jo bhi message bhejenge, woh sirf **Channels** me instant broadcast hoga.",
        parse_mode="Markdown"
    )

async def set_broadcast_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_USER_IDS:
        return
    admin_modes[user.id] = "user"
    await update.message.reply_text(
        "👥 **User Broadcast Mode Active!**\n\nAb aap jo bhi message bhejenge, woh **Channels me nahi jayega**, sirf Bot ke **Users** ko private chat me jayega.",
        parse_mode="Markdown"
    )

# ------------------------------------------------------------------
# Stats Command (/stats)
# ------------------------------------------------------------------
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_USER_IDS:
        return

    total_users = users_collection.count_documents({"user_id": {"$nin": ADMIN_USER_IDS}})
    total_channels = channels_collection.count_documents({})
    current_mode = admin_modes.get(user.id, "channel")

    mode_str = "📢 Prediction Mode (Channels)" if current_mode == "channel" else "👥 Broadcast Mode (Users)"

    stats_text = (
        "📊 **BOT STATISTICS**\n\n"
        f"👥 **Total Bot Users:** `{total_users}`\n"
        f"📢 **Connected Channels:** `{total_channels}`\n"
        f"⚙️ **Current Mode:** {mode_str}"
    )

    await update.message.reply_text(stats_text, parse_mode="Markdown")

# ------------------------------------------------------------------
# Feature: Interactive Poll & Quiz Engine (/poll & /quiz)
# Format: /poll Question | Opt1 | Opt2 | Opt3
# Format: /quiz Question | Opt1 | Opt2 | CorrectIndex (0-based)
# ------------------------------------------------------------------
async def poll_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_USER_IDS:
        return

    text = " ".join(context.args)
    if "|" not in text:
        await update.message.reply_text("⚠️ **Format:** `/poll Question | Option1 | Option2 | Option3`", parse_mode="Markdown")
        return

    parts = [p.strip() for p in text.split("|") if p.strip()]
    if len(parts) < 3:
        await update.message.reply_text("⚠️ Minimum 1 question aur 2 options hona zaroori hain!", parse_mode="Markdown")
        return

    question = parts[0]
    options = parts[1:]
    current_mode = admin_modes.get(user.id, "channel")

    if current_mode == "channel":
        all_channels = list(channels_collection.find({}))
        success = 0
        for ch in all_channels:
            try:
                await safe_call(lambda: context.bot.send_poll(chat_id=ch["chat_id"], question=question, options=options, is_anonymous=True))
                success += 1
            except Exception as e:
                logger.error(f"Poll send failed for channel {ch['chat_id']}: {e}")
        await update.message.reply_text(f"📊 Poll broadcasted to {success}/{len(all_channels)} Channels!")
    else:
        all_users = list(users_collection.find({"user_id": {"$nin": ADMIN_USER_IDS}}))
        success = 0
        cleaned_users = 0
        for u in all_users:
            try:
                await safe_call(lambda: context.bot.send_poll(chat_id=u["user_id"], question=question, options=options, is_anonymous=True))
                success += 1
                await asyncio.sleep(0.04)
            except (Forbidden, BadRequest) as e:
                users_collection.delete_one({"user_id": u["user_id"]})
                cleaned_users += 1
            except Exception as e:
                logger.error(f"Poll send error for user {u['user_id']}: {e}")

        clean_msg = f" 🗑️ ({cleaned_users} Inactive users cleaned)" if cleaned_users > 0 else ""
        await update.message.reply_text(f"📊 Poll sent to {success}/{len(all_users)} Users!{clean_msg}")

async def quiz_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_USER_IDS:
        return

    text = " ".join(context.args)
    if "|" not in text:
        await update.message.reply_text("⚠️ **Format:** `/quiz Question | Option1 | Option2 | CorrectOptionIndex(0,1..)`", parse_mode="Markdown")
        return

    parts = [p.strip() for p in text.split("|") if p.strip()]
    if len(parts) < 4:
        await update.message.reply_text("⚠️ Question, kam se kam 2 options, aur sahi option ka index (0, 1, 2...) dena zaroori hai!", parse_mode="Markdown")
        return

    question = parts[0]
    options = parts[1:-1]
    try:
        correct_option_id = int(parts[-1])
    except ValueError:
        await update.message.reply_text("⚠️ Correct option index integer numeric hona chahiye (jaise: 0, 1, 2)!", parse_mode="Markdown")
        return

    current_mode = admin_modes.get(user.id, "channel")

    if current_mode == "channel":
        all_channels = list(channels_collection.find({}))
        success = 0
        for ch in all_channels:
            try:
                await safe_call(lambda: context.bot.send_poll(
                    chat_id=ch["chat_id"], question=question, options=options,
                    type="quiz", correct_option_id=correct_option_id, is_anonymous=True
                ))
                success += 1
            except Exception as e:
                logger.error(f"Quiz send failed for channel {ch['chat_id']}: {e}")
        await update.message.reply_text(f"💡 Quiz broadcasted to {success}/{len(all_channels)} Channels!")
    else:
        all_users = list(users_collection.find({"user_id": {"$nin": ADMIN_USER_IDS}}))
        success = 0
        cleaned_users = 0
        for u in all_users:
            try:
                await safe_call(lambda: context.bot.send_poll(
                    chat_id=u["user_id"], question=question, options=options,
                    type="quiz", correct_option_id=correct_option_id, is_anonymous=True
                ))
                success += 1
                await asyncio.sleep(0.04)
            except (Forbidden, BadRequest) as e:
                users_collection.delete_one({"user_id": u["user_id"]})
                cleaned_users += 1
            except Exception as e:
                logger.error(f"Quiz send error for user {u['user_id']}: {e}")

        clean_msg = f" 🗑️ ({cleaned_users} Inactive users cleaned)" if cleaned_users > 0 else ""
        await update.message.reply_text(f"💡 Quiz sent to {success}/{len(all_users)} Users!{clean_msg}")

# ------------------------------------------------------------------
# Delete Command (/del)
# ------------------------------------------------------------------
async def delete_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_USER_IDS:
        return

    message = update.message
    if not message.reply_to_message:
        await message.reply_text("⚠️ Kripya us message par reply karke /del likhein.")
        return

    replied_msg_id = message.reply_to_message.message_id
    mapping = mappings_collection.find_one({"admin_msg_id": replied_msg_id})

    if not mapping:
        await message.reply_text("⚠️ Yeh message kisi broadcast record mein nahi mila.")
        return

    channel_msg_map = mapping["channels"]

    async def delete_one(ch_str_id, ch_msg_id):
        try:
            await context.bot.delete_message(chat_id=int(ch_str_id), message_id=ch_msg_id)
            return True
        except Exception:
            return False

    results = await asyncio.gather(*(delete_one(cid, mid) for cid, mid in channel_msg_map.items()))
    deleted_count = sum(1 for r in results if r)

    mappings_collection.delete_one({"admin_msg_id": replied_msg_id})
    await message.reply_text(f"🗑️ Message deleted from {deleted_count} channels!")

# ------------------------------------------------------------------
# Admin & User Message Handler Logic
# ------------------------------------------------------------------
async def handle_all_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.message

    # 1. USER SIDE LOGIC (Non-Admins)
    if user.id not in ADMIN_USER_IDS:
        users_collection.update_one(
            {"user_id": user.id},
            {"$set": {"user_id": user.id, "first_name": user.first_name, "username": user.username}},
            upsert=True,
        )
        username_str = f"@{user.username}" if user.username else "No Username"
        caption_info = f"📩 **New Message From User:**\n👤 **Name:** {user.first_name}\n🔗 **Username:** {username_str}\n🆔 **User ID:** `{user.id}`\n\n"

        for admin_id in ADMIN_USER_IDS:
            try:
                if message.text:
                    await context.bot.send_message(chat_id=admin_id, text=caption_info + message.text, parse_mode="Markdown")
                else:
                    copied_msg = await context.bot.copy_message(chat_id=admin_id, from_chat_id=message.chat_id, message_id=message.message_id)
                    await context.bot.send_message(chat_id=admin_id, text=caption_info, reply_to_message_id=copied_msg.message_id, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Error forwarding user msg to admin: {e}")
        return

    # 2. ADMIN SIDE LOGIC
    # A. Check if Admin is replying to a forwarded User Message
    if message.reply_to_message:
        replied_text = message.reply_to_message.text or message.reply_to_message.caption or ""
        if "🆔 User ID:" in replied_text:
            try:
                target_user_id = int(replied_text.split("🆔 User ID:")[1].strip().split()[0].replace("`", ""))
                await send_clean_with_entities(context.bot, target_user_id, message)
                await message.reply_text(f"✅ Reply sent to User `{target_user_id}`!", parse_mode="Markdown")
                return
            except Exception as e:
                await message.reply_text(f"❌ User ko reply bhejne me dikkat aayi: {e}")
                return

    current_mode = admin_modes.get(user.id, "channel")

    # B. Mode 1: Prediction Mode (Channel Broadcast)
    if current_mode == "channel":
        all_channels = list(channels_collection.find({}))
        if not all_channels:
            await message.reply_text("⚠️ Kisi channel me bot Admin nahi hai!")
            return

        reply_mapping = None
        if message.reply_to_message:
            replied_admin_msg_id = message.reply_to_message.message_id
            mapping_doc = mappings_collection.find_one({"admin_msg_id": replied_admin_msg_id})
            if mapping_doc:
                reply_mapping = mapping_doc.get("channels", {})

        async def send_to_ch(ch):
            chat_id = ch["chat_id"]
   
