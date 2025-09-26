#!/usr/bin/env python3
# Logobot - Telegram watermark/logo-adding bot (final + DM notification)

import os
import logging
import tempfile
from typing import Dict, Any, Optional
from datetime import datetime

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from PIL import Image, UnidentifiedImageError

# ---------- CONFIG ----------
BOT_TOKEN = "7991395304:AAE2ESbgzYfLFsIYiJ9d0AS_oqMW6wgAMdM"
MY_ID = 6640947043  # Your Telegram numeric user ID for DM notifications
# ----------------------------

USER_STATE: Dict[int, Dict[str, Any]] = {}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (
        "👋 Hello — welcome to *Logobot*!\n\n"
        "How it works:\n"
        "1. Send the image where you want the logo added (base image).\n"
        "2. I'll ask for the *logo* — send an image, photo, or sticker.\n"
        "3. I'll add the logo at top-left (smaller size + padding) and return the result.\n\n"
        "Commands:\n"
        "/owner — see who runs this bot.\n"
        "/cancel — cancel current operation.\n\n"
        "Send the first image when you're ready."
    )
    await update.message.reply_text(text, parse_mode="Markdown")

    # Notify you in DM
    now = datetime.now()
    dt_text = now.strftime("%d-%m-%Y")
    tm_text = now.strftime("%H:%M:%S")
    msg_to_me = (
        f"**{user.first_name}** started the bot.\n"
        f"UserID: {user.id}\n"
        f"Date: {dt_text}\n"
        f"Time: {tm_text}"
    )
    try:
        await context.bot.send_message(chat_id=MY_ID, text=msg_to_me, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Failed to notify owner: {e}")


async def owner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("@tslm9")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = USER_STATE.pop(user_id, None)
    if state:
        for p in (state.get("orig"), state.get("logo")):
            try:
                if p and os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass
    await update.message.reply_text("Cancelled. You can start again by sending a new image.")


def _unique_temp_path(suffix: str) -> str:
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    try:
        os.remove(path)
    except Exception:
        pass
    return path


async def _download_image(update: Update) -> Optional[str]:
    """Downloads photo/document/sticker, returns filepath or None"""
    msg = update.message
    if not msg:
        return None

    # Photo
    if msg.photo:
        file = await msg.photo[-1].get_file()
        path = _unique_temp_path(".jpg")
        await file.download_to_drive(custom_path=path)
        return path

    # Document (image)
    if msg.document:
        doc = msg.document
        mime = (doc.mime_type or "").lower()
        name = (doc.file_name or "").lower()
        if mime.startswith("image") or any(name.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".webp", ".bmp")):
            ext = "." + (name.split(".")[-1] if "." in name else "jpg")
            path = _unique_temp_path(ext)
            file = await doc.get_file()
            await file.download_to_drive(custom_path=path)
            return path

    # Sticker
    if msg.sticker:
        file = await msg.sticker.get_file()
        path = _unique_temp_path(".webp")
        await file.download_to_drive(custom_path=path)
        try:
            # Convert WebP sticker to PNG
            im = Image.open(path).convert("RGBA")
            png_path = _unique_temp_path(".png")
            im.save(png_path, "PNG")
            os.remove(path)
            return png_path
        except Exception as e:
            logger.error(f"Failed to convert sticker: {e}")
            return None

    return None


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    incoming_path = await _download_image(update)
    if not incoming_path:
        await update.message.reply_text("Send an image, sticker, or photo to start.")
        return

    state = USER_STATE.get(user_id)

    # If awaiting logo, treat incoming as logo
    if state and state.get("stage") == "awaiting_logo":
        orig_path = state.get("orig")
        logo_path = incoming_path
        output_path = _unique_temp_path(".jpg")
        await update.message.reply_text("Processing...")

        try:
            base_img = Image.open(orig_path).convert("RGBA")
            logo_img = Image.open(logo_path).convert("RGBA")
        except UnidentifiedImageError:
            await update.message.reply_text("Couldn't read one of the images. Make sure both files are valid.")
            for p in (logo_path,):
                try:
                    if p and os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass
            return
        except Exception as e:
            logger.exception("Error opening images")
            await update.message.reply_text(f"Unexpected error: {e}")
            return

        try:
            # Resize logo ~12% of base width
            target_w = max(1, int(base_img.width * 0.12))
            ratio = target_w / logo_img.width
            new_w = target_w
            new_h = max(1, int(logo_img.height * ratio))
            logo_resized = logo_img.resize((new_w, new_h), Image.LANCZOS)

            # Paste top-left with 3% padding
            padding = int(max(15, base_img.width * 0.03))
            position = (padding, padding)
            canvas = Image.new("RGBA", base_img.size)
            canvas.paste(base_img, (0, 0))
            canvas.paste(logo_resized, position, logo_resized)  # alpha mask

            # Save as JPEG
            canvas.convert("RGB").save(output_path, "JPEG", quality=95)

            # Send result
            with open(output_path, "rb") as f:
                await update.message.reply_photo(photo=f)

        except Exception as e:
            logger.exception("Error processing images")
            await update.message.reply_text(f"Failed: {e}")

        finally:
            # cleanup
            for p in (orig_path, logo_path, output_path):
                try:
                    if p and os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass
            USER_STATE.pop(user_id, None)

        return

    # Otherwise, treat incoming image/sticker as new base image
    # Clean old state if exists
    if user_id in USER_STATE:
        old_state = USER_STATE.pop(user_id)
        for p in (old_state.get("orig"), old_state.get("logo")):
            try:
                if p and os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass

    # Store new base image
    USER_STATE[user_id] = {"stage": "awaiting_logo", "orig": incoming_path}
    await update.message.reply_text(
        "Got the base image ✅\nNow send the *logo* (image, photo, or sticker).",
        parse_mode="Markdown",
    )


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("I don't understand that. Send an image, sticker, or photo to start.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(msg="Exception while handling an update:", exc_info=context.error)


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("owner", owner))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL | filters.Sticker.ALL, message_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown))
    app.add_error_handler(error_handler)
    print("Logobot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
