import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from telegram import KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
SEARCH_RADIUS_KM = float(os.getenv("SEARCH_RADIUS_KM", "3"))
LOCATION_TTL_MINUTES = int(os.getenv("LOCATION_TTL_MINUTES", "30"))

REPORT_TYPES = {
    "🚗 Accidente": "accidente",
    "⛔ Vía cerrada": "via_cerrada",
    "🌊 Inundación": "inundacion",
    "🚦 Semáforo dañado": "semaforo",
    "🚧 Obras": "obras",
    "🐢 Congestión": "congestion",
}

OFFICIAL_CONTROL_LABEL = "🛂 Control vial oficial"
MAIN_MENU = [["🚨 Reportar incidente", "🗺 Ver mapa"], ["❓ Ayuda"]]


def location_keyboard():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📍 Compartir mi ubicación", request_location=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def main_menu_keyboard():
    return ReplyKeyboardMarkup(MAIN_MENU, resize_keyboard=True)


def incident_types_keyboard():
    rows = [
        ["🚗 Accidente", "⛔ Vía cerrada"],
        ["🌊 Inundación", "🚦 Semáforo dañado"],
        ["🚧 Obras", "🐢 Congestión"],
        [OFFICIAL_CONTROL_LABEL],
        ["↩️ Volver"],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def incident_location_keyboard():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("📍 Usar mi ubicación actual", request_location=True)],
            ["↩️ Cancelar reporte"],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def set_user_location(context: ContextTypes.DEFAULT_TYPE, lat: float, lng: float):
    context.user_data["user_location"] = {
        "lat": lat,
        "lng": lng,
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=LOCATION_TTL_MINUTES),
    }


def get_valid_user_location(context: ContextTypes.DEFAULT_TYPE):
    data = context.user_data.get("user_location")
    if not data:
        return None
    if datetime.now(timezone.utc) >= data["expires_at"]:
        context.user_data.pop("user_location", None)
        return None
    return data


def map_url(lat: float, lng: float) -> str:
    params = urlencode({"lat": lat, "lng": lng, "radius_km": SEARCH_RADIUS_KM})
    return f"{PUBLIC_BASE_URL}/map?{params}"


def _post_incident(payload: dict):
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        f"{PUBLIC_BASE_URL}/api/incidents",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=12) as response:
        return json.loads(response.read().decode("utf-8"))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "Para usar el bot primero debes compartir una ubicación actual. "
        f"La referencia vence después de {LOCATION_TTL_MINUTES} minutos y no se guarda como historial personal.",
        reply_markup=location_keyboard(),
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Flujo de uso:\n"
        "1. Comparte tu ubicación.\n"
        "2. Elige Reportar incidente o Ver mapa.\n"
        "3. Para reportar, selecciona el tipo y envía la ubicación del incidente.\n\n"
        "Tipos reportables: accidente, vía cerrada, inundación, semáforo dañado, obras y congestión.\n"
        "🛂 Control vial oficial se muestra únicamente cuando proviene de información oficial.",
        reply_markup=main_menu_keyboard() if get_valid_user_location(context) else location_keyboard(),
    )


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("pending_kind", None)
    if get_valid_user_location(context):
        await update.message.reply_text("Reporte cancelado.", reply_markup=main_menu_keyboard())
    else:
        await update.message.reply_text("Reporte cancelado. Comparte tu ubicación para continuar.", reply_markup=location_keyboard())


async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    loc = update.message.location
    pending_kind = context.user_data.get("pending_kind")

    if pending_kind:
        user_loc = get_valid_user_location(context)
        if not user_loc:
            context.user_data.pop("pending_kind", None)
            await update.message.reply_text(
                "Tu ubicación de sesión venció. Compártela nuevamente antes de reportar.",
                reply_markup=location_keyboard(),
            )
            return

        payload = {
            "kind": pending_kind,
            "latitude": loc.latitude,
            "longitude": loc.longitude,
            "description": "",
        }

        try:
            result = await asyncio.to_thread(_post_incident, payload)
        except HTTPError as exc:
            await update.message.reply_text(
                f"No se pudo registrar el incidente (HTTP {exc.code}). Intenta nuevamente.",
                reply_markup=main_menu_keyboard(),
            )
            context.user_data.pop("pending_kind", None)
            return
        except (URLError, TimeoutError, OSError):
            await update.message.reply_text(
                "No se pudo conectar con el servicio de incidentes. Intenta nuevamente en unos minutos.",
                reply_markup=main_menu_keyboard(),
            )
            context.user_data.pop("pending_kind", None)
            return

        context.user_data.pop("pending_kind", None)
        await update.message.reply_text(
            f"✅ Incidente registrado con ID {result.get('id')}.\n\n"
            f"🗺 Ver mapa:\n{map_url(user_loc['lat'], user_loc['lng'])}",
            reply_markup=main_menu_keyboard(),
        )
        return

    set_user_location(context, loc.latitude, loc.longitude)
    await update.message.reply_text(
        "✅ Ubicación recibida. Ya puedes consultar el mapa o reportar un incidente.",
        reply_markup=main_menu_keyboard(),
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    if text == "↩️ Volver":
        context.user_data.pop("pending_kind", None)
        if get_valid_user_location(context):
            await update.message.reply_text("Menú principal.", reply_markup=main_menu_keyboard())
        else:
            await update.message.reply_text("Comparte tu ubicación para continuar.", reply_markup=location_keyboard())
        return

    if text == "↩️ Cancelar reporte":
        context.user_data.pop("pending_kind", None)
        await update.message.reply_text("Reporte cancelado.", reply_markup=main_menu_keyboard())
        return

    user_loc = get_valid_user_location(context)
    if not user_loc:
        await update.message.reply_text(
            "Primero debes compartir una ubicación actual para habilitar las funciones del bot.",
            reply_markup=location_keyboard(),
        )
        return

    if text == "🗺 Ver mapa":
        await update.message.reply_text(
            f"🗺 Mapa de actividad vial cercana:\n{map_url(user_loc['lat'], user_loc['lng'])}",
            reply_markup=main_menu_keyboard(),
        )
        return

    if text == "🚨 Reportar incidente":
        await update.message.reply_text(
            "Selecciona el tipo de incidente:",
            reply_markup=incident_types_keyboard(),
        )
        return

    if text == OFFICIAL_CONTROL_LABEL:
        await update.message.reply_text(
            "🛂 Los controles viales oficiales no se aceptan como reportes comunitarios en tiempo real. "
            "Solo se muestran cuando provienen de información pública u oficial.",
            reply_markup=incident_types_keyboard(),
        )
        return

    if text in REPORT_TYPES:
        context.user_data["pending_kind"] = REPORT_TYPES[text]
        await update.message.reply_text(
            f"Seleccionaste {text}.\n"
            "Ahora envía la ubicación exacta del incidente. Puedes usar tu ubicación actual si estás en el lugar "
            "o adjuntar una ubicación desde Telegram.",
            reply_markup=incident_location_keyboard(),
        )
        return

    if text == "❓ Ayuda":
        await help_cmd(update, context)
        return

    await update.message.reply_text(
        "Usa los botones del menú para continuar.",
        reply_markup=main_menu_keyboard(),
    )


def build_application() -> Application:
    if not TOKEN:
        raise RuntimeError("Falta TELEGRAM_BOT_TOKEN")

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    return app


if __name__ == "__main__":
    build_application().run_polling(allowed_updates=Update.ALL_TYPES)
