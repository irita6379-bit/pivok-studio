"""
PÎVOK Studio — 24/7 Cloud App (Render Free-Tier Optimized: <150MB RAM, 1-Thread ONNX, RU + TR)
"""

import asyncio
import base64
import collections
import gc
import hashlib
import io
import json
import logging
import os
import shutil
import time
import urllib.parse
from contextlib import asynccontextmanager
from typing import Dict, Optional

import cv2
import numpy as np
from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
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
        "text": "Telegram Channel",
        "default_url": "https://t.me/pivoklaleli",
        "env_var": "LINK_TELEGRAM_CHANNEL",
    },
    {
        "key": "whatsapp",
        "text": "WhatsApp",
        "default_url": "https://chat.whatsapp.com/Ddh7BKb1atCAhRzogy9l9Y",
        "env_var": "LINK_WHATSAPP",
    },
    {
        "key": "instagram",
        "text": "Instagram",
        "default_url": "https://www.instagram.com/pivok_fashion?cnvx=MWRzeWJkaGs1MGpsaQ==",
        "env_var": "LINK_INSTAGRAM",
    },
    {
        "key": "whatsapp_channel",
        "text": "WhatsApp Channel",
        "default_url": "",
        "env_var": "LINK_WHATSAPP_CHANNEL",
    },
    {
        "key": "sale_channel",
        "text": "Sale Channel",
        "default_url": "",
        "env_var": "LINK_SALE_CHANNEL",
    },
    {
        "key": "video_channel",
        "text": "Video Channel",
        "default_url": "",
        "env_var": "LINK_VIDEO_CHANNEL",
    },
    {
        "key": "address",
        "text": "Address",
        "default_url": "",
        "env_var": "LINK_ADDRESS",
    },
]

# ==========================================
# EN (DEFAULT) + RU + TR LOCALIZATION DICTIONARY
# ==========================================

STRINGS = {
    "en": {
        "btn_create": "🖼 Create Collage",
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
        "btn_dl_files": "📥 Download Original Files",
        "btn_refresh": "🔄 Refresh Preview",
        "btn_add_color": "🎨 Add Color Photos",
        "btn_delete": "🗑 Delete",
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
        "no_photos_yet": "⚠️ You have not uploaded any photos yet!",
        "need_2_photos": "⚠️ Double collage requires at least 2 photos.",
        "processing_extra": "⏳ Processing additional photos...",
        "ask_grouping": "📸 **{n} photos** uploaded. How should pairs be grouped?",
        "ask_code": "🔖 Enter **Model Code** (e.g. `O-21476`):",
        "err_code": "⚠️ Please enter a valid model code:",
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
            "🔖 Code: `{code}` | 💰 Price: `{price}$` | 📏 Sizes: `{sizes}`\n\n"
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
    },
    "ru": {
        "btn_create": "🖼 Создать коллаж",
        "btn_channels": "📢 Каналы",
        "btn_lang": "🇹🇷 Türkçe / Язык",
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
        "btn_dl_files": "📥 Скачать файлами (без сжатия)",
        "btn_refresh": "🔄 Обновить превью",
        "btn_add_color": "🎨 Добавить фото цвета",
        "btn_delete": "🗑 Удалить",
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
        "no_photos_yet": "⚠️ Вы ещё не загрузили ни одной фотографии!",
        "need_2_photos": "⚠️ Для двойного коллажа нужно минимум 2 фотографии.",
        "processing_extra": "⏳ Обрабатываю новые фото...",
        "ask_grouping": "📸 Загружено **{n} фото**. Как сгруппировать пары?",
        "ask_code": "🔖 Введите **Код модели** (например: `O-21476`):",
        "err_code": "⚠️ Введите корректный код модели:",
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
            "🔖 Код: `{code}` | 💰 Цена: `{price}$` | 📏 Размеры: `{sizes}`\n\n"
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
    },
    "tr": {
        "btn_create": "🖼 Kolaj Oluştur",
        "btn_channels": "📢 Kanallar",
        "btn_lang": "🇷🇺 Русский / Dil",
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
        "btn_dl_files": "📥 Dosya Olarak İndir (Orijinal)",
        "btn_refresh": "🔄 Önizlemeyi Yenile",
        "btn_add_color": "🎨 Yeni Renk (Fotoğraf) Ekle",
        "btn_delete": "🗑 Sil",
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
        "no_photos_yet": "⚠️ Henüz hiç fotoğraf yüklemediniz!",
        "need_2_photos": "⚠️ İkili kolaj için en az 2 fotoğraf gereklidir.",
        "processing_extra": "⏳ Yeni fotoğraflar işleniyor...",
        "ask_grouping": "📸 **{n} fotoğraf** yüklendi. Eşleştirme nasıl yapılsın?",
        "ask_code": "🔖 **Model Kodunu** girin (örnek: `O-21476`):",
        "err_code": "⚠️ Lütfen geçerli bir model kodu girin:",
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
            "🔖 Kod: `{code}` | 💰 Fiyat: `{price}$` | 📏 Beden: `{sizes}`\n\n"
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
    },
}

CREATE_TEXTS = {STRINGS["en"]["btn_create"], STRINGS["ru"]["btn_create"], STRINGS["tr"]["btn_create"]}
CHANNELS_BTN_TEXTS = {STRINGS["en"]["btn_channels"], STRINGS["ru"]["btn_channels"], STRINGS["tr"]["btn_channels"]}
LANG_BTN_TEXTS = {STRINGS["en"]["btn_lang"], STRINGS["ru"]["btn_lang"], STRINGS["tr"]["btn_lang"]}
CANCEL_TEXTS = {
    STRINGS["en"]["btn_cancel"],
    "Cancel",
    "cancel",
    "❌ Отмена",
    "Отмена",
    "❌ İptal",
    "İptal",
    "iptal",
    "/cancel",
    "/stop",
}
DONE_PHOTO_TEXTS = {
    STRINGS["en"]["btn_all_loaded"],
    STRINGS["en"]["btn_pair_2"],
    STRINGS["en"]["btn_single_1"],
    STRINGS["ru"]["btn_all_loaded"],
    STRINGS["tr"]["btn_all_loaded"],
    STRINGS["ru"]["btn_pair_2"],
    STRINGS["tr"]["btn_pair_2"],
    STRINGS["ru"]["btn_single_1"],
    STRINGS["tr"]["btn_single_1"],
}
SINGLE_MODE_TEXTS = {
    STRINGS["en"]["btn_single_1"],
    STRINGS["ru"]["btn_single_1"],
    STRINGS["tr"]["btn_single_1"],
}
SMART_GROUP_TEXTS = {
    STRINGS["en"]["btn_group_smart"],
    STRINGS["ru"]["btn_group_smart"],
    STRINGS["tr"]["btn_group_smart"],
}
CONFIRM_GO_TEXTS = {
    STRINGS["en"]["btn_confirm_go"],
    STRINGS["ru"]["btn_confirm_go"],
    STRINGS["tr"]["btn_confirm_go"],
}
EDIT_CODE_TEXTS = {
    STRINGS["en"]["btn_edit_code"],
    STRINGS["ru"]["btn_edit_code"],
    STRINGS["tr"]["btn_edit_code"],
}
EDIT_SIZE_TEXTS = {
    STRINGS["en"]["btn_edit_size"],
    STRINGS["ru"]["btn_edit_size"],
    STRINGS["tr"]["btn_edit_size"],
}
EDIT_PRICE_TEXTS = {
    STRINGS["en"]["btn_edit_price"],
    STRINGS["ru"]["btn_edit_price"],
    STRINGS["tr"]["btn_edit_price"],
}


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
user_langs: Dict[str, str] = _STORE.get("user_langs_v3", {})
saved_channel_links: Dict[str, str] = _STORE.get("channel_links", {})


def persist_store():
    _STORE["allowed_users"] = ALLOWED_USERS
    _STORE["pending_dispatches"] = pending_dispatches
    _STORE["media_msgs"] = media_msgs_store
    _STORE["user_langs_v3"] = user_langs
    _STORE["channel_links"] = saved_channel_links
    _save_state_store(_STORE)


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


def prune_old_work_folders(keep_latest: int = 8):
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
        if saved in ("en", "ru", "tr"):
            return saved
    return "en"


def set_lang(user_id: int, lang: str):
    if lang in ("en", "ru", "tr"):
        user_langs[str(user_id)] = lang
        persist_store()


def tr_str(lang: str, key: str, **kwargs) -> str:
    text = STRINGS.get(lang, STRINGS["en"]).get(key, STRINGS["en"].get(key, key))
    return text.format(**kwargs) if kwargs else text


def build_keyboard(items, columns=2):
    builder = ReplyKeyboardBuilder()
    for item in items:
        builder.add(KeyboardButton(text=item))
    builder.adjust(columns)
    return builder.as_markup(resize_keyboard=True)


def main_menu_kb(lang: str):
    return build_keyboard([tr_str(lang, "btn_create"), tr_str(lang, "btn_channels")], 2)


def lang_inline_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🇬🇧 English", callback_data="setlang_en"),
                InlineKeyboardButton(text="🇷🇺 Русский", callback_data="setlang_ru"),
                InlineKeyboardButton(text="🇹🇷 Türkçe", callback_data="setlang_tr"),
            ]
        ]
    )


def _save_and_analyze_one_sync(raw_bytes: bytes, dest_path: str) -> dict:
    """
    Saves photo to disk (capping max dimension to 1920px so memory stays tiny),
    runs YOLOv8-pose ONCE, and extracts both full-body score & dominant LAB color.
    """
    with Image.open(io.BytesIO(raw_bytes)) as im:
        im_rgb = ImageOps.exif_transpose(im).convert("RGB")
        if max(im_rgb.width, im_rgb.height) > 1920:
            im_rgb.thumbnail((1920, 1920), Image.Resampling.BILINEAR)
        im_rgb.save(dest_path, "JPEG", quality=90)

        max_dim = 640
        if max(im_rgb.width, im_rgb.height) > max_dim:
            sc = max_dim / float(max(im_rgb.width, im_rgb.height))
            small_rgb = im_rgb.resize(
                (max(1, int(im_rgb.width * sc)), max(1, int(im_rgb.height * sc))),
                Image.Resampling.BILINEAR,
            )
        else:
            small_rgb = im_rgb.copy()

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
    gc.collect()

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
            if hasattr(event, "text") and event.text:
                entered = event.text.strip().lower()
                allowed_pw = {SECRET_PASSWORD.lower()}
                if SECRET_PASSWORD.lower() == "pivok":
                    allowed_pw.add("пивок")
                if entered in allowed_pw:
                    ALLOWED_USERS.append(user_id)
                    lang = get_lang(user_id, getattr(user, "language_code", None))
                    set_lang(user_id, lang)
                    await event.answer(
                        "✅ **Access granted!**\n\n" + tr_str(lang, "welcome"),
                        reply_markup=main_menu_kb(lang),
                        parse_mode="Markdown",
                    )
                    return
                await event.answer(
                    "🔒 This PÎVOK bot is password-protected. Please enter the access password:"
                )
                return
            if hasattr(event, "data") and event.data:
                await event.answer("🔒 Please enter the password in chat first.", show_alert=True)
                return
            return
        return await handler(event, data)


class CollageForm(StatesGroup):
    photo = State()
    grouping_mode = State()
    code = State()
    size = State()
    price = State()
    confirm = State()
    edit_field = State()


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
        [InlineKeyboardButton(text=tr_str(lang, "btn_dl_files"), callback_data=f"files_{short_id}")],
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


@dp.callback_query(F.data.in_({"setlang_en", "setlang_ru", "setlang_tr"}))
async def cb_set_language(callback: CallbackQuery, state: FSMContext):
    if callback.data.endswith("_tr"):
        new_lang = "tr"
        toast = "🇹🇷 Türkçe"
    elif callback.data.endswith("_ru"):
        new_lang = "ru"
        toast = "🇷🇺 Русский"
    else:
        new_lang = "en"
        toast = "🇬🇧 English"
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
        PHOTO_BUFFER[chat_id] = {"ids": [], "task": None, "lang": lang}
    PHOTO_BUFFER[chat_id]["ids"].append(file_id)
    PHOTO_BUFFER[chat_id]["lang"] = lang

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
    data = await state.get_data()
    photo_ids = data.get("photo_ids", [])
    photo_ids.extend(buf["ids"])
    await state.update_data(photo_ids=photo_ids)

    kb = (
        build_keyboard([tr_str(lang, "btn_all_loaded"), tr_str(lang, "btn_cancel")], 1)
        if data.get("is_add_color")
        else build_keyboard(
            [tr_str(lang, "btn_pair_2"), tr_str(lang, "btn_single_1"), tr_str(lang, "btn_cancel")], 2
        )
    )
    await bot.send_message(
        chat_id,
        tr_str(lang, "photos_buffered", n=len(photo_ids)),
        reply_markup=kb,
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
        p_ids.extend(buf.get("ids", []))
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
                pairs, labels = await asyncio.to_thread(color_clustering.cluster_analyzed_items, analyzed)
                for p_idx, pair in enumerate(pairs):
                    if len(pair) < 2:
                        for leftover in pair:
                            try:
                                os.remove(leftover["temp_path"])
                            except Exception:
                                pass
                        continue
                    lbl = labels[p_idx] if p_idx < len(labels) else "COLOR"
                    prefix = f"{next_idx + p_idx:02d}_{lbl}"
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

        await state.clear()
        try:
            await wait_m.delete()
        except Exception:
            pass
        await send_collage_preview(chat_id, folder_name, lang=lang)
        return

    is_single = message.text in SINGLE_MODE_TEXTS
    if not is_single and len(photo_ids) < 2:
        await message.answer(tr_str(lang, "need_2_photos"))
        return

    await state.update_data(is_single=is_single)
    if is_single or len(photo_ids) == 2:
        await state.update_data(grouping_mode="sequential" if is_single else "smart")
        await state.set_state(CollageForm.code)
        await message.answer(
            tr_str(lang, "ask_code"),
            reply_markup=build_keyboard([tr_str(lang, "btn_cancel")], 1),
            parse_mode="Markdown",
        )
    else:
        await state.set_state(CollageForm.grouping_mode)
        await message.answer(
            tr_str(lang, "ask_grouping", n=len(photo_ids)),
            reply_markup=build_keyboard(
                [tr_str(lang, "btn_group_smart"), tr_str(lang, "btn_group_seq"), tr_str(lang, "btn_cancel")], 2
            ),
            parse_mode="Markdown",
        )


@dp.message(CollageForm.grouping_mode)
async def handle_grouping_mode(message: Message, state: FSMContext):
    lang = get_lang(message.from_user.id, message.from_user.language_code)
    is_smart = (message.text in SMART_GROUP_TEXTS) or ("Умная" in (message.text or "")) or ("Akıllı" in (message.text or ""))
    await state.update_data(grouping_mode="smart" if is_smart else "sequential")
    await state.set_state(CollageForm.code)
    await message.answer(
        tr_str(lang, "ask_code"),
        reply_markup=build_keyboard([tr_str(lang, "btn_cancel")], 1),
        parse_mode="Markdown",
    )


@dp.message(CollageForm.code)
async def handle_code(message: Message, state: FSMContext):
    lang = get_lang(message.from_user.id, message.from_user.language_code)
    code_val = (message.text or "").strip().upper()
    if not code_val:
        await message.answer(tr_str(lang, "err_code"))
        return
    await state.update_data(code=code_val)
    await state.set_state(CollageForm.size)
    await message.answer(
        tr_str(lang, "ask_size"),
        reply_markup=build_keyboard(SIZE_LIST + [tr_str(lang, "btn_cancel")], 3),
        parse_mode="Markdown",
    )


@dp.message(CollageForm.size)
async def handle_size(message: Message, state: FSMContext):
    lang = get_lang(message.from_user.id, message.from_user.language_code)
    size_val = (message.text or "").strip()
    if not size_val:
        await message.answer(tr_str(lang, "err_size"))
        return
    await state.update_data(size=size_val)
    await state.set_state(CollageForm.price)
    await message.answer(
        tr_str(lang, "ask_price"),
        reply_markup=build_keyboard([tr_str(lang, "btn_cancel")], 1),
        parse_mode="Markdown",
    )


async def show_summary(message: Message, state: FSMContext, lang: str):
    data = await state.get_data()
    photo_cnt = len(data.get("photo_ids", []))
    is_single = data.get("is_single", False)
    est_collages = photo_cnt if is_single else photo_cnt // 2
    await state.set_state(CollageForm.confirm)
    await message.answer(
        tr_str(
            lang,
            "summary",
            code=data.get("code"),
            price=data.get("price"),
            size=data.get("size"),
            cnt=photo_cnt,
            est=est_collages,
        ),
        reply_markup=build_keyboard(
            [
                tr_str(lang, "btn_confirm_go"),
                tr_str(lang, "btn_edit_code"),
                tr_str(lang, "btn_edit_size"),
                tr_str(lang, "btn_edit_price"),
                tr_str(lang, "btn_cancel"),
            ],
            2,
        ),
        parse_mode="Markdown",
    )


@dp.message(CollageForm.price)
async def handle_price(message: Message, state: FSMContext):
    lang = get_lang(message.from_user.id, message.from_user.language_code)
    price_val = (message.text or "").strip().replace("$", "").strip()
    if not price_val:
        await message.answer(tr_str(lang, "err_price"))
        return
    await state.update_data(price=price_val)
    await show_summary(message, state, lang)


@dp.message(CollageForm.confirm)
async def handle_confirm(message: Message, state: FSMContext):
    lang = get_lang(message.from_user.id, message.from_user.language_code)
    txt = (message.text or "").strip()
    if txt in EDIT_CODE_TEXTS:
        await state.update_data(editing="code")
        await state.set_state(CollageForm.edit_field)
        await message.answer(
            tr_str(lang, "ask_edit_code"),
            reply_markup=build_keyboard([tr_str(lang, "btn_cancel")], 1),
            parse_mode="Markdown",
        )
        return
    if txt in EDIT_SIZE_TEXTS:
        await state.update_data(editing="size")
        await state.set_state(CollageForm.edit_field)
        await message.answer(
            tr_str(lang, "ask_edit_size"),
            reply_markup=build_keyboard(SIZE_LIST + [tr_str(lang, "btn_cancel")], 3),
            parse_mode="Markdown",
        )
        return
    if txt in EDIT_PRICE_TEXTS:
        await state.update_data(editing="price")
        await state.set_state(CollageForm.edit_field)
        await message.answer(
            tr_str(lang, "ask_edit_price"),
            reply_markup=build_keyboard([tr_str(lang, "btn_cancel")], 1),
            parse_mode="Markdown",
        )
        return
    if txt not in CONFIRM_GO_TEXTS:
        await message.answer(tr_str(lang, "choose_below"))
        return

    data = await state.get_data()
    await state.clear()

    chat_id = message.chat.id
    photo_ids = data.get("photo_ids", [])
    is_single = data.get("is_single", False)
    grouping_mode = data.get("grouping_mode", "smart")

    safe_code = data["code"].replace("|", "~").replace("_", "-").replace("/", "-")
    safe_size = data["size"].replace("|", "~").replace("_", "-")
    safe_price = data["price"].replace("|", "~").replace("_", "-")
    cat_tag = "ОДИНАРНЫЙ" if is_single else "ПЛАТЬЕ"
    folder_name = f"PIVOK_{cat_tag}_{safe_code}_{safe_size}_{safe_price}_{int(time.time())}"
    inc_folder = os.path.join(INCOMING_DIR, folder_name)

    prune_old_work_folders(keep_latest=8)
    os.makedirs(inc_folder, exist_ok=True)

    prog_msg = await message.answer(
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

            pose_cache = {}
            if is_single:
                for idx, it in enumerate(analyzed, 1):
                    final_name = f"{idx:02d}_COLOR-{idx}.jpg"
                    os.replace(it["temp_path"], os.path.join(inc_folder, final_name))
                    pose_cache[final_name] = it["pose"]
            else:
                if grouping_mode == "smart":
                    pairs, labels = await asyncio.to_thread(color_clustering.cluster_analyzed_items, analyzed)
                else:
                    pairs = [analyzed[i : i + 2] for i in range(0, len(analyzed), 2)]
                    labels = [f"COLOR{idx + 1}" for idx in range(len(pairs))]

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
            gc.collect()

            try:
                await prog_msg.edit_text(tr_str(lang, "prog_render"))
            except Exception:
                pass

            await asyncio.to_thread(collage_engine.generate_folder_collages, folder_name, inc_folder)
            gc.collect()

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


@dp.message(CollageForm.edit_field)
async def handle_edit_field(message: Message, state: FSMContext):
    lang = get_lang(message.from_user.id, message.from_user.language_code)
    data = await state.get_data()
    field = data.get("editing")
    val = (message.text or "").strip()
    if field == "code":
        await state.update_data(code=val.upper())
    elif field == "size":
        await state.update_data(size=val)
    elif field == "price":
        await state.update_data(price=val.replace("$", "").strip())
    await show_summary(message, state, lang)


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
    await asyncio.to_thread(yolo_local.preload_models)
    if bot:
        if PUBLIC_BASE_URL.startswith("https://"):
            wh_url = f"{PUBLIC_BASE_URL}/telegram/webhook"
            await bot.set_webhook(wh_url, secret_token=WEBHOOK_SECRET, drop_pending_updates=False)
            logger.info(f"✅ Telegram Webhook registered at {wh_url}")
        else:
            await bot.delete_webhook(drop_pending_updates=False)
            _polling_task = asyncio.create_task(dp.start_polling(bot))
            logger.info("🌸 Started local Telegram polling (no PUBLIC_BASE_URL)")
    yield
    if _polling_task:
        _polling_task.cancel()
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
    return {
        "status": "ok",
        "service": "PIVOK Cloud Studio 24/7",
        "version": "v3-en-channels-ready",
        "languages": ["en", "ru", "tr"],
        "default_language": "en",
        "webhook_mode": PUBLIC_BASE_URL.startswith("https://"),
    }


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    if not bot:
        return {"ok": False}
    sec = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if sec != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret token")
    payload = await request.json()
    upd_id = payload.get("update_id")
    if upd_id is not None:
        if upd_id in _SEEN_UPDATES:
            return {"ok": True}
        _SEEN_UPDATES.append(upd_id)
    update = Update.model_validate(payload, context={"bot": bot})
    asyncio.create_task(dp.feed_update(bot, update))
    return {"ok": True}


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
