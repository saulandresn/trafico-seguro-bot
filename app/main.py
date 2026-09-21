import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import select

from .db import Base, SessionLocal, engine
from .models import Incident, IncidentOwner, IncidentVote

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app = FastAPI(title="Tráfico Seguro Bot API", version="1.0.0")
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
AUTO_CLOSE_VOTES = 3
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


class IncidentCreate(BaseModel):
    kind: str
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    description: str = Field(default="", max_length=240)


class VoteCreate(BaseModel):
    action: str


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


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


def normalize_dt(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def is_expired(item: Incident, now: datetime | None = None) -> bool:
    now = now or now_utc()
    ttl = TTL_HOURS.get(item.kind, 12)
    return normalize_dt(item.created_at) < now - timedelta(hours=ttl)


def cleanup_expired(db) -> None:
    rows = db.scalars(select(Incident).where(Incident.active.is_(True))).all()
    changed = False
    now = now_utc()
    for item in rows:
        if is_expired(item, now):
            item.active = False
            item.updated_at = now
            changed = True
    if changed:
        db.commit()


def vote_counts(db, incident_id: int) -> dict:
    votes = db.scalars(select(IncidentVote).where(IncidentVote.incident_id == incident_id)).all()
    counts = {"confirm": 0, "gone": 0, "incorrect": 0}
    for vote in votes:
        if vote.action in counts:
            counts[vote.action] += 1
    return counts


def current_vote(db, incident_id: int, token: str | None) -> str | None:
    if not token:
        return None
    row = db.scalar(
        select(IncidentVote).where(
            IncidentVote.incident_id == incident_id,
            IncidentVote.voter_token == token,
        )
    )
    return row.action if row else None


def trust_label(confirmations: int, gone: int, incorrect: int) -> str:
    if incorrect >= 2:
        return "En revisión"
    if gone >= 2:
        return "Posiblemente resuelto"
    if confirmations >= 3 and incorrect == 0:
        return "Alta confianza"
    if confirmations >= 2:
        return "Confirmado"
    return "Pendiente de confirmar"


def serialize_incident(db, item: Incident, lat: float, lng: float, client_token: str | None) -> dict:
    counts = vote_counts(db, item.id)
    owner = db.get(IncidentOwner, item.id)
    total_confirmations = item.confirmations + counts["confirm"]
    age_seconds = max(0, int((now_utc() - normalize_dt(item.created_at)).total_seconds()))
    return {
        "id": item.id,
        "kind": item.kind,
        "description": item.description,
        "lat": item.latitude,
        "lng": item.longitude,
        "confirmations": total_confirmations,
        "gone_votes": counts["gone"],
        "incorrect_votes": counts["incorrect"],
        "distance_km": round(haversine_km(lat, lng, item.latitude, item.longitude), 2),
        "created_at": normalize_dt(item.created_at).isoformat(),
        "updated_at": normalize_dt(item.updated_at).isoformat(),
        "age_seconds": age_seconds,
        "trust": trust_label(total_confirmations, counts["gone"], counts["incorrect"]),
        "owned_by_me": bool(client_token and owner and owner.owner_token == client_token),
        "my_vote": current_vote(db, item.id, client_token),
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


def check_rate_limit(db, token: str) -> None:
    since = now_utc() - timedelta(minutes=RATE_LIMIT_WINDOW_MINUTES)
    owners = db.scalars(select(IncidentOwner).where(IncidentOwner.owner_token == token)).all()
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


def require_admin(key: str | None) -> None:
    if not ADMIN_KEY or key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Acceso de administración no autorizado")


@app.get("/health")
def health():
    return {"ok": True, "version": "1.0.0"}


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
const CACHE='trafico-seguro-v1';
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
    x_client_token: str | None = Header(default=None),
    x_official_report_key: str | None = Header(default=None),
):
    if payload.kind not in ALLOWED_KINDS:
        raise HTTPException(status_code=400, detail="Tipo de incidente no permitido")

    if payload.kind in OFFICIAL_ONLY_KINDS:
        if not OFFICIAL_REPORT_KEY or x_official_report_key != OFFICIAL_REPORT_KEY:
            raise HTTPException(status_code=403, detail="Este tipo solo admite información oficial")

    owner_token = clean_token(x_client_token)
    if payload.kind in USER_REPORTABLE_KINDS and not owner_token:
        raise HTTPException(status_code=400, detail="Falta identificador anónimo del dispositivo")

    with SessionLocal() as db:
        cleanup_expired(db)
        if owner_token:
            check_rate_limit(db, owner_token)

        candidates = db.scalars(
            select(Incident).where(
                Incident.active.is_(True),
                Incident.kind == payload.kind,
            )
        ).all()
        for existing in candidates:
            if haversine_km(
                payload.latitude,
                payload.longitude,
                existing.latitude,
                existing.longitude,
            ) <= DUPLICATE_RADIUS_KM:
                return JSONResponse(
                    status_code=409,
                    content={
                        "detail": "Ya existe un reporte similar muy cerca",
                        "duplicate_id": existing.id,
                        "distance_km": round(
                            haversine_km(
                                payload.latitude,
                                payload.longitude,
                                existing.latitude,
                                existing.longitude,
                            ),
                            3,
                        ),
                    },
                )

        row = Incident(
            kind=payload.kind,
            latitude=payload.latitude,
            longitude=payload.longitude,
            description=payload.description.strip(),
        )
        db.add(row)
        db.flush()
        if owner_token:
            db.add(IncidentOwner(incident_id=row.id, owner_token=owner_token))
        db.commit()
        db.refresh(row)
        return {"id": row.id, "status": "created"}


@app.get("/api/incidents/nearby")
def nearby(
    lat: float = Query(..., ge=-90, le=90),
    lng: float = Query(..., ge=-180, le=180),
    radius_km: float = Query(DEFAULT_RADIUS_KM, gt=0, le=MAX_RADIUS_KM),
    all_reports: bool = Query(False),
    kinds: str = Query(""),
    x_client_token: str | None = Header(default=None),
):
    client_token = clean_token(x_client_token)
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
            result.append(serialize_incident(db, item, lat, lng, client_token))

    result.sort(key=lambda x: (x["distance_km"], -x["confirmations"]))
    return {"radius_km": radius_km, "all_reports": all_reports, "incidents": result}


@app.get("/api/incidents/mine")
def my_incidents(
    lat: float = Query(..., ge=-90, le=90),
    lng: float = Query(..., ge=-180, le=180),
    x_client_token: str | None = Header(default=None),
):
    token = clean_token(x_client_token)
    if not token:
        raise HTTPException(status_code=400, detail="Falta identificador anónimo del dispositivo")

    with SessionLocal() as db:
        cleanup_expired(db)
        owners = db.scalars(select(IncidentOwner).where(IncidentOwner.owner_token == token)).all()
        result = []
        for owner in owners:
            item = db.get(Incident, owner.incident_id)
            if item:
                data = serialize_incident(db, item, lat, lng, token)
                data["active"] = item.active
                result.append(data)

    result.sort(key=lambda x: x["created_at"], reverse=True)
    return {"incidents": result}


@app.post("/api/incidents/{incident_id}/vote")
def vote_incident(
    incident_id: int,
    payload: VoteCreate,
    x_client_token: str | None = Header(default=None),
):
    client_token = clean_token(x_client_token)
    if not client_token:
        raise HTTPException(status_code=400, detail="Falta identificador anónimo del dispositivo")
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
                IncidentVote.voter_token == client_token,
            )
        )
        if existing:
            existing.action = payload.action
            existing.updated_at = now_utc()
        else:
            db.add(IncidentVote(
                incident_id=incident_id,
                voter_token=client_token,
                action=payload.action,
            ))

        row.updated_at = now_utc()
        db.flush()
        counts = vote_counts(db, incident_id)

        if counts["gone"] >= AUTO_CLOSE_VOTES or counts["incorrect"] >= AUTO_CLOSE_VOTES:
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
    x_client_token: str | None = Header(default=None),
):
    client_token = clean_token(x_client_token)
    if not client_token:
        raise HTTPException(status_code=400, detail="Falta identificador anónimo del dispositivo")

    with SessionLocal() as db:
        row = db.get(Incident, incident_id)
        owner = db.get(IncidentOwner, incident_id)
        if not row:
            raise HTTPException(status_code=404, detail="Incidente no encontrado")
        if not owner or owner.owner_token != client_token:
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
