#!/usr/bin/env python3
"""
Student Video Upload Telegram Bot
- Students pick class → pick name → upload video
- Video is compressed with FFmpeg before uploading to Google Drive
- Runs on Railway / Render / any cloud host (free tier)
"""

import os
import logging
import subprocess
import tempfile
import asyncio
from pathlib import Path
from datetime import datetime

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Config (set these as environment variables) ───────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GOOGLE_DRIVE_FOLDER_ID = os.environ["GOOGLE_DRIVE_FOLDER_ID"]  # Root folder in Drive

# ─── Conversation states ───────────────────────────────────────────────────────
CHOOSE_CLASS, CHOOSE_NAME, UPLOAD_VIDEO = range(3)

# ─── Classes & Students — Edit this to match your real classes/students ────────
CLASSES = {
    "Class A – Grade 7": [
        "Sophea Meas",
        "Dara Kosal",
        "Sreymom Chan",
        "Virak Phan",
        "Bopha Lim",
    ],
    "Class B – Grade 8": [
        "Ratanak Sok",
        "Chanthy Heng",
        "Piseth Ros",
        "Sreila Nhem",
        "Kosal Thy",
    ],
    "Class C – Grade 9": [
        "Makara Pen",
        "Sothea Kim",
        "Darina Yim",
        "Phearun Ouk",
        "Chenda Pov",
    ],
}

# ─── Video compression settings ───────────────────────────────────────────────
# Target resolution: 480p (good balance of quality vs size)
# CRF 28 = smaller file, CRF 23 = better quality. Range: 18 (best) – 35 (smallest)
FFMPEG_SETTINGS = {
    "scale": "854:480",       # 480p — change to "1280:720" for 720p
    "crf": "28",              # Compression quality
    "preset": "fast",         # Encoding speed (ultrafast/fast/medium/slow)
    "audio_bitrate": "64k",   # Audio quality
}

# ─── Google Drive setup ────────────────────────────────────────────────────────
def get_drive_service():
    """Build Google Drive API service using service account credentials."""
    import json

    creds_json = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    creds_dict = json.loads(creds_json)

    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/drive"],
    )
    return build("drive", "v3", credentials=creds)


def get_or_create_folder(service, name: str, parent_id: str) -> str:
    """Find an existing Drive folder or create it if missing."""
    query = (
        f"name='{name}' and mimeType='application/vnd.google-apps.folder' "
        f"and '{parent_id}' in parents and trashed=false"
    )
    results = service.files().list(q=query, fields="files(id)").execute()
    files = results.get("files", [])
    if files:
        return files[0]["id"]

    metadata = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    folder = service.files().create(body=metadata, fields="id").execute()
    return folder["id"]


def upload_to_drive(local_path: str, filename: str, class_name: str, student_name: str) -> str:
    """Upload a file to Google Drive under Class > Student folder."""
    service = get_drive_service()

    # Create nested folders: Root → Class → Student
    class_folder_id = get_or_create_folder(service, class_name, GOOGLE_DRIVE_FOLDER_ID)
    student_folder_id = get_or_create_folder(service, student_name, class_folder_id)

    media = MediaFileUpload(local_path, mimetype="video/mp4", resumable=True)
    file_metadata = {"name": filename, "parents": [student_folder_id]}

    uploaded = (
        service.files()
        .create(body=file_metadata, media_body=media, fields="id,webViewLink")
        .execute()
    )
    return uploaded.get("webViewLink", "")


# ─── Video compression ─────────────────────────────────────────────────────────
def compress_video(input_path: str, output_path: str) -> bool:
    """Compress video to 480p using FFmpeg. Returns True on success."""
    scale = FFMPEG_SETTINGS["scale"]
    crf = FFMPEG_SETTINGS["crf"]
    preset = FFMPEG_SETTINGS["preset"]
    audio_br = FFMPEG_SETTINGS["audio_bitrate"]

    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-vf", f"scale={scale}:force_original_aspect_ratio=decrease,pad={scale}:(ow-iw)/2:(oh-ih)/2",
        "-c:v", "libx264",
        "-crf", crf,
        "-preset", preset,
        "-c:a", "aac",
        "-b:a", audio_br,
        "-movflags", "+faststart",
        output_path,
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            logger.error(f"FFmpeg error: {result.stderr}")
            return False
        return True
    except subprocess.TimeoutExpired:
        logger.error("FFmpeg timed out")
        return False
    except FileNotFoundError:
        logger.error("FFmpeg not found — install it on your host")
        return False


# ─── Bot handlers ──────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point: show class selection keyboard."""
    context.user_data.clear()

    keyboard = [
        [InlineKeyboardButton(cls, callback_data=f"class:{cls}")]
        for cls in CLASSES
    ]
    await update.message.reply_text(
        "👋 *Welcome!*\n\nPlease select your class:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return CHOOSE_CLASS


async def choose_class(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle class selection, show student list."""
    query = update.callback_query
    await query.answer()

    selected_class = query.data.split("class:", 1)[1]
    context.user_data["class"] = selected_class

    students = CLASSES.get(selected_class, [])
    keyboard = [
        [InlineKeyboardButton(name, callback_data=f"name:{name}")]
        for name in students
    ]
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="back:class")])

    await query.edit_message_text(
        f"✅ Class: *{selected_class}*\n\nNow select your name:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return CHOOSE_NAME


async def choose_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle name selection, ask for video upload."""
    query = update.callback_query
    await query.answer()

    if query.data == "back:class":
        # Go back to class selection
        keyboard = [
            [InlineKeyboardButton(cls, callback_data=f"class:{cls}")]
            for cls in CLASSES
        ]
        await query.edit_message_text(
            "Please select your class:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return CHOOSE_CLASS

    selected_name = query.data.split("name:", 1)[1]
    context.user_data["name"] = selected_name

    await query.edit_message_text(
        f"✅ Class: *{context.user_data['class']}*\n"
        f"✅ Name: *{selected_name}*\n\n"
        f"📹 Please send your video now.\n\n"
        f"_Your video will be compressed to save storage before uploading._",
        parse_mode="Markdown",
    )
    return UPLOAD_VIDEO


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Download, compress, and upload video to Google Drive."""
    message = update.message
    student_name = context.user_data.get("name", "Unknown")
    class_name = context.user_data.get("class", "Unknown")

    # Accept video files or documents sent as video
    video = message.video or message.document
    if not video:
        await message.reply_text("⚠️ Please send a video file.")
        return UPLOAD_VIDEO

    # Check file size (Telegram free bots max = 20 MB download via bot API)
    file_size_mb = (video.file_size or 0) / (1024 * 1024)
    if file_size_mb > 2000:
        await message.reply_text("❌ File too large. Please send a video under 2 GB.")
        return UPLOAD_VIDEO

    status_msg = await message.reply_text("⏳ Downloading your video...")

    with tempfile.TemporaryDirectory() as tmpdir:
        # Download from Telegram
        tg_file = await context.bot.get_file(video.file_id)
        raw_path = os.path.join(tmpdir, "original.mp4")
        await tg_file.download_to_drive(raw_path)

        original_size_mb = os.path.getsize(raw_path) / (1024 * 1024)
        await status_msg.edit_text(
            f"✅ Downloaded ({original_size_mb:.1f} MB)\n⚙️ Compressing to 480p..."
        )

        # Compress
        compressed_path = os.path.join(tmpdir, "compressed.mp4")
        success = compress_video(raw_path, compressed_path)

        if success and os.path.exists(compressed_path):
            compressed_size_mb = os.path.getsize(compressed_path) / (1024 * 1024)
            savings = (1 - compressed_size_mb / original_size_mb) * 100
            await status_msg.edit_text(
                f"✅ Compressed: {original_size_mb:.1f} MB → {compressed_size_mb:.1f} MB "
                f"({savings:.0f}% saved)\n☁️ Uploading to Google Drive..."
            )
            upload_path = compressed_path
        else:
            # Fallback: upload original if compression fails
            await status_msg.edit_text(
                "⚠️ Compression skipped — uploading original...\n☁️ Uploading to Google Drive..."
            )
            upload_path = raw_path

        # Build filename: StudentName_YYYY-MM-DD_HH-MM.mp4
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
        safe_name = student_name.replace(" ", "_")
        filename = f"{safe_name}_{timestamp}.mp4"

        try:
            drive_link = upload_to_drive(upload_path, filename, class_name, student_name)
            await status_msg.edit_text(
                f"🎉 *Upload complete!*\n\n"
                f"👤 Student: {student_name}\n"
                f"📚 Class: {class_name}\n"
                f"📁 File: `{filename}`\n\n"
                f"[View on Google Drive]({drive_link})\n\n"
                f"_Send /start to upload another video._",
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
        except Exception as e:
            logger.error(f"Drive upload failed: {e}")
            await status_msg.edit_text(
                "❌ Upload to Google Drive failed. Please try again or contact your teacher."
            )

    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "❌ Cancelled. Send /start to begin again.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


# ─── Main ──────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSE_CLASS: [CallbackQueryHandler(choose_class, pattern="^class:")],
            CHOOSE_NAME: [CallbackQueryHandler(choose_name, pattern="^(name:|back:)")],
            UPLOAD_VIDEO: [
                MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(conv_handler)

    logger.info("Bot started. Polling for updates...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
