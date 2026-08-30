import logging
import os
from threading import Thread
from flask import Flask
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    CommandHandler,
    ChatMemberHandler,
    filters,
)
from pymongo import MongoClient

# Logging setup
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

# --- Flask Server (Render Port Timeout Fix) ---
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive and running 24x7!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_flask)
    t.start()


# --- Configuration ---
TOKEN = "8864401575:AAGa2k4LD_aeP_kgZbTUAoEFVDzfve3zUiI"
ADMIN_USER_IDS = [6829195326, 5785924075]

MONGO_URI = "mongodb+srv://predictionbot:raja0001@predictionbot.nbttlvr.mongodb.net/telegram_broadcast_bot?retryWrites=true&w=majority&appName=Predictionbot"

client = MongoClient(MONGO_URI)
db = client["telegram_broadcast_bot"]
channels_collection = db["active_channels"]
mappings_collection = db["broadcast_mappings"]


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
    if user.id not in ADMIN_USER_IDS:
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


# --- Helper Function: Send / Reply preserving Premium Custom Emojis & No Forward Tag ---
async def send_or_reply_content(bot, chat_id, message, reply_to_channel_msg_id=None):
    # Agar message mein sirf text (aur custom emojis/entities) hain
    if message.text:
        return await bot.send_message(
            chat_id=chat_id,
            text=message.text,
            entities=message.entities,  # Yeh line saare custom premium animated emojis ko retain rakhti hai!
            reply_to_message_id=reply_to_channel_msg_id,
            disable_web_page_preview=message.disable_web_page_preview if hasattr(message, 'disable_web_page_preview') else False
        )
    
    # Agar message mein photo hai
    elif message.photo:
        photo_file = message.photo[-1].file_id
        return await bot.send_photo(
            chat_id=chat_id,
            photo=photo_file,
            caption=message.caption,
            caption_entities=message.caption_entities,  # Photo ke caption ke premium emojis ke liye
            reply_to_message_id=reply_to_channel_msg_id
        )

    # Agar message mein video hai
    elif message.video:
        video_file = message.video.file_id
        return await bot.send_video(
            chat_id=chat_id,
            video=video_file,
            caption=message.caption,
            caption_entities=message.caption_entities,
            reply_to_message_id=reply_to_channel_msg_id
        )

    # Agar koi aur media type ho (jaise voice note / audio / document) toh safe fallback copy_message use karega
    else:
        return await bot.copy_message(
            chat_id=chat_id,
            from_chat_id=message.chat_id,
            message_id=message.message_id,
            reply_to_message_id=reply_to_channel_msg_id
        )


# --- 3. Broadcast & Reply-Threading Logic ---
async def handle_broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if user.id not in ADMIN_USER_IDS:
        return

    message = update.message
    all_channels = list(channels_collection.find({}))

    if not all_channels:
        await message.reply_text("⚠️ Pehle kisi channel mein bot ko admin banayein, koi channel connected nahi hai!")
        return

    success_count = 0
    fail_count = 0

    # --- Case A: Reply Threading ---
    if message.reply_to_message:
        replied_msg_id = message.reply_to_message.message_id
        mapping = mappings_collection.find_one({"admin_msg_id": replied_msg_id})

        if mapping:
            channel_msg_map = mapping["channels"]
            for ch_str_id, ch_msg_id in channel_msg_map.items():
                chat_id = int(ch_str_id)
                try:
                    await send_or_reply_content(
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
            sent_msg = await send_or_reply_content(
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


def main():
    keep_alive()

    application = ApplicationBuilder().token(TOKEN).build()

    application.add_handler(ChatMemberHandler(track_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    application.add_handler(CommandHandler("del", delete_broadcast))
    
    handler = MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, handle_broadcast_message)
    application.add_handler(handler)

    print("Advanced Reply-Threading & Deletion Bot is running...")
    
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
