import logging
import os
import re
import asyncio
from datetime import datetime, timezone
from threading import Thread

from flask import Flask
from telegram import (
    Update,
    MessageEntity,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    BotCommandScopeDefault,
    BotCommandScopeChat,
    InputMediaPhoto,
    InputMediaVideo,
    InputMediaDocument,
    InputMediaAudio,
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
pending_broadcast_messages = {}
pending_setup_messages = {}
admin_start_msg_buffers = {}
admin_join_msg_buffers = {}
media_group_buffers = {}

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
# Mongo Setup & Auto-TTL Index
# ------------------------------------------------------------------
try:
    client = MongoClient(MONGO_URI)
    db = client["telegram_broadcast_bot"]
    channels_collection = db["active_channels"]
    mappings_collection = db["broadcast_mappings"]
    users_collection = db["bot_users"]
    user_topics_collection = db["user_forum_topics"]
    settings_collection = db["welcome_settings"]

    mappings_collection.create_index("created_at", expireAfterSeconds=604800)
except Exception as e:
    logger.error(f"Mongo Connection Error: {e}")

# ------------------------------------------------------------------
# HELPER: Get Live Active Admin Channels Only (Removes Left/Kicked)
# ------------------------------------------------------------------
async def get_live_active_channels(bot):
    all_channels = list(channels_collection.find({}))
    valid_channels = []

    for ch in all_channels:
        ch_id = ch["chat_id"]
        try:
            member = await bot.get_chat_member(chat_id=ch_id, user_id=bot.id)
            if member.status in ("administrator", "creator"):
                valid_channels.append(ch)
            else:
                channels_collection.delete_one({"chat_id": ch_id})
        except Exception:
            channels_collection.delete_one({"chat_id": ch_id})

    return valid_channels

# ------------------------------------------------------------------
# EXACT MENU COMMANDS SETUP
# ------------------------------------------------------------------
async def setup_bot_commands(application):
    try:
        user_commands = [
            BotCommand("start", "🚀 Start Bot / Refresh karne ke liye ✅")
        ]
        await application.bot.set_my_commands(
            commands=user_commands,
            scope=BotCommandScopeDefault()
        )

        admin_commands = [
            BotCommand("start", "🚀 Start Bot / Refresh karne ke liye ✅"),
            BotCommand("updatestartmsg", "✏️ Set / Update Custom /start Messages ✅"),
            BotCommand("updatejoinrequest", "🤝 Set / Update Custom Join Request Welcome Messages ✅"),
            BotCommand("prediction", "📢 prediction dene or kon konse channel pe prediction dena hai set karne ke liye ✅"),
            BotCommand("broadcast", "🫂 User's ko broadcast karne ke liye ✅"),
            BotCommand("welcome", "🤝 automatic welcome msg kon konse channel pe jana chahiya kon konse pe nahi set karne ke liye ✅"),
            BotCommand("joinrequest", "📥 konse channel me join request auto accept honi chahiya konse me nahi set karne ke liye ✅"),
            BotCommand("quiz", "🧩 channel ya User's ko quiz bhejne ke liye ✅"),
            BotCommand("poll", "📊 channel ya User's ko poll bhejne ke liye ✅"),
            BotCommand("stats", "📊 View Bot Statistics dekhne ke liye ✅"),
            BotCommand("del", "🗑 channel pe dale kishi prediction ko delete karne ke liye 🟢")
        ]

        for admin_id in ADMIN_USER_IDS:
            try:
                await application.bot.set_my_commands(
                    commands=admin_commands,
                    scope=BotCommandScopeChat(chat_id=admin_id)
                )
                logger.info(f"✅ Exact Admin Menu Commands applied for Admin ID: {admin_id}")
            except Exception as err:
                logger.error(f"Could not set commands for admin {admin_id}: {err}")

    except Exception as e:
        logger.error(f"❌ Error setting bot commands: {e}")

# ------------------------------------------------------------------
# STYLED BUTTON HELPER
# ------------------------------------------------------------------
def styled_button(text, *, style=None, icon_custom_emoji_id=None, url=None, callback_data=None):
    action = {"url": url} if url else {"callback_data": callback_data or "noop"}
    api_kwargs = {}
    
    if style:
        api_kwargs["style"] = str(style).strip()
    if icon_custom_emoji_id:
        api_kwargs["icon_custom_emoji_id"] = str(icon_custom_emoji_id).strip()

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
            "custom_start_msgs": [],
            "custom_join_msgs": []
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
            continue

        if "http://" in line or "https://" in line or "t.me" in line:
            style_val = None
            icon_val = None
            
            if "| style" in line.lower():
                parts_s = re.split(r'\|\s*style\s*:', line, flags=re.IGNORECASE)
                style_val = parts_s[1].strip().split()[0].strip()
                line = parts_s[0].strip()

            if "| icon" in line.lower():
                parts_i = re.split(r'\|\s*icon\s*:', line, flags=re.IGNORECASE)
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

# ------------------------------------------------------------------
# RAW NATIVE ENGINE (PRESERVES ANIMATED CUSTOM EMOJIS FULLY)
# ------------------------------------------------------------------
async def send_clean_with_entities(bot, chat_id, message, reply_to_channel_msg_id=None, message_thread_id=None, custom_reply_markup=None, override_text=None, override_caption=None):
    reply_markup_to_use = custom_reply_markup if custom_reply_markup is not None else message.reply_markup

    if message.poll:
        async def _send_poll():
            poll = message.poll
            options = [opt.text for opt in poll.options]
            poll_kwargs = {
                "chat_id": chat_id,
                "question": poll.question,
                "options": options,
                "is_anonymous": poll.is_anonymous,
                "type": poll.type,
                "allows_multiple_answers": poll.allows_multiple_answers,
            }
            if poll.type == "quiz":
                if poll.correct_option_id is not None:
                    poll_kwargs["correct_option_id"] = poll.correct_option_id
                if poll.explanation:
                    poll_kwargs["explanation"] = poll.explanation
                    poll_kwargs["explanation_entities"] = poll.explanation_entities
            return await bot.send_poll(**poll_kwargs)
        return await safe_call(_send_poll)

    if override_text is not None or override_caption is not None:
        if message.text:
            async def _send_txt():
                return await bot.send_message(
                    chat_id=chat_id, text=override_text, entities=message.entities,
                    reply_to_message_id=reply_to_channel_msg_id, message_thread_id=message_thread_id,
                    reply_markup=reply_markup_to_use, disable_web_page_preview=False
                )
            return await safe_call(_send_txt)

        if message.photo:
            async def _send_ph():
                return await bot.send_photo(
                    chat_id=chat_id, photo=message.photo[-1].file_id, caption=override_caption,
                    caption_entities=message.caption_entities, reply_to_message_id=reply_to_channel_msg_id,
                    message_thread_id=message_thread_id, reply_markup=reply_markup_to_use
                )
            return await safe_call(_send_ph)

        if message.video:
            async def _send_vid():
                return await bot.send_video(
                    chat_id=chat_id, video=message.video.file_id, caption=override_caption,
                    caption_entities=message.caption_entities, reply_to_message_id=reply_to_channel_msg_id,
                    message_thread_id=message_thread_id, reply_markup=reply_markup_to_use
                )
            return await safe_call(_send_vid)

        if message.document:
            async def _send_doc():
                return await bot.send_document(
                    chat_id=chat_id, document=message.document.file_id, caption=override_caption,
                    caption_entities=message.caption_entities, reply_to_message_id=reply_to_channel_msg_id,
                    message_thread_id=message_thread_id, reply_markup=reply_markup_to_use
                )
            return await safe_call(_send_doc)

        if message.audio or message.voice:
            file_id = message.audio.file_id if message.audio else message.voice.file_id
            async def _send_aud():
                return await bot.send_audio(
                    chat_id=chat_id, audio=file_id, caption=override_caption,
                    caption_entities=message.caption_entities, reply_to_message_id=reply_to_channel_msg_id,
                    message_thread_id=message_thread_id, reply_markup=reply_markup_to_use
                )
            return await safe_call(_send_aud)

    async def _native_copy():
        return await bot.copy_message(
            chat_id=chat_id,
            from_chat_id=message.chat_id,
            message_id=message.message_id,
            reply_to_message_id=reply_to_channel_msg_id,
            message_thread_id=message_thread_id,
            reply_markup=reply_markup_to_use
        )
    return await safe_call(_native_copy)

# Helper function to send album / media groups intact with buttons support
async def send_media_group_intact(bot, target_chat_id, messages_list, message_thread_id=None):
    media_items = []
    markup_to_use = None

    for idx, msg in enumerate(messages_list):
        caption = msg.get("caption") if isinstance(msg, dict) else (msg.caption or None)
        entities = deserialize_entities(msg.get("entities")) if isinstance(msg, dict) else (msg.caption_entities or None)
        buttons = msg.get("buttons") if isinstance(msg, dict) else None
        
        if buttons and idx == len(messages_list) - 1:
            markup_to_use = build_inline_keyboard(buttons)

        if isinstance(msg, dict):
            file_id = msg.get("file_id")
            m_type = msg.get("type")
        else:
            file_id = msg.photo[-1].file_id if msg.photo else (msg.video.file_id if msg.video else (msg.document.file_id if msg.document else (msg.audio.file_id if msg.audio else (msg.voice.file_id if msg.voice else None))))
            m_type = "photo" if msg.photo else ("video" if msg.video else ("document" if msg.document else "audio"))

        if not file_id:
            continue

        if m_type == "photo":
            media_items.append(InputMediaPhoto(media=file_id, caption=caption, caption_entities=entities))
        elif m_type == "video":
            media_items.append(InputMediaVideo(media=file_id, caption=caption, caption_entities=entities))
        elif m_type == "document":
            media_items.append(InputMediaDocument(media=file_id, caption=caption, caption_entities=entities))
        elif m_type == "audio":
            media_items.append(InputMediaAudio(media=file_id, caption=caption, caption_entities=entities))

    if media_items:
        async def _send_group():
            kwargs = {"chat_id": target_chat_id, "media": media_items}
            if message_thread_id:
                kwargs["message_thread_id"] = message_thread_id
            return await bot.send_media_group(**kwargs)
        
        sent_group = await safe_call(_send_group)
        if markup_to_use and sent_group:
            try:
                last_sent_msg = sent_group[-1]
                await bot.edit_message_reply_markup(chat_id=target_chat_id, message_id=last_sent_msg.message_id, reply_markup=markup_to_use)
            except Exception:
                pass
        return sent_group

# Helper function to dispatch custom message list (start or join welcome)
async def deliver_custom_message_list(bot, target_chat_id, user_first_name, custom_msgs):
    for entry in custom_msgs:
        try:
            if isinstance(entry, list):
                await send_media_group_intact(bot, target_chat_id, entry)
            elif isinstance(entry, dict):
                msg_type = entry.get("type")
                file_id = entry.get("file_id")
                caption = entry.get("caption")
                if caption:
                    caption = caption.replace("{name}", f"**{user_first_name}**")
                entities = deserialize_entities(entry.get("entities"))
                buttons = entry.get("buttons")
                markup = build_inline_keyboard(buttons) if buttons else None

                if msg_type == "text":
                    text_content = entry.get("text", "")
                    text_content = text_content.replace("{name}", f"**{user_first_name}**")
                    await bot.send_message(
                        chat_id=target_chat_id,
                        text=text_content,
                        entities=entities,
                        reply_markup=markup,
                        parse_mode="Markdown" if not entities else None
                    )
                elif msg_type == "photo":
                    await bot.send_photo(chat_id=target_chat_id, photo=file_id, caption=caption, caption_entities=entities, reply_markup=markup)
                elif msg_type == "video":
                    await bot.send_video(chat_id=target_chat_id, video=file_id, caption=caption, caption_entities=entities, reply_markup=markup)
                elif msg_type == "document":
                    await bot.send_document(chat_id=target_chat_id, document=file_id, caption=caption, caption_entities=entities, reply_markup=markup)
                elif msg_type == "audio":
                    await bot.send_audio(chat_id=target_chat_id, audio=file_id, caption=caption, caption_entities=entities, reply_markup=markup)
                elif msg_type == "voice":
                    await bot.send_voice(chat_id=target_chat_id, voice=file_id, caption=caption, caption_entities=entities, reply_markup=markup)

            await asyncio.sleep(0.3)
        except Exception as e:
            logger.error(f"Error delivering custom message item: {e}")

# ------------------------------------------------------------------
# Forum Topic Resolver Engine (Pins First Profile Message Automatically)
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

        username_str = f"@{user.username}" if user.username else "No Username"
        profile_card = (
            f"👤 **USER PROFILE INFO**\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"▫️ **Name:** {user.first_name}\n"
            f"▫️ **Username:** {username_str}\n"
            f"▫️ **User ID:** `{user.id}`\n"
            f"━━━━━━━━━━━━━━━━━━"
        )
        contact_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"👤 {user.first_name} (Direct Chat)", url=f"tg://user?id={user.id}")]
        ])
        
        try:
            pinned_msg = await bot.send_message(
                chat_id=ADMIN_GROUP_ID,
                message_thread_id=thread_id,
                text=profile_card,
                reply_markup=contact_markup,
                parse_mode="Markdown"
            )
            await bot.pin_chat_message(
                chat_id=ADMIN_GROUP_ID,
                message_id=pinned_msg.message_id,
                disable_notification=True
            )
        except Exception as err:
            logger.error(f"❌ Error pinning top profile card: {err}")

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
            {"$setOnInsert": {
                "title": chat.title,
                "username": chat.username,
                "welcome_enabled": True,
                "prediction_enabled": True,
                "auto_accept_enabled": True
            }},
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
    
    chat_id = request.chat.id
    ch_doc = channels_collection.find_one({"chat_id": chat_id})
    
    auto_accept = ch_doc.get("auto_accept_enabled", True) if ch_doc else True
    if auto_accept:
        try:
            await request.approve()
            logger.info(f"✅ Approved join request for {request.from_user.id} in {request.chat.title}")
        except Exception as e:
            logger.error(f"Failed to approve join request in {chat_id}: {e}")

    if ch_doc and not ch_doc.get("welcome_enabled", True):
        logger.info(f"🚫 Auto-welcome disabled for channel {request.chat.title} ({chat_id})")
        return

    user = request.from_user
    settings = get_welcome_settings()
    user_first_name = user.first_name or "User"

    users_collection.update_one(
        {"user_id": user.id},
        {"$set": {"user_id": user.id, "first_name": user.first_name, "username": user.username}},
        upsert=True,
    )

    custom_join_msgs = settings.get("custom_join_msgs", [])
    if custom_join_msgs:
        await deliver_custom_message_list(context.bot, user.id, user_first_name, custom_join_msgs)
    else:
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

    logger.info(f"✅ Auto-welcome delivered to {user.first_name} ({user.id}) for channel {request.chat.title}")

# ------------------------------------------------------------------
# Callback Query Handler
# ------------------------------------------------------------------
async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    await query.answer()

    if user.id not in ADMIN_USER_IDS:
        return

    if query.data.startswith("toggle_accept_"):
        ch_id = int(query.data.split("toggle_accept_")[1])
        ch = channels_collection.find_one({"chat_id": ch_id})
        if ch:
            current_st = ch.get("auto_accept_enabled", True)
            channels_collection.update_one({"chat_id": ch_id}, {"$set": {"auto_accept_enabled": not current_st}})
            await show_join_request_channels_menu(query, context, is_edit=True)
        return

    if query.data.startswith("toggle_wel_"):
        ch_id = int(query.data.split("toggle_wel_")[1])
        ch = channels_collection.find_one({"chat_id": ch_id})
        if ch:
            current_st = ch.get("welcome_enabled", True)
            channels_collection.update_one({"chat_id": ch_id}, {"$set": {"welcome_enabled": not current_st}})
            await show_welcome_channels_menu(query, context, is_edit=True)
        return

    if query.data.startswith("toggle_pred_"):
        ch_id = int(query.data.split("toggle_pred_")[1])
        ch = channels_collection.find_one({"chat_id": ch_id})
        if ch:
            current_st = ch.get("prediction_enabled", True)
            channels_collection.update_one({"chat_id": ch_id}, {"$set": {"prediction_enabled": not current_st}})
            await show_prediction_channels_menu(query, context, is_edit=True)
        return

    if query.data == "set_custom_buttons":
        admin_states[user.id] = "awaiting_custom_buttons"
        instr = (
            "🔘 **ADD STYLED & EMOJI ICON BUTTONS**\n\n"
            "Format mein message bhejey:\n\n"
            "`Button 1 : AGENT CHANNEL - https://t.me/+aO4PoFUq5gU4YmNl | icon : 5418063924933173277 | style : success`\n\n"
            "`Button 2 : VKADDAGENCY - https://t.me/+rQ8jUMlvyZozNmE1 | icon : 4990182601252668309 | style : primary`\n\n"
            "`Target: Video`\n\n"
            "*(Style Options: `primary` (Blue), `success` (Green), `danger` (Red))*"
        )
        await query.edit_message_text(instr, parse_mode="Markdown")

# ------------------------------------------------------------------
# Interactive Channel Selectors Helper Functions
# ------------------------------------------------------------------
async def show_join_request_channels_menu(target_obj, context, is_edit=False):
    all_channels = await get_live_active_channels(context.bot)
    if not all_channels:
        msg = "⚠️ Kisi channel me bot Admin nahi hai!"
        if is_edit:
            await target_obj.edit_message_text(msg)
        else:
            await target_obj.message.reply_text(msg)
        return

    keyboard = []
    for ch in all_channels:
        title = ch.get("title", f"Channel {ch['chat_id']}")
        is_enabled = ch.get("auto_accept_enabled", True)
        status_str = "⚡ Auto-Accept: ON" if is_enabled else "🛑 Auto-Accept: OFF (Manual)"
        btn_text = f"{title} | {status_str}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"toggle_accept_{ch['chat_id']}")])

    markup = InlineKeyboardMarkup(keyboard)
    text = (
        "📥 **JOIN REQUEST AUTO-ACCEPT SETTINGS**\n\n"
        "Jis channel me **Instant Auto-Accept** chahiye uspar `⚡ ON` rakhein.\n"
        "Agar **Manually Accept** karna chahte hain toh click karke `🛑 OFF` kar dein:"
    )
    if is_edit:
        await target_obj.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
    else:
        await target_obj.message.reply_text(text, reply_markup=markup, parse_mode="Markdown")

async def show_welcome_channels_menu(target_obj, context, is_edit=False):
    all_channels = await get_live_active_channels(context.bot)
    if not all_channels:
        msg = "⚠️ Kisi channel me bot Admin nahi hai!"
        if is_edit:
            await target_obj.edit_message_text(msg)
        else:
            await target_obj.message.reply_text(msg)
        return

    keyboard = []
    for ch in all_channels:
        title = ch.get("title", f"Channel {ch['chat_id']}")
        is_enabled = ch.get("welcome_enabled", True)
        status_icon = "✅" if is_enabled else "❌"
        btn_text = f"{status_icon} {title}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"toggle_wel_{ch['chat_id']}")])

    markup = InlineKeyboardMarkup(keyboard)
    text = "⚙️ **AUTO-WELCOME CHANNEL SETTINGS**\n\nJis channel me Auto-Welcome ON rakhna hai uspar ✅ click karein, jisme OFF rakhna hai uspar ❌ click karein:"
    if is_edit:
        await target_obj.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
    else:
        await target_obj.message.reply_text(text, reply_markup=markup, parse_mode="Markdown")

async def show_prediction_channels_menu(target_obj, context, is_edit=False):
    all_channels = await get_live_active_channels(context.bot)
    if not all_channels:
        msg = "⚠️ Kisi channel me bot Admin nahi hai!"
        if is_edit:
            await target_obj.edit_message_text(msg)
        else:
            await target_obj.message.reply_text(msg)
        return

    keyboard = []
    for ch in all_channels:
        title = ch.get("title", f"Channel {ch['chat_id']}")
        is_enabled = ch.get("prediction_enabled", True)
        status_icon = "✅" if is_enabled else "❌"
        btn_text = f"{status_icon} {title}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"toggle_pred_{ch['chat_id']}")])

    markup = InlineKeyboardMarkup(keyboard)
    text = "📢 **PREDICTION MODE CHANNEL SETTINGS**\n\nPrediction Mode Active hai! Jinn channels par message post hona chahiye wahan ✅ select rakhein:"
    if is_edit:
        await target_obj.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
    else:
        await target_obj.message.reply_text(text, reply_markup=markup, parse_mode="Markdown")

# ------------------------------------------------------------------
# Admin & User Commands Setup
# ------------------------------------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    users_collection.update_one(
        {"user_id": user.id},
        {"$set": {"user_id": user.id, "first_name": user.first_name, "username": user.username}},
        upsert=True,
    )

    settings = get_welcome_settings()
    custom_msgs = settings.get("custom_start_msgs", [])

    if custom_msgs:
        await deliver_custom_message_list(context.bot, user.id, user.first_name, custom_msgs)
    else:
        await update.message.reply_text(
            f"👋 Welcome {user.first_name}!\nAapka swagat hai. Aap jo bhi message bhejenge, hamari team tak pahunch jayega."
        )

async def update_start_msg_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_USER_IDS:
        return

    admin_states[user.id] = "awaiting_start_msgs_setup"
    admin_start_msg_buffers[user.id] = []

    instr = (
        "✏️ **CUSTOM /START MESSAGE SETUP MODE ACTIVE!**\n\n"
        "Aap jo jo media ya text `/start` dabane par user ko bhejna chahte hain, bhejin.\n"
        "*(Agar kishi message ke sath button lagana ho, toh message ke sath `/iwantaddbutton` likh kar bhejin)*\n\n"
        "Jab saare messages bhej lein, toh niche **`done`** likh kar bhej dein!"
    )
    await update.message.reply_text(instr, parse_mode="Markdown")

async def update_join_request_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_USER_IDS:
        return

    admin_states[user.id] = "awaiting_join_msgs_setup"
    admin_join_msg_buffers[user.id] = []

    instr = (
        "✏️ **CUSTOM /UPDATEJOINREQUEST SETUP MODE ACTIVE!**\n\n"
        "Aap jo jo media ya text Join Request accept hone par user ko welcome me bhejna chahte hain, bhejin.\n"
        "*(Agar kishi message ke sath button lagana ho, toh message ke sath `/iwantaddbutton` likh kar bhejin)*\n\n"
        "Jab saare messages bhej lein, toh niche **`done`** likh kar bhej dein!"
    )
    await update.message.reply_text(instr, parse_mode="Markdown")

async def prediction_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_USER_IDS:
        return
    admin_modes[user.id] = "channel"
    await show_prediction_channels_menu(update, context)

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_USER_IDS:
        return
    admin_modes[user.id] = "user"
    await update.message.reply_text("👥 **User Broadcast Mode Active!**\n\nAb aap jo bhi message DM me bhejenge, woh **Channels me nahi jayega**, sirf Bot ke **Users** ko private chat me jayega.", parse_mode="Markdown")

async def welcome_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_USER_IDS:
        return
    await show_welcome_channels_menu(update, context)

async def joinrequest_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_USER_IDS:
        return
    await show_join_request_channels_menu(update, context)

async def quiz_poll_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_USER_IDS:
        return

    msg_text = update.message.text.strip()
    cmd = "/quiz" if msg_text.lower().startswith("/quiz") else "/poll"
    
    content = msg_text[len(cmd):].strip()
    if not content:
        await update.message.reply_text(f"⚠️ Usage:\n`{cmd} Question | Option 1 | Option 2`", parse_mode="Markdown")
        return

    if "|" in content:
        parts = [p.strip() for p in content.split("|") if p.strip()]
    else:
        lines = [line.strip() for line in content.split("\n") if line.strip()]
        parts = []
        for line in lines:
            cleaned = re.sub(r'^(Question\s*[-:]*|\d+[\.\)]\s*|Option\s*\d*[-:]*)', '', line, flags=re.IGNORECASE).strip()
            if cleaned:
                parts.append(cleaned)

    if cmd == "/poll":
        if len(parts) < 3:
            await update.message.reply_text("⚠️ `/poll Question | Option 1 | Option 2` Format me bhejey!")
            return
        question = parts[0]
        options = parts[1:]
        is_quiz = False
        correct_id = None
    else:
        if len(parts) < 4:
            await update.message.reply_text("⚠️ `/quiz Question | Option 1 | Option 2 | 1` Format me bhejey!")
            return
        try:
            correct_id = int(parts[-1]) - 1
            question = parts[0]
            options = parts[1:-1]
            is_quiz = True
        except ValueError:
            await update.message.reply_text("⚠️ Quiz me last part correct option index hona chahiye!")
            return

    current_mode = admin_modes.get(user.id, "channel")

    if current_mode == "channel":
        active_channels = [ch for ch in (await get_live_active_channels(context.bot)) if ch.get("prediction_enabled", True) != False]
        if not active_channels:
            await update.message.reply_text("⚠️ Koi bhi channel Prediction ke liye selected nahi hai!")
            return

        async def send_to_ch(ch):
            ch_id = ch["chat_id"]
            try:
                poll_kwargs = {
                    "chat_id": ch_id,
                    "question": question,
                    "options": options,
                    "is_anonymous": True,
                    "type": "quiz" if is_quiz else "regular",
                }
                if is_quiz and correct_id is not None:
                    poll_kwargs["correct_option_id"] = correct_id
                await context.bot.send_poll(**poll_kwargs)
                return True
            except Exception:
                channels_collection.delete_one({"chat_id": ch_id})
                return False

        results = await asyncio.gather(*(send_to_ch(ch) for ch in active_channels))
        success = sum(1 for r in results if r)

        prefix_icon = "💡" if is_quiz else "📊"
        msg_type = "Quiz" if is_quiz else "Poll"
        await update.message.reply_text(f"{prefix_icon} {msg_type} broadcasted to {success}/{len(active_channels)} Selected Channels!")

    else:
        all_users = list(users_collection.find({"user_id": {"$nin": ADMIN_USER_IDS}}))
        success = 0
        queue = asyncio.Queue()

        for u in all_users:
            await queue.put(u["user_id"])

        async def poll_worker():
            nonlocal success
            while not queue.empty():
                u_id = await queue.get()
                try:
                    poll_kwargs = {
                        "chat_id": u_id,
                        "question": question,
                        "options": options,
                        "is_anonymous": True,
                        "type": "quiz" if is_quiz else "regular",
                    }
                    if is_quiz and correct_id is not None:
                        poll_kwargs["correct_option_id"] = correct_id
                    await context.bot.send_poll(**poll_kwargs)
                    success += 1
                    await asyncio.sleep(0.03)
                except Exception:
                    pass
                finally:
                    queue.task_done()

        workers = [asyncio.create_task(poll_worker()) for _ in range(12)]
        await queue.join()
        for w in workers:
            w.cancel()

        prefix_icon = "💡" if is_quiz else "📊"
        msg_type = "Quiz" if is_quiz else "Poll"
        await update.message.reply_text(f"{prefix_icon} {msg_type} broadcasted to {success}/{len(all_users)} Users!")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_USER_IDS:
        return

    total_users = users_collection.count_documents({"user_id": {"$nin": ADMIN_USER_IDS}})
    live_channels = await get_live_active_channels(context.bot)
    total_channels = len(live_channels)
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
# Execution Helper for Dynamic Button Broadcast Dispatch
# ------------------------------------------------------------------
async def dispatch_broadcast_with_markup(context: ContextTypes.DEFAULT_TYPE, admin_id: int, message, custom_markup, override_text=None, override_caption=None):
    current_mode = admin_modes.get(admin_id, "channel")

    if current_mode == "channel":
        active_channels = [ch for ch in (await get_live_active_channels(context.bot)) if ch.get("prediction_enabled", True) != False]
        if not active_channels:
            await context.bot.send_message(chat_id=admin_id, text="⚠️ Koi bhi channel Prediction ke liye selected nahi hai!")
            return

        reply_mapping = None
        if message.reply_to_message:
            replied_admin_msg_id = message.reply_to_message.message_id
            mapping_doc = mappings_collection.find_one({"admin_msg_id": replied_admin_msg_id})
            if mapping_doc:
                reply_mapping = mapping_doc.get("channels", {})

        sent_channels = {}

        async def dispatch_channel(ch):
            ch_id = ch["chat_id"]
            reply_to_msg_id = reply_mapping.get(str(ch_id)) if reply_mapping else None
            try:
                sent_msg = await send_clean_with_entities(
                    context.bot, ch_id, message, reply_to_channel_msg_id=reply_to_msg_id,
                    custom_reply_markup=custom_markup, override_text=override_text, override_caption=override_caption
                )
                if sent_msg:
                    return str(ch_id), sent_msg.message_id
            except Exception as e:
                logger.error(f"Failed to post in channel {ch_id}: {e}")
                channels_collection.delete_one({"chat_id": ch_id})
            return None

        results = await asyncio.gather(*(dispatch_channel(ch) for ch in active_channels))
        for res in results:
            if res:
                ch_id_str, msg_id = res
                sent_channels[ch_id_str] = msg_id

        if sent_channels:
            mappings_collection.insert_one({
                "admin_msg_id": message.message_id,
                "channels": sent_channels,
                "created_at": datetime.now(timezone.utc)
            })

        await context.bot.send_message(chat_id=admin_id, text=f"📢 Broadcast (With Buttons) sent to {len(sent_channels)}/{len(active_channels)} Selected Channels!")

    else:
        all_users = list(users_collection.find({"user_id": {"$nin": ADMIN_USER_IDS}}))
        success = 0
        cleaned_users = 0
        queue = asyncio.Queue()

        for u in all_users:
            await queue.put(u["user_id"])

        async def broadcast_worker():
            nonlocal success, cleaned_users
            while not queue.empty():
                u_id = await queue.get()
                try:
                    await send_clean_with_entities(
                        context.bot, u_id, message, custom_reply_markup=custom_markup,
                        override_text=override_text, override_caption=override_caption
                    )
                    success += 1
                    await asyncio.sleep(0.03)
                except (Forbidden, BadRequest):
                    users_collection.delete_one({"user_id": u_id})
                    cleaned_users += 1
                except Exception as e:
                    logger.error(f"User broadcast failed for {u_id}: {e}")
                finally:
                    queue.task_done()

        workers = [asyncio.create_task(broadcast_worker()) for _ in range(15)]
        await queue.join()
        for w in workers:
            w.cancel()

        clean_msg = f" 🗑️ ({cleaned_users} Inactive users cleaned)" if cleaned_users > 0 else ""
        await context.bot.send_message(chat_id=admin_id, text=f"👥 Broadcast (With Buttons) sent to {success}/{len(all_users)} Users!{clean_msg}")

# ------------------------------------------------------------------
# MEDIA GROUP / ALBUM BATCH DISPATCH WORKER (UNIVERSAL INTACT)
# ------------------------------------------------------------------
async def process_media_group_buffer(media_group_id, context):
    await asyncio.sleep(0.8) # Wait for all album items to buffer
    msg_list = media_group_buffers.pop(media_group_id, [])
    if not msg_list:
        return

    first_msg = msg_list[0]
    user = first_msg.from_user
    chat = first_msg.chat

    # 1. IF ADMIN IS IN /updatestartmsg SETUP MODE
    if user.id in ADMIN_USER_IDS and admin_states.get(user.id) == "awaiting_start_msgs_setup":
        album_items = []
        for msg in msg_list:
            item = {}
            if msg.photo:
                item = {"type": "photo", "file_id": msg.photo[-1].file_id, "caption": msg.caption, "entities": serialize_entities(msg.caption_entities)}
            elif msg.video:
                item = {"type": "video", "file_id": msg.video.file_id, "caption": msg.caption, "entities": serialize_entities(msg.caption_entities)}
            elif msg.document:
                item = {"type": "document", "file_id": msg.document.file_id, "caption": msg.caption, "entities": serialize_entities(msg.caption_entities)}
            elif msg.audio:
                item = {"type": "audio", "file_id": msg.audio.file_id, "caption": msg.caption, "entities": serialize_entities(msg.caption_entities)}
            if item:
                album_items.append(item)
        if album_items:
            admin_start_msg_buffers[user.id].append(album_items)
            await context.bot.send_message(chat_id=user.id, text=f"📥 Album ({len(album_items)} items) added as a single unit to /start buffer! Aur bhejin ya **`done`** likhein.")
        return

    # 2. IF ADMIN IS IN /updatejoinrequest SETUP MODE
    if user.id in ADMIN_USER_IDS and admin_states.get(user.id) == "awaiting_join_msgs_setup":
        album_items = []
        for msg in msg_list:
            item = {}
            if msg.photo:
                item = {"type": "photo", "file_id": msg.photo[-1].file_id, "caption": msg.caption, "entities": serialize_entities(msg.caption_entities)}
            elif msg.video:
                item = {"type": "video", "file_id": msg.video.file_id, "caption": msg.caption, "entities": serialize_entities(msg.caption_entities)}
            elif msg.document:
                item = {"type": "document", "file_id": msg.document.file_id, "caption": msg.caption, "entities": serialize_entities(msg.caption_entities)}
            elif msg.audio:
                item = {"type": "audio", "file_id": msg.audio.file_id, "caption": msg.caption, "entities": serialize_entities(msg.caption_entities)}
            if item:
                album_items.append(item)
        if album_items:
            admin_join_msg_buffers[user.id].append(album_items)
            await context.bot.send_message(chat_id=user.id, text=f"📥 Album ({len(album_items)} items) added as a single unit to Join Request buffer! Aur bhejin ya **`done`** likhein.")
        return

    # 3. FORUM ROUTING FOR USER CHAT ALBUMS
    if user.id not in ADMIN_USER_IDS:
        if ADMIN_GROUP_ID != 0:
            thread_id = await get_or_create_user_topic(context.bot, user)
            if thread_id:
                try:
                    await send_media_group_intact(context.bot, ADMIN_GROUP_ID, msg_list, message_thread_id=thread_id)
                    return
                except Exception as e:
                    logger.error(f"Failed to route album to topic: {e}")
        return

    # 4. ADMIN BROADCAST / PREDICTION ALBUM ROUTING
    if chat.id != ADMIN_GROUP_ID and user.id in ADMIN_USER_IDS:
        current_mode = admin_modes.get(user.id, "channel")
        if current_mode == "channel":
            active_channels = [ch for ch in (await get_live_active_channels(context.bot)) if ch.get("prediction_enabled", True) != False]
            for ch in active_channels:
                try:
                    await send_media_group_intact(context.bot, ch["chat_id"], msg_list)
                except Exception as e:
                    logger.error(f"Failed to post album to channel {ch['chat_id']}: {e}")
            await context.bot.send_message(chat_id=user.id, text=f"📢 Album Broadcast sent to {len(active_channels)} Selected Channels!")
        else:
            all_users = list(users_collection.find({"user_id": {"$nin": ADMIN_USER_IDS}}))
            success = 0
            for u in all_users:
                try:
                    await send_media_group_intact(context.bot, u["user_id"], msg_list)
                    success += 1
                    await asyncio.sleep(0.05)
                except Exception:
                    pass
            await context.bot.send_message(chat_id=user.id, text=f"👥 Album Broadcast sent to {success}/{len(all_users)} Users!")

# ------------------------------------------------------------------
# Main Message Handler Logic
# ------------------------------------------------------------------
async def handle_all_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return

    # Auto-clean system pin notification if generated
    if message.pinned_message and message.chat.id == ADMIN_GROUP_ID:
        try:
            await message.delete()
        except Exception:
            pass
        return

    user = update.effective_user
    chat = update.effective_chat

    # 1. Media Group (Album) Detection and Buffering
    if message.media_group_id:
        mg_id = message.media_group_id
        if mg_id not in media_group_buffers:
            media_group_buffers[mg_id] = [message]
            asyncio.create_task(process_media_group_buffer(mg_id, context))
        else:
            media_group_buffers[mg_id].append(message)
        return

    # 2. Check if Admin is in Setup Mode awaiting buttons for a pending setup message
    if user.id in ADMIN_USER_IDS and admin_states.get(user.id) in ["awaiting_start_msg_buttons", "awaiting_join_msg_buttons"]:
        if message.text:
            target_setup_state = admin_states.pop(user.id, None)
            pending_msg_info = pending_setup_messages.pop(user.id, None)
            
            if not pending_msg_info:
                await message.reply_text("⚠️ Koi pending message nahi mila. Kripya fir se koshish karein.")
                return

            parsed_btns, _ = parse_buttons_text(message.text)
            if parsed_btns:
                pending_msg_info["buttons"] = parsed_btns
                
                if target_setup_state == "awaiting_start_msg_buttons":
                    admin_start_msg_buffers[user.id].append(pending_msg_info)
                    await message.reply_text("✅ Message with Custom Buttons added to `/start` buffer! Aur messages bhejin ya **`done`** likhein.", parse_mode="Markdown")
                elif target_setup_state == "awaiting_join_msg_buttons":
                    admin_join_msg_buffers[user.id].append(pending_msg_info)
                    await message.reply_text("✅ Message with Custom Buttons added to Join Request buffer! Aur messages bhejin ya **`done`** likhein.", parse_mode="Markdown")
            else:
                await message.reply_text("⚠️ Buttons format galat tha! Kripya exact format me bhejey.")
            return

    # 3. Custom Start Message Setup Mode Interception
    if user.id in ADMIN_USER_IDS and admin_states.get(user.id) == "awaiting_start_msgs_setup":
        if message.text and message.text.strip().lower() == "done":
            admin_states.pop(user.id, None)
            buffered = admin_start_msg_buffers.pop(user.id, [])
            update_welcome_setting({"custom_start_msgs": buffered})
            await message.reply_text(f"✅ Successful! `{len(buffered)}` Custom Units/Messages saved for `/start` command!", parse_mode="Markdown")
            return

        msg_has_trigger = False
        clean_text = None
        clean_caption = None
        if message.text and "/iwantaddbutton" in message.text:
            msg_has_trigger = True
            clean_text = message.text.replace("/iwantaddbutton", "").strip()
        elif message.caption and "/iwantaddbutton" in message.caption:
            msg_has_trigger = True
            clean_caption = message.caption.replace("/iwantaddbutton", "").strip()

        item = {}
        if message.text:
            item = {"type": "text", "text": clean_text if msg_has_trigger else message.text, "entities": serialize_entities(message.entities)}
        elif message.photo:
            item = {"type": "photo", "file_id": message.photo[-1].file_id, "caption": clean_caption if msg_has_trigger else message.caption, "entities": serialize_entities(message.caption_entities)}
        elif message.video:
            item = {"type": "video", "file_id": message.video.file_id, "caption": clean_caption if msg_has_trigger else message.caption, "entities": serialize_entities(message.caption_entities)}
        elif message.document:
            item = {"type": "document", "file_id": message.document.file_id, "caption": clean_caption if msg_has_trigger else message.caption, "entities": serialize_entities(message.caption_entities)}
        elif message.audio:
            item = {"type": "audio", "file_id": message.audio.file_id, "caption": clean_caption if msg_has_trigger else message.caption, "entities": serialize_entities(message.caption_entities)}
        elif message.voice:
            item = {"type": "voice", "file_id": message.voice.file_id, "caption": clean_caption if msg_has_trigger else message.caption, "entities": serialize_entities(message.caption_entities)}

        if item:
            if msg_has_trigger:
                pending_setup_messages[user.id] = item
                admin_states[user.id] = "awaiting_start_msg_buttons"
                instr = (
                    "🔘 **ADD CUSTOM STYLED BUTTONS FOR THIS START MESSAGE**\n\n"
                    "Message hold kar liya gaya hai! Ab buttons ka format bhejey:\n\n"
                    "`Button 1 : AGENT CHANNEL - https://t.me/+aO4PoFUq5gU4YmNl | icon : 5418063924933173277 | style : success`\n\n"
                    "`Button 2 : VKADDAGENCY - https://t.me/+rQ8jUMlvyZozNmE1 | icon : 4990182601252668309 | style : primary`"
                )
                await message.reply_text(instr, parse_mode="Markdown")
            else:
                admin_start_msg_buffers[user.id].append(item)
                await message.reply_text("📥 Message added to buffer! Aur messages bhejin ya finished hone par **`done`** likhein.", parse_mode="Markdown")
        return

    # 4. Custom Join Request Welcome Setup Mode Interception
    if user.id in ADMIN_USER_IDS and admin_states.get(user.id) == "awaiting_join_msgs_setup":
        if message.text and message.text.strip().lower() == "done":
            admin_states.pop(user.id, None)
            buffered = admin_join_msg_buffers.pop(user.id, [])
            update_welcome_setting({"custom_join_msgs": buffered})
            await message.reply_text(f"✅ Successful! `{len(buffered)}` Custom Units/Messages saved for Join Request Welcome!", parse_mode="Markdown")
            return

        msg_has_trigger = False
        clean_text = None
        clean_caption = None
        if message.text and "/iwantaddbutton" in message.text:
            msg_has_trigger = True
            clean_text = message.text.replace("/iwantaddbutton", "").strip()
        elif message.caption and "/iwantaddbutton" in message.caption:
            msg_has_trigger = True
            clean_caption = message.caption.replace("/iwantaddbutton", "").strip()

        item = {}
        if message.text:
            item = {"type": "text", "text": clean_text if msg_has_trigger else message.text, "entities": serialize_entities(message.entities)}
        elif message.photo:
            item = {"type": "photo", "file_id": message.photo[-1].file_id, "caption": clean_caption if msg_has_trigger else message.caption, "entities": serialize_entities(message.caption_entities)}
        elif message.video:
            item = {"type": "video", "file_id": message.video.file_id, "caption": clean_caption if msg_has_trigger else message.caption, "entities": serialize_entities(message.caption_entities)}
        elif message.document:
            item = {"type": "document", "file_id": message.document.file_id, "caption": clean_caption if msg_has_trigger else message.caption, "entities": serialize_entities(message.caption_entities)}
        elif message.audio:
            item = {"type": "audio", "file_id": message.audio.file_id, "caption": clean_caption if msg_has_trigger else message.caption, "entities": serialize_entities(message.caption_entities)}
        elif message.voice:
            item = {"type": "voice", "file_id": message.voice.file_id, "caption": clean_caption if msg_has_trigger else message.caption, "entities": serialize_entities(message.caption_entities)}

        if item:
            if msg_has_trigger:
                pending_setup_messages[user.id] = item
                admin_states[user.id] = "awaiting_join_msg_buttons"
                instr = (
                    "🔘 **ADD CUSTOM STYLED BUTTONS FOR THIS JOIN WELCOME MESSAGE**\n\n"
                    "Message hold kar liya gaya hai! Ab buttons ka format bhejey:\n\n"
                    "`Button 1 : AGENT CHANNEL - https://t.me/+aO4PoFUq5gU4YmNl | icon : 5418063924933173277 | style : success`\n\n"
                    "`Button 2 : VKADDAGENCY - https://t.me/+rQ8jUMlvyZozNmE1 | icon : 4990182601252668309 | style : primary`"
                )
                await message.reply_text(instr, parse_mode="Markdown")
            else:
                admin_join_msg_buffers[user.id].append(item)
                await message.reply_text("📥 Message added to buffer! Aur messages bhejin ya finished hone par **`done`** likhein.", parse_mode="Markdown")
        return

    # 5. Normal Broadcast / Prediction Button Interception (/iwantaddbutton outside setup mode)
    msg_has_trigger = False
    if message.text and "/iwantaddbutton" in message.text:
        msg_has_trigger = True
    elif message.caption and "/iwantaddbutton" in message.caption:
        msg_has_trigger = True

    if user.id in ADMIN_USER_IDS and msg_has_trigger:
        pending_broadcast_messages[user.id] = message
        admin_states[user.id] = "awaiting_broadcast_buttons"

        instr = (
            "🔘 **ADD CUSTOM STYLED BUTTONS FOR THIS BROADCAST/PREDICTION**\n\n"
            "Aapka message hold kar liya gaya hai! Ab buttons ka format bhejey:\n\n"
            "`Button 1 : AGENT CHANNEL - https://t.me/+aO4PoFUq5gU4YmNl | icon : 5418063924933173277 | style : success`\n\n"
            "`Button 2 : VKADDAGENCY - https://t.me/+rQ8jUMlvyZozNmE1 | icon : 4990182601252668309 | style : primary`\n\n"
            "*(Style Options: `primary` (Blue), `success` (Green), `danger` (Red))*"
        )
        await message.reply_text(instr, parse_mode="Markdown")
        return

    # Admin Settings Inputs & Custom Button Interception
    if user.id in ADMIN_USER_IDS and user.id in admin_states:
        state = admin_states.pop(user.id)

        if state == "awaiting_broadcast_buttons" and message.text:
            pending_msg = pending_broadcast_messages.pop(user.id, None)
            if not pending_msg:
                await message.reply_text("⚠️ Koi pending message nahi mila. Kripya fir se koshish karein.")
                return

            parsed_btns, _ = parse_buttons_text(message.text)
            if parsed_btns:
                custom_markup = build_inline_keyboard(parsed_btns)
                
                clean_text = None
                clean_caption = None
                if pending_msg.text and "/iwantaddbutton" in pending_msg.text:
                    clean_text = pending_msg.text.replace("/iwantaddbutton", "").strip()
                elif pending_msg.caption and "/iwantaddbutton" in pending_msg.caption:
                    clean_caption = pending_msg.caption.replace("/iwantaddbutton", "").strip()

                await dispatch_broadcast_with_markup(
                    context, user.id, pending_msg, custom_markup,
                    override_text=clean_text, override_caption=clean_caption
                )
            else:
                await message.reply_text("⚠️ Buttons format galat tha! Kripya exact format me bhejey.")
            return

        if state == "awaiting_custom_buttons" and message.text:
            parsed_btns, target_media = parse_buttons_text(message.text)
            if parsed_btns:
                db_key = f"{target_media}_buttons"
                update_welcome_setting({db_key: parsed_btns})
                await message.reply_text(f"✅ `{len(parsed_btns)}` Styled Buttons successfully set for **{target_media.upper()}** message!", parse_mode="Markdown")
            else:
                await message.reply_text("⚠️ Buttons format galat tha! Kripya exact format me bhejey.")
            return

    # 6. MESSAGE FROM ADMIN FORUM GROUP -> ROUTE TO USER
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

    # 7. MESSAGE FROM NORMAL USER -> ROUTE TO USER'S FORUM TOPIC
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

    # NORMAL DIRECT BROADCAST/PREDICTION FLOW
    current_mode = admin_modes.get(user.id, "channel")

    if current_mode == "channel":
        active_channels = [ch for ch in (await get_live_active_channels(context.bot)) if ch.get("prediction_enabled", True) != False]
        if not active_channels:
            await message.reply_text("⚠️ Koi bhi channel Prediction ke liye selected nahi hai!")
            return

        reply_mapping = None
        if message.reply_to_message:
            replied_admin_msg_id = message.reply_to_message.message_id
            mapping_doc = mappings_collection.find_one({"admin_msg_id": replied_admin_msg_id})
            if mapping_doc:
                reply_mapping = mapping_doc.get("channels", {})

        sent_channels = {}

        async def dispatch_channel(ch):
            ch_id = ch["chat_id"]
            reply_to_msg_id = reply_mapping.get(str(ch_id)) if reply_mapping else None
            try:
                sent_msg = await send_clean_with_entities(
                    context.bot, ch_id, message, reply_to_channel_msg_id=reply_to_msg_id
                )
                if sent_msg:
                    return str(ch_id), sent_msg.message_id
            except Exception as e:
                logger.error(f"Failed to post in channel {ch_id}: {e}")
                channels_collection.delete_one({"chat_id": ch_id})
            return None

        results = await asyncio.gather(*(dispatch_channel(ch) for ch in active_channels))
        for res in results:
            if res:
                ch_id_str, msg_id = res
                sent_channels[ch_id_str] = msg_id

        if sent_channels:
            mappings_collection.insert_one({
                "admin_msg_id": message.message_id,
                "channels": sent_channels,
                "created_at": datetime.now(timezone.utc)
            })

        await message.reply_text(f"📢 Broadcast sent to {len(sent_channels)}/{len(active_channels)} Selected Channels!")

    else:
        all_users = list(users_collection.find({"user_id": {"$nin": ADMIN_USER_IDS}}))
        success = 0
        cleaned_users = 0
        queue = asyncio.Queue()

        for u in all_users:
            await queue.put(u["user_id"])

        async def broadcast_worker():
            nonlocal success, cleaned_users
            while not queue.empty():
                u_id = await queue.get()
                try:
                    await send_clean_with_entities(context.bot, u_id, message)
                    success += 1
                    await asyncio.sleep(0.03)
                except (Forbidden, BadRequest):
                    users_collection.delete_one({"user_id": u_id})
                    cleaned_users += 1
                except Exception as e:
                    logger.error(f"User broadcast failed for {u_id}: {e}")
                finally:
                    queue.task_done()

        workers = [asyncio.create_task(broadcast_worker()) for _ in range(15)]
        await queue.join()
        for w in workers:
            w.cancel()

        clean_msg = f" 🗑️ ({cleaned_users} Inactive users cleaned)" if cleaned_users > 0 else ""
        await message.reply_text(f"👥 Broadcast sent to {success}/{len(all_users)} Users!{clean_msg}")

# ------------------------------------------------------------------
# Main Entry Point
# ------------------------------------------------------------------
main():
    if not TOKEN:
        logger.error("BOT_TOKEN missing. System exiting...")
        return

    keep_alive()

    app_bot = ApplicationBuilder().token(TOKEN).build()

    app_bot.add_handler(CommandHandler("start", start_command))
    app_bot.add_handler(CommandHandler("updatestartmsg", update_start_msg_command))
    app_bot.add_handler(CommandHandler("updatejoinrequest", update_join_request_command))
    app_bot.add_handler(CommandHandler("prediction", prediction_command))
    app_bot.add_handler(CommandHandler("broadcast", broadcast_command))
    app_bot.add_handler(CommandHandler("welcome", welcome_command))
    app_bot.add_handler(CommandHandler("joinrequest", joinrequest_command))
    app_bot.add_handler(CommandHandler("quiz", quiz_poll_command))
    app_bot.add_handler(CommandHandler("poll", quiz_poll_command))
    app_bot.add_handler(CommandHandler("stats", stats_command))
    app_bot.add_handler(CommandHandler("del", delete_broadcast))

    app_bot.add_handler(ChatJoinRequestHandler(handle_join_request))
    app_bot.add_handler(CallbackQueryHandler(handle_callback_query))

    app_bot.add_handler(ChatMemberHandler(track_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app_bot.add_handler(MessageHandler((filters.ALL | filters.POLL) & ~filters.COMMAND, handle_all_messages))

    async def post_init(application):
        await setup_bot_commands(application)

    app_bot.post_init = post_init

    logger.info("🤖 VIP High-Speed Prediction & Broadcast Bot is running...")
    
    app_bot.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
