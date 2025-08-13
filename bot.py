#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram AI Bot — multilingual, auto-replies + AI fallback, context, TTS, SQLite
Author: Alouache Yacine
GitHub: https://github.com/Yactub (ضع رابط مشروعك)
License: MIT

Notes:
- Activation secrets are read from .env (don't put your keys in the code)
- Automatically supports Arabic/French/English + manual selection
- Programmed automatic responses (greetings, thanks, introduction) then reverts to AI when needed
"""

import os
import re
import logging
import sqlite3
import random
import tempfile
from typing import List, Dict, Tuple, Optional

import requests
from dotenv import load_dotenv
from gtts import gTTS
from langdetect import detect, DetectorFactory  # للكشف التلقائي عن اللغة (بسيط)
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.error import TelegramError

# --------- General Settings ---------

DetectorFactory.seed = 0  # لتثبيت نتيجة langdetect بين التشغيلات
load_dotenv()

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
MISTRAL_API_KEY  = os.getenv("MISTRAL_API_KEY", "")
MISTRAL_URL      = os.getenv("MISTRAL_URL", "https://api.mistral.ai/v1/chat/completions")
MISTRAL_MODEL    = os.getenv("MISTRAL_MODEL", "mistral-small")
ADMIN_USER_ID    = int(os.getenv("ADMIN_USER_ID", "0") or 0)

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is missing in .env")
if not MISTRAL_API_KEY:
    print("⚠️ Warning: MISTRAL_API_KEY missing — replies will fallback to error message.")

# --------- registration ---------

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("telegram-ai-bot")

# --------- Database ---------

DB_PATH = os.getenv("DB_PATH", "bot.db")
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cur  = conn.cursor()
cur.execute("""
CREATE TABLE IF NOT EXISTS users (
  user_id INTEGER PRIMARY KEY,
  language TEXT,
  auto_detect INTEGER DEFAULT 1  -- 1 = on, 0 = off
)
""")
cur.execute("""
CREATE TABLE IF NOT EXISTS logs (
  user_id INTEGER,
  role TEXT CHECK(role IN ('user','bot')),
  message TEXT
)
""")
cur.execute("""
CREATE TABLE IF NOT EXISTS feedback (
  user_id INTEGER,
  message TEXT
)
""")
conn.commit()

# --------- DB Functions ---------

def set_user_language(user_id: int, lang: str):
    cur.execute("INSERT INTO users(user_id, language, auto_detect) VALUES(?,?,COALESCE((SELECT auto_detect FROM users WHERE user_id=?),1)) ON CONFLICT(user_id) DO UPDATE SET language=excluded.language",
                (user_id, lang, user_id))
    conn.commit()

def set_auto_detect(user_id: int, enabled: bool):
    cur.execute("INSERT INTO users(user_id, language, auto_detect) VALUES(?, COALESCE((SELECT language FROM users WHERE user_id=?),'en'), ?) ON CONFLICT(user_id) DO UPDATE SET auto_detect=excluded.auto_detect",
                (user_id, user_id, 1 if enabled else 0))
    conn.commit()

def get_user_prefs(user_id: int) -> Tuple[str, bool]:
    cur.execute("SELECT language, auto_detect FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    if not row:
        return ("en", True)
    lang, auto = row
    return (lang or "en", bool(auto))

def log_message(user_id: int, role: str, message: str):
    cur.execute("INSERT INTO logs(user_id, role, message) VALUES(?,?,?)", (user_id, role, message))
    conn.commit()

def clear_logs(user_id: int):
    cur.execute("DELETE FROM logs WHERE user_id=?", (user_id,))
    conn.commit()

def get_history(user_id: int, limit: int = 12) -> List[Tuple[str, str]]:
    cur.execute("SELECT role, message FROM logs WHERE user_id=? ORDER BY rowid DESC LIMIT ?", (user_id, limit))
    rows = cur.fetchall()
    rows.reverse()
    return rows

# --------- أدوات نص ---------
def split_text(text: str, max_len: int = 4000) -> List[str]:
    if not text:
        return [""]
    out, t = [], text.strip()
    while len(t) > max_len:
        i = t.rfind("\n", 0, max_len)
        if i == -1: i = t.rfind(" ", 0, max_len)
        if i == -1: i = max_len
        out.append(t[:i].strip())
        t = t[i:].strip()
    if t: out.append(t)
    return out

# --------- Interface Messages ---------

UI = {
    "welcome": {
        "ar": lambda n: f"🤖 مرحبًا {n}! اختَر لغتك لبدء الاستخدام:",
        "fr": lambda n: f"🤖 Bonjour {n} ! Choisissez votre langue pour démarrer :",
        "en": lambda n: f"🤖 Hello {n}! Choose your language to start:",
    },
    "help": {
        "ar": (
            "📌 الأوامر:\n"
            "/start اختيار اللغة\n"
            "/language تغيير اللغة\n"
            "/auto تفعيل/تعطيل كشف اللغة\n"
            "/clear مسح المحادثة\n"
            "/history آخر الرسائل\n"
            "/about عن البوت\n"
            "/voice آخر رد صوتيًا\n"
            "/details شرح مُفصّل لآخر سؤال"
        ),
        "fr": (
            "📌 Commandes :\n"
            "/start choisir la langue\n"
            "/language changer la langue\n"
            "/auto activer/désactiver détection automatique\n"
            "/clear effacer la conversation\n"
            "/history derniers messages\n"
            "/about à propos\n"
            "/voice dernier message en audio\n"
            "/details détailler la dernière question"
        ),
        "en": (
            "📌 Commands:\n"
            "/start choose language\n"
            "/language change language\n"
            "/auto toggle auto language detection\n"
            "/clear clear conversation\n"
            "/history recent messages\n"
            "/about about the bot\n"
            "/voice last reply as audio\n"
            "/details expand the last question"
        ),
    },
    "about": {
        "ar": "بوت متعدد اللغات من تطوير Alouache Yacine. يستخدم Mistral API مع سياق، وردود تلقائية منظمة.",
        "fr": "Bot multilingue développé par Alouache Yacine. Utilise l'API Mistral avec contexte et réponses auto.",
        "en": "Multilingual bot by Alouache Yacine. Uses Mistral API with context and structured auto-replies.",
    },
    "loading": {"ar": "⏳ جاري المعالجة...", "fr": "⏳ Traitement...", "en": "⏳ Processing..."},
    "cleared": {"ar": "🗑️ تم مسح المحادثة.", "fr": "🗑️ Conversation effacée.", "en": "🗑️ Conversation cleared."},
    "no_history": {"ar": "📭 لا يوجد سجل.", "fr": "📭 Aucun historique.", "en": "📭 No history found."},
    "no_voice": {"ar": "⚠ لا يوجد رد لإرساله صوتيًا.", "fr": "⚠ Aucun message vocal.", "en": "⚠ No voice message."},
    "toggled": {
        True: {"ar":"✅ تم تفعيل الكشف التلقائي.","fr":"✅ Détection auto activée.","en":"✅ Auto-detect enabled."},
        False:{"ar":"⛔ تم تعطيل الكشف التلقائي.","fr":"⛔ Détection auto désactivée.","en":"⛔ Auto-detect disabled."}
    }
}

def t(msg_key: str, lang: str, name: Optional[str] = None) -> str:
    data = UI.get(msg_key, {})
    val = data.get(lang) or data.get("en")
    return val(name) if callable(val) else val

# --------- Language Reveal ---------

LANG_MAP = {"ar":"ar","fr":"fr","en":"en"}
def detect_lang(text: str) -> str:
    try:
        code = detect(text or "")
        # تبسيط النتيجة إلى ar/fr/en فقط
        if code.startswith("ar"): return "ar"
        if code.startswith("fr"): return "fr"
        return "en"
    except Exception:
        return "en"

# --------- Simple Auto Replies ---------

AUTO_PATTERNS = {
    "ar": [
        (re.compile(r"^(sal[aā]m|سلام|السلام عليكم)\b", re.I), "وعليكم السلام! كيف نقدر نعاونك؟"),
        (re.compile(r"(شكرا|يعطيك الصحة|بارك الله فيك)", re.I), "على الرحب والسعة! ✨"),
        (re.compile(r"(من (.*)انت|شنو هاد|واش انت)", re.I), "أنا مساعد ذكي من تطوير علواش ياسين 😄"),
    ],
    "fr": [
        (re.compile(r"^(salut|bonjour|bonsoir)\b", re.I), "Salut ! Comment puis-je t’aider ?"),
        (re.compile(r"(merci|thanks)", re.I), "Avec plaisir ! ✨"),
        (re.compile(r"(tu es qui|c'est quoi ce bot)", re.I), "Je suis un assistant IA développé par Alouache Yacine 😄"),
    ],
    "en": [
        (re.compile(r"^(hi|hello|hey)\b", re.I), "Hey! How can I help?"),
        (re.compile(r"(thanks|thank you)", re.I), "You're welcome! ✨"),
        (re.compile(r"(who are you|what are you)", re.I), "I'm an assistant built by Alouache Yacine 😄"),
    ],
}

def try_auto_reply(text: str, lang: str) -> Optional[str]:
    for rx, reply in AUTO_PATTERNS.get(lang, []):
        if rx.search(text or ""):
            return reply
    return None

# --------- Building messages for context ---------

def build_context_messages(user_id: int, lang: str, max_items: int = 14):
    sys = {
        "ar": "أنت مساعد مختصر ودقيق بالعربية. استعمل سياق المحادثة عند الحاجة.",
        "fr": "Tu es un assistant concis en français. Utilise le contexte si pertinent.",
        "en": "You are a concise English assistant. Use conversation context when relevant.",
    }
    messages = [{"role":"system","content": sys.get(lang, sys["en"])}]
    for role, msg in get_history(user_id, max_items):
        messages.append({"role": "user" if role=="user" else "assistant", "content": msg})
    return messages

# --------- Summon Mistral ---------

def call_mistral(messages: list, timeout: int = 18) -> str:
    try:
        headers = {"Authorization": f"Bearer {MISTRAL_API_KEY}", "Content-Type": "application/json"}
        payload = {"model": MISTRAL_MODEL, "messages": messages}
        r = requests.post(MISTRAL_URL, headers=headers, json=payload, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        logger.exception("Mistral call failed")
        return "Sorry, the AI service is unavailable right now. Please try again later."

# --------- Summon Mistral ---------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name or "there"
    kb = [
        [InlineKeyboardButton("🇦🇪 العربية", callback_data="lang_ar")],
        [InlineKeyboardButton("🇫🇷 Français", callback_data="lang_fr")],
        [InlineKeyboardButton("🇬🇧 English", callback_data="lang_en")],
    ]
    await update.message.reply_text(t("welcome","en",name), reply_markup=InlineKeyboardMarkup(kb))

async def language_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    lang = q.data.split("_",1)[1]
    set_user_language(q.from_user.id, lang)
    await q.edit_message_text({
        "ar":"✅ تم ضبط اللغة العربية.",
        "fr":"✅ Le français est sélectionné.",
        "en":"✅ English selected.",
    }.get(lang,"✅ Language updated."))

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang,_ = get_user_prefs(update.effective_user.id)
    await update.message.reply_text(UI["help"].get(lang, UI["help"]["en"]))

async def language_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("🇦🇪 العربية", callback_data="lang_ar")],
        [InlineKeyboardButton("🇫🇷 Français", callback_data="lang_fr")],
        [InlineKeyboardButton("🇬🇧 English", callback_data="lang_en")],
    ]
    await update.message.reply_text("🌐 Choose a language:", reply_markup=InlineKeyboardMarkup(kb))

async def auto_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user.id
    lang, auto = get_user_prefs(user)
    set_auto_detect(user, not auto)
    msg = UI["toggled"][not auto].get(lang, UI["toggled"][not auto]["en"])
    await update.message.reply_text(msg)

async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_logs(update.effective_user.id)
    lang,_ = get_user_prefs(update.effective_user.id)
    await update.message.reply_text(UI["cleared"].get(lang, UI["cleared"]["en"]))

async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang,_ = get_user_prefs(uid)
    rows = get_history(uid, 10)
    if not rows:
        await update.message.reply_text(UI["no_history"].get(lang, UI["no_history"]["en"]))
        return
    txt = []
    for role, m in rows:
        txt.append(("👤 You: " if role=="user" else "🤖 Bot: ") + m)
    for chunk in split_text("\n".join(txt)):
        await update.message.reply_text(chunk)

async def about_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang,_ = get_user_prefs(update.effective_user.id)
    await update.message.reply_text(UI["about"].get(lang, UI["about"]["en"]))

async def voice_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang,_ = get_user_prefs(uid)
    cur.execute("SELECT message FROM logs WHERE user_id=? AND role='bot' ORDER BY rowid DESC LIMIT 1", (uid,))
    row = cur.fetchone()
    if not row:
        await update.message.reply_text(UI["no_voice"].get(lang, UI["no_voice"]["en"]))
        return
    text = row[0]
    tts_lang = "ar" if lang=="ar" else ("fr" if lang=="fr" else "en")
    try:
        tts = gTTS(text=text, lang=tts_lang)
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=True) as tmp:
            tts.save(tmp.name)
            tmp.seek(0)
            await update.message.reply_voice(voice=InputFile(tmp.name))
    except Exception:
        await update.message.reply_text({"ar":"خطأ في إنشاء الصوت.","fr":"Erreur de synthèse vocale.","en":"Error generating voice."}.get(lang,"Error."))

async def details_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang,_ = get_user_prefs(uid)
    # ابحث عن آخر رسالة مستخدم
    cur.execute("SELECT message FROM logs WHERE user_id=? AND role='user' ORDER BY rowid DESC LIMIT 1", (uid,))
    row = cur.fetchone()
    if not row:
        await update.message.reply_text({"ar":"لا يوجد سؤال سابق.","fr":"Aucun message précédent.","en":"No previous message."}.get(lang,"No previous message."))
        return
    loading = await update.message.reply_text(UI["loading"].get(lang, UI["loading"]["en"]))
    msgs = build_context_messages(uid, lang, 18)
    # نطلب تفصيلًا أكثر عبر system prompt إضافي صغير
    msgs[0]["content"] += {
        "ar":" قدم شرحًا مفصلًا مع أمثلة عند اللزوم.",
        "fr":" Donne une explication détaillée avec exemples si pertinent.",
        "en":" Provide a detailed explanation with examples when relevant.",
    }.get(lang," Provide a detailed explanation.")
    reply = call_mistral(msgs)
    log_message(uid, "bot", reply)
    chunks = split_text(reply)
    try:
        await loading.edit_text(chunks[0])
        for c in chunks[1:]:
            await update.message.reply_text(c)
    except Exception:
        for c in chunks:
            await update.message.reply_text(c)

# --------- Main message handler ---------

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text or ""
    pref_lang, auto = get_user_prefs(uid)

    # Language detection (if enabled)
    lang = detect_lang(text) if auto else pref_lang

    # Store user message for context
    log_message(uid, "user", text)

    # Try auto-replies
    auto_reply = try_auto_reply(text, lang)
    if auto_reply:
        log_message(uid, "bot", auto_reply)
        await update.message.reply_text(auto_reply)
        return

    # Otherwise -> AI with context
    loading = await update.message.reply_text(UI["loading"].get(lang, UI["loading"]["en"]))
    msgs = build_context_messages(uid, lang, 18)
    reply = call_mistral(msgs)
    log_message(uid, "bot", reply)

    chunks = split_text(reply)
    try:
        await loading.edit_text(chunks[0])
        for c in chunks[1:]:
            await update.message.reply_text(c)
    except Exception:
        for c in chunks:
            await update.message.reply_text(c)


# --------- General Errors ---------

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Exception: %s", context.error)


def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Orders
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(language_cb, pattern=r"^lang_"))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("language", language_cmd))
    app.add_handler(CommandHandler("auto", auto_cmd))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("about", about_cmd))
    app.add_handler(CommandHandler("voice", voice_cmd))
    app.add_handler(CommandHandler("details", details_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    app.add_error_handler(on_error)
    logger.info("✅ Bot started.")
    app.run_polling()

if __name__ == "__main__":
    main()
