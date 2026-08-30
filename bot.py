import os
import logging
import asyncio
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
from pymongo import MongoClient
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ChatJoinRequestHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# Logging Setup
logging.basicConfig(level=logging.INFO)

# ==================== CONFIGURATION ====================
BOT_TOKEN = "8864401575:AAGa2k4LD_aeP_kgZbTUAoEFVDzfve3zUiI" 

# Multiple Admins Support
ADMIN_IDS = [6829195326, 5785924075]

# MongoDB Atlas URI
MONGO_URI = "mongodb+srv://predictionbot:raja0001@predictionbot.nbttlvr.mongodb.net/telegram_broadcast_bot?retryWrites=true&w=majority&appName=Predictionbot"

# Source Chat & Message IDs (Agar zaroorat ho)
SOURCE_CHAT_ID = 5785924075
# =======================================================

# --- MONGODB SETUP ---
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["telegram_broadcast_bot"]
channels_collection = db["active_channels"]
mappings_collection = db["broadcast_mappings"]
users_collection = db["users"]


def save_user_to_mongo(user_id, first_name, username):
    try:
        users_collection.update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "user_id": user_id,
                    "first_name": first_name,
                    "username": username
                }
            },
            upsert=True
        )
    except Exception as e:
        logging.error(f"MongoDB Error: {e}")


# --- KEEP-ALIVE WEB SERVER (Fixed for UptimeRobot) ---
class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()
        self.wfile.write(bytes("<html><body><h1>Bot is Live and MongoDB Connected!</h1></body></html>", "utf-8"))

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()
    
    def log_message(self, format, *args):
        return


def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), SimpleHTTPRequestHandler)
    server.serve_forever()


# --- 1. Dynamic Channel Tracking ---
async def track_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = update.my_chat_member
    if not result:
        return

    chat = result.chat
    if chat.type != "channel":
        return

    new_status = result.new_chat_member.status

    if new_status in ["administrator", "creator"]:
        channels_collection.update_one(
            {"chat_id": chat.id},
            {"$set": {"title": chat.title, "username": chat.username}},
            upsert=True,
        )
        logging.info(f"Added channel: {chat.title} ({chat.id})")

    elif new_status in ["left", "kicked", "member"]:
        channels_collection.delete_one({"chat_id": chat.id})
        logging.info(f"Removed channel: {chat.title} ({chat.id})")


# --- 2. Delete Logic (/del command) ---
async def delete_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        return

    message = update.message

    if not message.reply_to_message:
        await message.reply_text("⚠️ Kripya us broadcast kiye gaye message par reply karke `/del` likhein jise delete karna hai.")
        return

    replied_msg_id = message.reply_to_message.message_id
    mapping = mappings_collection.find_one({"admin_msg_id": replied_msg_id})

    if mapping:
        channel_msg_map = mapping["channels"]
        deleted_count = 0

        for ch_str_id, ch_msg_id in channel_msg_map.items():
            chat_id = int(ch_str_id)
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=ch_msg_id)
                deleted_count += 1
            except Exception as e:
                logging.error(f"Failed to delete in channel {chat_id}: {e}")

        mappings_collection.delete_one({"admin_msg_id": replied_msg_id})
        await message.reply_text(f"🗑️ Sabhi channels se message delete kar diya gaya hai! ({deleted_count} channels)")
    else:
        await message.reply_text("⚠️ Yeh message kisi broadcast record mein nahi mila.")


# --- 3. Core Sender Function (Ensures Animated Icons & No Forward Tag) ---
async def send_custom_content(bot, chat_id, message, reply_to_channel_msg_id=None):
    """
    Jaise aapke doosre working bot mein hota hai: yeh function text, photo, video, 
    aur unke custom animated emoji entities (`entities` / `caption_entities`) ko 
    bina kisi 'Forwarded from' tag ke direct clean bhejta hai.
    """
    if message.text:
        return await bot.send_message(
            chat_id=chat_id,
            text=message.text,
            entities=message.entities,  # Custom animated emojis retain karne ke liye
            reply_to_message_id=reply_to_channel_msg_id,
            disable_web_page_preview=message.disable_web_page_preview if hasattr(message, 'disable_web_page_preview') else False
        )
    elif message.photo:
        return await bot.send_photo(
            chat_id=chat_id,
            photo=message.photo[-1].file_id,
            caption=message.caption,
            caption_entities=message.caption_entities,  # Photo caption ke animated emojis ke liye
            reply_to_message_id=reply_to_channel_msg_id
        )
    elif message.video:
        return await bot.send_video(
            chat_id=chat_id,
            video=message.video.file_id,
            caption=message.caption,
            caption_entities=message.caption_entities,
            reply_to_message_id=reply_to_channel_msg_id
        )
    elif message.audio:
        return await bot.send_audio(
            chat_id=chat_id,
            audio=message.audio.file_id,
            caption=message.caption,
            caption_entities=message.caption_entities,
            reply_to_message_id=reply_to_channel_msg_id
        )
    elif message.voice:
        return await bot.send_voice(
            chat_id=chat_id,
            voice=message.voice.file_id,
            caption=message.caption,
            caption_entities=message.caption_entities,
            reply_to_message_id=reply_to_channel_msg_id
        )
    elif message.document:
        return await bot.send_document(
            chat_id=chat_id,
            document=message.document.file_id,
            caption=message.caption,
            caption_entities=message.caption_entities,
            reply_to_message_id=reply_to_channel_msg_id
        )
    else:
        # Fallback agar koi aur complex media ho
        return await bot.copy_message(
            chat_id=chat_id,
            from_chat_id=message.chat_id,
            message_id=message.message_id,
            reply_to_message_id=reply_to_channel_msg_id
        )


# --- 4. Broadcast & Reply-Threading Logic ---
async def handle_broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if user.id not in ADMIN_IDS:
        return

    message = update.message
    all_channels = list(channels_collection.find({}))

    if not all_channels:
        await message.reply_text("⚠️ Pehle kisi channel mein bot ko admin banayein, koi channel connected nahi hai!")
        return

    success_count = 0
    fail_count = 0

    # --- Case A: Reply Threading (Purane message ka reply) ---
    if message.reply_to_message:
        replied_msg_id = message.reply_to_message.message_id
        mapping = mappings_collection.find_one({"admin_msg_id": replied_msg_id})

        if mapping:
            channel_msg_map = mapping["channels"]
            for ch_str_id, ch_msg_id in channel_msg_map.items():
                chat_id = int(ch_str_id)
                try:
                    await send_custom_content(
                        bot=context.bot,
                        chat_id=chat_id,
                        message=message,
                        reply_to_channel_msg_id=int(ch_msg_id)
                    )
                    success_count += 1
                except Exception as e:
                    logging.error(f"Failed to reply in channel {chat_id}: {e}")
                    fail_count += 1

            if fail_count > 0:
                await message.reply_text(f"⚠️ Reply sent with errors!\n❌ Failed in {fail_count} channels.")
            return
        else:
            await message.reply_text("⚠️ Yeh message kisi broadcast post ka reply nahi hai, normal broadcast kar raha hoon.")

    # --- Case B: Fresh Broadcast Message ---
    channel_mapping_data = {}

    for ch in all_channels:
        chat_id = ch["chat_id"]
        try:
            sent_msg = await send_custom_content(
                bot=context.bot,
                chat_id=chat_id,
                message=message
            )
            channel_mapping_data[str(chat_id)] = sent_msg.message_id
            success_count += 1
        except Exception as e:
            logging.error(f"Failed to send to channel {chat_id}: {e}")
            fail_count += 1
            if "bot was kicked" in str(e).lower() or "chat not found" in str(e).lower():
                channels_collection.delete_one({"chat_id": chat_id})

    if channel_mapping_data:
        mappings_collection.insert_one({
            "admin_msg_id": message.message_id,
            "channels": channel_mapping_data
        })

    if fail_count > 0:
        await message.reply_text(f"⚠️ Broadcast completed, but failed in {fail_count} channels.")


# --- STATS COMMAND ---
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id in ADMIN_IDS:
        total_users = users_collection.count_documents({})
        total_channels = channels_collection.count_documents({})
        await update.message.reply_text(f"📊 **Total Users:** `{total_users}`\n📢 **Total Connected Channels:** `{total_channels}`", parse_mode="Markdown")


def main():
    Thread(target=run_web_server, daemon=True).start()

    application = ApplicationBuilder().token(BOT_TOKEN).build()

    application.add_handler(ChatMemberHandler(track_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    application.add_handler(CommandHandler("del", delete_broadcast))
    application.add_handler(CommandHandler("stats", stats))
    
    # Admin broadcast / reply handler
    handler = MessageHandler(filters.User(ADMIN_IDS) & filters.ChatType.PRIVATE & ~filters.COMMAND, handle_broadcast_message)
    application.add_handler(handler)

    print("Channel Broadcast & Reply-Threading Bot with Working Entity Logic is running...")
    
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
