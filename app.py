"""
PГЋVOK Studio вЂ” 24/7 Cloud App (Render Free-Tier Optimized: <150MB RAM, 1-Thread ONNX, RU + TR)
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
import subprocess
import threading
import time
import urllib.parse
import zipfile
from contextlib import asynccontextmanager
from typing import Dict, Optional

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
        "text": "вњ€пёЏ Telegram Channel",
        "default_url": "https://t.me/pivoklaleli",
        "env_var": "LINK_TELEGRAM_CHANNEL",
    },
    {
        "key": "whatsapp",
        "text": "рџ’¬ WhatsApp",
        "default_url": "https://chat.whatsapp.com/Ddh7BKb1atCAhRzogy9l9Y",
        "env_var": "LINK_WHATSAPP",
    },
    {
        "key": "instagram",
        "text": "рџ“ё Instagram",
        "default_url": "https://www.instagram.com/pivok_fashion?cnvx=MWRzeWJkaGs1MGpsaQ==",
        "env_var": "LINK_INSTAGRAM",
    },
    {
        "key": "whatsapp_channel",
        "text": "рџ“ў WhatsApp Channel",
        "default_url": "https://chat.whatsapp.com/Ddh7BKb1atCAhRzogy9l9Y",
        "env_var": "LINK_WHATSAPP_CHANNEL",
    },
    {
        "key": "sale_channel",
        "text": "рџ”Ґ Sale Channel",
        "default_url": "https://t.me/pivoksale",
        "env_var": "LINK_SALE_CHANNEL",
    },
    {
        "key": "video_channel",
        "text": "рџ“№ Video Channel",
        "default_url": "https://t.me/pivokvideo",
        "env_var": "LINK_VIDEO_CHANNEL",
    },
    {
        "key": "address",
        "text": "рџ“Ќ Address",
        "default_url": "https://www.google.com/maps?q=41.007667,28.955222",
        "env_var": "LINK_ADDRESS",
    },
]

# ==========================================
# EN (DEFAULT) + RU + TR LOCALIZATION DICTIONARY
# ==========================================

STRINGS = {
    "en": {
        "btn_create": "рџ–ј Create Collage",
        "btn_wa_qr": "рџ“± WhatsApp QR",
        "btn_channels": "рџ“ў Channels",
        "btn_lang": "рџЊђ Language",
        "btn_cancel": "вќЊ Cancel",
        "btn_pair_2": "вњ… Double Collage (2 Photos)",
        "btn_single_1": "вњ… Single Collage (1 Photo)",
        "btn_all_loaded": "вњ… ALL PHOTOS UPLOADED",
        "btn_group_smart": "рџ§  Smart Grouping (Auto)",
        "btn_group_seq": "рџ”ў Sequential Pairs (1-2, 3-4)",
        "btn_confirm_go": "вњ… Generate Collages",
        "btn_edit_code": "вњЏпёЏ Edit Code",
        "btn_edit_size": "вњЏпёЏ Edit Sizes",
        "btn_edit_price": "вњЏпёЏ Edit Price",
        "btn_pub_both": "рџљЂ Send to TG Channel & WhatsApp",
        "btn_pub_tg": "вњ€пёЏ Send to TG Channel",
        "btn_pub_wa": "рџ’¬ Send to WhatsApp",
        "btn_dl_files": "рџ“Ґ Download Original Files",
        "btn_refresh": "рџ”„ Refresh Preview",
        "btn_add_color": "рџЋЁ Add Color Photos",
        "btn_delete": "рџ—‘ Delete",
        "welcome": "рџЊё **PГЋVOK Studio (24/7)**\nChoose an action from the menu below:",
        "channels_title": "рџ“ў **PГЋVOK Official Channels & Links:**",
        "channels_empty": "в„№пёЏ Channel links have not been configured yet.",
        "cancelled": "вќЊ Action cancelled.",
        "lang_prompt": "рџЊђ **Select interface language:**",
        "lang_switched": "рџ‡¬рџ‡§ Language switched to **English**!",
        "send_photos_prompt": (
            "рџ“ё **Send all photos for one model** (as an album or files).\n\n"
            "Once all photos are uploaded, select below:\n"
            "вЂў **вњ… Double Collage (2 Photos)** вЂ” PГЋVOK 4Г—3 format\n"
            "вЂў **вњ… Single Collage (1 Photo)**"
        ),
        "only_images": "вљ пёЏ Please send image files only.",
        "photos_buffered": "рџ“Ґ Photos received: **{n}**\nUpload more or press a button below:",
        "no_photos_yet": "вљ пёЏ You have not uploaded any photos yet!",
        "need_2_photos": "вљ пёЏ Double collage requires at least 2 photos.",
        "processing_extra": "вЏі Processing additional photos...",
        "ask_grouping": "рџ“ё **{n} photos** uploaded. How should pairs be grouped?",
        "ask_code": "рџ”– Enter **Model Code** (e.g. `O-21476`):",
        "err_code": "вљ пёЏ Please enter a valid model code:",
        "ask_size": "рџ“Џ Select or type **Sizes** (e.g. `38-44`):",
        "err_size": "вљ пёЏ Please specify the size range:",
        "ask_price": "рџ’° Enter **Price in $** (e.g. `79`):",
        "err_price": "вљ пёЏ Please enter a valid price:",
        "summary": (
            "рџ“‹ **Review PГЋVOK Collage Details:**\n\n"
            "рџ”– **Code:** `{code}`\n"
            "рџ’° **Price:** `{price}$`\n"
            "рџ“Џ **Sizes:** `{size}`\n"
            "рџ–ј **Photos:** {cnt} в†’ **{est} collages** (4Г—3 format)"
        ),
        "ask_edit_code": "рџ”– Enter new **Model Code**:",
        "ask_edit_size": "рџ“Џ Select or enter new **Sizes**:",
        "ask_edit_price": "рџ’° Enter new **Price ($)**:",
        "choose_below": "Please choose a button below рџ‘‡",
        "generating": "вљ™пёЏ Processing {n} photos in the cloud...",
        "prog_step": "вЏі Analyzing photo: **{cur} of {tot}**...",
        "prog_render": "рџЋЁ Assembling PГЋVOK collages (4Г—3)...",
        "preview_ready": (
            "вњЁ **PГЋVOK вЂ” Collages Ready ({n})**\n"
            "рџ”– Code: `{code}` | рџ’° Price: `{price}$` | рџ“Џ Sizes: `{sizes}`\n\n"
            "Adjust framing via рџ›  if needed, or choose an action:"
        ),
        "next_model_hint": "Done! To create collages for the next model, tap the button below:",
        "err_gen": "вќЊ Error generating collages: {e}",
        "folder_missing": "вљ пёЏ Collage folder not found.",
        "gen_empty": "вљ пёЏ Could not generate collages.",
        "sending_originals": "Sending original files...",
        "refreshing": "Refreshing preview...",
        "ask_add_color": "рџ“ё Send additional photos (new color) for this model, then press **В«вњ… ALL PHOTOS UPLOADEDВ»**.",
        "deleted_toast": "Deleted",
        "deleted_msg": "рџ—‘ Collages deleted.",
        "catch_all": "рџЊё **PГЋVOK Studio**\nPress **В«рџ–ј Create CollageВ»** below (or send photos directly here):",
        "wa_ready": "вњ… **WhatsApp is connected and ready!**\nрџ“Њ Target Group: `{grp}`",
        "wa_relink_btn": "рџ”„ Relink / Reset WhatsApp",
        "wa_refresh_qr_btn": "рџ”„ Refresh QR Code",
        "wa_gen_qr": "вЏі Generating WhatsApp QR code, please wait 3 seconds...",
        "wa_starting": "вЏі WhatsApp bridge is starting up. Please tap **рџ“± WhatsApp QR** again in 5 seconds.",
        "wa_qr_caption": (
            "рџ“± **Connect WhatsApp for Automatic Group Posting**\n\n"
            "1. Open **WhatsApp** on your phone\n"
            "2. Go to **Settings в†’ Linked Devices в†’ Link a Device**\n"
            "3. Scan this QR code (only needed once!)"
        ),
        "wa_connected_push": "вњ… **WhatsApp connected!**\nрџ“Њ Group: `{grp}`\nвЃпёЏ Session saved in cloud.",
        "pub_wait": "вЏі Publishing collages, please wait...",
        "pub_tg_ok": "вњ… Telegram ({ch}): Published!",
        "pub_tg_err": "вќЊ Telegram ({ch}): Make sure this bot is an Administrator ({e})",
        "pub_wa_ok": "вњ… WhatsApp Group: Published!",
        "pub_wa_not_linked": "вљ пёЏ WhatsApp Group: Not connected yet вЂ” scan the QR code below, then tap рџ’¬!",
    },
    "ru": {
        "btn_create": "рџ–ј РЎРѕР·РґР°С‚СЊ РєРѕР»Р»Р°Р¶",
        "btn_wa_qr": "рџ“± WhatsApp QR",
        "btn_channels": "рџ“ў РљР°РЅР°Р»С‹",
        "btn_lang": "рџЊђ РЇР·С‹Рє / Dil / Ш§Щ„Щ„ШєШ©",
        "btn_cancel": "вќЊ РћС‚РјРµРЅР°",
        "btn_pair_2": "вњ… Р”РІРѕР№РЅРѕР№ РєРѕР»Р»Р°Р¶ (РїРѕ 2 С„РѕС‚Рѕ)",
        "btn_single_1": "вњ… РћРґРёРЅР°СЂРЅС‹Р№ РєРѕР»Р»Р°Р¶ (РїРѕ 1 С„РѕС‚Рѕ)",
        "btn_all_loaded": "вњ… Р’РЎР• Р¤РћРўРћ Р—РђР“Р РЈР–Р•РќР«",
        "btn_group_smart": "рџ§  РЈРјРЅР°СЏ РіСЂСѓРїРїРёСЂРѕРІРєР° (РђРІС‚Рѕ)",
        "btn_group_seq": "рџ”ў РџРѕ РїР°СЂР°Рј (1-2, 3-4)",
        "btn_confirm_go": "вњ… РЎРѕР·РґР°С‚СЊ РєРѕР»Р»Р°Р¶Рё",
        "btn_edit_code": "вњЏпёЏ РР·РјРµРЅРёС‚СЊ РљРѕРґ",
        "btn_edit_size": "вњЏпёЏ РР·РјРµРЅРёС‚СЊ Р Р°Р·РјРµСЂС‹",
        "btn_edit_price": "вњЏпёЏ РР·РјРµРЅРёС‚СЊ Р¦РµРЅСѓ",
        "btn_pub_both": "рџљЂ РћС‚РїСЂР°РІРёС‚СЊ РІ TG Рё WhatsApp",
        "btn_pub_tg": "вњ€пёЏ РћС‚РїСЂР°РІРёС‚СЊ РІ TG РєР°РЅР°Р»",
        "btn_pub_wa": "рџ’¬ РћС‚РїСЂР°РІРёС‚СЊ РІ WhatsApp",
        "btn_dl_files": "рџ“Ґ РЎРєР°С‡Р°С‚СЊ С„Р°Р№Р»Р°РјРё (Р±РµР· СЃР¶Р°С‚РёСЏ)",
        "btn_refresh": "рџ”„ РћР±РЅРѕРІРёС‚СЊ РїСЂРµРІСЊСЋ",
        "btn_add_color": "рџЋЁ Р”РѕР±Р°РІРёС‚СЊ С„РѕС‚Рѕ С†РІРµС‚Р°",
        "btn_delete": "рџ—‘ РЈРґР°Р»РёС‚СЊ",
        "welcome": "рџЊё **PГЋVOK Cloud Studio (24/7)**\nР’С‹Р±РµСЂРёС‚Рµ РґРµР№СЃС‚РІРёРµ РІ РјРµРЅСЋ РЅРёР¶Рµ:",
        "cancelled": "вќЊ Р”РµР№СЃС‚РІРёРµ РѕС‚РјРµРЅРµРЅРѕ.",
        "lang_prompt": "рџЊђ **Р’С‹Р±РµСЂРёС‚Рµ СЏР·С‹Рє РёРЅС‚РµСЂС„РµР№СЃР° / LГјtfen dil seГ§in:**",
        "lang_switched": "рџ‡·рџ‡є РЇР·С‹Рє РїРµСЂРµРєР»СЋС‡С‘РЅ РЅР° **Р СѓСЃСЃРєРёР№**!",
        "send_photos_prompt": (
            "рџ“ё **РћС‚РїСЂР°РІСЊС‚Рµ РІСЃРµ С„РѕС‚РѕРіСЂР°С„РёРё РѕРґРЅРѕР№ РјРѕРґРµР»Рё** (Р°Р»СЊР±РѕРјРѕРј РёР»Рё С„Р°Р№Р»Р°РјРё).\n\n"
            "РљРѕРіРґР° Р·Р°РіСЂСѓР·РёС‚Рµ РІСЃРµ С„РѕС‚Рѕ, РІС‹Р±РµСЂРёС‚Рµ РІРЅРёР·Сѓ:\n"
            "вЂў **вњ… Р”РІРѕР№РЅРѕР№ РєРѕР»Р»Р°Р¶ (РїРѕ 2 С„РѕС‚Рѕ)** вЂ” С„РѕСЂРјР°С‚ PГЋVOK 4Г—3\n"
            "вЂў **вњ… РћРґРёРЅР°СЂРЅС‹Р№ РєРѕР»Р»Р°Р¶ (РїРѕ 1 С„РѕС‚Рѕ)**"
        ),
        "only_images": "вљ пёЏ РџРѕР¶Р°Р»СѓР№СЃС‚Р°, РѕС‚РїСЂР°РІР»СЏР№С‚Рµ С‚РѕР»СЊРєРѕ РёР·РѕР±СЂР°Р¶РµРЅРёСЏ.",
        "photos_buffered": "рџ“Ґ РџСЂРёРЅСЏС‚Рѕ С„РѕС‚РѕРіСЂР°С„РёР№: **{n} С€С‚.**\nР”РѕРіСЂСѓР·РёС‚Рµ РµС‰С‘ РёР»Рё РЅР°Р¶РјРёС‚Рµ РєРЅРѕРїРєСѓ РЅРёР¶Рµ:",
        "no_photos_yet": "вљ пёЏ Р’С‹ РµС‰С‘ РЅРµ Р·Р°РіСЂСѓР·РёР»Рё РЅРё РѕРґРЅРѕР№ С„РѕС‚РѕРіСЂР°С„РёРё!",
        "need_2_photos": "вљ пёЏ Р”Р»СЏ РґРІРѕР№РЅРѕРіРѕ РєРѕР»Р»Р°Р¶Р° РЅСѓР¶РЅРѕ РјРёРЅРёРјСѓРј 2 С„РѕС‚РѕРіСЂР°С„РёРё.",
        "processing_extra": "вЏі РћР±СЂР°Р±Р°С‚С‹РІР°СЋ РЅРѕРІС‹Рµ С„РѕС‚Рѕ...",
        "ask_grouping": "рџ“ё Р—Р°РіСЂСѓР¶РµРЅРѕ **{n} С„РѕС‚Рѕ**. РљР°Рє СЃРіСЂСѓРїРїРёСЂРѕРІР°С‚СЊ РїР°СЂС‹?",
        "ask_code": "рџ”– Р’РІРµРґРёС‚Рµ **РљРѕРґ РјРѕРґРµР»Рё** (РЅР°РїСЂРёРјРµСЂ: `O-21476`):",
        "err_code": "вљ пёЏ Р’РІРµРґРёС‚Рµ РєРѕСЂСЂРµРєС‚РЅС‹Р№ РєРѕРґ РјРѕРґРµР»Рё:",
        "ask_size": "рџ“Џ Р’С‹Р±РµСЂРёС‚Рµ РёР»Рё РЅР°РїРёС€РёС‚Рµ **Р Р°Р·РјРµСЂС‹** (РЅР°РїСЂРёРјРµСЂ: `38-44`):",
        "err_size": "вљ пёЏ РЈРєР°Р¶РёС‚Рµ СЂР°Р·РјРµСЂРЅС‹Р№ СЂСЏРґ:",
        "ask_price": "рџ’° Р’РІРµРґРёС‚Рµ **Р¦РµРЅСѓ РІ $** (РЅР°РїСЂРёРјРµСЂ: `79`):",
        "err_price": "вљ пёЏ Р’РІРµРґРёС‚Рµ С†РµРЅСѓ:",
        "summary": (
            "рџ“‹ **РџСЂРѕРІРµСЂСЊС‚Рµ РґР°РЅРЅС‹Рµ РєРѕР»Р»Р°Р¶Р° PГЋVOK:**\n\n"
            "рџ”– **РљРѕРґ:** `{code}`\n"
            "рџ’° **Р¦РµРЅР°:** `{price}$`\n"
            "рџ“Џ **Р Р°Р·РјРµСЂС‹:** `{size}`\n"
            "рџ–ј **Р¤РѕС‚РѕРіСЂР°С„РёР№:** {cnt} С€С‚. в†’ **{est} РєРѕР»Р».** (С„РѕСЂРјР°С‚ 4Г—3)"
        ),
        "ask_edit_code": "рџ”– Р’РІРµРґРёС‚Рµ РЅРѕРІС‹Р№ **РљРѕРґ РјРѕРґРµР»Рё**:",
        "ask_edit_size": "рџ“Џ Р’С‹Р±РµСЂРёС‚Рµ РёР»Рё РІРІРµРґРёС‚Рµ РЅРѕРІС‹Рµ **Р Р°Р·РјРµСЂС‹**:",
        "ask_edit_price": "рџ’° Р’РІРµРґРёС‚Рµ РЅРѕРІСѓСЋ **Р¦РµРЅСѓ ($)**:",
        "choose_below": "Р’С‹Р±РµСЂРёС‚Рµ РєРЅРѕРїРєСѓ РІРЅРёР·Сѓ рџ‘‡",
        "generating": "вљ™пёЏ РќР°С‡РёРЅР°СЋ РѕР±СЂР°Р±РѕС‚РєСѓ {n} С„РѕС‚Рѕ РІ РѕР±Р»Р°РєРµ...",
        "prog_step": "вЏі РђРЅР°Р»РёР· С„РѕС‚Рѕ: **{cur} РёР· {tot}**...",
        "prog_render": "рџЋЁ РЎР±РѕСЂРєР° РєРѕР»Р»Р°Р¶РµР№ PГЋVOK (4Г—3)...",
        "preview_ready": (
            "вњЁ **PГЋVOK вЂ” РљРѕР»Р»Р°Р¶Рё РіРѕС‚РѕРІС‹ ({n} С€С‚.)**\n"
            "рџ”– РљРѕРґ: `{code}` | рџ’° Р¦РµРЅР°: `{price}$` | рџ“Џ Р Р°Р·РјРµСЂС‹: `{sizes}`\n\n"
            "РџСЂРё РЅРµРѕР±С…РѕРґРёРјРѕСЃС‚Рё РїРѕРїСЂР°РІСЊС‚Рµ РєСЂРѕРї С‡РµСЂРµР· рџ›  РёР»Рё РІС‹Р±РµСЂРёС‚Рµ РґРµР№СЃС‚РІРёРµ:"
        ),
        "next_model_hint": "Р“РѕС‚РѕРІРѕ! Р§С‚РѕР±С‹ СЃРѕР±СЂР°С‚СЊ СЃР»РµРґСѓСЋС‰СѓСЋ РјРѕРґРµР»СЊ, РЅР°Р¶РјРёС‚Рµ РєРЅРѕРїРєСѓ РЅРёР¶Рµ:",
        "err_gen": "вќЊ РћС€РёР±РєР° РїСЂРё СЃРѕР·РґР°РЅРёРё РєРѕР»Р»Р°Р¶РµР№: {e}",
        "folder_missing": "вљ пёЏ РџР°РїРєР° СЃ РіРѕС‚РѕРІС‹РјРё РєРѕР»Р»Р°Р¶Р°РјРё РЅРµ РЅР°Р№РґРµРЅР°.",
        "gen_empty": "вљ пёЏ РќРµ СѓРґР°Р»РѕСЃСЊ СЃРіРµРЅРµСЂРёСЂРѕРІР°С‚СЊ РєРѕР»Р»Р°Р¶Рё.",
        "sending_originals": "РћС‚РїСЂР°РІР»СЏСЋ РѕСЂРёРіРёРЅР°Р»С‹...",
        "refreshing": "РћР±РЅРѕРІР»СЏСЋ РїСЂРµРІСЊСЋ...",
        "ask_add_color": "рџ“ё РћС‚РїСЂР°РІСЊС‚Рµ РґРѕРїРѕР»РЅРёС‚РµР»СЊРЅС‹Рµ С„РѕС‚Рѕ (РЅРѕРІС‹Р№ С†РІРµС‚) РґР»СЏ СЌС‚РѕР№ РјРѕРґРµР»Рё, Р·Р°С‚РµРј РЅР°Р¶РјРёС‚Рµ **В«вњ… Р’РЎР• Р¤РћРўРћ Р—РђР“Р РЈР–Р•РќР«В»**.",
        "deleted_toast": "РЈРґР°Р»РµРЅРѕ",
        "deleted_msg": "рџ—‘ РљРѕР»Р»Р°Р¶Рё СѓРґР°Р»РµРЅС‹.",
        "catch_all": "рџЊё **PГЋVOK Cloud Studio**\nРќР°Р¶РјРёС‚Рµ **В«рџ–ј РЎРѕР·РґР°С‚СЊ РєРѕР»Р»Р°Р¶В»** РІРЅРёР·Сѓ (РёР»Рё РѕС‚РїСЂР°РІСЊС‚Рµ С„РѕС‚Рѕ РїСЂСЏРјРѕ СЃСЋРґР°):",
        "channels_title": "рџ“ў **РћС„РёС†РёР°Р»СЊРЅС‹Рµ РєР°РЅР°Р»С‹ Рё СЃСЃС‹Р»РєРё PГЋVOK:**",
        "channels_empty": "в„№пёЏ РЎСЃС‹Р»РєРё РЅР° РєР°РЅР°Р»С‹ РїРѕРєР° РЅРµ РЅР°СЃС‚СЂРѕРµРЅС‹.",
        "wa_ready": "вњ… **WhatsApp РїРѕРґРєР»СЋС‡С‘РЅ Рё РіРѕС‚РѕРІ Рє РѕС‚РїСЂР°РІРєРµ!**\nрџ“Њ Р“СЂСѓРїРїР°: `{grp}`",
        "wa_relink_btn": "рџ”„ РџРµСЂРµРїСЂРёРІСЏР·Р°С‚СЊ WhatsApp",
        "wa_refresh_qr_btn": "рџ”„ РћР±РЅРѕРІРёС‚СЊ QR-РєРѕРґ",
        "wa_gen_qr": "вЏі Р“РµРЅРµСЂРёСЂСѓСЋ QR-РєРѕРґ WhatsApp, РїРѕРґРѕР¶РґРёС‚Рµ 3 СЃРµРєСѓРЅРґС‹...",
        "wa_starting": "вЏі РњРѕРґСѓР»СЊ WhatsApp Р·Р°РїСѓСЃРєР°РµС‚СЃСЏ. РќР°Р¶РјРёС‚Рµ **рџ“± WhatsApp QR** РµС‰С‘ СЂР°Р· С‡РµСЂРµР· 5 СЃРµРєСѓРЅРґ.",
        "wa_qr_caption": (
            "рџ“± **РџРѕРґРєР»СЋС‡РµРЅРёРµ WhatsApp РґР»СЏ Р°РІС‚Рѕ-РѕС‚РїСЂР°РІРєРё РІ РіСЂСѓРїРїСѓ**\n\n"
            "1. РћС‚РєСЂРѕР№С‚Рµ **WhatsApp** РЅР° С‚РµР»РµС„РѕРЅРµ\n"
            "2. Р—Р°Р№РґРёС‚Рµ РІ **РќР°СЃС‚СЂРѕР№РєРё в†’ РЎРІСЏР·Р°РЅРЅС‹Рµ СѓСЃС‚СЂРѕР№СЃС‚РІР° в†’ РџСЂРёРІСЏР·Р°С‚СЊ СѓСЃС‚СЂРѕР№СЃС‚РІРѕ**\n"
            "3. РћС‚СЃРєР°РЅРёСЂСѓР№С‚Рµ СЌС‚РѕС‚ QR-РєРѕРґ (РґРѕСЃС‚Р°С‚РѕС‡РЅРѕ 1 СЂР°Р·!)"
        ),
        "wa_connected_push": "вњ… **WhatsApp СѓСЃРїРµС€РЅРѕ РїРѕРґРєР»СЋС‡С‘РЅ!**\nрџ“Њ Р“СЂСѓРїРїР°: `{grp}`\nвЃпёЏ РЎРµСЃСЃРёСЏ СЃРѕС…СЂР°РЅРµРЅР° РІ РѕР±Р»Р°РєРµ.",
        "pub_wait": "вЏі РћС‚РїСЂР°РІР»СЏСЋ РєРѕР»Р»Р°Р¶Рё, РїРѕРґРѕР¶РґРёС‚Рµ...",
        "pub_tg_ok": "вњ… Telegram ({ch}): РћРїСѓР±Р»РёРєРѕРІР°РЅРѕ!",
        "pub_tg_err": "вќЊ Telegram ({ch}): Р”РѕР±Р°РІСЊС‚Рµ Р±РѕС‚Р° РІ РђРґРјРёРЅРёСЃС‚СЂР°С‚РѕСЂС‹ РєР°РЅР°Р»Р° ({e})",
        "pub_wa_ok": "вњ… WhatsApp-РіСЂСѓРїРїР°: РћРїСѓР±Р»РёРєРѕРІР°РЅРѕ!",
        "pub_wa_not_linked": "вљ пёЏ WhatsApp РµС‰С‘ РЅРµ РїСЂРёРІСЏР·Р°РЅ вЂ” РѕС‚СЃРєР°РЅРёСЂСѓР№С‚Рµ QR-РєРѕРґ РЅРёР¶Рµ Рё РЅР°Р¶РјРёС‚Рµ рџ’¬ РћС‚РїСЂР°РІРёС‚СЊ РІ WhatsApp!",
    },
    "tr": {
        "btn_create": "рџ–ј Kolaj OluЕџtur",
        "btn_wa_qr": "рџ“± WhatsApp QR",
        "btn_channels": "рџ“ў Kanallar",
        "btn_lang": "рџЊђ Dil / РЇР·С‹Рє / Ш§Щ„Щ„ШєШ©",
        "btn_cancel": "вќЊ Д°ptal",
        "btn_pair_2": "вњ… Д°kili Kolaj (2 FotoДџraf)",
        "btn_single_1": "вњ… Tekli Kolaj (1 FotoДџraf)",
        "btn_all_loaded": "вњ… TГњM FOTOДћRAFLAR YГњKLENDД°",
        "btn_group_smart": "рџ§  AkД±llД± Gruplama (Otomatik)",
        "btn_group_seq": "рџ”ў SД±rayla Д°kili (1-2, 3-4)",
        "btn_confirm_go": "вњ… KolajlarД± OluЕџtur",
        "btn_edit_code": "вњЏпёЏ Kodu DeДџiЕџtir",
        "btn_edit_size": "вњЏпёЏ Bedeni DeДџiЕџtir",
        "btn_edit_price": "вњЏпёЏ FiyatД± DeДџiЕџtir",
        "btn_pub_both": "рџљЂ TG KanalД± ve WhatsApp'a GГ¶nder",
        "btn_pub_tg": "вњ€пёЏ TG KanalД±na GГ¶nder",
        "btn_pub_wa": "рџ’¬ WhatsApp'a GГ¶nder",
        "btn_dl_files": "рџ“Ґ Dosya Olarak Д°ndir (Orijinal)",
        "btn_refresh": "рџ”„ Г–nizlemeyi Yenile",
        "btn_add_color": "рџЋЁ Yeni Renk (FotoДџraf) Ekle",
        "btn_delete": "рџ—‘ Sil",
        "welcome": "рџЊё **PГЋVOK Cloud Studio (7/24)**\nLГјtfen aЕџaДџД±daki menГјden iЕџlem seГ§in:",
        "cancelled": "вќЊ Д°Еџlem iptal edildi.",
        "lang_prompt": "рџЊђ **LГјtfen dil seГ§in / Р’С‹Р±РµСЂРёС‚Рµ СЏР·С‹Рє РёРЅС‚РµСЂС„РµР№СЃР°:**",
        "lang_switched": "рџ‡№рџ‡· Dil **TГјrkГ§e** olarak deДџiЕџtirildi!",
        "send_photos_prompt": (
            "рџ“ё **Bir modele ait tГјm fotoДџraflarД± gГ¶nderin** (albГјm veya dosya olarak).\n\n"
            "TГјm fotoДџraflar yГјklendiДџinde aЕџaДџД±dan seГ§in:\n"
            "вЂў **вњ… Д°kili Kolaj (2 FotoДџraf)** вЂ” PГЋVOK 4Г—3 formatД±\n"
            "вЂў **вњ… Tekli Kolaj (1 FotoДџraf)**"
        ),
        "only_images": "вљ пёЏ LГјtfen yalnД±zca gГ¶rsel / fotoДџraf gГ¶nderin.",
        "photos_buffered": "рџ“Ґ AlД±nan fotoДџraf: **{n} adet**\nDaha fazla yГјkleyin veya aЕџaДџД±daki butona basД±n:",
        "no_photos_yet": "вљ пёЏ HenГјz hiГ§ fotoДџraf yГјklemediniz!",
        "need_2_photos": "вљ пёЏ Д°kili kolaj iГ§in en az 2 fotoДџraf gereklidir.",
        "processing_extra": "вЏі Yeni fotoДџraflar iЕџleniyor...",
        "ask_grouping": "рџ“ё **{n} fotoДџraf** yГјklendi. EЕџleЕџtirme nasД±l yapД±lsД±n?",
        "ask_code": "рџ”– **Model Kodunu** girin (Г¶rnek: `O-21476`):",
        "err_code": "вљ пёЏ LГјtfen geГ§erli bir model kodu girin:",
        "ask_size": "рџ“Џ **Beden aralД±ДџД±nД±** seГ§in veya yazД±n (Г¶rnek: `38-44`):",
        "err_size": "вљ пёЏ LГјtfen beden aralД±ДџД±nД± belirtin:",
        "ask_price": "рџ’° **FiyatД± ($)** girin (Г¶rnek: `79`):",
        "err_price": "вљ пёЏ LГјtfen fiyat girin:",
        "summary": (
            "рџ“‹ **PГЋVOK kolaj bilgilerini kontrol edin:**\n\n"
            "рџ”– **Kod:** `{code}`\n"
            "рџ’° **Fiyat:** `{price}$`\n"
            "рџ“Џ **Beden:** `{size}`\n"
            "рџ–ј **FotoДџraf:** {cnt} adet в†’ **{est} kolaj** (4Г—3 formatД±)"
        ),
        "ask_edit_code": "рџ”– Yeni **Model Kodunu** girin:",
        "ask_edit_size": "рџ“Џ Yeni **Beden aralД±ДџД±nД±** seГ§in veya yazД±n:",
        "ask_edit_price": "рџ’° Yeni **FiyatД± ($)** girin:",
        "choose_below": "LГјtfen aЕџaДџД±daki butonlardan birini seГ§in рџ‘‡",
        "generating": "вљ™пёЏ {n} fotoДџraf bulutta iЕџleniyor...",
        "prog_step": "вЏі FotoДџraf analizi: **{cur} / {tot}**...",
        "prog_render": "рџЋЁ PГЋVOK kolajlarД± (4Г—3) birleЕџtiriliyor...",
        "preview_ready": (
            "вњЁ **PГЋVOK вЂ” Kolajlar hazД±r ({n} adet)**\n"
            "рџ”– Kod: `{code}` | рџ’° Fiyat: `{price}$` | рџ“Џ Beden: `{sizes}`\n\n"
            "Gerekirse рџ›  ile kadrajД± dГјzenleyin veya iЕџlem seГ§in:"
        ),
        "next_model_hint": "TamamlandД±! Yeni bir model hazД±rlamak iГ§in aЕџaДџД±daki butona basД±n:",
        "err_gen": "вќЊ Kolaj oluЕџturulurken hata oluЕџtu: {e}",
        "folder_missing": "вљ пёЏ Kolaj klasГ¶rГј bulunamadД±.",
        "gen_empty": "вљ пёЏ Kolaj oluЕџturulamadД±.",
        "sending_originals": "Orijinal dosyalar gГ¶nderiliyor...",
        "refreshing": "Г–nizleme yenileniyor...",
        "ask_add_color": "рџ“ё Bu model iГ§in ek fotoДџraflarД± (yeni renk) gГ¶nderin, ardД±ndan **В«вњ… TГњM FOTOДћRAFLAR YГњKLENDД°В»** butonuna basД±n.",
        "deleted_toast": "Silindi",
        "deleted_msg": "рџ—‘ Kolajlar silindi.",
        "catch_all": "рџЊё **PГЋVOK Cloud Studio**\nAЕџaДџД±daki **В«рџ–ј Kolaj OluЕџturВ»** butonuna basД±n (veya fotoДџraflarД± doДџrudan buraya gГ¶nderin):",
        "channels_title": "рџ“ў **PГЋVOK Resmi Kanallar ve Linkler:**",
        "channels_empty": "в„№пёЏ Kanal linkleri henГјz ayarlanmadД±.",
        "wa_ready": "вњ… **WhatsApp baДџlД± ve gГ¶nderime hazД±r!**\nрџ“Њ Grup: `{grp}`",
        "wa_relink_btn": "рџ”„ WhatsApp BaДџlantД±sД±nД± SД±fД±rla",
        "wa_refresh_qr_btn": "рџ”„ QR Kodunu Yenile",
        "wa_gen_qr": "вЏі WhatsApp QR kodu oluЕџturuluyor, lГјtfen 3 saniye bekleyin...",
        "wa_starting": "вЏі WhatsApp modГјlГј baЕџlatД±lД±yor. 5 saniye sonra tekrar **рџ“± WhatsApp QR** tuЕџuna basД±n.",
        "wa_qr_caption": (
            "рџ“± **Otomatik Grup GГ¶nderimi iГ§in WhatsApp BaДџlantД±sД±**\n\n"
            "1. Telefonunuzda **WhatsApp** uygulamasД±nД± aГ§Д±n\n"
            "2. **Ayarlar в†’ BaДџlД± Cihazlar в†’ Cihaz BaДџla** bГ¶lГјmГјne girin\n"
            "3. Bu QR kodunu okutun (sadece 1 kez yeterlidir!)"
        ),
        "wa_connected_push": "вњ… **WhatsApp baЕџarД±yla baДџlandД±!**\nрџ“Њ Grup: `{grp}`\nвЃпёЏ Oturum buluta kaydedildi.",
        "pub_wait": "вЏі Kolajlar gГ¶nderiliyor, lГјtfen bekleyin...",
        "pub_tg_ok": "вњ… Telegram ({ch}): YayД±nlandД±!",
        "pub_tg_err": "вќЊ Telegram ({ch}): Botun kanalda YГ¶netici olduДџundan emin olun ({e})",
        "pub_wa_ok": "вњ… WhatsApp Grubu: GГ¶nderildi!",
        "pub_wa_not_linked": "вљ пёЏ WhatsApp henГјz baДџlД± deДџil вЂ” aЕџaДџД±daki QR kodunu okutup tekrar рџ’¬ butonuna basД±n!",
    },
    "ar": {
        "btn_create": "рџ–ј ШҐЩ†ШґШ§ШЎ ЩѓЩ€Щ„Ш§Ш¬",
        "btn_wa_qr": "рџ“± WhatsApp QR",
        "btn_channels": "рџ“ў Ш§Щ„Щ‚Щ†Щ€Ш§ШЄ",
        "btn_lang": "рџЊђ Ш§Щ„Щ„ШєШ© / РЇР·С‹Рє / Dil",
        "btn_cancel": "вќЊ ШҐЩ„ШєШ§ШЎ",
        "btn_pair_2": "вњ… ЩѓЩ€Щ„Ш§Ш¬ Щ…ШІШЇЩ€Ш¬ (ШµЩ€Ш±ШЄШ§Щ†)",
        "btn_single_1": "вњ… ЩѓЩ€Щ„Ш§Ш¬ Щ…ЩЃШ±ШЇ (ШµЩ€Ш±Ш© Щ€Ш§Ш­ШЇШ©)",
        "btn_all_loaded": "вњ… ШЄЩ… Ш±ЩЃШ№ Ш¬Щ…ЩЉШ№ Ш§Щ„ШµЩ€Ш±",
        "btn_group_smart": "рџ§  ШЄШ¬Щ…ЩЉШ№ Ш°ЩѓЩЉ (ШЄЩ„Щ‚Ш§Ш¦ЩЉ)",
        "btn_group_seq": "рџ”ў ШЈШІЩ€Ш§Ш¬ Щ…ШЄШЄШ§Щ„ЩЉШ© (1-2ШЊ 3-4)",
        "btn_confirm_go": "вњ… ШҐЩ†ШґШ§ШЎ Ш§Щ„ЩѓЩ€Щ„Ш§Ш¬",
        "btn_edit_code": "вњЏпёЏ ШЄШ№ШЇЩЉЩ„ Ш§Щ„ЩѓЩ€ШЇ",
        "btn_edit_size": "вњЏпёЏ ШЄШ№ШЇЩЉЩ„ Ш§Щ„Щ…Щ‚Ш§ШіШ§ШЄ",
        "btn_edit_price": "вњЏпёЏ ШЄШ№ШЇЩЉЩ„ Ш§Щ„ШіШ№Ш±",
        "btn_pub_both": "рџљЂ ШҐШ±ШіШ§Щ„ ШҐЩ„Щ‰ ШЄЩЉЩ„ЩЉШ¬Ш±Ш§Щ… Щ€Щ€Ш§ШЄШіШ§ШЁ",
        "btn_pub_tg": "вњ€пёЏ ШҐШ±ШіШ§Щ„ ШҐЩ„Щ‰ Щ‚Щ†Ш§Ш© ШЄЩЉЩ„ЩЉШ¬Ш±Ш§Щ…",
        "btn_pub_wa": "рџ’¬ ШҐШ±ШіШ§Щ„ ШҐЩ„Щ‰ Щ€Ш§ШЄШіШ§ШЁ",
        "btn_dl_files": "рџ“Ґ ШЄШ­Щ…ЩЉЩ„ Ш§Щ„Щ…Щ„ЩЃШ§ШЄ Ш§Щ„ШЈШµЩ„ЩЉШ©",
        "btn_refresh": "рџ”„ ШЄШ­ШЇЩЉШ« Ш§Щ„Щ…Ш№Ш§ЩЉЩ†Ш©",
        "btn_add_color": "рџЋЁ ШҐШ¶Ш§ЩЃШ© ШµЩ€Ш± Щ„Щ€Щ† Ш¬ШЇЩЉШЇ",
        "btn_delete": "рџ—‘ Ш­Ш°ЩЃ",
        "welcome": "рџЊё **Ш§ШіШЄЩ€ШЇЩЉЩ€ PГЋVOK Ш§Щ„ШіШ­Ш§ШЁЩЉ (24/7)**\nШ§Ш®ШЄШ± ШҐШ¬Ш±Ш§ШЎЩ‹ Щ…Щ† Ш§Щ„Щ‚Ш§Ш¦Щ…Ш© ШЈШЇЩ†Ш§Щ‡:",
        "cancelled": "вќЊ ШЄЩ… ШҐЩ„ШєШ§ШЎ Ш§Щ„Ш№Щ…Щ„ЩЉШ©.",
        "lang_prompt": "рџЊђ **Ш§Ш®ШЄШ± Ш§Щ„Щ„ШєШ© / Р’С‹Р±РµСЂРёС‚Рµ СЏР·С‹Рє / Dil seГ§in:**",
        "lang_switched": "рџ‡ёрџ‡¦ ШЄЩ… ШЄШєЩЉЩЉШ± Ш§Щ„Щ„ШєШ© ШҐЩ„Щ‰ **Ш§Щ„Ш№Ш±ШЁЩЉШ©**!",
        "send_photos_prompt": (
            "рџ“ё **ШЈШ±ШіЩ„ Ш¬Щ…ЩЉШ№ ШµЩ€Ш± Ш§Щ„Щ…Щ€ШЇЩЉЩ„ Ш§Щ„Щ€Ш§Ш­ШЇ** (ЩѓШЈЩ„ШЁЩ€Щ… ШЈЩ€ Щ…Щ„ЩЃШ§ШЄ).\n\n"
            "Ш№Щ†ШЇ Ш§Щ„Ш§Щ†ШЄЩ‡Ш§ШЎ Щ…Щ† Ш±ЩЃШ№ Ш§Щ„ШµЩ€Ш±ШЊ Ш§Ш®ШЄШ± Щ…Щ† Ш§Щ„ШЈШіЩЃЩ„:\n"
            "вЂў **вњ… ЩѓЩ€Щ„Ш§Ш¬ Щ…ШІШЇЩ€Ш¬ (ШµЩ€Ш±ШЄШ§Щ†)** вЂ” Щ…Щ‚Ш§Ші PГЋVOK 4Г—3\n"
            "вЂў **вњ… ЩѓЩ€Щ„Ш§Ш¬ Щ…ЩЃШ±ШЇ (ШµЩ€Ш±Ш© Щ€Ш§Ш­ШЇШ©)**"
        ),
        "only_images": "вљ пёЏ ЩЉШ±Ш¬Щ‰ ШҐШ±ШіШ§Щ„ Ш§Щ„ШµЩ€Ш± ЩЃЩ‚Ш·.",
        "photos_buffered": "рџ“Ґ Ш§Щ„ШµЩ€Ш± Ш§Щ„Щ…ШіШЄЩ„Щ…Ш©: **{n}**\nШЈШ±ШіЩ„ Ш§Щ„Щ…ШІЩЉШЇ ШЈЩ€ Ш§Ш¶ШєШ· Ш№Щ„Щ‰ Ш§Щ„ШІШ± ШЈШЇЩ†Ш§Щ‡:",
        "no_photos_yet": "вљ пёЏ Щ„Щ… ШЄЩ‚Щ… ШЁШ±ЩЃШ№ ШЈЩЉ ШµЩ€Ш± ШЁШ№ШЇ!",
        "need_2_photos": "вљ пёЏ Ш§Щ„ЩѓЩ€Щ„Ш§Ш¬ Ш§Щ„Щ…ШІШЇЩ€Ш¬ ЩЉШЄШ·Щ„ШЁ ШµЩ€Ш±ШЄЩЉЩ† Ш№Щ„Щ‰ Ш§Щ„ШЈЩ‚Щ„.",
        "processing_extra": "вЏі Ш¬Ш§Ш±ЩЉ Щ…Ш№Ш§Щ„Ш¬Ш© Ш§Щ„ШµЩ€Ш± Ш§Щ„ШҐШ¶Ш§ЩЃЩЉШ©...",
        "ask_grouping": "рџ“ё ШЄЩ… Ш±ЩЃШ№ **{n} ШµЩ€Ш±**. ЩѓЩЉЩЃ ШЄШ±ЩЉШЇ ШЄШ¬Щ…ЩЉШ№ Ш§Щ„ШЈШІЩ€Ш§Ш¬Шџ",
        "ask_code": "рџ”– ШЈШЇШ®Щ„ **ЩѓЩ€ШЇ Ш§Щ„Щ…Щ€ШЇЩЉЩ„** (Щ…Ш«Ш§Щ„: `O-21476`):",
        "err_code": "вљ пёЏ ЩЉШ±Ш¬Щ‰ ШҐШЇШ®Ш§Щ„ ЩѓЩ€ШЇ Щ…Щ€ШЇЩЉЩ„ ШµШ­ЩЉШ­:",
        "ask_size": "рџ“Џ Ш§Ш®ШЄШ± ШЈЩ€ Ш§ЩѓШЄШЁ **Ш§Щ„Щ…Щ‚Ш§ШіШ§ШЄ** (Щ…Ш«Ш§Щ„: `38-44`):",
        "err_size": "вљ пёЏ ЩЉШ±Ш¬Щ‰ ШЄШ­ШЇЩЉШЇ Ш§Щ„Щ…Щ‚Ш§ШіШ§ШЄ:",
        "ask_price": "рџ’° ШЈШЇШ®Щ„ **Ш§Щ„ШіШ№Ш± ШЁШ§Щ„ШЇЩ€Щ„Ш§Ш± ($)** (Щ…Ш«Ш§Щ„: `79`):",
        "err_price": "вљ пёЏ ЩЉШ±Ш¬Щ‰ ШҐШЇШ®Ш§Щ„ Ш§Щ„ШіШ№Ш±:",
        "summary": (
            "рџ“‹ **Щ…Ш±Ш§Ш¬Ш№Ш© ШЁЩЉШ§Щ†Ш§ШЄ ЩѓЩ€Щ„Ш§Ш¬ PГЋVOK:**\n\n"
            "рџ”– **Ш§Щ„ЩѓЩ€ШЇ:** `{code}`\n"
            "рџ’° **Ш§Щ„ШіШ№Ш±:** `{price}$`\n"
            "рџ“Џ **Ш§Щ„Щ…Щ‚Ш§ШіШ§ШЄ:** `{size}`\n"
            "рџ–ј **Ш§Щ„ШµЩ€Ш±:** {cnt} в†ђ **{est} ЩѓЩ€Щ„Ш§Ш¬** (Щ…Щ‚Ш§Ші 4Г—3)"
        ),
        "ask_edit_code": "рџ”– ШЈШЇШ®Щ„ **ЩѓЩ€ШЇ Ш§Щ„Щ…Щ€ШЇЩЉЩ„** Ш§Щ„Ш¬ШЇЩЉШЇ:",
        "ask_edit_size": "рџ“Џ Ш§Ш®ШЄШ± ШЈЩ€ ШЈШЇШ®Щ„ **Ш§Щ„Щ…Щ‚Ш§ШіШ§ШЄ** Ш§Щ„Ш¬ШЇЩЉШЇШ©:",
        "ask_edit_price": "рџ’° ШЈШЇШ®Щ„ **Ш§Щ„ШіШ№Ш± Ш§Щ„Ш¬ШЇЩЉШЇ ($)**:",
        "choose_below": "ЩЉШ±Ш¬Щ‰ Ш§Ш®ШЄЩЉШ§Ш± ШІШ± Щ…Щ† Ш§Щ„ШЈШіЩЃЩ„ рџ‘‡",
        "generating": "вљ™пёЏ Ш¬Ш§Ш±ЩЉ Щ…Ш№Ш§Щ„Ш¬Ш© {n} ШµЩ€Ш± ЩЃЩЉ Ш§Щ„ШіШ­Ш§ШЁШ©...",
        "prog_step": "вЏі ШЄШ­Щ„ЩЉЩ„ Ш§Щ„ШµЩ€Ш±Ш©: **{cur} Щ…Щ† {tot}**...",
        "prog_render": "рџЋЁ Ш¬Ш§Ш±ЩЉ ШЄШ¬Щ…ЩЉШ№ ЩѓЩ€Щ„Ш§Ш¬ PГЋVOK (4Г—3)...",
        "preview_ready": (
            "вњЁ **PГЋVOK вЂ” Ш§Щ„ЩѓЩ€Щ„Ш§Ш¬ Ш¬Ш§Щ‡ШІ ({n})**\n"
            "рџ”– Ш§Щ„ЩѓЩ€ШЇ: `{code}` | рџ’° Ш§Щ„ШіШ№Ш±: `{price}$` | рџ“Џ Ш§Щ„Щ…Щ‚Ш§ШіШ§ШЄ: `{sizes}`\n\n"
            "ЩЉЩ…ЩѓЩ†Щѓ ШЄШ№ШЇЩЉЩ„ Ш§Щ„Щ‚Шµ Ш№ШЁШ± рџ›  Ш№Щ†ШЇ Ш§Щ„Ш­Ш§Ш¬Ш© ШЈЩ€ Ш§Ш®ШЄЩЉШ§Ш± ШҐШ¬Ш±Ш§ШЎ:"
        ),
        "next_model_hint": "ШЄЩ…! Щ„ШҐЩ†ШґШ§ШЎ ЩѓЩ€Щ„Ш§Ш¬ Щ„Щ„Щ…Щ€ШЇЩЉЩ„ Ш§Щ„ШЄШ§Щ„ЩЉШЊ Ш§Ш¶ШєШ· Ш№Щ„Щ‰ Ш§Щ„ШІШ± ШЈШЇЩ†Ш§Щ‡:",
        "err_gen": "вќЊ Ш­ШЇШ« Ш®Ш·ШЈ ШЈШ«Щ†Ш§ШЎ ШҐЩ†ШґШ§ШЎ Ш§Щ„ЩѓЩ€Щ„Ш§Ш¬: {e}",
        "folder_missing": "вљ пёЏ Щ…Ш¬Щ„ШЇ Ш§Щ„ЩѓЩ€Щ„Ш§Ш¬ ШєЩЉШ± Щ…Щ€Ш¬Щ€ШЇ.",
        "gen_empty": "вљ пёЏ ШЄШ№Ш°Ш± ШҐЩ†ШґШ§ШЎ Ш§Щ„ЩѓЩ€Щ„Ш§Ш¬.",
        "sending_originals": "Ш¬Ш§Ш±ЩЉ ШҐШ±ШіШ§Щ„ Ш§Щ„Щ…Щ„ЩЃШ§ШЄ Ш§Щ„ШЈШµЩ„ЩЉШ©...",
        "refreshing": "Ш¬Ш§Ш±ЩЉ ШЄШ­ШЇЩЉШ« Ш§Щ„Щ…Ш№Ш§ЩЉЩ†Ш©...",
        "ask_add_color": "рџ“ё ШЈШ±ШіЩ„ Ш§Щ„ШµЩ€Ш± Ш§Щ„ШҐШ¶Ш§ЩЃЩЉШ© (Ш§Щ„Щ„Щ€Щ† Ш§Щ„Ш¬ШЇЩЉШЇ) Щ„Щ‡Ш°Ш§ Ш§Щ„Щ…Щ€ШЇЩЉЩ„ШЊ Ш«Щ… Ш§Ш¶ШєШ· **В«вњ… ШЄЩ… Ш±ЩЃШ№ Ш¬Щ…ЩЉШ№ Ш§Щ„ШµЩ€Ш±В»**.",
        "deleted_toast": "ШЄЩ… Ш§Щ„Ш­Ш°ЩЃ",
        "deleted_msg": "рџ—‘ ШЄЩ… Ш­Ш°ЩЃ Ш§Щ„ЩѓЩ€Щ„Ш§Ш¬.",
        "catch_all": "рџЊё **Ш§ШіШЄЩ€ШЇЩЉЩ€ PГЋVOK**\nШ§Ш¶ШєШ· **В«рџ–ј ШҐЩ†ШґШ§ШЎ ЩѓЩ€Щ„Ш§Ш¬В»** ШЁШ§Щ„ШЈШіЩЃЩ„ (ШЈЩ€ ШЈШ±ШіЩ„ Ш§Щ„ШµЩ€Ш± Щ…ШЁШ§ШґШ±Ш© Щ‡Щ†Ш§):",
        "channels_title": "рџ“ў **Щ‚Щ†Щ€Ш§ШЄ Щ€Ш±Щ€Ш§ШЁШ· PГЋVOK Ш§Щ„Ш±ШіЩ…ЩЉШ©:**",
        "channels_empty": "в„№пёЏ Щ„Щ… ЩЉШЄЩ… ШҐШ№ШЇШ§ШЇ Ш±Щ€Ш§ШЁШ· Ш§Щ„Щ‚Щ†Щ€Ш§ШЄ ШЁШ№ШЇ.",
        "wa_ready": "вњ… **Щ€Ш§ШЄШіШ§ШЁ Щ…ШЄШµЩ„ Щ€Ш¬Ш§Щ‡ШІ Щ„Щ„ШҐШ±ШіШ§Щ„!**\nрџ“Њ Ш§Щ„Щ…Ш¬Щ…Щ€Ш№Ш©: `{grp}`",
        "wa_relink_btn": "рџ”„ ШҐШ№Ш§ШЇШ© Ш±ШЁШ· Щ€Ш§ШЄШіШ§ШЁ",
        "wa_refresh_qr_btn": "рџ”„ ШЄШ­ШЇЩЉШ« Ш±Щ…ШІ QR",
        "wa_gen_qr": "вЏі Ш¬Ш§Ш±ЩЉ ШҐЩ†ШґШ§ШЎ Ш±Щ…ШІ QR Щ„Щ€Ш§ШЄШіШ§ШЁШЊ ЩЉШ±Ш¬Щ‰ Ш§Щ„Ш§Щ†ШЄШёШ§Ш± 3 Ш«Щ€Ш§Щ†ЩЌ...",
        "wa_starting": "вЏі Ш¬Ш§Ш±ЩЉ ШЄШґШєЩЉЩ„ Ш®ШЇЩ…Ш© Щ€Ш§ШЄШіШ§ШЁ. ЩЉШ±Ш¬Щ‰ Ш§Щ„Ш¶ШєШ· Ш№Щ„Щ‰ **рџ“± WhatsApp QR** ШЁШ№ШЇ 5 Ш«Щ€Ш§Щ†ЩЌ.",
        "wa_qr_caption": (
            "рџ“± **Ш±ШЁШ· Щ€Ш§ШЄШіШ§ШЁ Щ„Щ„Щ†ШґШ± Ш§Щ„ШЄЩ„Щ‚Ш§Ш¦ЩЉ ЩЃЩЉ Ш§Щ„Щ…Ш¬Щ…Щ€Ш№Ш©**\n\n"
            "1. Ш§ЩЃШЄШ­ **Щ€Ш§ШЄШіШ§ШЁ** Ш№Щ„Щ‰ Щ‡Ш§ШЄЩЃЩѓ\n"
            "2. Ш§Ш°Щ‡ШЁ ШҐЩ„Щ‰ **Ш§Щ„ШҐШ№ШЇШ§ШЇШ§ШЄ в†ђ Ш§Щ„ШЈШ¬Щ‡ШІШ© Ш§Щ„Щ…Ш±ШЄШЁШ·Ш© в†ђ Ш±ШЁШ· Ш¬Щ‡Ш§ШІ**\n"
            "3. Ш§Щ…ШіШ­ Ш±Щ…ШІ QR Щ‡Ш°Ш§ (Щ…Ш±Ш© Щ€Ш§Ш­ШЇШ© ЩЃЩ‚Ш·!)"
        ),
        "wa_connected_push": "вњ… **ШЄЩ… Ш±ШЁШ· Щ€Ш§ШЄШіШ§ШЁ ШЁЩ†Ш¬Ш§Ш­!**\nрџ“Њ Ш§Щ„Щ…Ш¬Щ…Щ€Ш№Ш©: `{grp}`\nвЃпёЏ ШЄЩ… Ш­ЩЃШё Ш§Щ„Ш¬Щ„ШіШ© ЩЃЩЉ Ш§Щ„ШіШ­Ш§ШЁШ©.",
        "pub_wait": "вЏі Ш¬Ш§Ш±ЩЉ Щ†ШґШ± Ш§Щ„ЩѓЩ€Щ„Ш§Ш¬ШЊ ЩЉШ±Ш¬Щ‰ Ш§Щ„Ш§Щ†ШЄШёШ§Ш±...",
        "pub_tg_ok": "вњ… ШЄЩЉЩ„ЩЉШ¬Ш±Ш§Щ… ({ch}): ШЄЩ… Ш§Щ„Щ†ШґШ±!",
        "pub_tg_err": "вќЊ ШЄЩЉЩ„ЩЉШ¬Ш±Ш§Щ… ({ch}): ШЄШЈЩѓШЇ Щ…Щ† ШЈЩ† Ш§Щ„ШЁЩ€ШЄ Щ…ШґШ±ЩЃ ЩЃЩЉ Ш§Щ„Щ‚Щ†Ш§Ш© ({e})",
        "pub_wa_ok": "вњ… Щ…Ш¬Щ…Щ€Ш№Ш© Щ€Ш§ШЄШіШ§ШЁ: ШЄЩ… Ш§Щ„ШҐШ±ШіШ§Щ„!",
        "pub_wa_not_linked": "вљ пёЏ Щ€Ш§ШЄШіШ§ШЁ ШєЩЉШ± Щ…Ш±ШЄШЁШ· ШЁШ№ШЇ вЂ” Ш§Щ…ШіШ­ Ш±Щ…ШІ QR ШЈШЇЩ†Ш§Щ‡ Ш«Щ… Ш§Ш¶ШєШ· рџ’¬!",
    },
}

ALL_LANGS = ("ru", "tr", "ar", "en")
CREATE_TEXTS = {STRINGS[lg]["btn_create"] for lg in ALL_LANGS}
WA_QR_BTN_TEXTS = {STRINGS[lg]["btn_wa_qr"] for lg in ALL_LANGS}
CHANNELS_BTN_TEXTS = {STRINGS[lg]["btn_channels"] for lg in ALL_LANGS}
LANG_BTN_TEXTS = {STRINGS[lg]["btn_lang"] for lg in ALL_LANGS} | {"рџ‡№рџ‡· TГјrkГ§e / РЇР·С‹Рє", "рџ‡·рџ‡є Р СѓСЃСЃРєРёР№ / Dil"}
CANCEL_TEXTS = (
    {STRINGS[lg]["btn_cancel"] for lg in ALL_LANGS}
    | {"Cancel", "cancel", "РћС‚РјРµРЅР°", "Д°ptal", "iptal", "ШҐЩ„ШєШ§ШЎ", "/cancel", "/stop"}
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


def persist_store():
    _STORE["allowed_users"] = ALLOWED_USERS
    _STORE["pending_dispatches"] = pending_dispatches
    _STORE["media_msgs"] = media_msgs_store
    _STORE["user_langs_v4"] = user_langs
    _STORE["channel_links"] = saved_channel_links
    _STORE["published_records"] = published_records
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
        )
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(WA_AUTH_DIR)
        logger.info("вњ… Restored WhatsApp session from private HF dataset")
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
        logger.info("вЃпёЏ Backed up WhatsApp session to private HF dataset")
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
                    [node_bin, "--max-old-space-size=96", script_path],
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
                InlineKeyboardButton(text="рџ‡·рџ‡є Р СѓСЃСЃРєРёР№", callback_data="setlang_ru"),
                InlineKeyboardButton(text="рџ‡№рџ‡· TГјrkГ§e", callback_data="setlang_tr"),
                InlineKeyboardButton(text="рџ‡ёрџ‡¦ Ш§Щ„Ш№Ш±ШЁЩЉШ©", callback_data="setlang_ar"),
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
                    allowed_pw.add("РїРёРІРѕРє")
                if entered in allowed_pw:
                    ALLOWED_USERS.append(user_id)
                    lang = get_lang(user_id, getattr(user, "language_code", None))
                    set_lang(user_id, lang)
                    await event.answer(
                        "вњ… **Р”РѕСЃС‚СѓРї РѕС‚РєСЂС‹С‚! / EriЕџim aГ§Д±ldД±! / ШЄЩ… ЩЃШЄШ­ Ш§Щ„Щ€ШµЩ€Щ„!**\n\n" + tr_str(lang, "welcome"),
                        reply_markup=main_menu_kb(lang),
                        parse_mode="Markdown",
                    )
                    return
                await event.answer(
                    "рџ”’ Р‘РѕС‚ PГЋVOK Р·Р°С‰РёС‰С‘РЅ. Р’РІРµРґРёС‚Рµ РїР°СЂРѕР»СЊ:\n"
                    "рџ”’ PГЋVOK botu korumalД±dД±r. LГјtfen Еџifreyi girin:\n"
                    "рџ”’ Ш§Щ„ШЁЩ€ШЄ Щ…Ш­Щ…ЩЉ. ЩЉШ±Ш¬Щ‰ ШҐШЇШ®Ш§Щ„ ЩѓЩ„Щ…Ш© Ш§Щ„Щ…Ш±Щ€Ш±:"
                )
                return
            if hasattr(event, "data") and event.data:
                await event.answer("рџ”’ Р’РІРµРґРёС‚Рµ РїР°СЂРѕР»СЊ / Ећifreyi girin / ШЈШЇШ®Щ„ ЩѓЩ„Щ…Ш© Ш§Щ„Щ…Ш±Щ€Ш±", show_alert=True)
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
                    text=f"рџ›  {display_name}",
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
        await callback.answer("рџ”„")
        await asyncio.sleep(2.5)
    else:
        await callback.answer("рџ”„")
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
        toast = "рџ‡№рџ‡· TГјrkГ§e"
    elif callback.data.endswith("_ar"):
        new_lang = "ar"
        toast = "рџ‡ёрџ‡¦ Ш§Щ„Ш№Ш±ШЁЩЉШ©"
    elif callback.data.endswith("_en"):
        new_lang = "en"
        toast = "рџ‡¬рџ‡§ English"
    else:
        new_lang = "ru"
        toast = "рџ‡·рџ‡є Р СѓСЃСЃРєРёР№"
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


@dp.message(CollageForm.grouping_mode)
async def handle_grouping_mode(message: Message, state: FSMContext):
    lang = get_lang(message.from_user.id, message.from_user.language_code)
    is_smart = (message.text in SMART_GROUP_TEXTS) or ("РЈРјРЅР°СЏ" in (message.text or "")) or ("AkД±llД±" in (message.text or ""))
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
    cat_tag = "РћР”РРќРђР РќР«Р™" if is_single else "РџР›РђРўР¬Р•"
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


def _build_tg_collage_caption(code: str, sizes: str, price: str) -> str:
    wa_url = get_channel_link_url(PIVOK_CHANNEL_LINKS[1]) or "https://chat.whatsapp.com/Ddh7BKb1atCAhRzogy9l9Y"
    ig_url = get_channel_link_url(PIVOK_CHANNEL_LINKS[2]) or "https://www.instagram.com/pivok_fashion?cnvx=MWRzeWJkaGs1MGpsaQ=="
    sale_url = get_channel_link_url(PIVOK_CHANNEL_LINKS[4]) or "https://t.me/pivoksale"
    vid_url = get_channel_link_url(PIVOK_CHANNEL_LINKS[5]) or "https://t.me/pivokvideo"
    return (
        f"рџЊё <b>PГЋVOK</b>\n"
        f"рџ†• <b>NEW MODEL</b>\n"
        f"рџ”– <b>CODE:</b> <code>{code}</code>\n"
        f"вћ–вћ–вћ–вћ–вћ–вћ–вћ–вћ–\n"
        f"рџ›Ќ <b>Order & Catalog:</b>\n"
        f"<a href='{wa_url}'>рџџў WhatsApp</a> | "
        f"<a href='{ig_url}'>рџ“ё Instagram</a> | "
        f"<a href='{vid_url}'>рџ“№ Video</a> | "
        f"<a href='{sale_url}'>рџ”Ґ Sale</a>"
    )


def _build_wa_collage_caption(code: str, sizes: str, price: str) -> str:
    ig_url = "https://www.instagram.com/pivok_fashion"
    tg_url = "https://t.me/pivoklaleli"
    return (
        f"рџЊё *PГЋVOK* вЂ” *NEW MODEL*\n"
        f"рџ”– *CODE:* {code}\n"
        f"вњ€пёЏ Telegram: {tg_url}\n"
        f"рџ“ё Instagram: {ig_url}"
    )


@dp.callback_query(F.data.startswith(("pub_all_", "pub_tg_", "pub_wa_")))
async def cb_publish_collages(callback: CallbackQuery):
    if not bot:
        return
    if _PUB_LOCK.locked():
        await callback.answer("вЏі Sending in progress, please wait...", show_alert=False)
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

    do_tg = mode in ("all", "tg")
    do_wa = mode in ("all", "wa")

    if do_tg and not do_wa and rec.get("tg"):
        await callback.answer("вљ пёЏ Already sent to Telegram Channel!", show_alert=True)
        return
    if do_wa and not do_tg and rec.get("wa"):
        await callback.answer("вљ пёЏ Already sent to WhatsApp Group!", show_alert=True)
        return
    if do_tg and do_wa and rec.get("tg") and rec.get("wa"):
        await callback.answer("вљ пёЏ Already published to both TG Channel & WhatsApp!", show_alert=True)
        return

    await callback.answer("рџљЂ")
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
                            results_lines.append(f"вќЊ WhatsApp: {rj.get('error', resp.text)}")
                    except Exception as e:
                        results_lines.append(f"вќЊ WhatsApp: {e}")

    try:
        await status_msg.edit_text("\n".join(results_lines))
    except Exception:
        await bot.send_message(chat_id, "\n".join(results_lines))


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
                    logger.info(f"вњ… Telegram Webhook registered at {wh_url}")
                else:
                    logger.info(f"вљЎ Telegram Webhook already active at {wh_url}")
            except Exception as e:
                logger.warning(f"Webhook setup check warning: {e}")
                await bot.set_webhook(wh_url, secret_token=WEBHOOK_SECRET, drop_pending_updates=False)
        else:
            await bot.delete_webhook(drop_pending_updates=False)
            _polling_task = asyncio.create_task(dp.start_polling(bot))
            logger.info("рџЊё Started local Telegram polling (no PUBLIC_BASE_URL)")
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
        "version": "v4.3-wa-qr-page",
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
        return HTMLResponse(f"<html><body style='{style}'><h2>вњ… WhatsApp РїРѕРґРєР»СЋС‡С‘РЅ!</h2><p>Р“СЂСѓРїРїР°: {grp}</p></body></html>")
    qr_str = st.get("qr")
    if not qr_str:
        return HTMLResponse(
            f"<html><head><meta http-equiv='refresh' content='3'></head><body style='{style}'><h2>вЏі Р“РµРЅРµСЂРёСЂСѓСЋ QR-РєРѕРґ...</h2></body></html>"
        )
    png_b64 = base64.b64encode(await asyncio.to_thread(make_qr_png_bytes, qr_str)).decode()
    return HTMLResponse(
        f"<html><head><meta http-equiv='refresh' content='8'></head><body style='{style}'>"
        f"<h2 style='color:#d6a474'>рџ“± PГЋVOK вЂ” РїРѕРґРєР»СЋС‡РµРЅРёРµ WhatsApp</h2>"
        f"<img src='data:image/png;base64,{png_b64}' style='width:340px;border:14px solid #fff;border-radius:12px'/>"
        f"<p>WhatsApp в†’ РќР°СЃС‚СЂРѕР№РєРё в†’ РЎРІСЏР·Р°РЅРЅС‹Рµ СѓСЃС‚СЂРѕР№СЃС‚РІР° в†’ РџСЂРёРІСЏР·Р°С‚СЊ СѓСЃС‚СЂРѕР№СЃС‚РІРѕ</p>"
        f"<p style='opacity:.6'>РљРѕРґ РѕР±РЅРѕРІР»СЏРµС‚СЃСЏ Р°РІС‚РѕРјР°С‚РёС‡РµСЃРєРё</p></body></html>"
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
    category = parts[1] if len(parts) >= 5 else "РџР›РђРўР¬Р•"

    color_groups = {}
    for p in all_photos:
        prefix = p.split("-")[0] if "-" in p else "DEFAULT"
        color_groups.setdefault(prefix, []).append(p)

    is_single = (category.upper() in ("РћР”РРќРђР РќР«Р™", "SINGLE")) or all(
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
