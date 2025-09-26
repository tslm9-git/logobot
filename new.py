#!/usr/bin/env python3
"""
Logobot (bulk) - Telegram watermark bot (Termux-ready)

Flow:
1. User sends one or more images -> bot stores them.
2. User sends text "confirm" -> bot replies "Now send the logo".
3. User sends logo (image/file or static sticker) -> bot processes stored images,
   overlays logo at top-left (moderate size + padding), sends processed images back.
4. /cancel clears the current batch.
"""

import os
import tempfile
import shutil
import logging
import asyncio
import subprocess
from datetime import datetime
from typing import Dict, List, Optional

from PIL import Image, UnidentifiedImageError
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---------------- CONFIG ----------------
BOT_TOKEN = "8007455843:AAFcynxZRrZqwdqb4Ik7hFkPIkwiRFvMWYk"
OWNER_ID = 6640947043  # owner to notify on /start
# ----------------------------------------

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Per-user transient storage
# Structure: user_state[user_id] = {"images": [local_paths], "confirmed": False, "waiting_logo": False}
user_state: Dict[int, Dict] = {}


# ---------- Helpers ----------
def _unique_temp_path(suffix: str) -> str:
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    try:
        os.remove(path)
    except Exception:
        pass
    return path


async def download_photo_or_document(file_obj, dest_path: str):
    """
    Downloads a telegram File object to dest_path (async).
    """
    await file_obj.download_to_drive(custom_path=dest_path)


def try_convert_webp_to_png(webp_path: str) -> Optional[str]:
    """
    Attempt to convert .webp to .png.
    First tries Pillow; if Pillow can't open webp, tries dwebp (if available).
    Returns png path or None on failure.
    """
    png_path = _unique_temp_path(".png")
    # Try Pillow
    try:
        im = Image.open(webp_path).convert("RGBA")
        im.save(png_path, "PNG")
        return png_path
    except Exception as e:
        logger.debug("Pillow couldn't open webp (%s), trying dwebp: %s", webp_path, e)

    # Try dwebp fallback (external binary)
    try:
        res = subprocess.run(["dwebp", webp_path, "-o", png_path], capture_output=True, text=True, timeout=15)
        if res.returncode == 0 and os.path.exists(png_path):
            return png_path
        else:
            logger.error("dwebp failed: %s / stdout: %s", res.stderr, res.stdout)
    except Exception as e:
        logger.error("dwebp conversion failed: %s", e)

    # Cleanup on failure
    try:
        if os.path.exists(png_path):
            os.remove(png_path)
    except Exception:
        pass
    return None


def cleanup_files(paths: List[str]):
    for p in paths:
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except Exception:
            pass


def ensure_user_state(uid: int):
    if uid not in user_state:
        user_state[uid] = {"images": [], "confirmed": False, "waiting_logo": False}


# ---------- Command handlers ----------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user_state(user.id)
    text = (
        f"👋 Hello {user.first_name}!\n\n"
        "Send all the images you want watermarking.\n"
        "When finished, send the text: <code>confirm</code>\n"
        "I will then ask you to send the <b>logo</b> (image file, photo or static sticker).\n"
        "After you send the logo I'll return all the processed images.\n\n"
        "Commands:\n"
        "/owner - show owner\n"
        "/cancel - cancel and clear current batch\n\n"
        "Stickers (static) are supported as logos.\n"
    )
    # Plain text with minimal HTML tags should be fine; to be safe we'll send plain text
    await update.message.reply_text(text)

    # Notify owner
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    notify = (
        f"{user.first_name} (@{user.username or 'N/A'}) started Logobot\n"
        f"UserID: {user.id}\nDateTime: {now}"
    )
    try:
        await context.bot.send_message(chat_id=OWNER_ID, text=notify)
    except Exception as e:
        logger.warning("Failed to notify owner: %s", e)


async def owner_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👑 Bot owner: @tslm9")


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state = user_state.pop(uid, None)
    if state:
        cleanup_files(state.get("images", []))
        # logo files not stored long-term here
    await update.message.reply_text("Cancelled and cleared your pending images.")


# ---------- Message handlers ----------
async def handle_image_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles incoming photos or image documents as base images (before confirm).
    """
    uid = update.effective_user.id
    ensure_user_state(uid)

    # If user already confirmed and waiting for logo, instruct them
    if user_state[uid]["waiting_logo"]:
        await update.message.reply_text("You already confirmed. Please send the logo now or /cancel to abort.")
        return

    # Get file object (photo or document)
    file_obj = None
    suffix = ".jpg"
    if update.message.photo:
        file_obj = await update.message.photo[-1].get_file()
        suffix = ".jpg"
    elif update.message.document and (update.message.document.mime_type or "").lower().startswith("image"):
        file_obj = await update.message.document.get_file()
        # preserve extension if available
        fn = (update.message.document.file_name or "").lower()
        if "." in fn:
            suffix = "." + fn.rsplit(".", 1)[1]
        else:
            suffix = ".jpg"
    else:
        # Not an image; ignore here
        return

    dest = _unique_temp_path(suffix)
    try:
        await download_photo_or_document(file_obj, dest)
        user_state[uid]["images"].append(dest)
        user_state[uid]["confirmed"] = False
        await update.message.reply_text(f"✅ Image saved ({len(user_state[uid]['images'])} total). Send more or send 'confirm' when done.")
    except Exception as e:
        logger.exception("Failed to download user image")
        if os.path.exists(dest):
            os.remove(dest)
        await update.message.reply_text("⚠️ Failed to save image. Try again.")


async def handle_sticker_or_document_as_logo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles sticker or image sent as logo when waiting_logo == True.
    If not waiting_logo, and sticker received, inform user stickers are used as logos only.
    """
    uid = update.effective_user.id
    ensure_user_state(uid)

    # If not confirmed yet:
    if not user_state[uid]["confirmed"]:
        # if they send a sticker but didn't confirm images, instruct them
        await update.message.reply_text("You haven't confirmed the image batch yet. Send all images, then 'confirm'.")
        return

    if not user_state[uid]["waiting_logo"]:
        await update.message.reply_text("Please wait for the bot to ask for the logo (send 'confirm' first).")
        return

    # Determine logo file: photo, document image, or static sticker
    logo_tmp: Optional[str] = None
    try:
        if update.message.photo:
            file_obj = await update.message.photo[-1].get_file()
            logo_tmp = _unique_temp_path(".png")
            await download_photo_or_document(file_obj, logo_tmp)
        elif update.message.document and (update.message.document.mime_type or "").lower().startswith("image"):
            file_obj = await update.message.document.get_file()
            # save as suffix from filename or png
            fn = (update.message.document.file_name or "").lower()
            suffix = ".png"
            if "." in fn:
                suffix = "." + fn.rsplit(".", 1)[1]
            logo_tmp = _unique_temp_path(suffix)
            await download_photo_or_document(file_obj, logo_tmp)
        elif update.message.sticker and (not update.message.sticker.is_animated):
            # static sticker; download .webp then convert
            file_obj = await update.message.sticker.get_file()
            webp_path = _unique_temp_path(".webp")
            await download_photo_or_document(file_obj, webp_path)
            converted = try_convert_webp_to_png(webp_path)
            # remove webp
            try:
                if os.path.exists(webp_path):
                    os.remove(webp_path)
            except Exception:
                pass
            if converted:
                logo_tmp = converted
            else:
                await update.message.reply_text("⚠️ Couldn't process sticker as logo (webp conversion failed). Install libwebp and reinstall Pillow, or install dwebp.")
                return
        else:
            await update.message.reply_text("Send an image file, photo, or a static sticker as the logo.")
            return

        # Now we have logo_tmp path; process all stored images
        await update.message.reply_text("Processing all images... This may take a moment depending on number/size.")

        processed_paths: List[str] = []
        for idx, base_path in enumerate(list(user_state[uid]["images"]), start=1):
            out_path = _unique_temp_path(".jpg")
            try:
                base_img = Image.open(base_path).convert("RGBA")
                logo_img = Image.open(logo_tmp).convert("RGBA")

                # Resize logo to 12% of base width (tweakable)
                target_w = max(1, int(base_img.width * 0.12))
                ratio = target_w / float(logo_img.width)
                new_w = target_w
                new_h = max(1, int(logo_img.height * ratio))
                logo_resized = logo_img.resize((new_w, new_h), Image.LANCZOS)

                # Padding 3% of width, min 15px
                padding = int(max(15, base_img.width * 0.03))
                position = (padding, padding)

                # Composite
                canvas = Image.new("RGBA", base_img.size)
                canvas.paste(base_img, (0, 0))
                canvas.paste(logo_resized, position, logo_resized)

                # Save JPEG
                canvas.convert("RGB").save(out_path, "JPEG", quality=92)
                processed_paths.append(out_path)

                # Send processed image (one-by-one)
                with open(out_path, "rb") as f:
                    await update.message.reply_photo(photo=f, caption=f"Image {idx} / {len(user_state[uid]['images'])}")
                # small short delay to avoid flooding
                await asyncio.sleep(0.25)

            except UnidentifiedImageError:
                logger.exception("Pillow couldn't identify base or logo image")
                await update.message.reply_text("⚠️ Error processing one of the images (unrecognized format). Skipping it.")
                if os.path.exists(out_path):
                    os.remove(out_path)
            except Exception as e:
                logger.exception("Error while processing image")
                if os.path.exists(out_path):
                    os.remove(out_path)
                await update.message.reply_text("⚠️ Failed processing an image; continuing with others.")

        # Cleanup: all base + processed + logo_tmp
        cleanup_files(user_state[uid]["images"])
        cleanup_files(processed_paths)
        try:
            if logo_tmp and os.path.exists(logo_tmp):
                os.remove(logo_tmp)
        except Exception:
            pass

        # Reset state
        user_state[uid] = {"images": [], "confirmed": False, "waiting_logo": False}
        await update.message.reply_text("✅ Done. All images processed and sent. Batch cleared.")

    except Exception as e:
        logger.exception("Unexpected error in logo handling")
        if logo_tmp and os.path.exists(logo_tmp):
            os.remove(logo_tmp)
        await update.message.reply_text("⚠️ Unexpected error while processing logo. Try again.")


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles text messages; specifically watches for 'confirm' (case-insensitive)
    """
    uid = update.effective_user.id
    txt = (update.message.text or "").strip().lower()

    if txt == "confirm":
        ensure_user_state(uid)
        if not user_state[uid]["images"]:
            await update.message.reply_text("You haven't sent any images yet. Send images first.")
            return
        if user_state[uid]["confirmed"]:
            await update.message.reply_text("You already confirmed. Please send the logo now.")
            return
        # mark confirmed and waiting for logo
        user_state[uid]["confirmed"] = True
        user_state[uid]["waiting_logo"] = True
        await update.message.reply_text("Confirmed. Now send the logo (image file, photo, or static sticker).")
        return

    # If user types /cancel or other commands are handled elsewhere; ignore other texts
    return


# ---------- Main ----------
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("owner", owner_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))

    # Photos & image documents (base images)
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_image_message))

    # Stickers & images used when logo phase
    # We'll direct sticker/document/photo to logo handler if the user is waiting_logo
    # but since handlers are global, we put sticker handler that checks state inside
    app.add_handler(MessageHandler(filters.Sticker.ALL, handle_sticker_or_document_as_logo))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_sticker_or_document_as_logo), group=1)

    # Text messages (confirm)
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text_message))

    logger.info("Starting Logobot (bulk watermark) ...")
    app.run_polling()


if __name__ == "__main__":
    main()
