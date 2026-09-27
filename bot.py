import logging
import os
from threading import Thread
from flask import Flask
import asyncio
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
TOKEN = os.environ.get("BOT_TOKEN", "8864401575:AAGa2k4LD_aeP_kgZbTUAoEFVDzfve3zUiI")
MONGO_URI = os.environ.get("MONGO_URI", "mongodb+srv://predictionbot:raja0001@predictionbot.nbttlvr.mongodb.net/telegram_broadcast_bot?retryWrites=true&w=majority&appName=Predictionbot")

# Admin IDs
ADMIN_USER_IDS = [6829195326, 5785924075]

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
        
        async def delete_single_msg(ch_str_id, ch_msg_id):
            chat_id = int(ch_str_id)
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=ch_msg_id)
                return True
            except Exception as e:
                logging.error(f"Failed to delete in channel {chat_id}: {e}")
                return False

        # Parallel deletion for max speed
        tasks = [delete_single_msg(ch_id, msg_id) for ch_id, msg_id in channel_msg_map.items()]
        results = await asyncio.gather(*tasks)
        deleted_count = sum(1 for r in results if r)

        mappings_collection.delete_one({"admin_msg_id": replied_msg_id})
        await message.reply_text(f"🗑️ Sabhi channels se message delete kar diya gaya hai! ({deleted_count} channels)")
    else:
        await message.reply_text("⚠️ Yeh message kisi broadcast record mein nahi mila.")


# --- 3. Core Delivery Engine (Guaranteed Premium Emojis) ---
async def deliver_message(bot, chat_id, message):
    """
    forward_message is the ONLY method in Telegram API that preserves
    100% custom/animated premium emojis without converting them to standard text.
    """
    return await bot.forward_message(
        chat_id=chat_id,
        from_chat_id=message.chat_id,
        message_id=message.message_id
    )


# --- 4. High-Speed Instant Parallel Broadcast ---
async def handle_broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if user.id not in ADMIN_USER_IDS:
        return

    message = update.message
    all_channels = list(channels_collection.find({}))

    if not all_channels:
        await message.reply_text("⚠️ Pehle kisi channel mein bot ko admin banayein, koi channel connected nahi hai!")
        return

    # --- Case A: Reply Threading ---
    if message.reply_to_message:
        replied_msg_id = message.reply_to_message.message_id
        mapping = mappings_collection.find_one({"admin_msg_id": replied_msg_id})

        if mapping:
            channel_msg_map = mapping["channels"]

            async def send_reply_task(ch_str_id, ch_msg_id):
                chat_id = int(ch_str_id)
                try:
                    await deliver_message(
                        bot=context.bot,
                        chat_id=chat_id,
                        message=message
                    )
                    return True
                except Exception as e:
                    logging.error(f"Failed to reply in channel {chat_id}: {e}")
                    return False

            tasks = [send_reply_task(ch_str_id, ch_msg_id) for ch_str_id, ch_msg_id in channel_msg_map.items()]
            results = await asyncio.gather(*tasks)

            success_count = sum(1 for r in results if r)
            fail_count = len(results) - success_count

            await message.reply_text(
                f"✅ **Reply Sent in Channels!**\nSuccess: `{success_count}` | Failed: `{fail_count}`", 
                parse_mode="Markdown"
            )
            return
        else:
            await message.reply_text("⚠️ Yeh message kisi broadcast post ka reply nahi hai, normal broadcast kar raha hoon.")

    # --- Case B: Fresh Instant Broadcast ---
    async def send_broadcast_task(ch):
        chat_id = ch["chat_id"]
        try:
            sent_msg = await deliver_message(
                bot=context.bot,
                chat_id=chat_id,
                message=message
            )
            return str(chat_id), sent_msg.message_id
        except Exception as e:
            logging.error(f"Failed to send to channel {chat_id}: {e}")
            if "bot was kicked" in str(e).lower() or "chat not found" in str(e).lower():
                channels_collection.delete_one({"chat_id": chat_id})
            return str(chat_id), None

    # Instant Parallel Broadcast across all channels at once
    tasks = [send_broadcast_task(ch) for ch in all_channels]
    results = await asyncio.gather(*tasks)

    channel_mapping_data = {}
    success_count = 0
    fail_count = 0

    for ch_id, msg_id in results:
        if msg_id:
            channel_mapping_data[ch_id] = msg_id
            success_count += 1
        else:
            fail_count += 1

    if channel_mapping_data:
        mappings_collection.insert_one({
            "admin_msg_id": message.message_id,
            "channels": channel_mapping_data
        })

    await message.reply_text(
        f"✅ **Broadcast Done!**\nSent to: `{success_count}` channels | Failed: `{fail_count}`", 
        parse_mode="Markdown"
    )


def main():
    keep_alive()

    application = ApplicationBuilder().token(TOKEN).build()

    application.add_handler(ChatMemberHandler(track_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    application.add_handler(CommandHandler("del", delete_broadcast))
    
    handler = MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, handle_broadcast_message)
    application.add_handler(handler)

    print("Prediction Broadcast Bot (High-Speed + Premium Emojis Preserved) is running...")
    
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
