# Tráfico Seguro Bot

MVP de un bot de Telegram para consultar y reportar **incidentes viales** cercanos en un mapa.

## Alcance

Incluye: accidentes, vías cerradas, inundaciones, semáforos averiados, congestión y obras.

Por seguridad, el proyecto no está diseñado para localizar ni seguir en tiempo real a policías, militares u otros cuerpos de seguridad.

## Regla principal de privacidad

El usuario debe compartir voluntariamente su ubicación desde Telegram para abrir el mapa. La ubicación del usuario no se persiste en la base de datos en este MVP: se usa como parámetro de consulta para filtrar incidentes dentro del radio permitido.

## Stack

- Python 3.12+
- FastAPI
- python-telegram-bot
- SQLAlchemy
- PostgreSQL en producción / SQLite para desarrollo
- Leaflet + OpenStreetMap
- Railway o Render para despliegue

## Estructura

```text
app/
  bot.py          Bot de Telegram
  main.py         API FastAPI + mapa
  models.py       Modelo de incidentes
  db.py           Conexión a base de datos
  templates/
    map.html      Mapa Leaflet
.env.example
requirements.txt
Procfile
```

## Ejecutar localmente

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
cp .env.example .env
```

Configura `TELEGRAM_BOT_TOKEN` y luego inicia la API:

```bash
uvicorn app.main:app --reload
```

En otra terminal inicia el bot:

```bash
python -m app.bot
```

## API principal

- `GET /health`
- `POST /api/incidents`
- `GET /api/incidents/nearby?lat=...&lng=...&radius_km=3`
- `POST /api/incidents/{id}/confirm`
- `POST /api/incidents/{id}/close`
- `GET /map?lat=...&lng=...&radius_km=3`

## Despliegue recomendado

### Railway

1. Conecta el repositorio de GitHub.
2. Crea un servicio web para FastAPI.
3. Añade PostgreSQL.
4. Configura `DATABASE_URL`, `TELEGRAM_BOT_TOKEN` y `PUBLIC_BASE_URL`.
5. Despliega el bot como segundo servicio/worker usando `python -m app.bot`.

### Seguridad antes de producción

Antes de abrirlo a usuarios reales conviene añadir autenticación de reportes, anti-spam, deduplicación geográfica, límites por usuario, reputación, moderación y caducidad automática de incidentes.
