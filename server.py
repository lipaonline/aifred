"""
Serveur web d'AIfred : la page du téléphone, les jetons LiveKit, les liens d'invitation, les photos,
la fiche d'intervention validée.

    uv run uvicorn server:app --port 8080 --reload

Les jetons reprennent LearnFloo (convex/livekit.ts, action join) : identité, nom, rôle, droits,
durée. Différence : le jeton de l'équipe demande l'agent « aifred » (dispatch à la création de la
salle), celui d'un artisan ou d'un propriétaire non, pour qu'AIfred n'ouvre jamais une salle seul avec eux.
"""

import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import date, timedelta

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from livekit import api
from pydantic import BaseModel

import config
import db

WEB = config.ROOT / "web"
PHOTOS = config.DATA_DIR / "photos"
ROOM_RE = re.compile(r"^[a-z0-9-]{4,40}$")
PHOTO_RE = re.compile(r"^[a-f0-9]{16,32}\.jpg$")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.seed()
    PHOTOS.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(title="AIfred", lifespan=lifespan)


@app.middleware("http")
async def no_cache_api(request, call_next):
    # Sur Bunny, le CDN est devant le conteneur : il ne doit garder ni les jetons ni les invitations
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def _token(identity: str, name: str, room: str, role: str, with_agent: bool, sujet: str = "") -> str:
    if not (config.LIVEKIT_URL and config.LIVEKIT_API_KEY and config.LIVEKIT_API_SECRET):
        raise HTTPException(503, "LiveKit n'est pas configuré (LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET)")
    token = (
        api.AccessToken(config.LIVEKIT_API_KEY, config.LIVEKIT_API_SECRET)
        .with_identity(identity)
        .with_name(name)
        .with_ttl(timedelta(hours=4))
        # L'agent lit le rôle pour savoir qui peut confirmer une action (seulement l'équipe)
        .with_attributes({"role": role, "sujet": sujet})
        .with_grants(api.VideoGrants(
            room_join=True,
            room=room,
            can_publish=True,
            can_subscribe=True,
            can_publish_data=True,
            can_update_own_metadata=False,
        ))
    )
    if with_agent:
        token = token.with_room_config(api.RoomConfiguration(
            agents=[api.RoomAgentDispatch(agent_name=config.AGENT_NAME)],
        ))
    return token.to_jwt()


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", db._plain(name)).strip("-")[:20] or "membre"


def _team_allowed(code: str) -> bool:
    if not config.TEAM_CODE:
        # Sans code d'équipe, seulement en local : en ligne, n'importe qui pourrait ouvrir des visios.
        return config.PUBLIC_URL.startswith("http://localhost")
    return secrets.compare_digest(code, config.TEAM_CODE)


class TeamJoin(BaseModel):
    code: str = ""
    name: str
    room: str | None = None
    sujet: str = ""


@app.post("/api/team/join")
def team_join(body: TeamJoin):
    if not _team_allowed(body.code):
        raise HTTPException(403, "Code d'équipe incorrect")
    name = body.name.strip()[:40]
    if not name:
        raise HTTPException(400, "Prénom manquant")
    room = body.room or config.CANAL_ROOM
    if not ROOM_RE.match(room):
        raise HTTPException(400, "Nom de salle invalide")
    identity = f"equipe-{_slug(name)}-{secrets.token_hex(2)}"
    return {
        "token": _token(identity, name, room, "equipe", with_agent=True, sujet=body.sujet.strip()[:120]),
        "url": config.LIVEKIT_URL,
        "room": room,
    }


class TeamCode(BaseModel):
    code: str = ""


class AgentCall(TeamCode):
    room: str


_agent_called: dict[str, float] = {}


@app.post("/api/team/agent")
async def call_agent(body: AgentCall):
    """Le canal peut survivre à l'agent (mise en ligne, panne) : LiveKit ne l'envoie qu'à la création de la
    salle, donc la page le rappelle quand il n'est plus là."""
    if not _team_allowed(body.code):
        raise HTTPException(403, "Code d'équipe incorrect")
    if not ROOM_RE.match(body.room):
        raise HTTPException(400, "Nom de salle invalide")
    now = time.time()
    # Tous les téléphones appellent en même temps : un seul agent à la fois
    if now - _agent_called.get(body.room, 0) < 15:
        return {"ok": True}
    _agent_called[body.room] = now
    url = config.LIVEKIT_URL.replace("wss://", "https://")
    async with api.LiveKitAPI(url, config.LIVEKIT_API_KEY, config.LIVEKIT_API_SECRET) as lk:
        people = await lk.room.list_participants(api.ListParticipantsRequest(room=body.room))
        if not any(p.kind == api.ParticipantInfo.Kind.AGENT for p in people.participants):
            await lk.agent_dispatch.create_dispatch(
                api.CreateAgentDispatchRequest(agent_name=config.AGENT_NAME, room=body.room))
    return {"ok": True}


@app.post("/api/team/contacts")
def team_contacts(body: TeamCode):
    # Sans les numéros : la page n'en a pas besoin, et ils restent côté serveur
    if not _team_allowed(body.code):
        raise HTTPException(403, "Code d'équipe incorrect")
    return {"contacts": [{"id": c["id"], "name": c["name"], "trade": c["trade"]} for c in db.contacts()]}


@app.post("/api/team/houses")
def team_houses(body: TeamCode):
    if not _team_allowed(body.code):
        raise HTTPException(403, "Code d'équipe incorrect")
    return {"houses": [h["name"] for h in db.houses()]}


class Fiche(TeamCode):
    maison: str = ""
    equipement: str = ""
    code_erreur: str = ""
    panne: str = ""
    diagnostic: str = ""
    solution: str = ""
    par: str = ""


@app.post("/api/fiche")
def save_report(body: Fiche):
    """La fiche validée rejoint l'historique de la maison et met le tableau de la journée à jour."""
    if not _team_allowed(body.code):
        raise HTTPException(403, "Code d'équipe incorrect")
    found = db.find_houses(body.maison)
    if not found:
        raise HTTPException(400, "Choisis la maison avant de valider.")
    house = found[0]["name"]
    problem = " ; ".join(t for t in (body.panne.strip(), body.diagnostic.strip()) if t)[:300]
    with db.connect() as conn:
        db.add_intervention(conn, date.today().isoformat(), house, body.equipement.strip()[:80], body.code_erreur.strip()[:40],
                            problem, body.solution.strip()[:300], body.par.strip()[:80])
    if body.solution.strip():
        db.set_stay_status(house, "Réglé")
    return {"ok": True, "maison": house}


@app.get("/api/guest/{code}")
def guest_info(code: str):
    inv = db.invitation(code)
    if not inv:
        raise HTTPException(404, "Ce lien n'est plus valable")
    return {"name": inv["name"], "trade": inv["trade"], "reason": inv["reason"], "invited_by": inv["invited_by"],
            "role": inv["role"], "photo": f"/photos/{inv['photo']}" if inv["photo"] else None}


class GuestJoin(BaseModel):
    code: str


@app.post("/api/guest/join")
def guest_join(body: GuestJoin):
    inv = db.invitation(body.code)
    if not inv:
        raise HTTPException(404, "Ce lien n'est plus valable")
    # Même identité à chaque ouverture du lien : une reconnexion remplace l'ancienne session
    identity = f"invite-{body.code}"
    return {
        "token": _token(identity, inv["name"], inv["room"], inv["role"], with_agent=inv["role"] == "equipe", sujet=inv["reason"]),
        "url": config.LIVEKIT_URL,
        "room": inv["room"],
    }


@app.get("/photos/{name}")
def photo(name: str):
    path = PHOTOS / name
    if not PHOTO_RE.match(name) or not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/jpeg")


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/")
@app.get("/v/{code}")
def page(code: str | None = None):
    return HTMLResponse((WEB / "index.html").read_text("utf-8"), headers={"Cache-Control": "no-store"})
