"""
PÎVOK Studio — 24/7 Cloud App (Render Free-Tier Optimized: <150MB RAM, 1-Thread ONNX, RU + TR)
"""

import asyncio
import base64
import collections
import ctypes
import gc
import hashlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import zipfile
from contextlib import asynccontextmanager
from typing import Dict, Optional

def trim_memory():
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass

import cv2
import numpy as np
import qrcode
import requests
from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaDocument,
    InputMediaPhoto,
    KeyboardButton,
    Message,
    ReplyKeyboardRemove,
    Update,
    WebAppInfo,
)
from aiogram.utils.keyboard import ReplyKeyboardBuilder
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps

import collage_engine
import color_clustering
import yolo_local

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("pivok-cloud")

_LOG_RECORDS = collections.deque(maxlen=250)


class InMemoryLogHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            _LOG_RECORDS.append(msg)
        except Exception:
            pass


_in_mem_handler = InMemoryLogHandler()
_in_mem_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
logging.getLogger().addHandler(_in_mem_handler)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
SECRET_PASSWORD = os.environ.get("BOT_SECRET_PASSWORD", "pivok").strip()
SPACE_HOST = os.environ.get("SPACE_HOST", "").strip()
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
PUBLIC_BASE_URL = (
    os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
    or RENDER_EXTERNAL_URL
    or (f"https://{SPACE_HOST}" if SPACE_HOST else "")
)
WEBHOOK_SECRET = hashlib.sha256((BOT_TOKEN + ":pivok_wh").encode()).hexdigest()[:32] if BOT_TOKEN else "pivok"

INCOMING_DIR = collage_engine.WORK_DIR
OUTPUT_DIR = collage_engine.SAVE_DIR
STATE_FILE = os.path.join(collage_engine.WORK_ROOT, "cloud_state.json")
WA_AUTH_DIR = os.path.join(collage_engine.WORK_ROOT, "wa_auth_baileys")

PIVOK_TG_CHANNEL = os.environ.get("PIVOK_CHANNEL", "@pivoklaleli").strip()
WA_BRIDGE_PORT = int(os.environ.get("WA_BRIDGE_PORT", "3006"))
WA_BRIDGE_URL = f"http://127.0.0.1:{WA_BRIDGE_PORT}"
HF_TOKEN = os.environ.get("HF_TOKEN", "").strip()
HF_WA_REPO = os.environ.get("HF_WA_REPO", "is015566/pivok-wa-state").strip()

SIZE_LIST = [
    "36-42",
    "38-44",
    "40-46",
    "42-48",
    "44-50",
    "46-52",
    "48-54",
    "50-56",
    "1-2-3",
    "STD",
]

PHOTO_BUFFER: Dict[int, dict] = {}
_SEEN_UPDATES = collections.deque(maxlen=300)
_GEN_LOCK = asyncio.Lock()
_PUB_LOCK = asyncio.Lock()
_WA_QR_WAITERS: set[int] = set()
_WA_BACKUP_TIMER: Optional[threading.Timer] = None
_WA_PROC: Optional[subprocess.Popen] = None

# ==========================================
# PIVOK CHANNEL LINKS CONFIGURATION
# Ready for adding/updating channel URLs via:
#   1) Environment variables (LINK_TELEGRAM_CHANNEL, LINK_WHATSAPP, LINK_INSTAGRAM,
#      LINK_WHATSAPP_CHANNEL, LINK_SALE_CHANNEL, LINK_VIDEO_CHANNEL, LINK_ADDRESS)
#   2) Persistent store (cloud_state.json -> "channel_links")
#   3) Default values below (no bot logic rewrite required)
# ==========================================

PIVOK_CHANNEL_LINKS = [
    {
        "key": "telegram_channel",
        "text": "✈️ Telegram Channel",
        "default_url": "https://t.me/pivoklaleli",
        "env_var": "LINK_TELEGRAM_CHANNEL",
    },
    {
        "key": "whatsapp",
        "text": "💬 WhatsApp",
        "default_url": "https://chat.whatsapp.com/Ddh7BKb1atCAhRzogy9l9Y",
        "env_var": "LINK_WHATSAPP",
    },
    {
        "key": "instagram",
        "text": "📸 Instagram",
        "default_url": "https://www.instagram.com/pivok_fashion?cnvx=MWRzeWJkaGs1MGpsaQ==",
        "env_var": "LINK_INSTAGRAM",
    },
    {
        "key": "whatsapp_channel",
        "text": "📢 WhatsApp Channel",
        "default_url": "https://chat.whatsapp.com/Ddh7BKb1atCAhRzogy9l9Y",
        "env_var": "LINK_WHATSAPP_CHANNEL",
    },
    {
        "key": "sale_channel",
        "text": "🔥 Sale Channel",
        "default_url": "https://t.me/pivoksale",
        "env_var": "LINK_SALE_CHANNEL",
    },
    {
        "key": "video_channel",
        "text": "📹 Video Channel",
        "default_url": "https://t.me/pivokvideo",
        "env_var": "LINK_VIDEO_CHANNEL",
    },
    {
        "key": "address",
        "text": "📍 Address",
        "default_url": "https://www.google.com/maps?q=41.007667,28.955222",
        "env_var": "LINK_ADDRESS",
    },
]

# ==========================================
# EN (DEFAULT) + RU + TR LOCALIZATION DICTIONARY
# ==========================================

STRINGS = {
    "en": {
        "btn_create": "🖼 Create Collage",
        "btn_wa_qr": "📱 WhatsApp QR",
        "btn_channels": "📢 Channels",
        "btn_lang": "🌐 Language",
        "btn_cancel": "❌ Cancel",
        "btn_pair_2": "✅ Double Collage (2 Photos)",
        "btn_single_1": "✅ Single Collage (1 Photo)",
        "btn_all_loaded": "✅ ALL PHOTOS UPLOADED",
        "btn_group_smart": "🧠 Smart Grouping (Auto)",
        "btn_group_seq": "🔢 Sequential Pairs (1-2, 3-4)",
        "btn_confirm_go": "✅ Generate Collages",
        "btn_edit_code": "✏️ Edit Code",
        "btn_edit_size": "✏️ Edit Sizes",
        "btn_edit_price": "✏️ Edit Price",
        "btn_pub_both": "🚀 Send to TG Channel & WhatsApp",
        "btn_pub_tg": "✈️ Send to TG Channel",
        "btn_pub_wa": "💬 Send to WhatsApp",
        "btn_dl_files": "📥 Download Original Files",
        "btn_refresh": "🔄 Refresh Preview",
        "btn_add_color": "🎨 Add Color Photos",
        "btn_delete": "🗑 Delete",
        "btn_add_video": "🎬 Add Video",
        "btn_add_video_menu": "🎬 Add Video",
        "btn_video_recent": "🏷️ For model {code}",
        "welcome": "🌸 **PÎVOK Studio (24/7)**\nChoose an action from the menu below:",
        "channels_title": "📢 **PÎVOK Official Channels & Links:**",
        "channels_empty": "ℹ️ Channel links have not been configured yet.",
        "cancelled": "❌ Action cancelled.",
        "lang_prompt": "🌐 **Select interface language:**",
        "lang_switched": "🇬🇧 Language switched to **English**!",
        "send_photos_prompt": (
            "📸 **Send all photos for one model** (as an album or files).\n\n"
            "Once all photos are uploaded, select below:\n"
            "• **✅ Double Collage (2 Photos)** — PÎVOK 4×3 format\n"
            "• **✅ Single Collage (1 Photo)**"
        ),
        "only_images": "⚠️ Please send image files only.",
        "photos_buffered": "📥 Photos received: **{n}**\nUpload more or press a button below:",
        "photos_buffered_ask_code": "📸 **{n} photos** uploaded.\n\n🔖 Enter **Model Code** (e.g. `0-4062`):",
        "no_photos_yet": "⚠️ You have not uploaded any photos yet!",
        "need_2_photos": "⚠️ Double collage requires at least 2 photos.",
        "processing_extra": "⏳ Processing additional photos...",
        "ask_grouping": "📸 **{n} photos** uploaded. How should pairs be grouped?",
        "ask_code": "🔖 Enter **Model Code** (e.g. `0-4062`):",
        "err_code": "⚠️ Please enter a valid model code:",
        "ask_video_prompt": "🎬 **PÎVOK — Video Upload**\n🏷️ Model: `{code}`\n\nPlease send the video file directly here ⬇️",
        "ask_video_code": "🎬 **PÎVOK — Video Upload**\n\nEnter **Model Code** (e.g. `0-4062`) or send video with code in caption:",
        "video_uploading": "⏳ Uploading and publishing video for model **{code}**...",
        "video_done": "✅ **Video published successfully!**\n🏷️ Model: `{code}`\n✈️ Telegram: {tg}\n💬 WhatsApp: {wa}",
        "video_invalid": "⚠️ Please send a video file (MP4, MOV, etc.) or tap Cancel.",
        "ask_size": "📏 Select or type **Sizes** (e.g. `38-44`):",
        "err_size": "⚠️ Please specify the size range:",
        "ask_price": "💰 Enter **Price in $** (e.g. `79`):",
        "err_price": "⚠️ Please enter a valid price:",
        "summary": (
            "📋 **Review PÎVOK Collage Details:**\n\n"
            "🔖 **Code:** `{code}`\n"
            "💰 **Price:** `{price}$`\n"
            "📏 **Sizes:** `{size}`\n"
            "🖼 **Photos:** {cnt} → **{est} collages** (4×3 format)"
        ),
        "ask_edit_code": "🔖 Enter new **Model Code**:",
        "ask_edit_size": "📏 Select or enter new **Sizes**:",
        "ask_edit_price": "💰 Enter new **Price ($)**:",
        "choose_below": "Please choose a button below 👇",
        "generating": "⚙️ Processing {n} photos in the cloud...",
        "prog_step": "⏳ Analyzing photo: **{cur} of {tot}**...",
        "prog_render": "🎨 Assembling PÎVOK collages (4×3)...",
        "preview_ready": (
            "✨ **PÎVOK — Collages Ready ({n})**\n"
            "🏷️ Code: `{code}` | 💰 Price: `{price}$` | 📏 Sizes: `{sizes}`\n\n"
            "Adjust framing via 🛠 if needed, or choose an action:"
        ),
        "next_model_hint": "Done! To create collages for the next model, tap the button below:",
        "err_gen": "❌ Error generating collages: {e}",
        "folder_missing": "⚠️ Collage folder not found.",
        "gen_empty": "⚠️ Could not generate collages.",
        "sending_originals": "Sending original files...",
        "refreshing": "Refreshing preview...",
        "ask_add_color": "📸 Send additional photos (new color) for this model, then press **«✅ ALL PHOTOS UPLOADED»**.",
        "deleted_toast": "Deleted",
        "deleted_msg": "🗑 Collages deleted.",
        "catch_all": "🌸 **PÎVOK Studio**\nPress **«🖼 Create Collage»** below (or send photos directly here):",
        "wa_ready": "✅ **WhatsApp is connected and ready!**\n📌 Target Group: `{grp}`",
        "wa_relink_btn": "🔄 Relink / Reset WhatsApp",
        "wa_refresh_qr_btn": "🔄 Refresh QR Code",
        "wa_gen_qr": "⏳ Generating WhatsApp QR code, please wait 3 seconds...",
        "wa_starting": "⏳ WhatsApp bridge is starting up. Please tap **📱 WhatsApp QR** again in 5 seconds.",
        "wa_qr_caption": (
            "📱 **Connect WhatsApp for Automatic Group Posting**\n\n"
            "1. Open **WhatsApp** on your phone\n"
            "2. Go to **Settings → Linked Devices → Link a Device**\n"
            "3. Scan this QR code (only needed once!)"
        ),
        "wa_connected_push": "✅ **WhatsApp connected!**\n📌 Group: `{grp}`\n☁️ Session saved in cloud.",
        "pub_wait": "⏳ Publishing collages, please wait...",
        "pub_tg_ok": "✅ Telegram ({ch}): Published!",
        "pub_tg_err": "❌ Telegram ({ch}): Make sure this bot is an Administrator ({e})",
        "pub_wa_ok": "✅ WhatsApp Group: Published!",
        "pub_wa_not_linked": "⚠️ WhatsApp Group: Not connected yet — scan the QR code below, then tap 💬!",
    },
    "ru": {
        "btn_create": "🖼 Создать коллаж",
        "btn_wa_qr": "📱 WhatsApp QR",
        "btn_channels": "📢 Каналы",
        "btn_lang": "🌐 Язык / Dil / اللغة",
        "btn_cancel": "❌ Отмена",
        "btn_pair_2": "✅ Двойной коллаж (по 2 фото)",
        "btn_single_1": "✅ Одинарный коллаж (по 1 фото)",
        "btn_all_loaded": "✅ ВСЕ ФОТО ЗАГРУЖЕНЫ",
        "btn_group_smart": "🧠 Умная группировка (Авто)",
        "btn_group_seq": "🔢 По парам (1-2, 3-4)",
        "btn_confirm_go": "✅ Создать коллажи",
        "btn_edit_code": "✏️ Изменить Код",
        "btn_edit_size": "✏️ Изменить Размеры",
        "btn_edit_price": "✏️ Изменить Цену",
        "btn_pub_both": "🚀 Отправить в TG и WhatsApp",
        "btn_pub_tg": "✈️ Отправить в TG канал",
        "btn_pub_wa": "💬 Отправить в WhatsApp",
        "btn_dl_files": "📥 Скачать файлами (без сжатия)",
        "btn_refresh": "🔄 Обновить превью",
        "btn_add_color": "🎨 Добавить фото цвета",
        "btn_delete": "🗑 Удалить",
        "btn_add_video": "🎬 Догрузить видео",
        "btn_add_video_menu": "🎬 Догрузить видео",
        "btn_video_recent": "🏷️ Для модели {code}",
        "welcome": "🌸 **PÎVOK Cloud Studio (24/7)**\nВыберите действие в меню ниже:",
        "cancelled": "❌ Действие отменено.",
        "lang_prompt": "🌐 **Выберите язык интерфейса / Lütfen dil seçin:**",
        "lang_switched": "🇷🇺 Язык переключён на **Русский**!",
        "send_photos_prompt": (
            "📸 **Отправьте все фотографии одной модели** (альбомом или файлами).\n\n"
            "Когда загрузите все фото, выберите внизу:\n"
            "• **✅ Двойной коллаж (по 2 фото)** — формат PÎVOK 4×3\n"
            "• **✅ Одинарный коллаж (по 1 фото)**"
        ),
        "only_images": "⚠️ Пожалуйста, отправляйте только изображения.",
        "photos_buffered": "📥 Принято фотографий: **{n} шт.**\nДогрузите ещё или нажмите кнопку ниже:",
        "photos_buffered_ask_code": "📸 Загружено **{n} фото**.\n\n🔖 Введите **Код модели** (например: `0-4062`):",
        "no_photos_yet": "⚠️ Вы ещё не загрузили ни одной фотографии!",
        "need_2_photos": "⚠️ Для двойного коллажа нужно минимум 2 фотографии.",
        "processing_extra": "⏳ Обрабатываю новые фото...",
        "ask_grouping": "📸 Загружено **{n} фото**. Как сгруппировать пары?",
        "ask_code": "🔖 Введите **Код модели** (например: `0-4062`):",
        "err_code": "⚠️ Введите корректный код модели:",
        "ask_video_prompt": "🎬 **PÎVOK — Догрузка видео**\n🏷️ Модель: `{code}`\n\nПожалуйста, отправьте видео-файл прямо сюда ⬇️",
        "ask_video_code": "🎬 **PÎVOK — Догрузка видео**\n\nВведите **Код модели** (например: `0-4062`) или отправьте видео с кодом в подписи:",
        "video_uploading": "⏳ Догружаю и отправляю видео для модели **{code}**...",
        "video_done": "✅ **Видео успешно опубликовано!**\n🏷️ Модель: `{code}`\n✈️ Telegram: {tg}\n💬 WhatsApp: {wa}",
        "video_invalid": "⚠️ Пожалуйста, отправьте именно видео-файл (MP4, MOV и др.) или нажмите «Отмена».",
        "ask_size": "📏 Выберите или напишите **Размеры** (например: `38-44`):",
        "err_size": "⚠️ Укажите размерный ряд:",
        "ask_price": "💰 Введите **Цену в $** (например: `79`):",
        "err_price": "⚠️ Введите цену:",
        "summary": (
            "📋 **Проверьте данные коллажа PÎVOK:**\n\n"
            "🔖 **Код:** `{code}`\n"
            "💰 **Цена:** `{price}$`\n"
            "📏 **Размеры:** `{size}`\n"
            "🖼 **Фотографий:** {cnt} шт. → **{est} колл.** (формат 4×3)"
        ),
        "ask_edit_code": "🔖 Введите новый **Код модели**:",
        "ask_edit_size": "📏 Выберите или введите новые **Размеры**:",
        "ask_edit_price": "💰 Введите новую **Цену ($)**:",
        "choose_below": "Выберите кнопку внизу 👇",
        "generating": "⚙️ Начинаю обработку {n} фото в облаке...",
        "prog_step": "⏳ Анализ фото: **{cur} из {tot}**...",
        "prog_render": "🎨 Сборка коллажей PÎVOK (4×3)...",
        "preview_ready": (
            "✨ **PÎVOK — Коллажи готовы ({n} шт.)**\n"
            "🏷️ Код: `{code}` | 💰 Цена: `{price}$` | 📏 Размеры: `{sizes}`\n\n"
            "При необходимости поправьте кроп через 🛠 или выберите действие:"
        ),
        "next_model_hint": "Готово! Чтобы собрать следующую модель, нажмите кнопку ниже:",
        "err_gen": "❌ Ошибка при создании коллажей: {e}",
        "folder_missing": "⚠️ Папка с готовыми коллажами не найдена.",
        "gen_empty": "⚠️ Не удалось сгенерировать коллажи.",
        "sending_originals": "Отправляю оригиналы...",
        "refreshing": "Обновляю превью...",
        "ask_add_color": "📸 Отправьте дополнительные фото (новый цвет) для этой модели, затем нажмите **«✅ ВСЕ ФОТО ЗАГРУЖЕНЫ»**.",
        "deleted_toast": "Удалено",
        "deleted_msg": "🗑 Коллажи удалены.",
        "catch_all": "🌸 **PÎVOK Cloud Studio**\nНажмите **«🖼 Создать коллаж»** внизу (или отправьте фото прямо сюда):",
        "channels_title": "📢 **Официальные каналы и ссылки PÎVOK:**",
        "channels_empty": "ℹ️ Ссылки на каналы пока не настроены.",
        "wa_ready": "✅ **WhatsApp подключён и готов к отправке!**\n📌 Группа: `{grp}`",
        "wa_relink_btn": "🔄 Перепривязать WhatsApp",
        "wa_refresh_qr_btn": "🔄 Обновить QR-код",
        "wa_gen_qr": "⏳ Генерирую QR-код WhatsApp, подождите 3 секунды...",
        "wa_starting": "⏳ Модуль WhatsApp запускается. Нажмите **📱 WhatsApp QR** ещё раз через 5 секунд.",
        "wa_qr_caption": (
            "📱 **Подключение WhatsApp для авто-отправки в группу**\n\n"
            "1. Откройте **WhatsApp** на телефоне\n"
            "2. Зайдите в **Настройки → Связанные устройства → Привязать устройство**\n"
            "3. Отсканируйте этот QR-код (достаточно 1 раз!)"
        ),
        "wa_connected_push": "✅ **WhatsApp успешно подключён!**\n📌 Группа: `{grp}`\n☁️ Сессия сохранена в облаке.",
        "pub_wait": "⏳ Отправляю коллажи, подождите...",
        "pub_tg_ok": "✅ Telegram ({ch}): Опубликовано!",
        "pub_tg_err": "❌ Telegram ({ch}): Добавьте бота в Администраторы канала ({e})",
        "pub_wa_ok": "✅ WhatsApp-группа: Опубликовано!",
        "pub_wa_not_linked": "⚠️ WhatsApp ещё не привязан — отсканируйте QR-код ниже и нажмите 💬 Отправить в WhatsApp!",
    },
    "tr": {
        "btn_create": "🖼 Kolaj Oluştur",
        "btn_wa_qr": "📱 WhatsApp QR",
        "btn_channels": "📢 Kanallar",
        "btn_lang": "🌐 Dil / Язык / اللغة",
        "btn_cancel": "❌ İptal",
        "btn_pair_2": "✅ İkili Kolaj (2 Fotoğraf)",
        "btn_single_1": "✅ Tekli Kolaj (1 Fotoğraf)",
        "btn_all_loaded": "✅ TÜM FOTOĞRAFLAR YÜKLENDİ",
        "btn_group_smart": "🧠 Akıllı Gruplama (Otomatik)",
        "btn_group_seq": "🔢 Sırayla İkili (1-2, 3-4)",
        "btn_confirm_go": "✅ Kolajları Oluştur",
        "btn_edit_code": "✏️ Kodu Değiştir",
        "btn_edit_size": "✏️ Bedeni Değiştir",
        "btn_edit_price": "✏️ Fiyatı Değiştir",
        "btn_pub_both": "🚀 TG Kanalı ve WhatsApp'a Gönder",
        "btn_pub_tg": "✈️ TG Kanalına Gönder",
        "btn_pub_wa": "💬 WhatsApp'a Gönder",
        "btn_dl_files": "📥 Dosya Olarak İndir (Orijinal)",
        "btn_refresh": "🔄 Önizlemeyi Yenile",
        "btn_add_color": "🎨 Yeni Renk (Fotoğraf) Ekle",
        "btn_delete": "🗑 Sil",
        "btn_add_video": "🎬 Video Yükle",
        "btn_add_video_menu": "🎬 Video Yükle",
        "btn_video_recent": "🏷️ Model {code} için",
        "welcome": "🌸 **PÎVOK Cloud Studio (7/24)**\nLütfen aşağıdaki menüden işlem seçin:",
        "cancelled": "❌ İşlem iptal edildi.",
        "lang_prompt": "🌐 **Lütfen dil seçin / Выберите язык интерфейса:**",
        "lang_switched": "🇹🇷 Dil **Türkçe** olarak değiştirildi!",
        "send_photos_prompt": (
            "📸 **Bir modele ait tüm fotoğrafları gönderin** (albüm veya dosya olarak).\n\n"
            "Tüm fotoğraflar yüklendiğinde aşağıdan seçin:\n"
            "• **✅ İkili Kolaj (2 Fotoğraf)** — PÎVOK 4×3 formatı\n"
            "• **✅ Tekli Kolaj (1 Fotoğraf)**"
        ),
        "only_images": "⚠️ Lütfen yalnızca görsel / fotoğraf gönderin.",
        "photos_buffered": "📥 Alınan fotoğraf: **{n} adet**\nDaha fazla yükleyin veya aşağıdaki butona basın:",
        "photos_buffered_ask_code": "📸 **{n} fotoğraf** yüklendi.\n\n🔖 **Model Kodunu** girin (örnek: `0-4062`):",
        "no_photos_yet": "⚠️ Henüz hiç fotoğraf yüklemediniz!",
        "need_2_photos": "⚠️ İkili kolaj için en az 2 fotoğraf gereklidir.",
        "processing_extra": "⏳ Yeni fotoğraflar işleniyor...",
        "ask_grouping": "📸 **{n} fotoğraf** yüklendi. Eşleştirme nasıl yapılsın?",
        "ask_code": "🔖 **Model Kodunu** girin (örnek: `0-4062`):",
        "err_code": "⚠️ Lütfen geçerli bir model kodu girin:",
        "ask_video_prompt": "🎬 **PÎVOK — Video Yükleme**\n🏷️ Model: `{code}`\n\nLütfen video dosyasını doğrudan buraya gönderin ⬇️",
        "ask_video_code": "🎬 **PÎVOK — Video Yükleme**\n\n**Model Kodunu** girin (örnek: `0-4062`) veya videoyu açıklamasında kodla gönderin:",
        "video_uploading": "⏳ Model **{code}** için video yükleniyor ve yayınlanıyor...",
        "video_done": "✅ **Video başarıyla yayınlandı!**\n🏷️ Model: `{code}`\n✈️ Telegram: {tg}\n💬 WhatsApp: {wa}",
        "video_invalid": "⚠️ Lütfen bir video dosyası gönderin (MP4, MOV vb.) veya İptal'e basın.",
        "ask_size": "📏 **Beden aralığını** seçin veya yazın (örnek: `38-44`):",
        "err_size": "⚠️ Lütfen beden aralığını belirtin:",
        "ask_price": "💰 **Fiyatı ($)** girin (örnek: `79`):",
        "err_price": "⚠️ Lütfen fiyat girin:",
        "summary": (
            "📋 **PÎVOK kolaj bilgilerini kontrol edin:**\n\n"
            "🔖 **Kod:** `{code}`\n"
            "💰 **Fiyat:** `{price}$`\n"
            "📏 **Beden:** `{size}`\n"
            "🖼 **Fotoğraf:** {cnt} adet → **{est} kolaj** (4×3 formatı)"
        ),
        "ask_edit_code": "🔖 Yeni **Model Kodunu** girin:",
        "ask_edit_size": "📏 Yeni **Beden aralığını** seçin veya yazın:",
        "ask_edit_price": "💰 Yeni **Fiyatı ($)** girin:",
        "choose_below": "Lütfen aşağıdaki butonlardan birini seçin 👇",
        "generating": "⚙️ {n} fotoğraf bulutta işleniyor...",
        "prog_step": "⏳ Fotoğraf analizi: **{cur} / {tot}**...",
        "prog_render": "🎨 PÎVOK kolajları (4×3) birleştiriliyor...",
        "preview_ready": (
            "✨ **PÎVOK — Kolajlar hazır ({n} adet)**\n"
            "🏷️ Kod: `{code}` | 💰 Fiyat: `{price}$` | 📏 Bedenler: `{sizes}`\n\n"
            "Gerekirse 🛠 ile kadrajı düzenleyin veya işlem seçin:"
        ),
        "next_model_hint": "Tamamlandı! Yeni bir model hazırlamak için aşağıdaki butona basın:",
        "err_gen": "❌ Kolaj oluşturulurken hata oluştu: {e}",
        "folder_missing": "⚠️ Kolaj klasörü bulunamadı.",
        "gen_empty": "⚠️ Kolaj oluşturulamadı.",
        "sending_originals": "Orijinal dosyalar gönderiliyor...",
        "refreshing": "Önizleme yenileniyor...",
        "ask_add_color": "📸 Bu model için ek fotoğrafları (yeni renk) gönderin, ardından **«✅ TÜM FOTOĞRAFLAR YÜKLENDİ»** butonuna basın.",
        "deleted_toast": "Silindi",
        "deleted_msg": "🗑 Kolajlar silindi.",
        "catch_all": "🌸 **PÎVOK Cloud Studio**\nAşağıdaki **«🖼 Kolaj Oluştur»** butonuna basın (veya fotoğrafları doğrudan buraya gönderin):",
        "channels_title": "📢 **PÎVOK Resmi Kanallar ve Linkler:**",
        "channels_empty": "ℹ️ Kanal linkleri henüz ayarlanmadı.",
        "wa_ready": "✅ **WhatsApp bağlı ve gönderime hazır!**\n📌 Grup: `{grp}`",
        "wa_relink_btn": "🔄 WhatsApp Bağlantısını Sıfırla",
        "wa_refresh_qr_btn": "🔄 QR Kodunu Yenile",
        "wa_gen_qr": "⏳ WhatsApp QR kodu oluşturuluyor, lütfen 3 saniye bekleyin...",
        "wa_starting": "⏳ WhatsApp modülü başlatılıyor. 5 saniye sonra tekrar **📱 WhatsApp QR** tuşuna basın.",
        "wa_qr_caption": (
            "📱 **Otomatik Grup Gönderimi için WhatsApp Bağlantısı**\n\n"
            "1. Telefonunuzda **WhatsApp** uygulamasını açın\n"
            "2. **Ayarlar → Bağlı Cihazlar → Cihaz Bağla** bölümüne girin\n"
            "3. Bu QR kodunu okutun (sadece 1 kez yeterlidir!)"
        ),
        "wa_connected_push": "✅ **WhatsApp başarıyla bağlandı!**\n📌 Grup: `{grp}`\n☁️ Oturum buluta kaydedildi.",
        "pub_wait": "⏳ Kolajlar gönderiliyor, lütfen bekleyin...",
        "pub_tg_ok": "✅ Telegram ({ch}): Yayınlandı!",
        "pub_tg_err": "❌ Telegram ({ch}): Botun kanalda Yönetici olduğundan emin olun ({e})",
        "pub_wa_ok": "✅ WhatsApp Grubu: Gönderildi!",
        "pub_wa_not_linked": "⚠️ WhatsApp henüz bağlı değil — aşağıdaki QR kodunu okutup tekrar 💬 butonuna basın!",
    },
    "ar": {
        "btn_create": "🖼 إنشاء كولاج",
        "btn_wa_qr": "📱 WhatsApp QR",
        "btn_channels": "📢 القنوات",
        "btn_lang": "🌐 اللغة / Язык / Dil",
        "btn_cancel": "❌ إلغاء",
        "btn_pair_2": "✅ كولاج مزدوج (صورتان)",
        "btn_single_1": "✅ كولاج مفرد (صورة واحدة)",
        "btn_all_loaded": "✅ تم رفع جميع الصور",
        "btn_group_smart": "🧠 تجميع ذكي (تلقائي)",
        "btn_group_seq": "🔢 أزواج متتالية (1-2، 3-4)",
        "btn_confirm_go": "✅ إنشاء الكولاج",
        "btn_edit_code": "✏️ تعديل الكود",
        "btn_edit_size": "✏️ تعديل المقاسات",
        "btn_edit_price": "✏️ تعديل السعر",
        "btn_pub_both": "🚀 إرسال إلى تيليجرام وواتساب",
        "btn_pub_tg": "✈️ إرسال إلى قناة تيليجرام",
        "btn_pub_wa": "💬 إرسال إلى واتساب",
        "btn_dl_files": "📥 تحميل الملفات الأصلية",
        "btn_refresh": "🔄 تحديث المعاينة",
        "btn_add_color": "🎨 إضافة صور لون جديد",
        "btn_delete": "🗑 حذف",
        "btn_add_video": "🎬 تحميل فيديو",
        "btn_add_video_menu": "🎬 تحميل فيديو",
        "btn_video_recent": "🏷️ للموديل {code}",
        "welcome": "🌸 **استوديو PÎVOK السحابي (24/7)**\nاختر إجراءً من القائمة أدناه:",
        "cancelled": "❌ تم إلغاء العملية.",
        "lang_prompt": "🌐 **اختر اللغة / Выберите язык / Dil seçin:**",
        "lang_switched": "🇸🇦 تم تغيير اللغة إلى **العربية**!",
        "send_photos_prompt": (
            "📸 **أرسل جميع صور الموديل الواحد** (كألبوم أو ملفات).\n\n"
            "عند الانتهاء من رفع الصور، اختر من الأسفل:\n"
            "• **✅ كولاج مزدوج (صورتان)** — مقاس PÎVOK 4×3\n"
            "• **✅ كولاج مفرد (صورة واحدة)**"
        ),
        "only_images": "⚠️ يرجى إرسال الصور فقط.",
        "photos_buffered": "📥 الصور المستلمة: **{n}**\nأرسل المزيد أو اضغط على الزر أدناه:",
        "photos_buffered_ask_code": "📸 تم استلام **{n} صور**\n\n🔖 أدخل **كود الموديل** (مثال: `0-4062`):",
        "no_photos_yet": "⚠️ لم تقم برفع أي صور بعد!",
        "need_2_photos": "⚠️ الكولاج المزدوج يتطلب صورتين على الأقل.",
        "processing_extra": "⏳ جاري معالجة الصور الإضافية...",
        "ask_grouping": "📸 تم رفع **{n} صور**. كيف تريد تجميع الأزواج؟",
        "ask_code": "🔖 أدخل **كود الموديل** (مثال: `0-4062`):",
        "err_code": "⚠️ يرجى إدخال كود موديل صحيح:",
        "ask_video_prompt": "🎬 **PÎVOK — تحميل فيديو**\n🏷️ الموديل: `{code}`\n\nيرجى إرسال ملف الفيديو هنا مباشرة ⬇️",
        "ask_video_code": "🎬 **PÎVOK — تحميل فيديو**\n\nأدخل **كود الموديل** (مثال: `0-4062`) أو أرسل فيديو مع الكود في الوصف:",
        "video_uploading": "⏳ جاري رفع ونشر الفيديو للموديل **{code}**...",
        "video_done": "✅ **تم نشر الفيديو بنجاح!**\n🏷️ الموديل: `{code}`\n✈️ تيليجرام: {tg}\n💬 واتساب: {wa}",
        "video_invalid": "⚠️ يرجى إرسال ملف فيديو (MP4, MOV إلخ) أو الضغط على إلغاء.",
        "ask_size": "📏 اختر أو اكتب **المقاسات** (مثال: `38-44`):",
        "err_size": "⚠️ يرجى تحديد المقاسات:",
        "ask_price": "💰 أدخل **السعر بالدولار ($)** (مثال: `79`):",
        "err_price": "⚠️ يرجى إدخال السعر:",
        "summary": (
            "📋 **مراجعة بيانات كولاج PÎVOK:**\n\n"
            "🔖 **الكود:** `{code}`\n"
            "💰 **السعر:** `{price}$`\n"
            "📏 **المقاسات:** `{size}`\n"
            "🖼 **الصور:** {cnt} ← **{est} كولاج** (مقاس 4×3)"
        ),
        "ask_edit_code": "🔖 أدخل **كود الموديل** الجديد:",
        "ask_edit_size": "📏 اختر أو أدخل **المقاسات** الجديدة:",
        "ask_edit_price": "💰 أدخل **السعر الجديد ($)**:",
        "choose_below": "يرجى اختيار زر من الأسفل 👇",
        "generating": "⚙️ جاري معالجة {n} صور في السحابة...",
        "prog_step": "⏳ تحليل الصورة: **{cur} من {tot}**...",
        "prog_render": "🎨 جاري تجميع كولاج PÎVOK (4×3)...",
        "preview_ready": (
            "✨ **PÎVOK — الكولاج جاهز ({n})**\n"
            "🏷️ الكود: `{code}` | 💰 السعر: `{price}$` | 📏 المقاسات: `{sizes}`\n\n"
            "يمكنك تعديل القص عبر 🛠 عند الحاجة أو اختيار إجراء:"
        ),
        "next_model_hint": "تم! لإنشاء كولاج للموديل التالي، اضغط على الزر أدناه:",
        "err_gen": "❌ حدث خطأ أثناء إنشاء الكولاج: {e}",
        "folder_missing": "⚠️ مجلد الكولاج غير موجود.",
        "gen_empty": "⚠️ تعذر إنشاء الكولاج.",
        "sending_originals": "جاري إرسال الملفات الأصلية...",
        "refreshing": "جاري تحديث المعاينة...",
        "ask_add_color": "📸 أرسل الصور الإضافية (اللون الجديد) لهذا الموديل، ثم اضغط **«✅ تم رفع جميع الصور»**.",
        "deleted_toast": "تم الحذف",
        "deleted_msg": "🗑 تم حذف الكولاج.",
        "catch_all": "🌸 **استوديو PÎVOK**\nاضغط **«🖼 إنشاء كولاج»** بالأسفل (أو أرسل الصور مباشرة هنا):",
        "channels_title": "📢 **قنوات وروابط PÎVOK الرسمية:**",
        "channels_empty": "ℹ️ لم يتم إعداد روابط القنوات بعد.",
        "wa_ready": "✅ **واتساب متصل وجاهز للإرسال!**\n📌 المجموعة: `{grp}`",
        "wa_relink_btn": "🔄 إعادة ربط واتساب",
        "wa_refresh_qr_btn": "🔄 تحديث رمز QR",
        "wa_gen_qr": "⏳ جاري إنشاء رمز QR لواتساب، يرجى الانتظار 3 ثوانٍ...",
        "wa_starting": "⏳ جاري تشغيل خدمة واتساب. يرجى الضغط على **📱 WhatsApp QR** بعد 5 ثوانٍ.",
        "wa_qr_caption": (
            "📱 **ربط واتساب للنشر التلقائي في المجموعة**\n\n"
            "1. افتح **واتساب** على هاتفك\n"
            "2. اذهب إلى **الإعدادات ← الأجهزة المرتبطة ← ربط جهاز**\n"
            "3. امسح رمز QR هذا (مرة واحدة فقط!)"
        ),
        "wa_connected_push": "✅ **تم ربط واتساب بنجاح!**\n📌 المجموعة: `{grp}`\n☁️ تم حفظ الجلسة في السحابة.",
        "pub_wait": "⏳ جاري نشر الكولاج، يرجى الانتظار...",
        "pub_tg_ok": "✅ تيليجرام ({ch}): تم النشر!",
        "pub_tg_err": "❌ تيليجرام ({ch}): تأكد من أن البوت مشرف في القناة ({e})",
        "pub_wa_ok": "✅ مجموعة واتساب: تم الإرسال!",
        "pub_wa_not_linked": "⚠️ واتساب غير مرتبط بعد — امسح رمز QR أدناه ثم اضغط 💬!",
    },
}

ALL_LANGS = ("ru", "tr", "ar", "en")
CREATE_TEXTS = {STRINGS[lg]["btn_create"] for lg in ALL_LANGS}
ADD_VIDEO_TEXTS = (
    {STRINGS[lg]["btn_add_video_menu"] for lg in ALL_LANGS}
    | {"🎬 Догрузить видео", "🎬 Add Video", "🎬 Video Yükle", "🎬 تحميل فيديو", "догрузить видео", "видео", "video", "/video"}
)
WA_QR_BTN_TEXTS = {STRINGS[lg]["btn_wa_qr"] for lg in ALL_LANGS}
CHANNELS_BTN_TEXTS = {STRINGS[lg]["btn_channels"] for lg in ALL_LANGS}
LANG_BTN_TEXTS = {STRINGS[lg]["btn_lang"] for lg in ALL_LANGS} | {"🇹🇷 Türkçe / Язык", "🇷🇺 Русский / Dil"}
CANCEL_TEXTS = (
    {STRINGS[lg]["btn_cancel"] for lg in ALL_LANGS}
    | {"Cancel", "cancel", "Отмена", "отмена", "❌ Отмена", "İptal", "iptal", "إلغاء", "/cancel", "/stop"}
)
DONE_PHOTO_TEXTS = (
    {STRINGS[lg]["btn_all_loaded"] for lg in ALL_LANGS}
    | {STRINGS[lg]["btn_pair_2"] for lg in ALL_LANGS}
    | {STRINGS[lg]["btn_single_1"] for lg in ALL_LANGS}
)
SINGLE_MODE_TEXTS = {STRINGS[lg]["btn_single_1"] for lg in ALL_LANGS}
SMART_GROUP_TEXTS = {STRINGS[lg]["btn_group_smart"] for lg in ALL_LANGS}
CONFIRM_GO_TEXTS = {STRINGS[lg]["btn_confirm_go"] for lg in ALL_LANGS}
EDIT_CODE_TEXTS = {STRINGS[lg]["btn_edit_code"] for lg in ALL_LANGS}
EDIT_SIZE_TEXTS = {STRINGS[lg]["btn_edit_size"] for lg in ALL_LANGS}
EDIT_PRICE_TEXTS = {STRINGS[lg]["btn_edit_price"] for lg in ALL_LANGS}


def _load_state_store() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_state_store(store: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


_STORE = _load_state_store()
_env_ids = [
    int(x.strip())
    for x in os.environ.get("ALLOWED_USER_IDS", "").split(",")
    if x.strip().isdigit()
]
ALLOWED_USERS: list[int] = list(set(_STORE.get("allowed_users", []) + _env_ids))
pending_dispatches: Dict[str, str] = _STORE.get("pending_dispatches", {})
media_msgs_store: Dict[str, list[int]] = _STORE.get("media_msgs", {})
user_langs: Dict[str, str] = _STORE.get("user_langs_v4", {})
saved_channel_links: Dict[str, str] = _STORE.get("channel_links", {})
published_records: Dict[str, dict] = _STORE.get("published_records", {})
_RECENT_CODES: Dict[str, str] = _STORE.get("recent_codes", {})


def persist_store():
    _STORE["allowed_users"] = ALLOWED_USERS
    _STORE["pending_dispatches"] = pending_dispatches
    _STORE["media_msgs"] = media_msgs_store
    _STORE["user_langs_v4"] = user_langs
    _STORE["channel_links"] = saved_channel_links
    _STORE["published_records"] = published_records
    _STORE["recent_codes"] = _RECENT_CODES
    _save_state_store(_STORE)


def restore_wa_session_from_hf():
    if not HF_TOKEN or not HF_WA_REPO:
        return
    creds_p = os.path.join(WA_AUTH_DIR, "creds.json")
    if os.path.exists(creds_p):
        return
    try:
        from huggingface_hub import hf_hub_download

        os.makedirs(WA_AUTH_DIR, exist_ok=True)
        zip_path = hf_hub_download(
            repo_id=HF_WA_REPO,
            filename="wa_auth.zip",
            repo_type="dataset",
            token=HF_TOKEN,
            force_download=True,
        )
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(WA_AUTH_DIR)
        logger.info("✅ Restored WhatsApp session from private HF dataset")
    except Exception as e:
        logger.info(f"[WA Session] No cloud backup restored yet: {e}")


def _backup_wa_session_sync():
    if not HF_TOKEN or not HF_WA_REPO:
        return
    creds_p = os.path.join(WA_AUTH_DIR, "creds.json")
    if not os.path.exists(creds_p):
        return
    try:
        from huggingface_hub import HfApi

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for fname in os.listdir(WA_AUTH_DIR):
                fpath = os.path.join(WA_AUTH_DIR, fname)
                if os.path.isfile(fpath):
                    zf.write(fpath, arcname=fname)
        buf.seek(0)
        api = HfApi(token=HF_TOKEN)
        api.create_repo(repo_id=HF_WA_REPO, repo_type="dataset", private=True, exist_ok=True)
        api.upload_file(
            path_or_fileobj=buf,
            path_in_repo="wa_auth.zip",
            repo_id=HF_WA_REPO,
            repo_type="dataset",
            commit_message="Sync WhatsApp session",
        )
        logger.info("☁️ Backed up WhatsApp session to private HF dataset")
    except Exception as e:
        logger.warning(f"[WA Session] Cloud backup warning: {e}")


def schedule_wa_session_backup():
    global _WA_BACKUP_TIMER
    if not HF_TOKEN:
        return
    if _WA_BACKUP_TIMER is not None:
        _WA_BACKUP_TIMER.cancel()
    _WA_BACKUP_TIMER = threading.Timer(5.0, _backup_wa_session_sync)
    _WA_BACKUP_TIMER.daemon = True
    _WA_BACKUP_TIMER.start()


def find_node_binary() -> Optional[str]:
    local_node = os.path.join(BASE_DIR, ".node_runtime", "bin", "node")
    if os.path.exists(local_node):
        return local_node
    return shutil.which("node")


def start_wa_bridge_daemon():
    node_bin = find_node_binary()
    script_path = os.path.join(BASE_DIR, "wa_bridge", "server.js")
    if not node_bin or not os.path.exists(script_path):
        logger.warning(f"[WA Bridge] Node or script missing (node={node_bin})")
        return

    def _runner():
        global _WA_PROC
        restore_wa_session_from_hf()
        env = os.environ.copy()
        env["WA_BRIDGE_PORT"] = str(WA_BRIDGE_PORT)
        env["WA_AUTH_DIR"] = WA_AUTH_DIR
        env["PORT"] = os.environ.get("PORT", "7860")
        while True:
            try:
                _WA_PROC = subprocess.Popen(
                    [node_bin, "--max-old-space-size=64", script_path],
                    cwd=os.path.join(BASE_DIR, "wa_bridge"),
                    env=env,
                )
                _WA_PROC.wait()
            except Exception as e:
                logger.error(f"[WA Bridge] Process error: {e}")
            time.sleep(3)

    threading.Thread(target=_runner, daemon=True).start()


def make_qr_png_bytes(qr_data: str) -> bytes:
    qr = qrcode.QRCode(version=None, box_size=9, border=3)
    qr.add_data(qr_data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def get_wa_bridge_status() -> dict:
    try:
        r = requests.get(f"{WA_BRIDGE_URL}/status", timeout=4)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return {"connected": False, "qr": None, "group_jid": None}


def get_channel_link_url(item: dict) -> str:
    key = item["key"]
    if key in saved_channel_links and saved_channel_links[key]:
        return saved_channel_links[key].strip()
    env_val = os.environ.get(item["env_var"], "").strip()
    if env_val:
        return env_val
    return (item.get("default_url") or "").strip()


def build_channel_links_kb() -> Optional[InlineKeyboardMarkup]:
    active = []
    for item in PIVOK_CHANNEL_LINKS:
        url = get_channel_link_url(item)
        if url:
            active.append(InlineKeyboardButton(text=item["text"], url=url))
    if not active:
        return None
    rows = [active[i : i + 2] for i in range(0, len(active), 2)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def prune_old_work_folders(keep_latest: int = 3):
    for base_d in (INCOMING_DIR, OUTPUT_DIR):
        try:
            if not os.path.isdir(base_d):
                continue
            entries = [
                (os.path.getmtime(os.path.join(base_d, d)), os.path.join(base_d, d))
                for d in os.listdir(base_d)
                if os.path.isdir(os.path.join(base_d, d))
            ]
            entries.sort(reverse=True)
            for _, old_path in entries[keep_latest:]:
                shutil.rmtree(old_path, ignore_errors=True)
        except Exception:
            pass


def get_lang(user_id: Optional[int], tg_lang_code: Optional[str] = None) -> str:
    if user_id is not None:
        saved = user_langs.get(str(user_id))
        if saved in ("ru", "tr", "ar", "en"):
            return saved
    return "ru"


def set_lang(user_id: int, lang: str):
    if lang in ("ru", "tr", "ar", "en"):
        user_langs[str(user_id)] = lang
        persist_store()


def tr_str(lang: str, key: str, **kwargs) -> str:
    text = STRINGS.get(lang, STRINGS["ru"]).get(key, STRINGS["ru"].get(key, key))
    return text.format(**kwargs) if kwargs else text


def build_keyboard(items, columns=2):
    builder = ReplyKeyboardBuilder()
    for item in items:
        builder.add(KeyboardButton(text=item))
    builder.adjust(columns)
    return builder.as_markup(resize_keyboard=True)


def main_menu_kb(lang: str):
    return build_keyboard(
        [
            tr_str(lang, "btn_create"),
            tr_str(lang, "btn_add_video_menu"),
            tr_str(lang, "btn_wa_qr"),
            tr_str(lang, "btn_channels"),
            tr_str(lang, "btn_lang"),
        ],
        2,
    )


def lang_inline_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🇷🇺 Русский", callback_data="setlang_ru"),
                InlineKeyboardButton(text="🇹🇷 Türkçe", callback_data="setlang_tr"),
                InlineKeyboardButton(text="🇸🇦 العربية", callback_data="setlang_ar"),
            ]
        ]
    )


def _save_and_analyze_one_sync(raw_bytes: bytes, dest_path: str) -> dict:
    """
    Saves photo to disk (capping max dimension to 1440px so memory stays tiny),
    runs YOLOv8-pose ONCE, and extracts both full-body score & dominant LAB color.
    """
    with Image.open(io.BytesIO(raw_bytes)) as im:
        im_rgb = ImageOps.exif_transpose(im).convert("RGB")
        if max(im_rgb.width, im_rgb.height) > 1440:
            im_rgb.thumbnail((1440, 1440), Image.Resampling.BILINEAR)
        im_rgb.save(dest_path, "JPEG", quality=88, optimize=True)

        max_dim = 640
        if max(im_rgb.width, im_rgb.height) > max_dim:
            sc = max_dim / float(max(im_rgb.width, im_rgb.height))
            small_rgb = im_rgb.resize(
                (max(1, int(im_rgb.width * sc)), max(1, int(im_rgb.height * sc))),
                Image.Resampling.BILINEAR,
            )
        else:
            small_rgb = im_rgb.copy()

    del im_rgb
    small_bgr = np.array(small_rgb)[:, :, ::-1]
    del small_rgb
    sh, sw = small_bgr.shape[:2]

    pose_data = yolo_local.detect_yolo(small_bgr, "yolov8n-pose")

    score = 0.0
    kps_all = pose_data.get("keypoints", [])
    if kps_all and len(kps_all[0]) >= 17:
        kps = kps_all[0]
        if any(5 < kps[idx][0] < sw - 5 and 5 < kps[idx][1] < sh - 5 for idx in (15, 16)):
            score = 2.0
        elif any(5 < kps[idx][0] < sw - 5 and 5 < kps[idx][1] < sh - 5 for idx in (13, 14)):
            score = 0.5
        else:
            score = -1.0

    lab = color_clustering.extract_dominant_lab_from_pose(small_bgr, pose_data)
    del small_bgr
    trim_memory()

    return {
        "temp_path": dest_path,
        "ext": "jpg",
        "score": score,
        "lab": lab.tolist() if lab is not None else None,
        "pose": pose_data,
    }


async def download_and_analyze_photo(f_id: str, dest_path: str, bot_inst: Bot) -> dict:
    for attempt in range(3):
        try:
            file_info = await asyncio.wait_for(bot_inst.get_file(f_id), timeout=20.0)
            downloaded = await asyncio.wait_for(bot_inst.download_file(file_info.file_path), timeout=45.0)
            raw_bytes = downloaded.read()
            del downloaded
            res = await asyncio.to_thread(_save_and_analyze_one_sync, raw_bytes, dest_path)
            del raw_bytes
            trim_memory()
            return res
        except Exception as e:
            logger.error(f"Error downloading/analyzing {f_id} (attempt {attempt + 1}): {e}")
            await asyncio.sleep(0.4)
    raise RuntimeError(f"Download failed: {f_id}")


class SecurityMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        user_id = user.id if user else None
        if user_id and user_id not in ALLOWED_USERS:
            ALLOWED_USERS.append(user_id)
            persist_store()
        return await handler(event, data)


class CollageForm(StatesGroup):
    photo = State()
    grouping_mode = State()
    code = State()
    size = State()
    price = State()
    confirm = State()
    edit_field = State()


class VideoUploadForm(StatesGroup):
    waiting_for_code = State()
    waiting_for_video = State()


bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()
dp.message.outer_middleware(SecurityMiddleware())
dp.callback_query.outer_middleware(SecurityMiddleware())


def build_preview_keyboard(
    folder_name: str, photos: list[str], chat_id: int, short_id: str, lang: str
) -> InlineKeyboardMarkup:
    encoded_folder = urllib.parse.quote(folder_name)
    crop_buttons = []
    if PUBLIC_BASE_URL.startswith("https://"):
        for i, p_path in enumerate(photos):
            stem = os.path.splitext(os.path.basename(p_path))[0]
            display_name = stem.split("_", 1)[-1] if "_" in stem else stem
            crop_buttons.append(
                InlineKeyboardButton(
                    text=f"🛠 {display_name}",
                    web_app=WebAppInfo(
                        url=f"{PUBLIC_BASE_URL}/webapp/index.html?folder={encoded_folder}&chat_id={chat_id}&pair={i}&lang={lang}"
                    ),
                )
            )

    crop_rows = [crop_buttons[i : i + 3] for i in range(0, len(crop_buttons), 3)]
    layout = [
        [InlineKeyboardButton(text=tr_str(lang, "btn_pub_both"), callback_data=f"pub_all_{short_id}")],
        [
            InlineKeyboardButton(text=tr_str(lang, "btn_pub_tg"), callback_data=f"pub_tg_{short_id}"),
            InlineKeyboardButton(text=tr_str(lang, "btn_pub_wa"), callback_data=f"pub_wa_{short_id}"),
        ],
        [InlineKeyboardButton(text=tr_str(lang, "btn_add_video"), callback_data=f"addvid_{short_id}")],
    ]
    layout.extend(crop_rows)
    layout.extend(
        [
            [InlineKeyboardButton(text=tr_str(lang, "btn_refresh"), callback_data=f"refresh_{short_id}")],
            [InlineKeyboardButton(text=tr_str(lang, "btn_add_color"), callback_data=f"addcolor_{short_id}")],
            [InlineKeyboardButton(text=tr_str(lang, "btn_delete"), callback_data=f"del_{short_id}")],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=layout)


async def send_collage_preview(chat_id: int, folder_name: str, lang: Optional[str] = None):
    if not bot:
        return
    lang = lang or get_lang(chat_id)
    folder_path = os.path.join(OUTPUT_DIR, folder_name)
    if not os.path.isdir(folder_path):
        await bot.send_message(chat_id, tr_str(lang, "folder_missing"))
        return

    photos = sorted(
        os.path.join(folder_path, f)
        for f in os.listdir(folder_path)
        if f.lower().endswith((".jpg", ".jpeg", ".png"))
    )
    if not photos:
        await bot.send_message(chat_id, tr_str(lang, "gen_empty"))
        return

    short_id = hashlib.md5(folder_name.encode("utf-8")).hexdigest()[:8]
    for mid in media_msgs_store.get(short_id, []):
        try:
            await bot.delete_message(chat_id, mid)
        except Exception:
            pass

    all_sent_ids = []
    for chunk_start in range(0, len(photos), 10):
        chunk = photos[chunk_start : chunk_start + 10]
        msgs = await bot.send_media_group(chat_id, media=[InputMediaPhoto(media=FSInputFile(p)) for p in chunk])
        all_sent_ids.extend(m.message_id for m in msgs)

    media_msgs_store[short_id] = all_sent_ids
    pending_dispatches[short_id] = folder_path
    persist_store()

    _, _, code, sizes, price = collage_engine.parse_folder_metadata(folder_name)
    kb = build_preview_keyboard(folder_name, photos, chat_id, short_id, lang)
    await bot.send_message(
        chat_id,
        tr_str(lang, "preview_ready", n=len(photos), code=code, price=price, sizes=sizes),
        reply_markup=kb,
        parse_mode="Markdown",
    )


@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    PHOTO_BUFFER.pop(message.chat.id, None)
    await state.clear()
    lang = get_lang(message.from_user.id, message.from_user.language_code)
    await message.answer(
        tr_str(lang, "welcome"),
        reply_markup=main_menu_kb(lang),
        parse_mode="Markdown",
    )


@dp.message(Command("whatsapp", "wa", "qr"))
@dp.message(F.text.in_(WA_QR_BTN_TEXTS))
async def cmd_whatsapp_qr(message: Message):
    lang = get_lang(message.from_user.id, message.from_user.language_code)
    st = await asyncio.to_thread(get_wa_bridge_status)
    if st.get("connected"):
        grp = st.get("group_subject") or st.get("group_jid") or "PIVOK"
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=tr_str(lang, "wa_relink_btn"), callback_data="wa_reset_session")]
            ]
        )
        await message.answer(
            tr_str(lang, "wa_ready", grp=grp),
            reply_markup=kb,
            parse_mode="Markdown",
        )
        return

    _WA_QR_WAITERS.add(message.chat.id)
    qr_str = st.get("qr")
    if not qr_str:
        wait_msg = await message.answer(tr_str(lang, "wa_gen_qr"))
        for _ in range(8):
            await asyncio.sleep(1.2)
            st = await asyncio.to_thread(get_wa_bridge_status)
            if st.get("connected"):
                await wait_msg.edit_text(tr_str(lang, "wa_ready", grp="PIVOK"), parse_mode="Markdown")
                return
            if st.get("qr"):
                qr_str = st["qr"]
                break
        try:
            await wait_msg.delete()
        except Exception:
            pass

    if not qr_str:
        await message.answer(
            tr_str(lang, "wa_starting"),
            parse_mode="Markdown",
        )
        return

    png_bytes = await asyncio.to_thread(make_qr_png_bytes, qr_str)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=tr_str(lang, "wa_refresh_qr_btn"), callback_data="wa_refresh_qr")]]
    )
    await message.answer_photo(
        photo=BufferedInputFile(png_bytes, filename="whatsapp_qr.png"),
        caption=tr_str(lang, "wa_qr_caption"),
        reply_markup=kb,
        parse_mode="Markdown",
    )


@dp.callback_query(F.data.in_({"wa_refresh_qr", "wa_reset_session"}))
async def cb_wa_manage(callback: CallbackQuery):
    lang = get_lang(callback.from_user.id, callback.from_user.language_code)
    if callback.data == "wa_reset_session":
        await asyncio.to_thread(lambda: requests.post(f"{WA_BRIDGE_URL}/reset-session", timeout=6))
        await callback.answer("🔄")
        await asyncio.sleep(2.5)
    else:
        await callback.answer("🔄")
    _WA_QR_WAITERS.add(callback.message.chat.id)
    st = await asyncio.to_thread(get_wa_bridge_status)
    if st.get("connected"):
        await callback.message.answer(tr_str(lang, "wa_ready", grp="PIVOK"), parse_mode="Markdown")
        return
    qr_str = st.get("qr")
    if qr_str:
        png_bytes = await asyncio.to_thread(make_qr_png_bytes, qr_str)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=tr_str(lang, "wa_refresh_qr_btn"), callback_data="wa_refresh_qr")]]
        )
        await callback.message.answer_photo(
            photo=BufferedInputFile(png_bytes, filename="whatsapp_qr.png"),
            caption=tr_str(lang, "wa_qr_caption"),
            reply_markup=kb,
            parse_mode="Markdown",
        )
    else:
        await callback.message.answer(tr_str(lang, "wa_starting"), parse_mode="Markdown")


@dp.message(Command("channels"))
@dp.message(F.text.in_(CHANNELS_BTN_TEXTS))
async def cmd_channels(message: Message):
    lang = get_lang(message.from_user.id, message.from_user.language_code)
    kb = build_channel_links_kb()
    if kb:
        await message.answer(
            tr_str(lang, "channels_title"),
            reply_markup=kb,
            parse_mode="Markdown",
        )
    else:
        await message.answer(tr_str(lang, "channels_empty"))


@dp.message(Command("lang", "language", "dil"))
@dp.message(F.text.in_(LANG_BTN_TEXTS))
async def cmd_language(message: Message):
    lang = get_lang(message.from_user.id, message.from_user.language_code)
    await message.answer(
        tr_str(lang, "lang_prompt"),
        reply_markup=lang_inline_kb(),
        parse_mode="Markdown",
    )


@dp.callback_query(F.data.in_({"setlang_ru", "setlang_tr", "setlang_ar", "setlang_en"}))
async def cb_set_language(callback: CallbackQuery, state: FSMContext):
    if callback.data.endswith("_tr"):
        new_lang = "tr"
        toast = "🇹🇷 Türkçe"
    elif callback.data.endswith("_ar"):
        new_lang = "ar"
        toast = "🇸🇦 العربية"
    elif callback.data.endswith("_en"):
        new_lang = "en"
        toast = "🇬🇧 English"
    else:
        new_lang = "ru"
        toast = "🇷🇺 Русский"
    set_lang(callback.from_user.id, new_lang)
    await callback.answer(toast)
    try:
        await callback.message.delete()
    except Exception:
        pass
    cur_state = await state.get_state()
    if cur_state is None:
        await bot.send_message(
            callback.message.chat.id,
            tr_str(new_lang, "lang_switched") + "\n\n" + tr_str(new_lang, "welcome"),
            reply_markup=main_menu_kb(new_lang),
            parse_mode="Markdown",
        )
    else:
        await bot.send_message(
            callback.message.chat.id,
            tr_str(new_lang, "lang_switched"),
            parse_mode="Markdown",
        )


@dp.message(Command("cancel", "stop"))
@dp.message(F.text.in_(CANCEL_TEXTS))
async def cmd_cancel(message: Message, state: FSMContext):
    PHOTO_BUFFER.pop(message.chat.id, None)
    await state.clear()
    lang = get_lang(message.from_user.id, message.from_user.language_code)
    await message.answer(tr_str(lang, "cancelled"), reply_markup=main_menu_kb(lang))


@dp.message(F.text.in_(CREATE_TEXTS))
async def cmd_create_collage(message: Message, state: FSMContext):
    PHOTO_BUFFER.pop(message.chat.id, None)
    await state.clear()
    await state.set_state(CollageForm.photo)
    lang = get_lang(message.from_user.id, message.from_user.language_code)
    await message.answer(
        tr_str(lang, "send_photos_prompt"),
        reply_markup=build_keyboard(
            [tr_str(lang, "btn_pair_2"), tr_str(lang, "btn_single_1"), tr_str(lang, "btn_cancel")], 2
        ),
        parse_mode="Markdown",
    )


def extract_code_from_text(text: str) -> Optional[str]:
    if not text:
        return None
    raw = text.strip()
    m = re.search(r"(?:CODE|KOD|КОД)\s*[-:]?\s*([A-Za-z0-9_-]+)", raw, flags=re.IGNORECASE)
    if m:
        return m.group(1).upper()
    first_line = raw.splitlines()[0].strip()
    first_line = re.sub(r"[\U00010000-\U0010ffff]|[\uD800-\uDBFF][\uDC00-\uDFFF]", " ", first_line)
    tokens = [t for t in re.split(r"[\s,;|]+", first_line) if t]
    for t in tokens:
        if re.match(r"^[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*$", t) and (any(c.isdigit() for c in t) or len(t) >= 4):
            if t.endswith("$") or t.startswith("$"):
                continue
            if re.match(r"^\d{2}-\d{2}$", t) and int(t.split("-")[0]) >= 34:
                continue
            return t.upper()
    return None


async def _publish_video_file(
    bot_inst: Bot,
    chat_id: int,
    video_path: str,
    code: str,
    lang: str = "ru",
) -> dict:
    tg_caption = _build_tg_collage_caption(code)
    wa_caption = _build_wa_collage_caption(code, "", "")

    # 1. Telegram
    tg_sent = []
    target_channels = []
    vid_ch = os.environ.get("LINK_VIDEO_CHANNEL", "@pivokvideo").strip()
    if vid_ch:
        if "t.me/" in vid_ch:
            vid_ch = "@" + vid_ch.split("t.me/")[-1].strip("/")
        target_channels.append(vid_ch)
    main_ch = os.environ.get("TELEGRAM_CHANNEL", "@pivoklaleli").strip()
    if main_ch and main_ch not in target_channels:
        target_channels.append(main_ch)

    for ch in target_channels:
        try:
            await bot_inst.send_video(
                chat_id=ch,
                video=FSInputFile(video_path),
                caption=tg_caption,
                parse_mode="HTML",
            )
            tg_sent.append(ch)
        except Exception as e:
            logger.warning(f"Failed sending video to {ch}: {e}")

    # 2. WhatsApp
    wa_ok = False
    wa_err = ""
    try:
        wa_st = await asyncio.to_thread(get_wa_bridge_status)
        if wa_st.get("connected"):
            target_grp = os.environ.get("WHATSAPP_GROUP_ID", "").strip() or wa_st.get("group_jid") or ""

            def _post_wa_vid():
                return requests.post(
                    f"{WA_BRIDGE_URL}/send-video",
                    json={
                        "target": target_grp,
                        "file_path": video_path,
                        "caption": wa_caption,
                    },
                    timeout=120,
                )

            resp = await asyncio.to_thread(_post_wa_vid)
            if resp.status_code == 200 and resp.json().get("success"):
                wa_ok = True
            else:
                wa_err = resp.json().get("error", resp.text)
        else:
            wa_err = tr_str(lang, "pub_wa_not_linked")
    except Exception as e:
        wa_err = str(e)

    return {
        "tg_sent": tg_sent,
        "wa_ok": wa_ok,
        "wa_err": wa_err,
    }


async def _execute_collage_creation(
    chat_id: int,
    photo_ids: list[str],
    code: str,
    sizes: str = "",
    price: str = "",
    is_single: bool = False,
    grouping_mode: str = "smart",
    lang: str = "ru",
):
    safe_code = code.replace("|", "~").replace("_", "-").replace("/", "-")
    safe_size = sizes.replace("|", "~").replace("_", "-") if sizes else "-"
    safe_price = price.replace("|", "~").replace("_", "-") if price else "-"
    cat_tag = "ОДИНАРНЫЙ" if is_single else "ПЛАТЬЕ"
    folder_name = f"PIVOK_{cat_tag}_{safe_code}_{safe_size}_{safe_price}_{int(time.time())}"
    inc_folder = os.path.join(INCOMING_DIR, folder_name)

    prune_old_work_folders(keep_latest=3)
    os.makedirs(inc_folder, exist_ok=True)

    prog_msg = await bot.send_message(
        chat_id,
        tr_str(lang, "generating", n=len(photo_ids)),
        reply_markup=ReplyKeyboardRemove(),
    )

    try:
        async with _GEN_LOCK:
            analyzed = []
            tot = len(photo_ids)
            last_edit_t = time.time()

            for idx, fid in enumerate(photo_ids, 1):
                tmp_p = os.path.join(inc_folder, f"__raw_{idx:03d}.tmpjpg")
                item = await download_and_analyze_photo(fid, tmp_p, bot)
                analyzed.append(item)

                now_t = time.time()
                if now_t - last_edit_t >= 2.0 or idx == tot:
                    last_edit_t = now_t
                    try:
                        await prog_msg.edit_text(
                            tr_str(lang, "prog_step", cur=idx, tot=tot),
                            parse_mode="Markdown",
                        )
                    except Exception:
                        pass
                await asyncio.sleep(0.04)

            _RECENT_CODES[str(chat_id)] = code.upper()
            persist_store()

            pose_cache = {}
            if is_single or tot == 1:
                for idx, it in enumerate(analyzed, 1):
                    final_name = f"{idx:02d}_COLOR-{idx}.jpg"
                    os.replace(it["temp_path"], os.path.join(inc_folder, final_name))
                    pose_cache[final_name] = it["pose"]
            else:
                pairs = [analyzed[i : i + 2] for i in range(0, len(analyzed), 2)]
                labels = [f"COLOR{p_idx + 1}" for p_idx in range(len(pairs))]

                for p_idx, pair in enumerate(pairs):
                    if len(pair) < 2:
                        for leftover in pair:
                            try:
                                os.remove(leftover["temp_path"])
                            except Exception:
                                pass
                        continue
                    lbl = labels[p_idx] if p_idx < len(labels) else f"COLOR{p_idx + 1}"
                    prefix = f"{p_idx + 1:02d}_{lbl}"
                    for k, it in enumerate(pair, 1):
                        final_name = f"{prefix}-{k}.jpg"
                        os.replace(it["temp_path"], os.path.join(inc_folder, final_name))
                        pose_cache[final_name] = it["pose"]

            with open(os.path.join(inc_folder, "pose_cache.json"), "w", encoding="utf-8") as pf:
                json.dump(pose_cache, pf)

            del analyzed
            trim_memory()

            try:
                await prog_msg.edit_text(tr_str(lang, "prog_render"))
            except Exception:
                pass

            await asyncio.to_thread(collage_engine.generate_folder_collages, folder_name, inc_folder)
            trim_memory()

        try:
            await prog_msg.delete()
        except Exception:
            pass

        await send_collage_preview(chat_id, folder_name, lang=lang)
        await bot.send_message(
            chat_id,
            tr_str(lang, "next_model_hint"),
            reply_markup=main_menu_kb(lang),
        )
    except Exception as e:
        logger.exception("Error generating collages")
        await bot.send_message(
            chat_id,
            tr_str(lang, "err_gen", e=str(e)),
            reply_markup=main_menu_kb(lang),
        )


def _extract_sorted_ids(buf: dict) -> list[str]:
    items = buf.get("items", [])
    if items:
        items.sort(key=lambda x: x[0])
        return [x[1] for x in items]
    return buf.get("ids", [])


@dp.message(StateFilter(None, CollageForm.photo), F.photo | F.document)
async def handle_incoming_photo(message: Message, state: FSMContext):
    lang = get_lang(message.from_user.id, message.from_user.language_code)
    if message.document:
        if not (message.document.mime_type or "").startswith("image/"):
            await message.answer(tr_str(lang, "only_images"))
            return
        file_id = message.document.file_id
    else:
        file_id = message.photo[-1].file_id

    if await state.get_state() is None:
        await state.set_state(CollageForm.photo)

    chat_id = message.chat.id
    if chat_id not in PHOTO_BUFFER:
        PHOTO_BUFFER[chat_id] = {"items": [], "ids": [], "task": None, "lang": lang, "detected_code": None}
    PHOTO_BUFFER[chat_id]["items"].append((message.message_id, file_id))
    PHOTO_BUFFER[chat_id]["ids"].append(file_id)
    PHOTO_BUFFER[chat_id]["lang"] = lang

    # Auto-detect code from caption if provided with photos (super fast!)
    if message.caption:
        cand_code = extract_code_from_text(message.caption)
        if cand_code:
            PHOTO_BUFFER[chat_id]["detected_code"] = cand_code

    if PHOTO_BUFFER[chat_id]["task"]:
        PHOTO_BUFFER[chat_id]["task"].cancel()
    PHOTO_BUFFER[chat_id]["task"] = asyncio.create_task(_flush_photo_buffer(chat_id, state))


async def _flush_photo_buffer(chat_id: int, state: FSMContext):
    try:
        await asyncio.sleep(0.85)
    except asyncio.CancelledError:
        return

    buf = PHOTO_BUFFER.pop(chat_id, None)
    if not buf:
        return

    lang = buf.get("lang") or get_lang(chat_id)
    detected_code = buf.get("detected_code")
    data = await state.get_data()
    photo_ids = data.get("photo_ids", [])
    photo_ids.extend(_extract_sorted_ids(buf))
    await state.update_data(photo_ids=photo_ids)

    # If this was extra photos for adding a color:
    if data.get("is_add_color") and data.get("target_folder"):
        kb = build_keyboard([tr_str(lang, "btn_all_loaded"), tr_str(lang, "btn_cancel")], 1)
        await bot.send_message(
            chat_id,
            tr_str(lang, "photos_buffered", n=len(photo_ids)),
            reply_markup=kb,
            parse_mode="Markdown",
        )
        return

    # FAST-PATH 1: If user sent photos WITH a caption containing the code, proceed to ask sizes:
    if detected_code:
        is_single = len(photo_ids) == 1
        await state.set_state(CollageForm.size)
        await state.update_data(
            photo_ids=photo_ids,
            code=detected_code,
            is_single=is_single,
            grouping_mode="smart",
        )
        await bot.send_message(
            chat_id,
            f"🏷️ `{detected_code}`\n\n" + tr_str(lang, "ask_size"),
            reply_markup=build_keyboard(SIZE_LIST + [tr_str(lang, "btn_cancel")], 3),
            parse_mode="Markdown",
        )
        return

    # FAST-PATH 2: Ask for Code (then size and price in fast sequence, no confirmation screen)
    is_single = len(photo_ids) == 1
    await state.set_state(CollageForm.code)
    await state.update_data(is_single=is_single, grouping_mode="smart")
    await bot.send_message(
        chat_id,
        tr_str(lang, "photos_buffered_ask_code", n=len(photo_ids)),
        reply_markup=build_keyboard([tr_str(lang, "btn_cancel")], 1),
        parse_mode="Markdown",
    )


@dp.message(CollageForm.photo, F.text.in_(DONE_PHOTO_TEXTS))
async def handle_all_photos_loaded(message: Message, state: FSMContext):
    chat_id = message.chat.id
    lang = get_lang(message.from_user.id, message.from_user.language_code)
    if chat_id in PHOTO_BUFFER:
        buf = PHOTO_BUFFER.pop(chat_id)
        if buf.get("task"):
            buf["task"].cancel()
        data = await state.get_data()
        p_ids = data.get("photo_ids", [])
        p_ids.extend(_extract_sorted_ids(buf))
        await state.update_data(photo_ids=p_ids)

    data = await state.get_data()
    photo_ids = data.get("photo_ids", [])
    if not photo_ids:
        await message.answer(tr_str(lang, "no_photos_yet"))
        return

    if data.get("is_add_color") and data.get("target_folder"):
        folder_name = data["target_folder"]
        inc_folder = os.path.join(INCOMING_DIR, folder_name)
        os.makedirs(inc_folder, exist_ok=True)
        wait_m = await message.answer(tr_str(lang, "processing_extra"), reply_markup=ReplyKeyboardRemove())

        async with _GEN_LOCK:
            pose_cache_path = os.path.join(inc_folder, "pose_cache.json")
            pose_cache = {}
            if os.path.exists(pose_cache_path):
                try:
                    with open(pose_cache_path, "r", encoding="utf-8") as pf:
                        pose_cache = json.load(pf)
                except Exception:
                    pass

            existing = [f for f in os.listdir(inc_folder) if f.lower().endswith((".jpg", ".jpeg", ".png"))]
            next_idx = (len(existing) // 2) + 1

            analyzed = []
            for idx, fid in enumerate(photo_ids, 1):
                tmp_p = os.path.join(inc_folder, f"__raw_extra_{idx}.tmpjpg")
                item = await download_and_analyze_photo(fid, tmp_p, bot)
                analyzed.append(item)
                await asyncio.sleep(0.04)

            if len(analyzed) >= 2:
                pairs = [analyzed[i : i + 2] for i in range(0, len(analyzed), 2)]
                for p_idx, pair in enumerate(pairs):
                    if len(pair) < 2:
                        for leftover in pair:
                            try:
                                os.remove(leftover["temp_path"])
                            except Exception:
                                pass
                        continue
                    prefix = f"{next_idx + p_idx:02d}_COLOR{next_idx + p_idx}"
                    for k, it in enumerate(pair, 1):
                        final_name = f"{prefix}-{k}.jpg"
                        final_path = os.path.join(inc_folder, final_name)
                        os.replace(it["temp_path"], final_path)
                        pose_cache[final_name] = it["pose"]
                with open(pose_cache_path, "w", encoding="utf-8") as pf:
                    json.dump(pose_cache, pf)
                await asyncio.to_thread(collage_engine.generate_folder_collages, folder_name, inc_folder)
            else:
                out_folder = os.path.join(OUTPUT_DIR, folder_name)
                os.makedirs(out_folder, exist_ok=True)
                shutil.move(analyzed[0]["temp_path"], os.path.join(out_folder, f"{next_idx:02d}_EXTRA.jpg"))
            del analyzed
            trim_memory()

        await state.clear()
        try:
            await wait_m.delete()
        except Exception:
            pass
        await send_collage_preview(chat_id, folder_name, lang=lang)
        return

    is_single = message.text in SINGLE_MODE_TEXTS
    await state.update_data(
        is_single=is_single,
        grouping_mode="sequential" if is_single else "smart",
    )
    await state.set_state(CollageForm.code)
    await message.answer(
        tr_str(lang, "ask_code"),
        reply_markup=build_keyboard([tr_str(lang, "btn_cancel")], 1),
        parse_mode="Markdown",
    )


@dp.message(CollageForm.code)
async def handle_code(message: Message, state: FSMContext):
    lang = get_lang(message.from_user.id, message.from_user.language_code)
    txt = (message.text or "").strip()
    if txt in CANCEL_TEXTS:
        PHOTO_BUFFER.pop(message.chat.id, None)
        await state.clear()
        await message.answer(tr_str(lang, "cancelled"), reply_markup=main_menu_kb(lang))
        return

    code_val = extract_code_from_text(txt) or txt.upper()
    if not code_val:
        await message.answer(tr_str(lang, "err_code"))
        return

    await state.update_data(code=code_val)
    await state.set_state(CollageForm.size)
    size_prompt = tr_str(lang, "ask_size")
    size_kb = build_keyboard(SIZE_LIST + [tr_str(lang, "btn_cancel")], 3)
    try:
        await message.answer(size_prompt, reply_markup=size_kb, parse_mode="Markdown")
    except Exception:
        await message.answer(size_prompt, reply_markup=size_kb)


@dp.message(CollageForm.size)
async def handle_size(message: Message, state: FSMContext):
    lang = get_lang(message.from_user.id, message.from_user.language_code)
    txt = (message.text or "").strip()
    if txt in CANCEL_TEXTS:
        PHOTO_BUFFER.pop(message.chat.id, None)
        await state.clear()
        await message.answer(tr_str(lang, "cancelled"), reply_markup=main_menu_kb(lang))
        return

    if not txt:
        await message.answer(tr_str(lang, "err_size"))
        return

    await state.update_data(size=txt)
    await state.set_state(CollageForm.price)
    price_prompt = tr_str(lang, "ask_price")
    price_kb = build_keyboard([tr_str(lang, "btn_cancel")], 1)
    try:
        await message.answer(price_prompt, reply_markup=price_kb, parse_mode="Markdown")
    except Exception:
        await message.answer(price_prompt, reply_markup=price_kb)


@dp.message(CollageForm.price)
async def handle_price(message: Message, state: FSMContext):
    lang = get_lang(message.from_user.id, message.from_user.language_code)
    txt = (message.text or "").strip()
    if txt in CANCEL_TEXTS:
        PHOTO_BUFFER.pop(message.chat.id, None)
        await state.clear()
        await message.answer(tr_str(lang, "cancelled"), reply_markup=main_menu_kb(lang))
        return

    price_val = txt.replace("$", "").strip()
    if not price_val:
        await message.answer(tr_str(lang, "err_price"))
        return

    data = await state.get_data()
    photo_ids = data.get("photo_ids", [])
    code = data.get("code") or "PIVOK"
    size = data.get("size") or ""
    is_single = data.get("is_single", False)
    grouping_mode = data.get("grouping_mode", "smart")
    await state.clear()

    if not photo_ids:
        await message.answer(tr_str(lang, "no_photos_yet"), reply_markup=main_menu_kb(lang))
        return

    # Fast creation with code, size and price (no redundant confirmation screen!)
    await _execute_collage_creation(
        chat_id=message.chat.id,
        photo_ids=photo_ids,
        code=code,
        sizes=size,
        price=price_val,
        is_single=is_single,
        grouping_mode=grouping_mode,
        lang=lang,
    )


# -------------------------------------------------------------
# VIDEO UPLOAD HANDLERS ("окошко для отправки видео как в микс")
# -------------------------------------------------------------

@dp.callback_query(F.data.startswith("addvid_"))
async def cb_add_video_prompt(callback: CallbackQuery, state: FSMContext):
    lang = get_lang(callback.from_user.id, callback.from_user.language_code)
    short_id = callback.data.split("_", 1)[1]
    folder_path = pending_dispatches.get(short_id) or ""
    folder_name = os.path.basename(folder_path) if folder_path else ""
    code = "PIVOK"
    if folder_name:
        _, _, code, _, _ = collage_engine.parse_folder_metadata(folder_name)
    elif short_id in published_records:
        code = published_records[short_id].get("code", "PIVOK")

    await state.set_state(VideoUploadForm.waiting_for_video)
    await state.update_data(vid_code=code, short_id=short_id)
    await callback.answer()

    cancel_kb = build_keyboard([tr_str(lang, "btn_cancel")], 1)
    await bot.send_message(
        callback.message.chat.id,
        tr_str(lang, "ask_video_prompt", code=code),
        reply_markup=cancel_kb,
        parse_mode="Markdown",
    )


@dp.message(F.text.in_(ADD_VIDEO_TEXTS))
async def cmd_add_video(message: Message, state: FSMContext):
    lang = get_lang(message.from_user.id, message.from_user.language_code)
    recent = _RECENT_CODES.get(str(message.chat.id)) or _RECENT_CODES.get(message.chat.id)

    await state.set_state(VideoUploadForm.waiting_for_code)
    buttons = []
    if recent:
        buttons.append(tr_str(lang, "btn_video_recent", code=recent))
    buttons.append(tr_str(lang, "btn_cancel"))

    await message.answer(
        tr_str(lang, "ask_video_code"),
        reply_markup=build_keyboard(buttons, 1),
        parse_mode="Markdown",
    )


@dp.message(VideoUploadForm.waiting_for_code)
async def handle_video_code_input(message: Message, state: FSMContext):
    lang = get_lang(message.from_user.id, message.from_user.language_code)
    txt = (message.text or "").strip()
    if txt in CANCEL_TEXTS:
        await state.clear()
        await message.answer(tr_str(lang, "cancelled"), reply_markup=main_menu_kb(lang))
        return

    recent = _RECENT_CODES.get(str(message.chat.id)) or _RECENT_CODES.get(message.chat.id)
    if recent and recent in txt:
        code = recent
    else:
        code = extract_code_from_text(txt) or txt.upper()

    if not code:
        await message.answer(tr_str(lang, "err_code"))
        return

    _RECENT_CODES[str(message.chat.id)] = code
    persist_store()
    await state.set_state(VideoUploadForm.waiting_for_video)
    await state.update_data(vid_code=code)
    await message.answer(
        tr_str(lang, "ask_video_prompt", code=code),
        reply_markup=build_keyboard([tr_str(lang, "btn_cancel")], 1),
        parse_mode="Markdown",
    )


@dp.message(VideoUploadForm.waiting_for_video, F.text.in_(CANCEL_TEXTS))
async def cancel_video_upload(message: Message, state: FSMContext):
    await state.clear()
    lang = get_lang(message.from_user.id, message.from_user.language_code)
    await message.answer(tr_str(lang, "cancelled"), reply_markup=main_menu_kb(lang))


@dp.message(VideoUploadForm.waiting_for_video, F.video | F.document)
async def handle_incoming_video(message: Message, state: FSMContext):
    lang = get_lang(message.from_user.id, message.from_user.language_code)
    data = await state.get_data()
    code = data.get("vid_code") or _RECENT_CODES.get(str(message.chat.id)) or _RECENT_CODES.get(message.chat.id) or "PIVOK"

    media_obj = None
    if message.video:
        media_obj = message.video
    elif message.document:
        mime = (message.document.mime_type or "").lower()
        fname = (message.document.file_name or "").lower()
        if mime.startswith("video/") or fname.endswith((".mp4", ".mov", ".m4v", ".avi", ".mkv")):
            media_obj = message.document

    if not media_obj:
        await message.answer(tr_str(lang, "video_invalid"))
        return

    await state.clear()
    status_msg = await message.answer(tr_str(lang, "video_uploading", code=code), parse_mode="Markdown")

    vid_dir = os.path.join(TEMP_DIR, "videos")
    os.makedirs(vid_dir, exist_ok=True)
    temp_vid_path = os.path.join(vid_dir, f"vid_{message.message_id}_{int(time.time())}.mp4")

    try:
        await bot.download(media_obj, destination=temp_vid_path)
        res = await _publish_video_file(bot, message.chat.id, temp_vid_path, code=code, lang=lang)

        tg_res_str = ", ".join(res["tg_sent"]) if res["tg_sent"] else "⚠️ Ошибка отправки"
        wa_res_str = "✅ Опубликовано в группе PÎVOK FASHION" if res["wa_ok"] else f"⚠️ {res['wa_err']}"

        report_text = tr_str(
            lang,
            "video_done",
            code=code,
            tg=tg_res_str,
            wa=wa_res_str,
        )
        try:
            await status_msg.edit_text(report_text, parse_mode="Markdown")
        except Exception:
            await message.answer(report_text, parse_mode="Markdown")
    except Exception as e:
        logger.exception("Error processing video upload")
        await message.answer(f"❌ Ошибка при отправке видео: {e}")
    finally:
        try:
            if os.path.exists(temp_vid_path):
                os.remove(temp_vid_path)
        except Exception:
            pass

    await bot.send_message(
        message.chat.id,
        tr_str(lang, "choose_below"),
        reply_markup=main_menu_kb(lang),
    )


@dp.message(F.video | F.document)
async def handle_direct_video(message: Message, state: FSMContext):
    media_obj = None
    if message.video:
        media_obj = message.video
    elif message.document:
        mime = (message.document.mime_type or "").lower()
        fname = (message.document.file_name or "").lower()
        if mime.startswith("video/") or fname.endswith((".mp4", ".mov", ".m4v", ".avi", ".mkv")):
            media_obj = message.document

    if not media_obj:
        return

    lang = get_lang(message.from_user.id, message.from_user.language_code)
    code = extract_code_from_text(message.caption or "") or _RECENT_CODES.get(str(message.chat.id)) or _RECENT_CODES.get(message.chat.id)
    if not code:
        await state.set_state(VideoUploadForm.waiting_for_code)
        await state.update_data(pending_video_file_id=media_obj.file_id)
        await message.answer(
            tr_str(lang, "ask_video_code"),
            reply_markup=build_keyboard([tr_str(lang, "btn_cancel")], 1),
            parse_mode="Markdown",
        )
        return

    status_msg = await message.answer(tr_str(lang, "video_uploading", code=code), parse_mode="Markdown")
    vid_dir = os.path.join(TEMP_DIR, "videos")
    os.makedirs(vid_dir, exist_ok=True)
    temp_vid_path = os.path.join(vid_dir, f"vid_{message.message_id}_{int(time.time())}.mp4")

    try:
        await bot.download(media_obj, destination=temp_vid_path)
        res = await _publish_video_file(bot, message.chat.id, temp_vid_path, code=code, lang=lang)

        tg_res_str = ", ".join(res["tg_sent"]) if res["tg_sent"] else "⚠️ Ошибка отправки"
        wa_res_str = "✅ Опубликовано в группе PÎVOK FASHION" if res["wa_ok"] else f"⚠️ {res['wa_err']}"

        report_text = tr_str(
            lang,
            "video_done",
            code=code,
            tg=tg_res_str,
            wa=wa_res_str,
        )
        try:
            await status_msg.edit_text(report_text, parse_mode="Markdown")
        except Exception:
            await message.answer(report_text, parse_mode="Markdown")
    except Exception as e:
        logger.exception("Error processing direct video upload")
        await message.answer(f"❌ Ошибка при отправке видео: {e}")
    finally:
        try:
            if os.path.exists(temp_vid_path):
                os.remove(temp_vid_path)
        except Exception:
            pass

    await bot.send_message(
        message.chat.id,
        tr_str(lang, "choose_below"),
        reply_markup=main_menu_kb(lang),
    )


@dp.message(CollageForm.edit_field)
async def handle_edit_field(message: Message, state: FSMContext):
    lang = get_lang(message.from_user.id, message.from_user.language_code)
    data = await state.get_data()
    val = (message.text or "").strip()
    photo_ids = data.get("photo_ids", [])
    code = data.get("code") or "PIVOK"
    if val:
        code = val.upper()
    await state.clear()
    if photo_ids:
        await _execute_collage_creation(
            chat_id=message.chat.id,
            photo_ids=photo_ids,
            code=code,
            lang=lang,
        )
    else:
        await message.answer(tr_str(lang, "cancelled"), reply_markup=main_menu_kb(lang))


def _build_tg_collage_caption(code: str, sizes: str = "", price: str = "") -> str:
    wa_url = get_channel_link_url(PIVOK_CHANNEL_LINKS[1]) or "https://chat.whatsapp.com/Ddh7BKb1atCAhRzogy9l9Y"
    ig_url = get_channel_link_url(PIVOK_CHANNEL_LINKS[2]) or "https://www.instagram.com/pivok_fashion?cnvx=MWRzeWJkaGs1MGpsaQ=="
    sale_url = get_channel_link_url(PIVOK_CHANNEL_LINKS[4]) or "https://t.me/pivoksale"
    vid_url = get_channel_link_url(PIVOK_CHANNEL_LINKS[5]) or "https://t.me/pivokvideo"

    lines = [
        f"🌸 <b>PÎVOK</b>",
        f"🆕 <b>NEW MODEL</b>",
        f"🔖 <b>CODE:</b> <code>{code}</code>",
    ]
    if sizes and sizes.strip() and sizes.strip() != "-":
        lines.append(f"📏 <b>SIZES:</b> {sizes}")
    if price and price.strip() and price.strip() != "-":
        lines.append(f"💰 <b>PRICE:</b> {price}$")
    lines.extend([
        f"➖➖➖➖➖➖➖➖",
        f"🛍 <b>Order & Catalog:</b>",
        f"<a href='{wa_url}'>🟢 WhatsApp</a> | "
        f"<a href='{ig_url}'>📸 Instagram</a> | "
        f"<a href='{vid_url}'>📹 Video</a> | "
        f"<a href='{sale_url}'>🔥 Sale</a>",
    ])
    return "\n".join(lines)


def _build_wa_collage_caption(code: str, sizes: str = "", price: str = "") -> str:
    ig_url = "https://www.instagram.com/pivok_fashion"
    tg_url = "https://t.me/pivoklaleli"
    lines = [
        f"🌸 *PÎVOK* — *NEW MODEL*",
        f"🔖 *CODE:* {code}",
    ]
    if sizes and sizes.strip() and sizes.strip() != "-":
        lines.append(f"📏 *SIZES:* {sizes}")
    if price and price.strip() and price.strip() != "-":
        lines.append(f"💰 *PRICE:* {price}$")
    lines.extend([
        f"✈️ Telegram: {tg_url}",
        f"📸 Instagram: {ig_url}",
    ])
    return "\n".join(lines)


@dp.callback_query(F.data.startswith(("pub_all_", "pub_tg_", "pub_wa_")))
async def cb_publish_collages(callback: CallbackQuery):
    if not bot:
        return
    if _PUB_LOCK.locked():
        await callback.answer("⏳ Sending in progress, please wait...", show_alert=False)
        return

    parts = callback.data.split("_", 2)
    mode = parts[1]  # 'all' | 'tg' | 'wa'
    short_id = parts[2]
    chat_id = callback.message.chat.id
    lang = get_lang(callback.from_user.id, callback.from_user.language_code)

    folder_path = pending_dispatches.get(short_id)
    if not folder_path or not os.path.exists(folder_path):
        await callback.answer(tr_str(lang, "folder_missing"), show_alert=True)
        return

    photos = sorted(
        os.path.join(folder_path, f)
        for f in os.listdir(folder_path)
        if f.lower().endswith((".jpg", ".jpeg", ".png"))
    )
    if not photos:
        await callback.answer(tr_str(lang, "gen_empty"), show_alert=True)
        return

    folder_name = os.path.basename(folder_path)
    _, _, code, sizes, price = collage_engine.parse_folder_metadata(folder_name)
    rec = published_records.setdefault(short_id, {"tg": False, "wa": False})
    rec["code"] = code
    _RECENT_CODES[str(chat_id)] = code
    persist_store()

    do_tg = mode in ("all", "tg")
    do_wa = mode in ("all", "wa")

    if do_tg and not do_wa and rec.get("tg"):
        await callback.answer("⚠️ Already sent to Telegram Channel!", show_alert=True)
        return
    if do_wa and not do_tg and rec.get("wa"):
        await callback.answer("⚠️ Already sent to WhatsApp Group!", show_alert=True)
        return
    if do_tg and do_wa and rec.get("tg") and rec.get("wa"):
        await callback.answer("⚠️ Already published to both TG Channel & WhatsApp!", show_alert=True)
        return

    await callback.answer("🚀")
    status_msg = await callback.message.answer(tr_str(lang, "pub_wait"))

    results_lines = []
    async with _PUB_LOCK:
        # 1. Publish to Telegram Channel (@pivoklaleli)
        if do_tg:
            if rec.get("tg"):
                results_lines.append(tr_str(lang, "pub_tg_ok", ch=PIVOK_TG_CHANNEL))
            else:
                try:
                    tg_cap = _build_tg_collage_caption(code, sizes, price)
                    for c_start in range(0, len(photos), 10):
                        chunk = photos[c_start : c_start + 10]
                        media = []
                        for idx_p, p_path in enumerate(chunk):
                            if c_start == 0 and idx_p == 0:
                                media.append(
                                    InputMediaPhoto(
                                        media=FSInputFile(p_path),
                                        caption=tg_cap,
                                        parse_mode="HTML",
                                    )
                                )
                            else:
                                media.append(InputMediaPhoto(media=FSInputFile(p_path)))
                        await bot.send_media_group(chat_id=PIVOK_TG_CHANNEL, media=media)
                    rec["tg"] = True
                    persist_store()
                    results_lines.append(tr_str(lang, "pub_tg_ok", ch=PIVOK_TG_CHANNEL))
                except Exception as e:
                    results_lines.append(tr_str(lang, "pub_tg_err", ch=PIVOK_TG_CHANNEL, e=str(e)))

        # 2. Publish to WhatsApp Group via Baileys Bridge
        if do_wa:
            if rec.get("wa"):
                results_lines.append(tr_str(lang, "pub_wa_ok"))
            else:
                wa_st = await asyncio.to_thread(get_wa_bridge_status)
                if not wa_st.get("connected"):
                    _WA_QR_WAITERS.add(chat_id)
                    results_lines.append(tr_str(lang, "pub_wa_not_linked"))
                    qr_str = wa_st.get("qr")
                    if qr_str:
                        png_bytes = await asyncio.to_thread(make_qr_png_bytes, qr_str)
                        await bot.send_photo(
                            chat_id=chat_id,
                            photo=BufferedInputFile(png_bytes, filename="whatsapp_qr.png"),
                            caption=tr_str(lang, "wa_qr_caption"),
                            parse_mode="Markdown",
                        )
                else:
                    try:
                        wa_cap = _build_wa_collage_caption(code, sizes, price)
                        target_grp = os.environ.get("WHATSAPP_GROUP_ID", "").strip() or wa_st.get("group_jid") or ""

                        def _post_wa_batch():
                            return requests.post(
                                f"{WA_BRIDGE_URL}/send-batch",
                                json={
                                    "target": target_grp,
                                    "files": photos,
                                    "caption": wa_cap,
                                    "append_dash": True,
                                },
                                timeout=120,
                            )

                        resp = await asyncio.to_thread(_post_wa_batch)
                        rj = resp.json()
                        if resp.status_code == 200 and rj.get("success"):
                            rec["wa"] = True
                            persist_store()
                            results_lines.append(tr_str(lang, "pub_wa_ok"))
                        else:
                            results_lines.append(f"❌ WhatsApp: {rj.get('error', resp.text)}")
                    except Exception as e:
                        results_lines.append(f"❌ WhatsApp: {e}")

    post_pub_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=tr_str(lang, "btn_add_video"), callback_data=f"addvid_{short_id}")]
        ]
    )
    try:
        await status_msg.edit_text("\n".join(results_lines), reply_markup=post_pub_kb)
    except Exception:
        await bot.send_message(chat_id, "\n".join(results_lines), reply_markup=post_pub_kb)


@dp.callback_query(F.data.startswith("files_"))
async def cb_send_uncompressed_files(callback: CallbackQuery):
    lang = get_lang(callback.from_user.id, callback.from_user.language_code)
    short_id = callback.data.split("_", 1)[1]
    folder_path = pending_dispatches.get(short_id)
    if not folder_path or not os.path.exists(folder_path):
        await callback.answer(tr_str(lang, "folder_missing"), show_alert=True)
        return
    photos = sorted(
        os.path.join(folder_path, f)
        for f in os.listdir(folder_path)
        if f.lower().endswith((".jpg", ".jpeg", ".png"))
    )
    await callback.answer(tr_str(lang, "sending_originals"))
    for chunk_start in range(0, len(photos), 10):
        chunk = photos[chunk_start : chunk_start + 10]
        await bot.send_media_group(
            callback.message.chat.id,
            media=[InputMediaDocument(media=FSInputFile(p)) for p in chunk],
        )


@dp.callback_query(F.data.startswith("refresh_"))
async def cb_refresh_preview(callback: CallbackQuery):
    lang = get_lang(callback.from_user.id, callback.from_user.language_code)
    short_id = callback.data.split("_", 1)[1]
    folder_path = pending_dispatches.get(short_id)
    if not folder_path or not os.path.exists(folder_path):
        await callback.answer(tr_str(lang, "folder_missing"), show_alert=True)
        return
    await callback.answer(tr_str(lang, "refreshing"))
    folder_name = os.path.basename(folder_path)
    try:
        await callback.message.delete()
    except Exception:
        pass
    await send_collage_preview(callback.message.chat.id, folder_name, lang=lang)


@dp.callback_query(F.data.startswith("addcolor_"))
async def cb_add_color(callback: CallbackQuery, state: FSMContext):
    lang = get_lang(callback.from_user.id, callback.from_user.language_code)
    short_id = callback.data.split("_", 1)[1]
    folder_path = pending_dispatches.get(short_id)
    if not folder_path or not os.path.exists(folder_path):
        await callback.answer(tr_str(lang, "folder_missing"), show_alert=True)
        return
    await state.update_data(is_add_color=True, target_folder=os.path.basename(folder_path), photo_ids=[])
    await state.set_state(CollageForm.photo)
    await callback.answer()
    await bot.send_message(
        callback.message.chat.id,
        tr_str(lang, "ask_add_color"),
        reply_markup=build_keyboard([tr_str(lang, "btn_all_loaded"), tr_str(lang, "btn_cancel")], 1),
        parse_mode="Markdown",
    )


@dp.callback_query(F.data.startswith("del_"))
async def cb_delete_collage(callback: CallbackQuery):
    lang = get_lang(callback.from_user.id, callback.from_user.language_code)
    short_id = callback.data.split("_", 1)[1]
    folder_path = pending_dispatches.pop(short_id, None)
    if folder_path:
        folder_name = os.path.basename(folder_path)
        shutil.rmtree(folder_path, ignore_errors=True)
        shutil.rmtree(os.path.join(INCOMING_DIR, folder_name), ignore_errors=True)

    for mid in media_msgs_store.pop(short_id, []):
        try:
            await bot.delete_message(callback.message.chat.id, mid)
        except Exception:
            pass
    persist_store()
    await callback.answer(tr_str(lang, "deleted_toast"))
    await callback.message.edit_text(tr_str(lang, "deleted_msg"))


@dp.message()
async def catch_all_unhandled(message: Message, state: FSMContext):
    if await state.get_state() is None:
        lang = get_lang(message.from_user.id, message.from_user.language_code)
        await message.answer(
            tr_str(lang, "catch_all"),
            reply_markup=main_menu_kb(lang),
            parse_mode="Markdown",
        )


# ==========================================
# FASTAPI + WEBHOOK + WEBAPP LIFECYCLE
# ==========================================

_polling_task = None


@asynccontextmanager
async def lifespan(app_inst: FastAPI):
    global _polling_task
    # Fast-boot: preload YOLO & start WhatsApp bridge in background so port binds immediately
    asyncio.get_running_loop().run_in_executor(None, yolo_local.preload_models)
    start_wa_bridge_daemon()
    if bot:
        if PUBLIC_BASE_URL.startswith("https://"):
            wh_url = f"{PUBLIC_BASE_URL}/telegram/webhook"
            try:
                info = await bot.get_webhook_info()
                if info.url != wh_url:
                    await bot.set_webhook(wh_url, secret_token=WEBHOOK_SECRET, drop_pending_updates=False)
                    logger.info(f"✅ Telegram Webhook registered at {wh_url}")
                else:
                    logger.info(f"⚡ Telegram Webhook already active at {wh_url}")
            except Exception as e:
                logger.warning(f"Webhook setup check warning: {e}")
                await bot.set_webhook(wh_url, secret_token=WEBHOOK_SECRET, drop_pending_updates=False)
        else:
            await bot.delete_webhook(drop_pending_updates=False)
            _polling_task = asyncio.create_task(dp.start_polling(bot))
            logger.info("🌸 Started local Telegram polling (no PUBLIC_BASE_URL)")
    yield
    if _polling_task:
        _polling_task.cancel()
    if _WA_PROC and _WA_PROC.poll() is None:
        try:
            _WA_PROC.terminate()
        except Exception:
            pass
    if bot:
        await bot.session.close()


app = FastAPI(title="PIVOK Cloud Studio", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/webapp", StaticFiles(directory=os.path.join(BASE_DIR, "webapp")), name="webapp")


@app.get("/")
@app.get("/health")
async def health():
    wa_st = await asyncio.to_thread(get_wa_bridge_status)
    return {
        "status": "ok",
        "service": "PIVOK Cloud Studio 24/7",
        "version": "v5.1-strict-sequential-pairs",
        "languages": ["ru", "tr", "ar"],
        "default_language": "ru",
        "whatsapp_connected": bool(wa_st.get("connected")),
        "webhook_mode": PUBLIC_BASE_URL.startswith("https://"),
    }


@app.get("/wa_qr")
async def wa_qr_page(key: str = ""):
    from fastapi.responses import HTMLResponse

    if key != SECRET_PASSWORD:
        return HTMLResponse("Access denied. Add ?key=PASSWORD to the link.", status_code=403)
    st = await asyncio.to_thread(get_wa_bridge_status)
    style = "background:#1a1620;color:#f5d8b8;font-family:sans-serif;display:flex;flex-direction:column;align-items:center;justify-content:center;height:100vh;margin:0;"
    if st.get("connected"):
        grp = st.get("group_subject") or st.get("group_jid") or "PIVOK"
        return HTMLResponse(
            f"<html><head><meta charset='UTF-8'></head><body style='{style}'>"
            f"<h2>✅ WhatsApp подключён!</h2><p>Группа: {grp}</p></body></html>"
        )
    qr_str = st.get("qr")
    if not qr_str:
        return HTMLResponse(
            f"<html><head><meta charset='UTF-8'><meta http-equiv='refresh' content='3'></head>"
            f"<body style='{style}'><h2>⏳ Генерирую QR-код...</h2></body></html>"
        )
    png_b64 = base64.b64encode(await asyncio.to_thread(make_qr_png_bytes, qr_str)).decode()
    return HTMLResponse(
        f"<html><head><meta charset='UTF-8'><meta http-equiv='refresh' content='8'></head><body style='{style}'>"
        f"<h2 style='color:#d6a474'>📱 PÎVOK — подключение WhatsApp</h2>"
        f"<img src='data:image/png;base64,{png_b64}' style='width:340px;border:14px solid #fff;border-radius:12px'/>"
        f"<p>WhatsApp → Настройки → Связанные устройства → Привязать устройство</p>"
        f"<p style='opacity:.6'>Код обновляется автоматически</p></body></html>"
    )


@app.post("/internal/wa_event")
async def internal_wa_event(request: Request):
    client_host = request.client.host if request.client else ""
    if client_host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=403, detail="Localhost only")
    payload = await request.json()
    ev = payload.get("event")
    if ev in ("creds_updated", "connected"):
        schedule_wa_session_backup()
    if ev == "connected" and bot and _WA_QR_WAITERS:
        grp = payload.get("group_subject") or payload.get("group_jid") or "PIVOK Group"
        waiters = list(_WA_QR_WAITERS)
        _WA_QR_WAITERS.clear()
        for cid in waiters:
            try:
                w_lang = get_lang(cid)
                await bot.send_message(
                    cid,
                    tr_str(w_lang, "wa_connected_push", grp=grp),
                    parse_mode="Markdown",
                )
            except Exception:
                pass
    return {"ok": True}


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    if not bot:
        return {"ok": False}
    sec = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if sec != WEBHOOK_SECRET:
        logger.warning(f"Webhook rejected: secret mismatch '{sec}' vs '{WEBHOOK_SECRET}'")
        raise HTTPException(status_code=403, detail="Invalid secret token")
    payload = await request.json()
    upd_id = payload.get("update_id")
    if upd_id is not None:
        if upd_id in _SEEN_UPDATES:
            return {"ok": True}
        _SEEN_UPDATES.append(upd_id)
    update = Update.model_validate(payload, context={"bot": bot})
    try:
        await dp.feed_update(bot, update)
    except Exception as e:
        logger.exception(f"Exception handling update {upd_id}: {e}")
    return {"ok": True}


@app.get("/debug_logs")
async def get_debug_logs(key: str = ""):
    if key != SECRET_PASSWORD:
        raise HTTPException(status_code=403, detail="Forbidden")
    return {"logs": list(_LOG_RECORDS)}


@app.get("/api/editor/{folder_name}")
async def get_editor_data(folder_name: str):
    folder_path = os.path.join(INCOMING_DIR, folder_name)
    if not os.path.isdir(folder_path):
        raise HTTPException(status_code=404, detail="Original photos not found")

    all_photos = sorted(
        f for f in os.listdir(folder_path) if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
    )
    parts = folder_name.split("_")
    category = parts[1] if len(parts) >= 5 else "ПЛАТЬЕ"

    color_groups = {}
    for p in all_photos:
        prefix = p.split("-")[0] if "-" in p else "DEFAULT"
        color_groups.setdefault(prefix, []).append(p)

    is_single = (category.upper() in ("ОДИНАРНЫЙ", "SINGLE")) or all(
        len(p_list) == 1 for p_list in color_groups.values()
    )

    swap_state = {}
    swap_file = os.path.join(folder_path, "swap_state.json")
    if os.path.exists(swap_file):
        try:
            with open(swap_file, "r", encoding="utf-8") as sf:
                swap_state = json.load(sf)
        except Exception:
            pass

    photos = []
    for prefix, p_list in color_groups.items():
        if is_single:
            photos.extend(p_list)
        elif prefix == "DEFAULT":
            for k in range(0, (len(p_list) // 2) * 2, 2):
                pair = p_list[k : k + 2]
                if swap_state.get(pair[0], False):
                    pair = [pair[1], pair[0]]
                photos.extend(pair)
        elif len(p_list) >= 2:
            pair = p_list[:2]
            if swap_state.get(pair[0], False):
                pair = [pair[1], pair[0]]
            photos.extend(pair)

    config_name = "SINGLE_config.json" if is_single else "STANDART_config.json"
    bg_name = "SINGLE_bg.png" if is_single else "STANDART_bg.png"

    with open(os.path.join(BASE_DIR, "resources", config_name), "r", encoding="utf-8") as f:
        config = json.load(f)

    encoded_photos = []
    for p in photos:
        with open(os.path.join(folder_path, p), "rb") as f:
            encoded_photos.append(f"data:image/jpeg;base64,{base64.b64encode(f.read()).decode('utf-8')}")

    with open(os.path.join(BASE_DIR, "resources", bg_name), "rb") as f:
        bg_data = f"data:image/png;base64,{base64.b64encode(f.read()).decode('utf-8')}"

    return {
        "is_single": is_single,
        "config": config,
        "bg_data": bg_data,
        "photos": encoded_photos,
        "photo_names": photos,
        "brand": "PIVOK",
        "category": category,
    }


@app.post("/api/editor/{folder_name}")
async def save_editor_data(folder_name: str, request: Request):
    data = await request.json()
    crop_data = data.get("images", [])
    chat_id = data.get("chat_id")
    lang = data.get("lang")
    folder_path = os.path.join(INCOMING_DIR, folder_name)
    if not os.path.isdir(folder_path):
        raise HTTPException(status_code=404, detail="Incoming folder not found")

    with open(os.path.join(folder_path, "manual_crop.json"), "w", encoding="utf-8") as f:
        json.dump(crop_data, f, ensure_ascii=False)

    async with _GEN_LOCK:
        await asyncio.to_thread(collage_engine.generate_folder_collages, folder_name, folder_path)
        gc.collect()
    if chat_id:
        asyncio.create_task(send_collage_preview(int(chat_id), folder_name, lang=lang))
    return {"status": "ok"}
