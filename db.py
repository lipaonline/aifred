"""
Base SQLite d'AIfred, partagée par le serveur web et l'agent (même conteneur, même disque).
Une connexion par appel : les tables sont petites et les deux processus écrivent peu.

Données de démo : fictives (règle du hackathon). Elles viennent de AIFRED_DEMO_JSON
(variable d'environnement, pour y mettre les vrais numéros des testeurs sans les commiter)
ou, à défaut, de demo.example.json. Chargées une seule fois, quand la base est vide.
"""

import json
import os
import secrets
import sqlite3
import time
import unicodedata
from contextlib import contextmanager

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    trade TEXT NOT NULL,           -- plombier, électricien, propriétaire…
    role TEXT NOT NULL,            -- artisan | proprietaire | equipe
    phone TEXT NOT NULL,           -- format international, jamais lu à voix haute
    telegram TEXT NOT NULL DEFAULT ''  -- identifiant Telegram (chat_id) pour l'invitation, jamais lu
);
CREATE TABLE IF NOT EXISTS houses (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    address TEXT NOT NULL,
    owner_id INTEGER REFERENCES contacts(id),
    notes TEXT NOT NULL            -- la fiche pratique : vannes, équipements, seuil du mandat…
);
CREATE TABLE IF NOT EXISTS invitations (
    code TEXT PRIMARY KEY,         -- dans le lien /v/<code>
    room TEXT NOT NULL,
    contact_id INTEGER NOT NULL REFERENCES contacts(id),
    reason TEXT NOT NULL,
    photo TEXT,                    -- nom du fichier dans data/photos
    invited_by TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS stays (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,            -- arrivee | depart, toujours pour aujourd'hui dans la démo
    at TEXT NOT NULL,              -- 11:00
    house TEXT NOT NULL,           -- le nom dans houses
    guest TEXT NOT NULL,
    transport TEXT NOT NULL,
    flight TEXT,
    flight_status TEXT,            -- simulé : fixé dans les données de démo
    staff TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS interventions (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    house TEXT NOT NULL,
    equipment TEXT NOT NULL,
    code TEXT NOT NULL,
    problem TEXT NOT NULL,
    fix TEXT NOT NULL,
    by_name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge (
    id INTEGER PRIMARY KEY,
    topic TEXT NOT NULL,           -- procédure, tarif, question fréquente
    text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS log (
    id INTEGER PRIMARY KEY,
    at REAL NOT NULL,
    room TEXT NOT NULL,
    what TEXT NOT NULL
);
"""


@contextmanager
def connect():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DATA_DIR / "aifred.db", timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        # Base créée avant Telegram : on ajoute la colonne (les identifiants viennent du carnet de démo)
        if "telegram" not in {r["name"] for r in conn.execute("PRAGMA table_info(contacts)")}:
            conn.execute("ALTER TABLE contacts ADD COLUMN telegram TEXT NOT NULL DEFAULT ''")
        yield conn
        conn.commit()
    finally:
        conn.close()


def _plain(text: str) -> str:
    """Minuscules sans accents, pour comparer « électricien » et « electricien »."""
    text = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in text if unicodedata.category(c) != "Mn")


def seed():
    with connect() as db:
        if db.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]:
            return
        raw = os.environ.get("AIFRED_DEMO_JSON") or (config.ROOT / "demo.example.json").read_text("utf-8")
        demo = json.loads(raw)
        ids = {}
        for c in demo["contacts"]:
            cur = db.execute(
                "INSERT INTO contacts (name, trade, role, phone, telegram) VALUES (?, ?, ?, ?, ?)",
                (c["name"], c["trade"], c["role"], c.get("phone", ""), str(c.get("telegram", ""))),
            )
            ids[c["name"]] = cur.lastrowid
        for h in demo["houses"]:
            db.execute(
                "INSERT INTO houses (name, address, owner_id, notes) VALUES (?, ?, ?, ?)",
                (h["name"], h["address"], ids.get(h.get("owner", "")), h["notes"]),
            )
        for s in demo.get("stays", []):
            db.execute(
                "INSERT INTO stays (kind, at, house, guest, transport, flight, flight_status, staff, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (s["kind"], s["at"], s["house"], s["guest"], s["transport"], s.get("flight"),
                 s.get("flight_status"), s["staff"], s["status"]),
            )
        for i in demo.get("interventions", []):
            add_intervention(db, i["at"], i["house"], i["equipment"], i["code"], i["problem"], i["fix"], i["by"])
        for k in demo.get("knowledge", []):
            db.execute("INSERT INTO knowledge (topic, text) VALUES (?, ?)", (k["topic"], k["text"]))


def search_contacts(text: str) -> list[dict]:
    """Contacts dont le nom ou le métier contient un des mots cherchés."""
    words = [w for w in _plain(text).split() if len(w) > 2]
    with connect() as db:
        rows = [dict(r) for r in db.execute("SELECT * FROM contacts")]
    return [r for r in rows if any(w in _plain(f"{r['name']} {r['trade']}") for w in words)]


def contacts() -> list[dict]:
    with connect() as db:
        return [dict(r) for r in db.execute("SELECT * FROM contacts ORDER BY role = 'equipe' DESC, name")]


def contact(contact_id: int) -> dict | None:
    with connect() as db:
        row = db.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
    return dict(row) if row else None


def houses() -> list[dict]:
    with connect() as db:
        return [dict(r) for r in db.execute("SELECT * FROM houses")]


def find_houses(text: str) -> list[dict]:
    """Les maisons citées : un mot de leur nom (« Mélèzes »), ou leur numéro (« réf 415 »)."""
    heard = _plain(text)
    found = []
    for h in houses():
        name = _plain(h["name"])
        number = "".join(c for c in name if c.isdigit())
        if (number and number in heard) or any(w in heard for w in name.split() if len(w) > 3):
            found.append(h)
    return found


def stays() -> list[dict]:
    """Le tableau de la journée : les arrivées et les départs, avec l'adresse de chaque maison."""
    with connect() as db:
        rows = db.execute(
            "SELECT s.*, h.address FROM stays s LEFT JOIN houses h ON h.name = s.house ORDER BY s.kind, s.at"
        ).fetchall()
    return [dict(r) for r in rows]


def set_stay_status(house: str, status: str) -> bool:
    with connect() as db:
        return db.execute("UPDATE stays SET status = ? WHERE house = ?", (status, house)).rowcount > 0


def add_intervention(db, at: str, house: str, equipment: str, code: str, problem: str, fix: str, by: str):
    db.execute(
        "INSERT INTO interventions (at, house, equipment, code, problem, fix, by_name) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (at, house, equipment, code, problem, fix, by),
    )


def interventions() -> list[dict]:
    with connect() as db:
        return [dict(r) for r in db.execute("SELECT * FROM interventions ORDER BY at DESC")]


def search_knowledge(text: str) -> list[dict]:
    """Procédures, tarifs et questions fréquentes dont le sujet contient un mot de la question."""
    words = [w for w in _plain(text).split() if len(w) > 3]
    with connect() as db:
        rows = [dict(r) for r in db.execute("SELECT * FROM knowledge")]
    return [r for r in rows if any(w in _plain(r["topic"]) for w in words)]


def create_invitation(room: str, contact_id: int, reason: str, photo: str | None, invited_by: str) -> str:
    code = secrets.token_urlsafe(9)
    now = time.time()
    with connect() as db:
        db.execute(
            "INSERT INTO invitations VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (code, room, contact_id, reason, photo, invited_by, now, now + config.INVITATION_HOURS * 3600),
        )
    return code


def invitation(code: str) -> dict | None:
    """L'invitation et son contact, si le lien est encore valable."""
    with connect() as db:
        row = db.execute(
            "SELECT i.*, c.name, c.trade, c.role FROM invitations i "
            "JOIN contacts c ON c.id = i.contact_id WHERE i.code = ? AND i.expires_at > ?",
            (code, time.time()),
        ).fetchone()
    return dict(row) if row else None


def log(room: str, what: str):
    with connect() as db:
        db.execute("INSERT INTO log (at, room, what) VALUES (?, ?, ?)", (time.time(), room, what))
