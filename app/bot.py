import os

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")


def mini_app_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(
                "🚦 Abrir Tráfico Seguro",
                web_app=WebAppInfo(url=f"{PUBLIC_BASE_URL}/mini-app"),
            )
        ]]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Abre la aplicación para consultar actividad vial y reportar incidentes. "
        "La aplicación comprobará si la ubicación está disponible en tu dispositivo antes de habilitar las funciones.",
        reply_markup=mini_app_markup(),
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Pulsa «🚦 Abrir Tráfico Seguro». "
        "Dentro de la Mini App se comprobará la disponibilidad de ubicación y, si hace falta, Telegram solicitará permiso. "
        "Después podrás ver el mapa o reportar un incidente.",
        reply_markup=mini_app_markup(),
    )


def build_application() -> Application:
    if not TOKEN:
        raise RuntimeError("Falta TELEGRAM_BOT_TOKEN")

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    return app


if __name__ == "__main__":
    build_application().run_polling(allowed_updates=Update.ALL_TYPES)
