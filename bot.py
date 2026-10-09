import logging
import os
import re
import asyncio
from threading import Thread

from flask import Flask
from telegram import (
    Update,
    MessageEntity,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.error import RetryAfter, Forbidden, BadRequest
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    CommandHandler,
    ChatMemberHandler,
    ChatJoinRequestHandler,
    CallbackQueryHandler,
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
# Safe Config
# ------------------------------------------------------------------
TOKEN = os.environ.get("BOT_TOKEN", "").strip()
MONGO_URI = os.environ.get("MONGO_URI", "").strip()

raw_group_id = os.environ.get("ADMIN_GROUP_ID", "0").strip()
try:
    ADMIN_GROUP_ID = int(raw_group_id)
except ValueError:
    logger.error(f"❌ Invalid ADMIN_GROUP_ID format: {raw_group_id}. Setting to 0.")
    ADMIN_GROUP_ID = 0

if not TOKEN or not MONGO_URI:
    logger.error("❌ BOT_TOKEN ya MONGO_URI missing hai Environment Variables me!")

ADMIN_USER_IDS = [5785924075]
admin_modes = {}
admin_states = {}

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
    t = Thread(target=run_flask, daemon=True)
    t.start()

# ------------------------------------------------------------------
# Mongo Setup
# ------------------------------------------------------------------
try:
    client = MongoClient(MONGO_URI)
    db = client["telegram_broadcast_bot"]
    channels_collection = db["active_channels"]
    mappings_collection = db["broadcast_mappings"]
    users_collection = db["bot_users"]
    user_topics_collection = db["user_forum_topics"]
    settings_collection = db["welcome_settings"]
except Exception as e:
    logger.error(f"Mongo Connection Error: {e}")

# ------------------------------------------------------------------
# STYLED BUTTON HELPER (Safe & Bulletproof)
# ------------------------------------------------------------------
def styled_button(text, *, style=None, icon_custom_emoji_id=None, url=None, callback_data=None):
    action = {"url": url} if url else {"callback_data": callback_data or "noop"}
    api_kwargs = {}
    
    if style:
        api_kwargs["style"] = style
    if icon_custom_emoji_id:
        api_kwargs["icon_custom_emoji_id"] = str(icon_custom_emoji_id)

    if api_kwargs:
        try:
            return InlineKeyboardButton(text=text, api_kwargs=api_kwargs, **action)
        except Exception:
            return InlineKeyboardButton(text=text, **action)
    return InlineKeyboardButton(text=text, **action)

# ------------------------------------------------------------------
# Settings Helper Functions
# ------------------------------------------------------------------
def get_welcome_settings():
    doc = settings_collection.find_one({"_id": "welcome_config"})
    if not doc:
        default_settings = {
            "_id": "welcome_config",
            "text": None,
            "video_id": None,
            "video_caption": None,
            "video_entities": None,
            "video_buttons": [],
            "apk_id": None,
            "apk_caption": None,
            "apk_entities": None,
            "apk_buttons": [],
            "audio_id": None,
            "audio_caption": None,
            "audio_entities": None,
            "audio_buttons": [],
        }
        settings_collection.insert_one(default_settings)
        return default_settings
    return doc

def update_welcome_setting(key_dict):
    settings_collection.update_one(
        {"_id": "welcome_config"},
        {"$set": key_dict},
        upsert=True
    )

def serialize_entities(entities):
    if not entities:
        return None
    res = []
    for e in entities:
        res.append({
            "type": e.type,
            "offset": e.offset,
            "length": e.length,
            "url": e.url,
            "custom_emoji_id": e.custom_emoji_id
        })
    return res

def deserialize_entities(raw_entities):
    if not raw_entities:
        return None
    res = []
    for e in raw_entities:
        res.append(MessageEntity(
            type=e["type"],
            offset=e["offset"],
            length=e["length"],
            url=e.get("url"),
            custom_emoji_id=e.get("custom_emoji_id")
        ))
    return res

def build_inline_keyboard(buttons_list):
    if not buttons_list:
        return None
    keyboard = []
    for btn in buttons_list:
        text = btn.get("text", "Button")
        url = btn.get("url")
        style = btn.get("style")
        icon_id = btn.get("icon_id")
        
        if url:
            btn_obj = styled_button(text=text, url=url, style=style, icon_custom_emoji_id=icon_id)
            keyboard.append([btn_obj])
            
    return InlineKeyboardMarkup(keyboard) if keyboard else None

def parse_buttons_text(raw_text):
    lines = raw_text.strip().split("\n")
    buttons = []
    target = "video"
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        if line.lower().startswith("target:") or line.lower() in ["video", "apk", "audio"]:
            target_val = line.split(":", 1)[1].strip().lower() if ":" in line else line.lower()
            if "video" in target_val:
                target = "video"
            elif "apk" in target_val or "doc" in target_val:
                target = "apk"
            elif "audio" in target_val or "voice" in target_val:
                target = "audio"
            elif "text" in target_val:
                target = "text"
            continue

        if "http://" in line or "https://" in line or "t.me" in line:
            style_val = None
            icon_val = None
            
            if "| style:" in line.lower():
                parts_s = re.split(r'\|\s*style:', line, flags=re.IGNORECASE)
                style_val = parts_s[1].strip().split()[0].strip()
                line = parts_s[0].strip()

            if "| icon:" in line.lower():
                parts_i = re.split(r'\|\s*icon:', line, flags=re.IGNORECASE)
                icon_val = parts_i[1].strip().split()[0].strip(" []")
                line = parts_i[0].strip()

            cleaned_line = re.sub(r'button\s*\d+\s*:', '', line, flags=re.IGNORECASE).strip()

            subparts = cleaned_line.rsplit("http", 1)
            if len(subparts) == 2:
                label_part = subparts[0].strip(" -:")
                link_part = "http" + subparts[1].strip()
                if label_part and link_part:
                    buttons.append({
                        "text": label_part,
                        "url": link_part,
                        "style": style_val,
                        "icon_id": icon_val
                    })

    return buttons, target

# ------------------------------------------------------------------
# Entity Sanitization & Safe Call
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
            logger.warning(f"FloodWait encountered. Retrying in {wait}s...")
            await asyncio.sleep(wait)
        except (Forbidden, BadRequest) as e:
            logger.error(f"Failed call permanently: {e}")
            raise
        except Exception as e:
            logger.warning(f"Transient error: {e}. Retrying... (Attempt {attempt+1}/{retries})")
            await asyncio.sleep(2)
    return await coro_factory()

async def send_clean_with_entities(bot, chat_id, message, reply_to_channel_msg_id=None, message_thread_id=None):
    text_entities = sanitize_entities(message.entities)
    caption_entities = sanitize_entities(message.caption_entities)

    if message.text:
        async def _send():
            return await bot.send_message(
                chat_id=chat_id, text=message.text, entities=text_entities,
                reply_to_message_id=reply_to_channel_msg_id, message_thread_id=message_thread_id,
                reply_markup=message.reply_markup, disable_web_page_preview=False,
            )
        return await safe_call(_send)

    if message.photo:
        async def _send():
            return await bot.send_photo(
                chat_id=chat_id, photo=message.photo[-1].file_id, caption=message.caption,
                caption_entities=caption_entities, reply_to_message_id=reply_to_channel_msg_id,
                message_thread_id=message_thread_id, reply_markup=message.reply_markup,
            )
        return await safe_call(_send)

    if message.video:
        async def _send():
            return await bot.send_video(
                chat_id=chat_id, video=message.video.file_id, caption=message.caption,
                caption_entities=caption_entities, reply_to_message_id=reply_to_channel_msg_id,
                message_thread_id=message_thread_id, reply_markup=message.reply_markup,
            )
        return await safe_call(_send)

    if message.audio:
        async def _send():
            return await bot.send_audio(
                chat_id=chat_id, audio=message.audio.file_id, caption=message.caption,
                caption_entities=caption_entities, reply_to_message_id=reply_to_channel_msg_id,
                message_thread_id=message_thread_id, reply_markup=message.reply_markup,
            )
        return await safe_call(_send)

    if message.voice:
        async def _send():
            return await bot.send_voice(
                chat_id=chat_id, voice=message.voice.file_id, caption=message.caption,
                caption_entities=caption_entities, reply_to_message_id=reply_to_channel_msg_id,
                message_thread_id=message_thread_id, reply_markup=message.reply_markup,
            )
        return await safe_call(_send)

    if message.document:
        async def _send():
            return await bot.send_document(
                chat_id=chat_id, document=message.document.file_id, caption=message.caption,
                caption_entities=caption_entities, reply_to_message_id=reply_to_channel_msg_id,
                message_thread_id=message_thread_id, reply_markup=message.reply_markup,
            )
        return await safe_call(_send)

    async def _copy():
        return await bot.copy_message(
            chat_id=chat_id, from_chat_id=message.chat_id, message_id=message.message_id,
            reply_to_message_id=reply_to_channel_msg_id, message_thread_id=message_thread_id,
            reply_markup=message.reply_markup,
        )
    return await safe_call(_copy)

# ------------------------------------------------------------------
# Forum Topic Resolver Engine
# ------------------------------------------------------------------
async def get_or_create_user_topic(bot, user) -> int:
    doc = user_topics_collection.find_one({"user_id": user.id})
    if doc and "thread_id" in doc:
        return doc["thread_id"]

    topic_name = f"{user.first_name} | {user.id}"
    try:
        topic = await bot.create_forum_topic(chat_id=ADMIN_GROUP_ID, name=topic_name[:128])
        thread_id = topic.message_thread_id
        
        user_topics_collection.update_one(
            {"user_id": user.id},
            {"$set": {"user_id": user.id, "thread_id": thread_id, "username": user.username}},
            upsert=True
        )
        user_topics_collection.update_one(
            {"thread_id": thread_id},
            {"$set": {"user_id": user.id}},
            upsert=True
        )
        return thread_id
    except Exception as e:
        logger.error(f"❌ Failed to create forum topic for user {user.id}: {e}")
        return None

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
# Join Request Handler
# ------------------------------------------------------------------
async def handle_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    request = update.chat_join_request
    if not request:
        return
    user = request.from_user
    settings = get_welcome_settings()

    user_first_name = user.first_name or "User"

    users_collection.update_one(
        {"user_id": user.id},
        {"$set": {"user_id": user.id, "first_name": user.first_name, "username": user.username}},
        upsert=True,
    )

    # 1. Video
    if settings.get("video_id"):
        try:
            vid_cap = settings.get("video_caption") or ""
            vid_cap = vid_cap.replace("{name}", f"**{user_first_name}**")
            vid_entities = deserialize_entities(settings.get("video_entities"))
            vid_markup = build_inline_keyboard(settings.get("video_buttons"))
            
            await context.bot.send_video(
                chat_id=user.id,
                video=settings["video_id"],
                caption=vid_cap if vid_cap else None,
                caption_entities=vid_entities,
                reply_markup=vid_markup
            )
        except Exception as e:
            logger.error(f"Error sending video to {user.id}: {e}")

    # 2. APK / Document
    if settings.get("apk_id"):
        try:
            apk_cap = settings.get("apk_caption") or ""
            apk_cap = apk_cap.replace("{name}", f"**{user_first_name}**")
            apk_entities = deserialize_entities(settings.get("apk_entities"))
            apk_markup = build_inline_keyboard(settings.get("apk_buttons"))
            
            await context.bot.send_document(
                chat_id=user.id,
                document=settings["apk_id"],
                caption=apk_cap if apk_cap else None,
                caption_entities=apk_entities,
                reply_markup=apk_markup
            )
        except Exception as e:
            logger.error(f"Error sending APK to {user.id}: {e}")

    # 3. Audio / Voice
    if settings.get("audio_id"):
        try:
            aud_cap = settings.get("audio_caption") or ""
            aud_cap = aud_cap.replace("{name}", f"**{user_first_name}**")
            aud_entities = deserialize_entities(settings.get("audio_entities"))
            aud_markup = build_inline_keyboard(settings.get("audio_buttons"))
            
            await context.bot.send_audio(
                chat_id=user.id,
                audio=settings["audio_id"],
                caption=aud_cap if aud_cap else None,
                caption_entities=aud_entities,
                reply_markup=aud_markup
            )
        except Exception as e:
            logger.error(f"Error sending audio to {user.id}: {e}")

    # 4. Standalone Text
    if settings.get("text"):
        try:
            raw_text = settings.get("text")
            formatted_text = raw_text.replace("{name}", f"**{user_first_name}**")
            
            await context.bot.send_message(
                chat_id=user.id,
                text=formatted_text,
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Error sending text to {user.id}: {e}")

    logger.info(f"✅ Join request handled for {user.first_name} ({user.id})")

# ------------------------------------------------------------------
# Callback Query Handler
# ------------------------------------------------------------------
async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    await query.answer()

    if query.data == "set_custom_buttons":
        admin_states[user.id] = "awaiting_custom_buttons"
        instr = (
            "🔘 **ADD STYLED & EMOJI ICON BUTTONS**\n\n"
            "Format mein message bhejey:\n\n"
            "`Button 1: 🔴 DM FOR LOSS RECOVERY - https://t.me/yourusername | icon: 6111716252932119427 | style: danger`\n"
            "`Button 2: 🟢 REGISTRATION LINK - https://bdg4.cc/#/register | icon: 5271604874419647061 | style: success`\n"
            "`Button 3: 🔵 VIP CHANNEL - https://t.me/+aO4PoFUq5gU4YmNl | icon: 5001508545976337554 | style: primary`\n"
            "`Target: Video`\n\n"
            "*(Style Options: `primary` (Blue), `success` (Green), `danger` (Red))*"
        )
        await query.edit_message_text(instr, parse_mode="Markdown")

    elif query.data.startswith("set_"):
        setting_type = query.data.split("set_")[1]
        admin_states[user.id] = f"awaiting_{setting_type}"
        
        labels = {
            "text": "📝 Naya Standalone Welcome Text bhejey (Use `{name}` for Bold Name):",
            "video": "🎥 Nayi Video file bhejey (With Caption & Emojis):",
            "apk": "📁 Nayi APK / Document file bhejey (With Caption & Emojis):",
            "audio": "🎵 Nayi Audio file bhejey (With Caption & Emojis):",
        }
        await query.edit_message_text(labels.get(setting_type, "Send input:"))

# ------------------------------------------------------------------
# /updatejoinrequest Command Admin Panel
# ------------------------------------------------------------------
async def update_join_request_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_USER_IDS:
        return

    settings = get_welcome_settings()
    
    vid_btns_cnt = len(settings.get("video_buttons", []))
    apk_btns_cnt = len(settings.get("apk_buttons", []))
    aud_btns_cnt = len(settings.get("audio_buttons", []))

    status_text = (
        "⚙️ **UPDATE JOIN REQUEST WELCOME PANEL**\n\n"
        f"🎥 **Video Configured:** `{'Yes' if settings.get('video_id') else 'No'}` | Buttons: `{vid_btns_cnt}`\n"
        f"📁 **APK Configured:** `{'Yes' if settings.get('apk_id') else 'No'}` | Buttons: `{apk_btns_cnt}`\n"
        f"🎵 **Audio Configured:** `{'Yes' if settings.get('audio_id') else 'No'}` | Buttons: `{aud_btns_cnt}`\n"
        f"📝 **Text Configured:** `{'Yes' if settings.get('text') else 'No'}`\n\n"
        "👇 Niche buttons par click karke media ya custom buttons update karein:"
    )

    keyboard = [
        [InlineKeyboardButton("🎥 Change Video", callback_data="set_video"), InlineKeyboardButton("📁 Change APK", callback_data="set_apk")],
        [InlineKeyboardButton("🎵 Change Audio", callback_data="set_audio"), InlineKeyboardButton("📝 Change Text", callback_data="set_text")],
        [InlineKeyboardButton("🔘 Add/Set Colorful Styled Buttons", callback_data="set_custom_buttons")],
    ]

    await update.message.reply_text(status_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

# ------------------------------------------------------------------
# Commands
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

async def set_prediction_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_USER_IDS:
        return
    admin_modes[user.id] = "channel"
    await update.message.reply_text("📢 **Prediction Mode Active!**\n\nAb aap jo bhi message DM me bhejenge, woh sirf **Channels** me instant broadcast hoga.", parse_mode="Markdown")

async def set_broadcast_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_USER_IDS:
        return
    admin_modes[user.id] = "user"
    await update.message.reply_text("👥 **User Broadcast Mode Active!**\n\nAb aap jo bhi message DM me bhejenge, woh **Channels me nahi jayega**, sirf Bot ke **Users** ko private chat me jayega.", parse_mode="Markdown")

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
        f"⚙️ **Current Mode:** {mode_str}\n"
        f"🏢 **Admin Group ID:** `{ADMIN_GROUP_ID}`"
    )

    await update.message.reply_text(stats_text, parse_mode="Markdown")

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
# Main Message Handler Logic
# ------------------------------------------------------------------
async def handle_all_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return

    user = update.effective_user
    chat = update.effective_chat

    # Admin Settings Inputs
    if user.id in ADMIN_USER_IDS and user.id in admin_states:
        state = admin_states.pop(user.id)
        
        if state == "awaiting_custom_buttons" and message.text:
            parsed_btns, target_media = parse_buttons_text(message.text)
            if parsed_btns:
                db_key = f"{target_media}_buttons"
                update_welcome_setting({db_key: parsed_btns})
                await message.reply_text(f"✅ `{len(parsed_btns)}` Styled Buttons successfully set for **{target_media.upper()}** message!", parse_mode="Markdown")
            else:
                await message.reply_text("⚠️ Buttons format galat tha! Kripya exact format me bhejey.")
            return

        elif state == "awaiting_text" and message.text:
            update_welcome_setting({"text": message.text})
            await message.reply_text("✅ Standalone Welcome Text update ho gaya hai!")
            return

        elif state == "awaiting_video" and message.video:
            entities_data = serialize_entities(message.caption_entities)
            update_welcome_setting({
                "video_id": message.video.file_id,
                "video_caption": message.caption,
                "video_entities": entities_data
            })
            await message.reply_text("✅ Video with exact Caption & Emojis update ho gayi hai!")
            return

        elif state == "awaiting_apk" and message.document:
            entities_data = serialize_entities(message.caption_entities)
            update_welcome_setting({
                "apk_id": message.document.file_id,
                "apk_caption": message.caption,
                "apk_entities": entities_data
            })
            await message.reply_text("✅ APK File with exact Caption & Emojis update ho gayi hai!")
            return

        elif state == "awaiting_audio" and (message.audio or message.voice):
            file_id = message.audio.file_id if message.audio else message.voice.file_id
            entities_data = serialize_entities(message.caption_entities)
            update_welcome_setting({
                "audio_id": file_id,
                "audio_caption": message.caption,
                "audio_entities": entities_data
            })
            await message.reply_text("✅ Audio with exact Caption & Emojis update ho gayi hai!")
            return

    # 1. MESSAGE FROM ADMIN FORUM GROUP -> ROUTE TO USER
    if chat.id == ADMIN_GROUP_ID:
        thread_id = message.message_thread_id
        if not thread_id:
            return

        doc = user_topics_collection.find_one({"thread_id": thread_id})
        if doc and "user_id" in doc:
            target_user_id = doc["user_id"]
            try:
                await send_clean_with_entities(context.bot, target_user_id, message)
            except Exception as e:
                logger.error(f"Error replying to user {target_user_id} from Forum: {e}")
        return

    # 2. MESSAGE FROM NORMAL USER -> ROUTE TO USER'S FORUM TOPIC
    if user.id not in ADMIN_USER_IDS:
        users_collection.update_one(
            {"user_id": user.id},
            {"$set": {"user_id": user.id, "first_name": user.first_name, "username": user.username}},
            upsert=True,
        )

        if ADMIN_GROUP_ID != 0:
            thread_id = await get_or_create_user_topic(context.bot, user)
            if thread_id:
                try:
                    await send_clean_with_entities(
                        context.bot, chat_id=ADMIN_GROUP_ID, message=message, message_thread_id=thread_id
                    )
                    return
                except Exception as e:
                    logger.error(f"❌ Could not forward msg to thread {thread_id}: {e}")

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

    # 3. ADMIN DIRECT DM MESSAGES -> CHANNELS / BROADCAST MODE
    current_mode = admin_modes.get(user.id, "channel")

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

        sent_channels = {}
        success = 0

        for ch in all_channels:
            ch_id = ch["chat_id"]
            reply_to_msg_id = reply_mapping.get(str(ch_id)) if reply_mapping else None
            try:
                sent_msg = await send_clean_with_entities(
                    context.bot, ch_id, message, reply_to_channel_msg_id=reply_to_msg_id
                )
                if sent_msg:
                    sent_channels[str(ch_id)] = sent_msg.message_id
                    success += 1
            except Exception as e:
                logger.error(f"Failed to post in channel {ch_id}: {e}")

        if sent_channels:
            mappings_collection.insert_one({
                "admin_msg_id": message.message_id,
                "channels": sent_channels
            })

        await message.reply_text(f"📢 Broadcast sent to {success}/{len(all_channels)} Channels!")

    else:
        all_users = list(users_collection.find({"user_id": {"$nin": ADMIN_USER_IDS}}))
        success = 0
        cleaned_users = 0

        for u in all_users:
            u_id = u["user_id"]
            try:
                await send_clean_with_entities(context.bot, u_id, message)
                success += 1
                await asyncio.sleep(0.04)
            except (Forbidden, BadRequest):
                users_collection.delete_one({"user_id": u_id})
                cleaned_users += 1
            except Exception as e:
                logger.error(f"User broadcast failed for {u_id}: {e}")

        clean_msg = f" 🗑️ ({cleaned_users} Inactive users cleaned)" if cleaned_users > 0 else ""
        await update.message.reply_text(f"👥 Broadcast sent to {success}/{len(all_users)} Users!{clean_msg}")

# ------------------------------------------------------------------
# Main Entry Point
# ------------------------------------------------------------------
def main():
    if not TOKEN:
        logger.error("BOT_TOKEN missing. System exiting...")
        return

    keep_alive()

    app_bot = ApplicationBuilder().token(TOKEN).build()

    # Commands
    app_bot.add_handler(CommandHandler("start", start_command))
    app_bot.add_handler(CommandHandler("prediction", set_prediction_mode))
    app_bot.add_handler(CommandHandler("broadcast", set_broadcast_mode))
    app_bot.add_handler(CommandHandler("stats", stats_command))
    app_bot.add_handler(CommandHandler("del", delete_broadcast))
    app_bot.add_handler(CommandHandler("updatejoinrequest", update_join_request_command))

    # Handlers for Join Request & Callbacks
    app_bot.add_handler(ChatJoinRequestHandler(handle_join_request))
    app_bot.add_handler(CallbackQueryHandler(handle_callback_query))

    # Core Event Handlers
    app_bot.add_handler(ChatMemberHandler(track_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app_bot.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_all_messages))

    logger.info("🤖 VIP Styled Button Broadcast Bot is running...")
    
    app_bot.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
