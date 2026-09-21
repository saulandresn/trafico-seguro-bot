import os

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
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


async def configure_bot(application: Application) -> None:
    await application.bot.set_my_commands([
        BotCommand("start", "Iniciar Tráfico Seguro"),
        BotCommand("help", "Ver ayuda"),
    ])
    await application.bot.set_my_description(
        description=(
            "Tráfico Seguro permite consultar y reportar incidentes viales "
            "dentro de la provincia de Loja. Pulsa INICIAR para comenzar."
        )
    )
    await application.bot.set_my_short_description(
        short_description="Incidentes viales en Loja. Pulsa INICIAR para comenzar."
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚦 Bienvenido a Tráfico Seguro.\n\n"
        "Consulta incidentes viales y contribuye con reportes dentro de la provincia de Loja. "
        "Para comenzar, pulsa el botón de abajo.",
        reply_markup=mini_app_markup(),
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Pulsa «🚦 Abrir Tráfico Seguro». "
        "La Mini App comprobará tu ubicación, te mostrará un tutorial breve y, si estás dentro "
        "de la provincia de Loja, podrás consultar y reportar incidentes.",
        reply_markup=mini_app_markup(),
    )


def build_application() -> Application:
    if not TOKEN:
        raise RuntimeError("Falta TELEGRAM_BOT_TOKEN")

    app = Application.builder().token(TOKEN).post_init(configure_bot).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    return app


if __name__ == "__main__":
    build_application().run_polling(allowed_updates=Update.ALL_TYPES)
