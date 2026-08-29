import logging
import os
from threading import Thread
from flask import Flask
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    ChatMemberHandler,
    filters,
)
from pymongo import MongoClient

# Logging setup
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

# --- Flask Server (Render Port Timeout Fix karne ke liye) ---
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
ADMIN_USER_IDS = [6829195326]

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


# --- 2. Broadcast & Reply-Threading Logic ---
async def handle_broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if user.id not in ADMIN_USER_IDS:
        return

    message = update.message
    all_channels = list(channels_collection.find({}))

    if not all_channels:
        await message.reply_text("Pehle kisi channel mein bot ko admin banayein, abhi koi channel connected nahi hai!")
        return

    success_count = 0
    fail_count = 0

    # Reply Threading Logic
    if message.reply_to_message:
        replied_msg_id = message.reply_to_message.message_id
        mapping = mappings_collection.find_one({"admin_msg_id": replied_msg_id})

        if mapping:
            channel_msg_map = mapping["channels"]
            for ch_str_id, ch_msg_id in channel_msg_map.items():
                chat_id = int(ch_str_id)
                try:
                    await message.copy(chat_id=chat_id, reply_to_message_id=ch_msg_id)
                    success_count += 1
                except Exception as e:
                    logging.error(f"Failed to reply in channel {chat_id}: {e}")
                    fail_count += 1

            await message.reply_text(f"Reply Broadcasted!\n✅ Success: {success_count}\n❌ Failed: {fail_count}")
            return
        else:
            await message.reply_text("⚠️ Yeh message kisi broadcast post ka reply nahi lag raha hai, isliye normal broadcast bhej raha hoon.")

    # Fresh Broadcast Message
    channel_mapping_data = {}

    for ch in all_channels:
        chat_id = ch["chat_id"]
        try:
            sent_msg = await message.copy(chat_id=chat_id)
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

    await message.reply_text(
        f"Broadcast Complete!\n✅ Success: {success_count}\n❌ Failed: {fail_count}"
    )


def main():
    # Pehle Flask server ko background mein start karo taaki Render port open mil jaye
    keep_alive()

    application = ApplicationBuilder().token(TOKEN).build()

    # Handlers add karein
    application.add_handler(ChatMemberHandler(track_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    
    handler = MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, handle_broadcast_message)
    application.add_handler(handler)

    print("Advanced Reply-Threading Broadcast Bot is running...")
    
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
