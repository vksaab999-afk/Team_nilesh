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
# Entity sanitization
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

async def safe_call(coro_factory, retries: int = 3):
    for attempt in range(retries):
        try:
            return await coro_factory()
        except RetryAfter as e:
            wait = int(e.retry_after) + 1
            await asyncio.sleep(wait)
        except (Forbidden, BadRequest) as e:
            logger.error(f"Error in safe_call: {e}")
            raise
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

        # Check if this message is replying to a previously broadcasted message in channels
        reply_mapping = None
        if message.reply_to_message:
            replied_admin_msg_id = message.reply_to_message.message_id
            mapping_doc = mappings_collection.find_one({"admin_msg_id": replied_admin_msg_id})
            if mapping_doc:
                reply_mapping = mapping_doc.get("channels", {})

        async def send_to_ch(ch):
            chat_id = ch["chat_id"]
            reply_msg_id = reply_mapping.get(str(chat_id)) if reply_mapping else None
            try:
                sent = await send_clean_with_entities(
                    context.bot, 
                    chat_id, 
                    message, 
                    reply_to_channel_msg_id=reply_msg_id
                )
                return str(chat_id), sent.message_id
            except Exception:
                return str(chat_id), None

        results = await asyncio.gather(*(send_to_ch(ch) for ch in all_channels))
        mapping = {cid: mid for cid, mid in results if mid}
        if mapping:
            mappings_collection.update_one({"admin_msg_id": message.message_id}, {"$set": {"channels": mapping}}, upsert=True)
        
        if reply_mapping:
            await message.reply_text(f"✅ Reply Broadcasted to {len(mapping)} Channels!")
        else:
            await message.reply_text(f"📢 Broadcasted to {len(mapping)} Channels!")

    # C. Mode 2: User Broadcast Mode (All Users)
    elif current_mode == "user":
        all_users = list(users_collection.find({"user_id": {"$nin": ADMIN_USER_IDS}}))
        if not all_users:
            await message.reply_text("⚠️ Database me koi users nahi hain! Jab naye users bot par /start karenge tab unhe message jayega.")
            return

        success = 0
        for u in all_users:
            try:
                await send_clean_with_entities(context.bot, u["user_id"], message)
                success += 1
                await asyncio.sleep(0.04)
            except Exception as e:
                logger.error(f"Failed to send to user {u['user_id']}: {e}")

        await message.reply_text(f"👥 Sent to {success}/{len(all_users)} Users!")

# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------
async def post_init(application):
    await check_premium_capability(application.bot)

def main():
    keep_alive()
    application = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

    application.add_handler(ChatMemberHandler(track_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("prediction", set_prediction_mode))
    application.add_handler(CommandHandler("broadcast", set_broadcast_mode))
    application.add_handler(CommandHandler("del", delete_broadcast))
    
    application.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, handle_all_messages)
    )

    logger.info("Bot is active and running...")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
