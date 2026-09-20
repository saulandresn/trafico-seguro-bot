import os
from urllib.parse import urlencode

from telegram import KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[KeyboardButton("📍 Compartir mi ubicación", request_location=True)]]
    await update.message.reply_text(
        "Para ver incidentes viales cercanos debes compartir tu ubicación actual. "
        "La consulta se limita a tu zona y no necesita almacenar tu ubicación permanentemente.",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True),
    )

async def location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    loc = update.message.location
    params = urlencode({"lat": loc.latitude, "lng": loc.longitude, "radius_km": 3})
    await update.message.reply_text(
        f"🗺 Mapa de incidentes viales cercanos:\n{PUBLIC_BASE_URL}/map?{params}\n\n"
        "La ubicación compartida se usa para generar esta consulta."
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/start — compartir ubicación y abrir actividad cercana\n"
        "/help — mostrar ayuda\n\n"
        "El MVP está orientado a accidentes, cierres, inundaciones, congestión, obras y semáforos averiados."
    )

def build_application() -> Application:
    if not TOKEN:
        raise RuntimeError("Falta TELEGRAM_BOT_TOKEN")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.LOCATION, location))
    return app

if __name__ == "__main__":
    build_application().run_polling(allowed_updates=Update.ALL_TYPES)
