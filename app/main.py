import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import select

from .db import Base, SessionLocal, engine
from .models import Incident, IncidentOwner, IncidentVote

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app = FastAPI(title="Tráfico Seguro Bot API", version="0.4.0")
Base.metadata.create_all(bind=engine)

USER_REPORTABLE_KINDS = {
    "accidente",
    "via_cerrada",
    "inundacion",
    "semaforo",
    "congestion",
    "obras",
}
OFFICIAL_ONLY_KINDS = {"revision_oficial"}
ALLOWED_KINDS = USER_REPORTABLE_KINDS | OFFICIAL_ONLY_KINDS

DEFAULT_RADIUS_KM = float(os.getenv("SEARCH_RADIUS_KM", "3"))
MAX_RADIUS_KM = 10.0
OFFICIAL_REPORT_KEY = os.getenv("OFFICIAL_REPORT_KEY", "")
AUTO_CLOSE_VOTES = 3


class IncidentCreate(BaseModel):
    kind: str
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    description: str = Field(default="", max_length=240)


class VoteCreate(BaseModel):
    action: str


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


def vote_counts(db, incident_id: int) -> dict:
    votes = db.scalars(
        select(IncidentVote).where(IncidentVote.incident_id == incident_id)
    ).all()
    counts = {"confirm": 0, "gone": 0, "incorrect": 0}
    for vote in votes:
        if vote.action in counts:
            counts[vote.action] += 1
    return counts


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/mini-app", response_class=HTMLResponse)
def mini_app(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="mini_app.html",
        context={"radius_km": DEFAULT_RADIUS_KM},
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

    row = Incident(
        kind=payload.kind,
        latitude=payload.latitude,
        longitude=payload.longitude,
        description=payload.description.strip(),
    )
    with SessionLocal() as db:
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
    x_client_token: str | None = Header(default=None),
):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=12)
    client_token = clean_token(x_client_token)

    with SessionLocal() as db:
        rows = db.scalars(
            select(Incident).where(Incident.active.is_(True), Incident.updated_at >= cutoff)
        ).all()

        result = []
        for item in rows:
            distance = haversine_km(lat, lng, item.latitude, item.longitude)
            if not all_reports and distance > radius_km:
                continue

            counts = vote_counts(db, item.id)
            owner = db.get(IncidentOwner, item.id)
            result.append({
                "id": item.id,
                "kind": item.kind,
                "description": item.description,
                "lat": item.latitude,
                "lng": item.longitude,
                "confirmations": item.confirmations + counts["confirm"],
                "gone_votes": counts["gone"],
                "incorrect_votes": counts["incorrect"],
                "distance_km": round(distance, 2),
                "updated_at": item.updated_at.isoformat(),
                "owned_by_me": bool(client_token and owner and owner.owner_token == client_token),
            })

    result.sort(key=lambda x: x["distance_km"])
    return {"radius_km": radius_km, "all_reports": all_reports, "incidents": result}


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
            existing.updated_at = datetime.now(timezone.utc)
        else:
            db.add(
                IncidentVote(
                    incident_id=incident_id,
                    voter_token=client_token,
                    action=payload.action,
                )
            )

        row.updated_at = datetime.now(timezone.utc)
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
        row.updated_at = datetime.now(timezone.utc)
        db.commit()
        return {"status": "deleted"}


@app.post("/api/incidents/{incident_id}/confirm")
def confirm_incident(incident_id: int):
    with SessionLocal() as db:
        row = db.get(Incident, incident_id)
        if not row or not row.active:
            raise HTTPException(status_code=404, detail="Incidente no encontrado")
        row.confirmations += 1
        row.updated_at = datetime.now(timezone.utc)
        db.commit()
        return {"status": "confirmed", "confirmations": row.confirmations}


@app.post("/api/incidents/{incident_id}/close")
def close_incident(incident_id: int):
    with SessionLocal() as db:
        row = db.get(Incident, incident_id)
        if not row:
            raise HTTPException(status_code=404, detail="Incidente no encontrado")
        row.active = False
        row.updated_at = datetime.now(timezone.utc)
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
