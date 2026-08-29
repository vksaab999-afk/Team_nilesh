import logging
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

# --- Configuration ---
TOKEN = "8864401575:AAGa2k4LD_aeP_kgZbTUAoEFVDzfve3zUiI"

# Aapke Admin IDs (Filhal aapki ID set hai, baad mein aur add kar sakte hain)
ADMIN_USER_IDS = [6829195326]

# Correct MongoDB Atlas URI (Password aur Database name ke sath)
MONGO_URI = "mongodb+srv://predictionbot:raja0001@predictionbot.nbttlvr.mongodb.net/telegram_broadcast_bot?retryWrites=true&w=majority&appName=Predictionbot"

client = MongoClient(MONGO_URI)
db = client["telegram_broadcast_bot"]
channels_collection = db["active_channels"]
mappings_collection = db["broadcast_mappings"]  # Reply mapping ke liye collection


# --- 1. Dynamic Channel Tracking (Admin bante hi save/remove hona) ---
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

    # Security check: Sirf authorized admins hi message bhej sakein
    if user.id not in ADMIN_USER_IDS:
        return

    message = update.message
    all_channels = list(channels_collection.find({}))

    if not all_channels:
        await message.reply_text("Pehle kisi channel mein bot ko admin banayein, abhi koi channel connected nahi hai!")
        return

    success_count = 0
    fail_count = 0

    # --- Case A: Agar yeh message kisi purane message ka REPLY hai ---
    if message.reply_to_message:
        replied_msg_id = message.reply_to_message.message_id
        
        # MongoDB se check karo ki yeh original message kin channel message IDs se map tha
        mapping = mappings_collection.find_one({"admin_msg_id": replied_msg_id})

        if mapping:
            channel_msg_map = mapping["channels"]  # {"chat_id": channel_msg_id}
            
            for ch_str_id, ch_msg_id in channel_msg_map.items():
                chat_id = int(ch_str_id)
                try:
                    # Channels par bhi exact usi message ID ke reply mein bhej do
                    await message.copy(chat_id=chat_id, reply_to_message_id=ch_msg_id)
                    success_count += 1
                except Exception as e:
                    logging.error(f"Failed to reply in channel {chat_id}: {e}")
                    fail_count += 1

            await message.reply_text(f"Reply Broadcasted!\n✅ Success: {success_count}\n❌ Failed: {fail_count}")
            return
        else:
            await message.reply_text("⚠️ Yeh message kisi broadcast post का reply nahi lag raha hai, isliye normal broadcast ki tarah bhej raha hoon.")

    # --- Case B: Naya Fresh Broadcast Message ---
    channel_mapping_data = {}

    for ch in all_channels:
        chat_id = ch["chat_id"]
        try:
            # Message copy karo aur return mein mili channel message ID ko save karo
            sent_msg = await message.copy(chat_id=chat_id)
            channel_mapping_data[str(chat_id)] = sent_msg.message_id
            success_count += 1
        except Exception as e:
            logging.error(f"Failed to send to channel {chat_id}: {e}")
            fail_count += 1
            if "bot was kicked" in str(e).lower() or "chat not found" in str(e).lower():
                channels_collection.delete_one({"chat_id": chat_id})

    # Agar kam se kam ek channel par bhi gaya hai, toh mapping database mein save karo
    if channel_mapping_data:
        mappings_collection.insert_one({
            "admin_msg_id": message.message_id,
            "channels": channel_mapping_data
        })

    await message.reply_text(
        f"Broadcast Complete!\n✅ Success: {success_count}\n❌ Failed: {fail_count}"
    )


def main():
    application = ApplicationBuilder().token(TOKEN).build()

    # Handler 1: Track channels where bot becomes admin
    application.add_handler(ChatMemberHandler(track_chat_member, chat_member_type=ChatMemberHandler.MY_CHAT_MEMBER))

    # Handler 2: Handle incoming posts and replies from admin in private chat
    handler = MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, handle_broadcast_message)
    application.add_handler(handler)

    print("Advanced Reply-Threading Broadcast Bot is running...")
    application.run_polling()


if __name__ == "__main__":
    main()
