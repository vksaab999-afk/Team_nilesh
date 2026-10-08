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
# Logging
# ------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(name)

# ------------------------------------------------------------------
# Config (NO hardcoded secrets)
# ------------------------------------------------------------------
TOKEN = os.environ["BOT_TOKEN"]
MONGO_URI = os.environ["MONGO_URI"]

ADMIN_USER_IDS = [int(x) for x in os.environ.get("ADMIN_USER_IDS", "").split(",") if x.strip()]

# ------------------------------------------------------------------
# Flask keep-alive
# ------------------------------------------------------------------
app = Flask("")

@app.route("/")
def home():
    return "VIP Prediction Bot is active 24x7!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

def keep_alive():
    Thread(target=run_flask, daemon=True).start()

# ------------------------------------------------------------------
# Mongo
# ------------------------------------------------------------------
client = MongoClient(MONGO_URI)
db = client["telegram_broadcast_bot"]
channels_collection = db["active_channels"]
mappings_collection = db["broadcast_mappings"]

# ------------------------------------------------------------------
# Premium-emoji capability check (runs once)
# ------------------------------------------------------------------
_PREMIUM_OK = None  # tri-state: None=unknown, True/False=resolved

async def check_premium_capability(bot) -> bool:
    """
    Send a throwaway message with a custom_emoji entity to ourselves
    and see if Telegram keeps or strips it.
    """
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
                    custom_emoji_id="5368324170671202286",  # Telegram's own sample ID
                )
            ],
        )
        kept = any(e.type == MessageEntity.CUSTOM_EMOJI for e in (test.entities or []))
        _PREMIUM_OK = kept
        try:
            await test.delete()
        except Exception:
            pass
        if not kept:
            logger.warning(
                "⚠️ Premium/custom emojis are NOT supported by this bot. "
                "The bot's owner account needs Telegram Premium."
            )
        else:
            logger.info("✅ Premium/custom emoji sending is enabled.")
    except Exception as e:
        logger.warning(f"Premium capability check failed: {e}")
        _PREMIUM_OK = False

    return _PREMIUM_OK

# ------------------------------------------------------------------
# Entity sanitization — strip entity types Bot API rejects
# ------------------------------------------------------------------
_ALLOWED_ENTITY_TYPES = {
    MessageEntity.MENTION,
    MessageEntity.HASHTAG,
    MessageEntity.CASHTAG,
    MessageEntity.BOT_COMMAND,
    MessageEntity.URL,
    MessageEntity.EMAIL,
    MessageEntity.PHONE_NUMBER,
    MessageEntity.BOLD,
    MessageEntity.ITALIC,
    MessageEntity.UNDERLINE,
    MessageEntity.STRIKETHROUGH,
    MessageEntity.SPOILER,
    MessageEntity.CODE,
    MessageEntity.PRE,
    MessageEntity.TEXT_LINK,
    MessageEntity.TEXT_MENTION,
    MessageEntity.CUSTOM_EMOJI,
    MessageEntity.BLOCKQUOTE,
    MessageEntity.EXPANDABLE_BLOCKQUOTE,
}

def sanitize_entities(entities):
    if not entities:
        return None
    cleaned = []
    for e in entities:
        if e.type in _ALLOWED_ENTITY_TYPES:
            cleaned.append(e)
        else:
            logger.debug(f"Skipping unsupported entity type: {e.type}")
    return cleaned or None

def has_custom_emoji(entities) -> bool:
    if not entities:
        return False
    return any(e.type == MessageEntity.CUSTOM_EMOJI for e in entities)

# ------------------------------------------------------------------
# 1. Channel tracking
# ------------------------------------------------------------------
async def track_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = update.my_chat_member
    if not result:
        return
    chat = result.chat
    if chat.type != "channel":
        return

    new_status = result.new_chat_member.status
    if new_status in ("administrator", "creator"):
        channels_collection.update_one(
            {"chat_id": chat.id},
            {"$set": {"title": chat.title, "username": chat.username}},
            upsert=True,
        )
        logger.info(f"Added channel: {chat.title} ({chat.id})")
    elif new_status in ("left", "kicked", "member"):
        channels_collection.delete_one({"chat_id": chat.id})
        logger.info(f"Removed channel: {chat.title} ({chat.id})")

# ------------------------------------------------------------------
# 2. Safe send with retry + flood handling
# ------------------------------------------------------------------
async def safe_call(coro_factory, retries: int = 3):
    """
    coro_factory: a callable returning a fresh coroutine each time.
    Handles RetryAfter (FloodWait) with backoff.
    """
    for attempt in range(retries):
        try:
            return await coro_factory()
        except RetryAfter as e:
            wait = int(e.retry_after) + 1
            logger.warning(f"FloodWait: sleeping {wait}s")
            await asyncio.sleep(wait)
        except Forbidden as e:
            logger.error(f"Forbidden: {e}")
            raise
        except BadRequest as e:
            logger.error(f"BadRequest: {e}")
            raise
    # Last attempt
    return await coro_factory()

# ------------------------------------------------------------------
# 3. Premium-safe entity sender
# ------------------------------------------------------------------
async def send_clean_with_entities(bot, chat_id, message, reply_to_channel_msg_id=None):
    """
    Sends a message that:
    - preserves entities (bold/italic/links/custom emoji)
    - strips unsupported entity types
    - has NO forward tag
    - retries on FloodWait
    """

    text_entities = sanitize_entities(message.entities)
    caption_entities = sanitize_entities(message.caption_entities)

    # --- TEXT ---
    if message.text:
        async def _send():
            sent = await bot.send_message(
                chat_id=chat_id,
                text=message.text,
                entities=text_entities,
                reply_to_message_id=reply_to_channel_msg_id,
                reply_markup=message.reply_markup,
                disable_web_page_preview=False,
            )
            _warn_if_stripped(sent, text_entities, chat_id)
            return sent
        return await safe_call(_send)

    # --- PHOTO ---
    if message.photo:
        async def _send():
            sent = await bot.send_photo(
                chat_id=chat_id,
                photo=message.photo[-1].file_id,
                caption=message.caption,
                caption_entities=caption_entities,
                reply_to_message_id=reply_to_channel_msg_id,
                reply_markup=message.reply_markup,
            )
            _warn_if_stripped(sent, caption_entities, chat_id)
            return sent
        return await safe_call(_send)# --- VIDEO ---
    if message.video:
        async def _send():
            sent = await bot.send_video(
                chat_id=chat_id,
                video=message.video.file_id,
                caption=message.caption,
                caption_entities=caption_entities,
                reply_to_message_id=reply_to_channel_msg_id,
                reply_markup=message.reply_markup,
            )
            _warn_if_stripped(sent, caption_entities, chat_id)
            return sent
        return await safe_call(_send)

    # --- AUDIO ---
    if message.audio:
        async def _send():
            return await bot.send_audio(
                chat_id=chat_id,
                audio=message.audio.file_id,
                caption=message.caption,
                caption_entities=caption_entities,
                reply_to_message_id=reply_to_channel_msg_id,
                reply_markup=message.reply_markup,
            )
        return await safe_call(_send)

    # --- VOICE ---
    if message.voice:
        async def _send():
            return await bot.send_voice(
                chat_id=chat_id,
                voice=message.voice.file_id,
                caption=message.caption,
                caption_entities=caption_entities,
                reply_to_message_id=reply_to_channel_msg_id,
                reply_markup=message.reply_markup,
            )
        return await safe_call(_send)

    # --- DOCUMENT ---
    if message.document:
        async def _send():
            return await bot.send_document(
                chat_id=chat_id,
                document=message.document.file_id,
                caption=message.caption,
                caption_entities=caption_entities,
                reply_to_message_id=reply_to_channel_msg_id,
                reply_markup=message.reply_markup,
            )
        return await safe_call(_send)

    # --- FALLBACK: copy_message ---
    async def _copy():
        return await bot.copy_message(
            chat_id=chat_id,
            from_chat_id=message.chat_id,
            message_id=message.message_id,
            reply_to_message_id=reply_to_channel_msg_id,
            reply_markup=message.reply_markup,
        )
    return await safe_call(_copy)


def _warn_if_stripped(sent_message, original_entities, chat_id):
    """If we tried to send custom emojis but Telegram stripped them, log it."""
    if not has_custom_emoji(original_entities):
        return
    sent_entities = sent_message.entities or sent_message.caption_entities or []
    if not has_custom_emoji(sent_entities):
        logger.warning(
            f"Custom emoji was STRIPPED for chat {chat_id}. "
            "Bot owner likely lacks Telegram Premium."
        )

# ------------------------------------------------------------------
# 4. /del command
# ------------------------------------------------------------------
async def delete_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_USER_IDS:
        return

    message = update.message
    if not message.reply_to_message:
        await message.reply_text(
            "⚠️ Kripya us broadcast kiye gaye message par reply karke /del likhein."
        )
        return

    replied_msg_id = message.reply_to_message.message_id
    mapping = mappings_collection.find_one({"admin_msg_id": replied_msg_id})

    if not mapping:
        await message.reply_text("⚠️ Yeh message kisi broadcast record mein nahi mila.")
        return

    channel_msg_map = mapping["channels"]

    async def delete_one(ch_str_id, ch_msg_id):
        chat_id = int(ch_str_id)
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=ch_msg_id)
            return True
        except RetryAfter as e:
            await asyncio.sleep(int(e.retry_after) + 1)
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=ch_msg_id)
                return True
            except Exception as e2:
                logger.error(f"Delete retry failed {chat_id}: {e2}")
                return False
        except Exception as e:
            logger.error(f"Failed to delete in channel {chat_id}: {e}")
            return False

    results = await asyncio.gather(*(delete_one(cid, mid) for cid, mid in channel_msg_map.items()))
    deleted_count = sum(1 for r in results if r)

    mappings_collection.delete_one({"admin_msg_id": replied_msg_id})
    await message.reply_text(
        f"🗑️ Message delete kar diya gaya! ({deleted_count}/{len(channel_msg_map)} channels)"
    )

# ------------------------------------------------------------------
# 5. Broadcast engine
# ------------------------------------------------------------------
async def handle_broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_USER_IDS:
        return

    message = update.message
    all_channels = list(channels_collection.find({}))
    if not all_channels:
        await message.reply_text(
            "⚠️ Pehle kisi channel mein bot ko admin banayein — koi channel connected nahi hai!"
        )
        return

    # ---- Reply-threaded broadcast ----
    if message.reply_to_message:
        replied_msg_id = message.reply_to_message.message_id
        mapping = mappings_collection.find_one({"admin_msg_id": replied_msg_id})

        if mapping:
            channel_msg_map = mapping["channels"]

            async def send_reply(ch_str_id, ch_msg_id):
                chat_id = int(ch_str_id)
                try:
                    await send_clean_with_entities(
                        bot=context.bot,
                        chat_id=chat_id,
                        message=message,
                        reply_to_channel_msg_id=int(ch_msg_id),
                    )
                    return True
                except Exception as e:
                    logger.error(f"Reply failed in {chat_id}: {e}")
                    return False

            results = await asyncio.gather(
                *(send_reply(cid, mid) for cid, mid in channel_msg_map.items())
            )
            ok = sum(1 for r in results if r)
            fail = len(results) - ok
            await message.reply_text(
                f"✅ Reply Broadcasted!\nSuccess: {ok} | Failed: {fail}",
                parse_mode="Markdown",
            )
            return

    # ---- Fresh broadcast ----
    async def send_broadcast(ch):
        chat_id = ch["chat_id"]
        try:
            sent = await send_clean_with_entities(
                bot=context.bot,
                chat_id=chat_id,
                message=message,
            )
            return str(chat_id), sent.message_id
        except Forbidden as e:
            logger.warning(f"Bot removed from {chat_id}: {e}")
            channels_collection.delete_one({"chat_id": chat_id})
            return str(chat_id), None
        except Exception as e:
            logger.error(f"Broadcast failed to {chat_id}: {e}")
            if "chat not found" in str(e).lower():
                channels_collection.delete_one({"chat_id": chat_id})
            return str(chat_id), None

    results = await asyncio.gather(*(send_broadcast(ch) for ch in all_channels))

    channel_mapping_data = {}
    success = fail = 0
    for ch_id, msg_id in results:
        if msg_id:
            channel_mapping_data[ch_id] = msg_id
            success += 1
        else:
            fail += 1

    if channel_mapping_data:
        mappings_collection.update_one(
            {"admin_msg_id": message.message_id},
            {"$set": {"channels": channel_mapping_data}},
            upsert=True,
        )

    await message.reply_text(
        f"✅ Broadcasted!\nSent: {success} | Failed: {fail}",
        parse_mode="Markdown",
    )

# ------------------------------------------------------------------
# 6. Entry point
# ------------------------------------------------------------------
async def post_init(application):
    await check_premium_capability(application.bot)

def main():
    keep_alive()application = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

    application.add_handler(
        ChatMemberHandler(track_chat_member, ChatMemberHandler.MY_CHAT_MEMBER)
    )
    application.add_handler(CommandHandler("del", delete_broadcast))
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & ~filters.COMMAND,
            handle_broadcast_message,
        )
    )

    logger.info("Prediction Broadcast Bot running...")
    application.run_polling(drop_pending_updates=True)


if name == "main":
    main()
