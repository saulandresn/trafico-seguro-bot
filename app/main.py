import hashlib
import hmac
import json
import math
import os
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import select

from .db import Base, SessionLocal, engine
from .models import Incident, IncidentOwner, IncidentVote

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app = FastAPI(title="Tráfico Seguro Bot API", version="1.1.0")
Base.metadata.create_all(bind=engine)

USER_REPORTABLE_KINDS = {
    "accidente",
    "via_cerrada",
    "inundacion",
    "semaforo",
    "congestion",
    "obras",
    "alarma",
}
OFFICIAL_ONLY_KINDS = {"revision_oficial"}
ALLOWED_KINDS = USER_REPORTABLE_KINDS | OFFICIAL_ONLY_KINDS

DEFAULT_RADIUS_KM = float(os.getenv("SEARCH_RADIUS_KM", "3"))
MAX_RADIUS_KM = 10.0
OFFICIAL_REPORT_KEY = os.getenv("OFFICIAL_REPORT_KEY", "")
ADMIN_KEY = os.getenv("ADMIN_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
INIT_DATA_MAX_AGE_SECONDS = int(os.getenv("INIT_DATA_MAX_AGE_SECONDS", "86400"))
RATE_LIMIT_COUNT = 5
RATE_LIMIT_WINDOW_MINUTES = 15
DUPLICATE_RADIUS_KM = 0.2

TTL_HOURS = {
    "congestion": 2,
    "alarma": 2,
    "accidente": 6,
    "via_cerrada": 12,
    "inundacion": 8,
    "semaforo": 24,
    "obras": 24,
    "revision_oficial": 12,
}

URL_RE = re.compile(
    r"(https?://|www\.|t\.me/|telegram\.me/|(?:[a-z0-9-]+\.)+(?:com|net|org|io|me|app|xyz|ru|co)(?:/|\b))",
    re.IGNORECASE,
)
REPEATED_CHAR_RE = re.compile(r"(.)\1{7,}")
REPEATED_WORD_RE = re.compile(r"\b([a-záéíóúñü]{2,})\b(?:\s+\1\b){4,}", re.IGNORECASE)
BLOCKED_WORDS = {
    "puta",
    "puto",
    "mierda",
    "idiota",
    "imbecil",
    "estupido",
    "pendejo",
    "pendeja",
    "maricon",
    "cabron",
}


class IncidentCreate(BaseModel):
    kind: str
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    description: str = Field(default="", max_length=240)


class IncidentUpdate(IncidentCreate):
    pass


class VoteCreate(BaseModel):
    action: str


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def normalize_dt(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def clean_token(token: str | None) -> str | None:
    if not token:
        return None
    token = token.strip()
    if not token or len(token) > 80:
        return None
    return token


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.lower())
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = re.sub(r"\s+", " ", value).strip()
    return value


def validate_telegram_init_data(raw_init_data: str | None) -> dict:
    if not TELEGRAM_BOT_TOKEN:
        raise HTTPException(status_code=503, detail="Autenticación de Telegram no configurada")
    if not raw_init_data:
        raise HTTPException(status_code=401, detail="Abre la aplicación desde Telegram")

    try:
        pairs = dict(parse_qsl(raw_init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Datos de Telegram inválidos") from exc

    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise HTTPException(status_code=401, detail="Datos de Telegram sin firma")

    data_check_string = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
    secret_key = hmac.new(
        b"WebAppData",
        TELEGRAM_BOT_TOKEN.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    calculated_hash = hmac.new(
        secret_key,
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        raise HTTPException(status_code=401, detail="Firma de Telegram inválida")

    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Fecha de autenticación inválida") from exc

    age = int(now_utc().timestamp()) - auth_date
    if age < -60 or age > INIT_DATA_MAX_AGE_SECONDS:
        raise HTTPException(status_code=401, detail="La sesión de Telegram expiró")

    try:
        user = json.loads(pairs.get("user", "{}"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=401, detail="Usuario de Telegram inválido") from exc

    user_id = user.get("id")
    if not isinstance(user_id, int):
        raise HTTPException(status_code=401, detail="Telegram no proporcionó un usuario válido")

    return {"user": user, "auth_date": auth_date, "raw": pairs}


def actor_token_from_telegram(raw_init_data: str | None) -> tuple[str, dict]:
    verified = validate_telegram_init_data(raw_init_data)
    user_id = verified["user"]["id"]
    actor = hmac.new(
        TELEGRAM_BOT_TOKEN.encode("utf-8"),
        f"traffic-user:{user_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return actor, verified["user"]


def owner_matches_and_migrate(
    db,
    incident_id: int,
    actor_token: str,
    legacy_token: str | None,
) -> bool:
    owner = db.get(IncidentOwner, incident_id)
    if not owner:
        return False
    if owner.owner_token == actor_token:
        return True
    if legacy_token and owner.owner_token == legacy_token:
        owner.owner_token = actor_token
        db.flush()
        return True
    return False


def vote_counts(db, incident_id: int) -> dict:
    votes = db.scalars(select(IncidentVote).where(IncidentVote.incident_id == incident_id)).all()
    counts = {"confirm": 0, "gone": 0, "incorrect": 0}
    for vote in votes:
        if vote.action in counts:
            counts[vote.action] += 1
    return counts


def current_vote(
    db,
    incident_id: int,
    actor_token: str | None,
    legacy_token: str | None = None,
) -> str | None:
    if not actor_token:
        return None
    row = db.scalar(
        select(IncidentVote).where(
            IncidentVote.incident_id == incident_id,
            IncidentVote.voter_token == actor_token,
        )
    )
    if not row and legacy_token:
        row = db.scalar(
            select(IncidentVote).where(
                IncidentVote.incident_id == incident_id,
                IncidentVote.voter_token == legacy_token,
            )
        )
        if row:
            row.voter_token = actor_token
            db.flush()
    return row.action if row else None


def reputation_for_actor(db, actor_token: str) -> dict:
    owners = db.scalars(select(IncidentOwner).where(IncidentOwner.owner_token == actor_token)).all()
    positive = 0
    negative = 0
    report_count = 0
    for owner in owners:
        item = db.get(Incident, owner.incident_id)
        if not item:
            continue
        report_count += 1
        counts = vote_counts(db, item.id)
        positive += counts["confirm"]
        negative += counts["incorrect"] * 2

    score = (positive + 3) / (positive + negative + 6)
    score_pct = round(score * 100)
    weight = round(0.75 + score, 2)
    return {
        "score": score_pct,
        "weight": weight,
        "reports": report_count,
        "positive_signals": positive,
        "negative_signals": negative,
    }


def trust_label(weighted_confirmations: float, gone: int, incorrect: int) -> str:
    if incorrect >= 1:
        return "En revisión"
    if gone >= 1:
        return "Posiblemente resuelto"
    if weighted_confirmations >= 4:
        return "Alta confianza"
    if weighted_confirmations >= 2.25:
        return "Confirmado"
    return "Pendiente de confirmar"


def is_expired(item: Incident, now: datetime | None = None) -> bool:
    now = now or now_utc()
    ttl = TTL_HOURS.get(item.kind, 12)
    return normalize_dt(item.created_at) < now - timedelta(hours=ttl)


def cleanup_expired(db) -> None:
    rows = db.scalars(select(Incident).where(Incident.active.is_(True))).all()
    changed = False
    now = now_utc()
    for item in rows:
        counts = vote_counts(db, item.id)
        marked_for_removal = counts["gone"] > 0 or counts["incorrect"] > 0
        if is_expired(item, now) or marked_for_removal:
            item.active = False
            item.updated_at = now
            changed = True
    if changed:
        db.commit()


def moderate_description(db, description: str, actor_token: str) -> str:
    text = description.strip()
    if not text:
        return ""

    normalized = normalize_text(text)
    if URL_RE.search(normalized):
        raise HTTPException(status_code=400, detail="No se permiten enlaces en la descripción")
    if REPEATED_CHAR_RE.search(normalized) or REPEATED_WORD_RE.search(normalized):
        raise HTTPException(status_code=400, detail="La descripción parece spam repetitivo")

    words = set(re.findall(r"[a-zñ]+", normalized))
    if words & BLOCKED_WORDS:
        raise HTTPException(status_code=400, detail="La descripción contiene lenguaje no permitido")

    since = now_utc() - timedelta(minutes=30)
    owners = db.scalars(select(IncidentOwner).where(IncidentOwner.owner_token == actor_token)).all()
    repeats = 0
    for owner in owners:
        item = db.get(Incident, owner.incident_id)
        if not item or normalize_dt(item.created_at) < since:
            continue
        if normalize_text(item.description) == normalized and normalized:
            repeats += 1
    if repeats >= 2:
        raise HTTPException(status_code=400, detail="La misma descripción se ha repetido demasiadas veces")

    return text


def check_rate_limit(db, actor_token: str) -> None:
    since = now_utc() - timedelta(minutes=RATE_LIMIT_WINDOW_MINUTES)
    owners = db.scalars(select(IncidentOwner).where(IncidentOwner.owner_token == actor_token)).all()
    recent = 0
    for owner in owners:
        item = db.get(Incident, owner.incident_id)
        if item and normalize_dt(item.created_at) >= since:
            recent += 1
    if recent >= RATE_LIMIT_COUNT:
        raise HTTPException(
            status_code=429,
            detail=f"Máximo {RATE_LIMIT_COUNT} reportes cada {RATE_LIMIT_WINDOW_MINUTES} minutos",
        )


def find_duplicate(
    db,
    kind: str,
    latitude: float,
    longitude: float,
    exclude_id: int | None = None,
) -> Incident | None:
    candidates = db.scalars(
        select(Incident).where(Incident.active.is_(True), Incident.kind == kind)
    ).all()
    for existing in candidates:
        if exclude_id is not None and existing.id == exclude_id:
            continue
        if haversine_km(latitude, longitude, existing.latitude, existing.longitude) <= DUPLICATE_RADIUS_KM:
            return existing
    return None


def serialize_incident(
    db,
    item: Incident,
    lat: float,
    lng: float,
    actor_token: str | None,
    legacy_token: str | None = None,
) -> dict:
    counts = vote_counts(db, item.id)
    total_confirmations = item.confirmations + counts["confirm"]
    owner = db.get(IncidentOwner, item.id)
    owner_reputation = (
        reputation_for_actor(db, owner.owner_token)
        if owner
        else {"score": 50, "weight": 1.25, "reports": 0}
    )
    weighted_confirmations = round(total_confirmations * owner_reputation["weight"], 2)
    age_seconds = max(0, int((now_utc() - normalize_dt(item.created_at)).total_seconds()))
    owned_by_me = False
    if actor_token:
        owned_by_me = owner_matches_and_migrate(db, item.id, actor_token, legacy_token)

    return {
        "id": item.id,
        "kind": item.kind,
        "description": item.description,
        "lat": item.latitude,
        "lng": item.longitude,
        "confirmations": total_confirmations,
        "weighted_confirmations": weighted_confirmations,
        "owner_reputation": owner_reputation["score"],
        "gone_votes": counts["gone"],
        "incorrect_votes": counts["incorrect"],
        "distance_km": round(haversine_km(lat, lng, item.latitude, item.longitude), 2),
        "created_at": normalize_dt(item.created_at).isoformat(),
        "updated_at": normalize_dt(item.updated_at).isoformat(),
        "age_seconds": age_seconds,
        "trust": trust_label(weighted_confirmations, counts["gone"], counts["incorrect"]),
        "owned_by_me": owned_by_me,
        "my_vote": current_vote(db, item.id, actor_token, legacy_token),
        "share_url": f"/incident/{item.id}",
        "expires_in_seconds": max(
            0,
            int(
                (
                    normalize_dt(item.created_at)
                    + timedelta(hours=TTL_HOURS.get(item.kind, 12))
                    - now_utc()
                ).total_seconds()
            ),
        ),
    }


def require_admin(key: str | None) -> None:
    if not ADMIN_KEY or key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Acceso de administración no autorizado")


@app.get("/health")
def health():
    return {"ok": True, "version": "1.1.0"}


@app.get("/manifest.webmanifest")
def manifest():
    return JSONResponse({
        "name": "Tráfico Seguro",
        "short_name": "Tráfico Seguro",
        "start_url": "/mini-app",
        "display": "standalone",
        "background_color": "#f6f7f9",
        "theme_color": "#2481cc",
        "description": "Mapa comunitario de incidentes viales.",
    })


@app.get("/service-worker.js")
def service_worker():
    code = """
const CACHE='trafico-seguro-v2';
self.addEventListener('install',e=>e.waitUntil(caches.open(CACHE).then(c=>c.addAll(['/mini-app']))));
self.addEventListener('fetch',e=>{
  if(e.request.method!=='GET') return;
  e.respondWith(fetch(e.request).catch(()=>caches.match(e.request)));
});
"""
    return Response(content=code, media_type="application/javascript")


@app.get("/mini-app", response_class=HTMLResponse)
def mini_app(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="mini_app.html",
        context={"radius_km": DEFAULT_RADIUS_KM},
    )


@app.get("/incident/{incident_id}", response_class=HTMLResponse)
def incident_page(request: Request, incident_id: int):
    with SessionLocal() as db:
        cleanup_expired(db)
        item = db.get(Incident, incident_id)
        if not item or not item.active:
            raise HTTPException(status_code=404, detail="Reporte no disponible")
        counts = vote_counts(db, item.id)
        return templates.TemplateResponse(
            request=request,
            name="incident.html",
            context={
                "incident": item,
                "counts": counts,
                "share_url": str(request.url),
            },
        )


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, key: str = Query("")):
    require_admin(key)
    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={"admin_key": key},
    )


@app.post("/api/incidents")
def create_incident(
    payload: IncidentCreate,
    x_telegram_init_data: str | None = Header(default=None),
    x_client_token: str | None = Header(default=None),
    x_official_report_key: str | None = Header(default=None),
):
    if payload.kind not in ALLOWED_KINDS:
        raise HTTPException(status_code=400, detail="Tipo de incidente no permitido")

    if payload.kind in OFFICIAL_ONLY_KINDS:
        if not OFFICIAL_REPORT_KEY or x_official_report_key != OFFICIAL_REPORT_KEY:
            raise HTTPException(status_code=403, detail="Este tipo solo admite información oficial")

    actor_token, _ = actor_token_from_telegram(x_telegram_init_data)
    legacy_token = clean_token(x_client_token)

    with SessionLocal() as db:
        cleanup_expired(db)
        check_rate_limit(db, actor_token)
        description = moderate_description(db, payload.description, actor_token)

        duplicate = find_duplicate(db, payload.kind, payload.latitude, payload.longitude)
        if duplicate:
            return JSONResponse(
                status_code=409,
                content={
                    "detail": "Ya existe un reporte similar muy cerca",
                    "duplicate_id": duplicate.id,
                    "distance_km": round(
                        haversine_km(
                            payload.latitude,
                            payload.longitude,
                            duplicate.latitude,
                            duplicate.longitude,
                        ),
                        3,
                    ),
                },
            )

        row = Incident(
            kind=payload.kind,
            latitude=payload.latitude,
            longitude=payload.longitude,
            description=description,
        )
        db.add(row)
        db.flush()
        db.add(IncidentOwner(incident_id=row.id, owner_token=actor_token))
        db.commit()
        db.refresh(row)
        return {"id": row.id, "status": "created", "share_url": f"/incident/{row.id}"}


@app.put("/api/incidents/{incident_id}")
def update_incident(
    incident_id: int,
    payload: IncidentUpdate,
    x_telegram_init_data: str | None = Header(default=None),
    x_client_token: str | None = Header(default=None),
):
    if payload.kind not in USER_REPORTABLE_KINDS:
        raise HTTPException(status_code=400, detail="Tipo de incidente no permitido")

    actor_token, _ = actor_token_from_telegram(x_telegram_init_data)
    legacy_token = clean_token(x_client_token)

    with SessionLocal() as db:
        cleanup_expired(db)
        row = db.get(Incident, incident_id)
        if not row or not row.active:
            raise HTTPException(status_code=404, detail="Incidente no encontrado")
        if not owner_matches_and_migrate(db, incident_id, actor_token, legacy_token):
            raise HTTPException(status_code=403, detail="Solo puedes editar tus propios reportes")

        duplicate = find_duplicate(
            db,
            payload.kind,
            payload.latitude,
            payload.longitude,
            exclude_id=incident_id,
        )
        if duplicate:
            raise HTTPException(status_code=409, detail="Ya existe un reporte similar muy cerca")

        row.kind = payload.kind
        row.description = moderate_description(db, payload.description, actor_token)
        row.latitude = payload.latitude
        row.longitude = payload.longitude
        row.updated_at = now_utc()
        db.commit()
        return {"id": row.id, "status": "updated", "share_url": f"/incident/{row.id}"}


@app.get("/api/incidents/nearby")
def nearby(
    lat: float = Query(..., ge=-90, le=90),
    lng: float = Query(..., ge=-180, le=180),
    radius_km: float = Query(DEFAULT_RADIUS_KM, gt=0, le=MAX_RADIUS_KM),
    all_reports: bool = Query(False),
    kinds: str = Query(""),
    x_telegram_init_data: str | None = Header(default=None),
    x_client_token: str | None = Header(default=None),
):
    actor_token = None
    if x_telegram_init_data:
        actor_token, _ = actor_token_from_telegram(x_telegram_init_data)
    legacy_token = clean_token(x_client_token)
    selected_kinds = {x for x in kinds.split(",") if x} if kinds else set()

    with SessionLocal() as db:
        cleanup_expired(db)
        rows = db.scalars(select(Incident).where(Incident.active.is_(True))).all()
        result = []
        for item in rows:
            if selected_kinds and item.kind not in selected_kinds:
                continue
            distance = haversine_km(lat, lng, item.latitude, item.longitude)
            if not all_reports and distance > radius_km:
                continue
            result.append(
                serialize_incident(db, item, lat, lng, actor_token, legacy_token)
            )
        db.commit()

    result.sort(key=lambda x: (x["distance_km"], -x["weighted_confirmations"]))
    return {"radius_km": radius_km, "all_reports": all_reports, "incidents": result}


@app.get("/api/incidents/mine")
def my_incidents(
    lat: float = Query(..., ge=-90, le=90),
    lng: float = Query(..., ge=-180, le=180),
    x_telegram_init_data: str | None = Header(default=None),
    x_client_token: str | None = Header(default=None),
):
    actor_token, _ = actor_token_from_telegram(x_telegram_init_data)
    legacy_token = clean_token(x_client_token)

    with SessionLocal() as db:
        cleanup_expired(db)
        owner_tokens = {actor_token}
        if legacy_token:
            owner_tokens.add(legacy_token)

        owners = db.scalars(select(IncidentOwner)).all()
        result = []
        for owner in owners:
            if owner.owner_token not in owner_tokens:
                continue
            if owner.owner_token != actor_token:
                owner.owner_token = actor_token
            item = db.get(Incident, owner.incident_id)
            if item:
                data = serialize_incident(db, item, lat, lng, actor_token, legacy_token)
                data["active"] = item.active
                result.append(data)
        reputation = reputation_for_actor(db, actor_token)
        db.commit()

    result.sort(key=lambda x: x["created_at"], reverse=True)
    return {
        "incidents": result,
        "reputation": reputation,
    }


@app.get("/api/me/reputation")
def my_reputation(
    x_telegram_init_data: str | None = Header(default=None),
):
    actor_token, _ = actor_token_from_telegram(x_telegram_init_data)
    with SessionLocal() as db:
        return reputation_for_actor(db, actor_token)


@app.post("/api/incidents/{incident_id}/vote")
def vote_incident(
    incident_id: int,
    payload: VoteCreate,
    x_telegram_init_data: str | None = Header(default=None),
    x_client_token: str | None = Header(default=None),
):
    actor_token, _ = actor_token_from_telegram(x_telegram_init_data)
    legacy_token = clean_token(x_client_token)

    if payload.action not in {"confirm", "gone", "incorrect"}:
        raise HTTPException(status_code=400, detail="Acción no válida")

    with SessionLocal() as db:
        cleanup_expired(db)
        row = db.get(Incident, incident_id)
        if not row or not row.active:
            raise HTTPException(status_code=404, detail="Incidente no encontrado")

        existing = db.scalar(
            select(IncidentVote).where(
                IncidentVote.incident_id == incident_id,
                IncidentVote.voter_token == actor_token,
            )
        )
        if not existing and legacy_token:
            existing = db.scalar(
                select(IncidentVote).where(
                    IncidentVote.incident_id == incident_id,
                    IncidentVote.voter_token == legacy_token,
                )
            )
            if existing:
                existing.voter_token = actor_token

        if existing:
            existing.action = payload.action
            existing.updated_at = now_utc()
        else:
            db.add(IncidentVote(
                incident_id=incident_id,
                voter_token=actor_token,
                action=payload.action,
            ))

        row.updated_at = now_utc()
        db.flush()
        counts = vote_counts(db, incident_id)

        if counts["gone"] > 0 or counts["incorrect"] > 0:
            row.active = False

        db.commit()
        return {
            "status": "voted",
            "action": payload.action,
            "active": row.active,
            "confirmations": row.confirmations + counts["confirm"],
            "gone_votes": counts["gone"],
            "incorrect_votes": counts["incorrect"],
        }


@app.delete("/api/incidents/{incident_id}")
def delete_own_incident(
    incident_id: int,
    x_telegram_init_data: str | None = Header(default=None),
    x_client_token: str | None = Header(default=None),
):
    actor_token, _ = actor_token_from_telegram(x_telegram_init_data)
    legacy_token = clean_token(x_client_token)

    with SessionLocal() as db:
        row = db.get(Incident, incident_id)
        if not row:
            raise HTTPException(status_code=404, detail="Incidente no encontrado")
        if not owner_matches_and_migrate(db, incident_id, actor_token, legacy_token):
            raise HTTPException(status_code=403, detail="Solo puedes eliminar tus propios reportes")

        row.active = False
        row.updated_at = now_utc()
        db.commit()
        return {"status": "deleted"}


@app.get("/api/stats")
def stats():
    with SessionLocal() as db:
        cleanup_expired(db)
        rows = db.scalars(select(Incident)).all()
        active = [x for x in rows if x.active]
        by_kind = {}
        for item in active:
            by_kind[item.kind] = by_kind.get(item.kind, 0) + 1
        return {
            "total": len(rows),
            "active": len(active),
            "resolved": len(rows) - len(active),
            "by_kind": by_kind,
        }


@app.get("/api/admin/incidents")
def admin_incidents(
    x_admin_key: str | None = Header(default=None),
):
    require_admin(x_admin_key)
    with SessionLocal() as db:
        cleanup_expired(db)
        rows = db.scalars(select(Incident)).all()
        rows.sort(key=lambda x: normalize_dt(x.created_at), reverse=True)
        return {
            "incidents": [
                {
                    "id": x.id,
                    "kind": x.kind,
                    "description": x.description,
                    "lat": x.latitude,
                    "lng": x.longitude,
                    "active": x.active,
                    "confirmations": x.confirmations,
                    "created_at": normalize_dt(x.created_at).isoformat(),
                }
                for x in rows[:300]
            ]
        }


@app.post("/api/admin/incidents/{incident_id}/close")
def admin_close_incident(
    incident_id: int,
    x_admin_key: str | None = Header(default=None),
):
    require_admin(x_admin_key)
    with SessionLocal() as db:
        row = db.get(Incident, incident_id)
        if not row:
            raise HTTPException(status_code=404, detail="Incidente no encontrado")
        row.active = False
        row.updated_at = now_utc()
        db.commit()
        return {"status": "closed"}


@app.get("/map", response_class=HTMLResponse)
def map_page(request: Request, lat: float, lng: float, radius_km: float = DEFAULT_RADIUS_KM):
    if radius_km <= 0 or radius_km > MAX_RADIUS_KM:
        raise HTTPException(status_code=400, detail="Radio inválido")
    return templates.TemplateResponse(
        request=request,
        name="map.html",
        context={"lat": lat, "lng": lng, "radius_km": radius_km},
    )
