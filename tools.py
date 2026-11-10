"""
Les outils d'AIfred : ce qu'il peut faire quand on lui parle. Le prompt est dans prompt.md.

Les 3 fonctionnalités :
1. Préparer la journée : ma_journee, preparer_rapport.
2. Le carnet des maisons : chercher_dans_la_maison, chercher_interventions, verifier_vol,
   changer_statut, noter_intervention, declarer_incident.
3. Faire venir en visio : preparer_invitation.

regarder est un sens, comme la voix. confirmer_envoi et annuler_envoi servent aux envois des
fonctionnalités 1 et 3.

Règle du hackathon : toute action qu'on ne peut pas annuler passe par une confirmation humaine.
Ici, preparer_rapport et preparer_invitation n'envoient rien. L'envoi part sur un « oui » de la personne
qui a activé AIfred, vérifié dans le code, ou quand cette personne touche Envoyer sur la carte.
"""

import asyncio
import base64
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import date

import anthropic
from livekit import rtc
from livekit.agents import RunContext, function_tool, llm

import config
import db
import mailer
import whatsapp

PHOTOS = config.DATA_DIR / "photos"
YES_RE = re.compile(r"\b(oui|ouais|vas-y|vas y|ok|okay|d'accord|daccord|envoie|envoie-le|confirme|go)\b")
NO_RE = re.compile(r"\b(non|attends|attend|attendez|annule\w*|stop|arrete\w*|surtout pas|plus tard|laisse tomber)\b")
# Une négation retourne le oui : « n'envoie pas », « pas d'accord », « ce n'est pas ok », « jamais »
NEGATION_RE = re.compile(r"\b(n'|ne |pas\b|jamais\b)")
# Des tournures qui contiennent « pas » mais restent un oui
NOT_A_NEGATION_RE = re.compile(r"\bpas de (souci|soucis|probleme|problemes)\b")
# Ce qui demande à AIfred de regarder : sans cela, ou sans « oui » à sa question, il ne prend pas de photo
LOOK_RE = re.compile(r"\b(regard\w*|vois|voit|voir|voyez|lis|lire|lit|photo\w*|camera|image|ecran|montre\w*)\b")
# Celui qui montre lui-même : c'est sa caméra qu'il faut regarder
MY_CAMERA_RE = re.compile(r"\b(je (te |vous )?montre|ma camera|mon telephone)\b")
# Le métier qui répare, d'après les mots du problème : sert à proposer un artisan, jamais à le contacter
TRADES = [
    ("plombier", re.compile(r"\b(fuite\w*|eau|robinet\w*|mitigeur\w*|douche\w*|wc|toilettes?|evier|lavabo|canalisation\w*|bouche\w*|chauffe-eau|ballon)\b")),
    ("chauffagiste", re.compile(r"\b(chauffage|chauffe pas|radiateur\w*|chaudiere|pompe a chaleur|pac|clim\w*)\b")),
    ("électricien", re.compile(r"\b(electri\w*|prise\w*|disjonct\w*|lumiere\w*|ampoule\w*|courant|plomb\w*|tableau)\b")),
    ("serrurier", re.compile(r"\b(serrure\w*|cle|cles|verrou\w*|porte bloquee|boite a cles|cylindre)\b")),
]
URGENCIES = ["basse", "normale", "haute"]
PENDING_SECONDS = 120
PHOTO_TOPIC = "aifred-photo"


def new_visio_name() -> str:
    return f"{config.VISIO_PREFIX}{secrets.token_hex(3)}"


@dataclass
class Visio:
    """Ce que les outils savent de la visio en cours (userdata de la session)."""
    room: rtc.Room
    cameras: "object"                       # agent.Cameras
    pending: dict | None = None             # invitation qui attend le « oui »
    last_photo: str | None = None           # fichier dans data/photos
    board_shown: bool = False               # le tableau de la journée est affiché
    noted: bool = False                     # l'intervention est déjà dans l'historique
    sujet: str = ""                         # le sujet de la visio, donné au lancement
    opened_by: str = ""                     # le prénom de celui qui a ouvert la salle : il survit à un rechargement de page
    guest_room: str | None = None           # sur le canal : la visio préparée pour les invités
    anthropic_client: anthropic.AsyncAnthropic = field(default_factory=anthropic.AsyncAnthropic)

    @property
    def is_canal(self) -> bool:
        return self.room.name.startswith(config.CANAL_PREFIX)

    def visio_room(self) -> str:
        """La salle où inviter : celle-ci en visio. Sur le canal, une visio qu'on ouvrira ensuite."""
        if not self.is_canal:
            return self.room.name
        self.guest_room = self.guest_room or new_visio_name()
        return self.guest_room

    def team(self) -> list[rtc.RemoteParticipant]:
        return [p for p in self.room.remote_participants.values() if p.attributes.get("role") == "equipe"]

    async def card(self, kind: str, **data):
        """Affiche une carte sur les téléphones de l'équipe (pas des invités : liens et numéros restent internes)."""
        team = [p.identity for p in self.team()]
        if team:
            payload = json.dumps({"type": kind, **data}, ensure_ascii=False)
            await self.room.local_participant.publish_data(payload, topic="aifred", destination_identities=team)

    async def signal(self, kind: str, **data):
        """Un signal pour tout le monde dans la salle, invités compris : « AIfred regarde »."""
        payload = json.dumps({"type": kind, **data}, ensure_ascii=False)
        await self.room.local_participant.publish_data(payload, topic="aifred")

    async def send_board(self):
        """Le tableau de la journée, à jour : il reste à l'écran et change avec les événements."""
        if self.board_shown:
            await self.card("journee", rows=db.stays())


def _last_user_turn(context: RunContext[Visio]) -> llm.ChatMessage | None:
    for item in reversed(context.session.history.items):
        if getattr(item, "role", None) == "user":
            return item
    return None


def _speaker(context: RunContext[Visio]) -> rtc.RemoteParticipant | None:
    """AIfred n'entend que la personne qui l'a activé."""
    return context.session.room_io.linked_participant


def _team_asker(context: RunContext[Visio]) -> rtc.RemoteParticipant | None:
    """Qui valide l'envoi demandé : la personne de l'équipe qui l'a demandé. Demandé par un invité, n'importe qui de l'équipe."""
    speaker = _speaker(context)
    return speaker if speaker and speaker.attributes.get("role") == "equipe" else None


def _last_user_text(context: RunContext[Visio]) -> str:
    turn = _last_user_turn(context)
    # Sans le « Prénom (rôle) : » du début : seuls ses mots comptent pour le « oui »
    return (turn.text_content or "").split(" : ", 1)[-1] if turn else ""


def _last_assistant_text(context: RunContext[Visio]) -> str:
    for item in reversed(context.session.history.items):
        if getattr(item, "role", None) == "assistant" and item.text_content:
            return item.text_content
    return ""


def is_clear_yes(text: str) -> bool:
    """Un oui clair : un mot d'accord, sans refus ni négation. Dans le doute, ce n'est pas un oui.
    « N'envoie pas », « pas d'accord » ou « oui mais attends » ne font rien partir."""
    heard = db._plain(text).replace("’", "'")
    heard = NOT_A_NEGATION_RE.sub(" ", heard)
    return bool(YES_RE.search(heard)) and not NO_RE.search(heard) and not NEGATION_RE.search(heard)


def _asked_to_look(context: RunContext[Visio]) -> bool:
    """On lui a demandé de regarder, ou on vient de répondre oui à sa question."""
    heard = db._plain(_last_user_text(context))
    if LOOK_RE.search(heard):
        return True
    return is_clear_yes(heard) and bool(LOOK_RE.search(db._plain(_last_assistant_text(context))))


def _has_camera(p: rtc.RemoteParticipant) -> bool:
    return any(t.source == rtc.TrackSource.SOURCE_CAMERA and not t.muted for t in p.track_publications.values())


async def _is_rear(v: Visio, p: rtc.RemoteParticipant) -> bool:
    """Seule la page sait quelle caméra est active. Un ordinateur ne répond jamais « arrière »."""
    try:
        return await v.room.local_participant.perform_rpc(
            destination_identity=p.identity, method="camera", payload="", response_timeout=2) == "environment"
    except (rtc.RpcError, TimeoutError):
        return False


async def _on_site(context: RunContext[Visio], name: str = "") -> rtc.RemoteParticipant | None:
    """La personne à regarder : celle qu'on nomme, celle qui dit montrer, sinon celle qui filme la panne.
    Celle qui filme : la caméra arrière d'abord. Puis pas celle qui a ouvert la visio, souvent le gérant à
    distance. Puis, dans l'équipe avant les invités, celle qui demande."""
    v = context.userdata
    heard = db._plain(_last_user_text(context))
    wanted = db._plain(name)
    # Un prénom compte s'il a été dit : Claude ne choisit pas la caméra à la place de l'équipe
    if wanted and wanted in heard:
        for p in v.room.remote_participants.values():
            if wanted in db._plain(p.name or ""):
                return p
    speaker = _speaker(context)
    if speaker and MY_CAMERA_RE.search(heard):
        return speaker
    filming = [p for p in v.room.remote_participants.values() if _has_camera(p)]
    if len(filming) < 2:
        return filming[0] if filming else None
    rear = await asyncio.gather(*(_is_rear(v, p) for p in filming))
    pool = [p for p, is_rear in zip(filming, rear) if is_rear] or filming
    pool = [p for p in pool if p.name != v.opened_by] or pool
    team = [p for p in pool if p.attributes.get("role") == "equipe"]
    return speaker if speaker in team else (team or pool)[-1]


async def shoot(v: Visio, person: rtc.RemoteParticipant) -> tuple[bytes, str] | None:
    """Demande une image au téléphone de la personne, la garde et l'affiche à l'équipe."""
    await v.signal("regarde", who=person.identity)
    jpeg = await v.cameras.capture(person.identity)
    if not jpeg:
        return None
    PHOTOS.mkdir(parents=True, exist_ok=True)
    name = f"{secrets.token_hex(12)}.jpg"
    (PHOTOS / name).write_bytes(jpeg)
    v.last_photo = name
    await v.card("photo", url=f"/photos/{name}", who=person.name)
    return jpeg, name


async def _take_photo(context: RunContext[Visio], name: str = "") -> tuple[bytes, str] | None:
    on_site = await _on_site(context, name)
    return await shoot(context.userdata, on_site) if on_site else None


async def _describe(v: Visio, jpeg: bytes, question: str) -> str:
    response = await v.anthropic_client.messages.create(
        model=config.LLM_MODEL,
        # Haiku réfléchit avant de répondre, et cette réflexion compte dans max_tokens
        max_tokens=1024,
        system=(
            "Tu regardes une photo prise par un employé de conciergerie pendant une intervention dans une "
            "maison. Réponds en français, en deux phrases au plus, comme à l'oral, sans liste. Dis ce que "
            "tu vois avec certitude et dis-le quand tu ne peux pas voir quelque chose."
        ),
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                             "data": base64.b64encode(jpeg).decode()}},
                {"type": "text", "text": question},
            ],
        }],
    )
    return "".join(b.text for b in response.content if b.type == "text") or "Je n'arrive pas à lire l'image."


async def what_i_see(v: Visio, jpeg: bytes) -> str:
    """Le bouton Photo : personne n'a posé de question, AIfred dit ce qu'on lui montre."""
    try:
        return await _describe(v, jpeg, "En une seule phrase courte qui commence par « Je vois » : l'appareil, ce qu'il affiche (message d'erreur, valeur d'un cadran ou d'un manomètre) et la marque s'ils sont lisibles. Si rien n'est lisible, demande de mieux cadrer.")
    except anthropic.APIError as e:
        db.log(v.room.name, f"Photo non lue : {e}")
        return "Je n'arrive pas à lire l'image."


@function_tool
async def regarder(context: RunContext[Visio], question: str, personne: str = "") -> str:
    """Prend une photo avec la caméra de la personne sur place et répond à une question sur ce qu'on voit.
    Seulement quand on te demande de regarder (« regarde ce message d'erreur », « tu vois la marque ? »).
    Parler d'une panne n'est pas une demande de regarder. Garde aussi la photo, pour l'envoyer si on le demande.

    Args:
        question: ce qu'il faut regarder sur l'image, en français.
        personne: le prénom de la personne à regarder, seulement si on la nomme dans la phrase. Sinon, laisse vide.
    """
    if context.userdata.is_canal:
        return "Sur le canal, il n'y a pas de caméra. Dis qu'il faut passer en visio pour que tu puisses voir."
    if not _asked_to_look(context):
        return "Aucune photo n'est prise : personne n'a demandé de regarder. Demande d'abord : « Tu veux que je regarde ? Montre-moi l'endroit. »"
    shot = await _take_photo(context, personne)
    if not shot:
        return "Aucune caméra n'est allumée. Demande à la personne sur place d'allumer sa caméra."
    jpeg, _name = shot
    return await _describe(context.userdata, jpeg, question)


@function_tool
async def chercher_dans_la_maison(context: RunContext[Visio], question: str) -> str:
    """Cherche dans la fiche pratique des maisons gérées : vanne d'arrêt, tableau électrique, marque et
    référence des équipements, seuil du mandat, voyageurs du moment. Affiche la fiche sur les téléphones
    de l'équipe.

    Args:
        question: ce qu'on cherche, avec le nom de la maison si on le connaît.
    """
    # Les fiches entières : Claude y trouve la réponse
    match = db.find_houses(question)
    known = db.search_knowledge(question)
    for h in match:
        await context.userdata.card("fiche", title=h["name"], text=h["notes"])
    for k in known:
        await context.userdata.card("fiche", title=k["topic"], text=k["text"])
    # Un objet, jamais une liste : le plugin Anthropic prend une liste JSON pour des blocs de contenu (erreur 400)
    return json.dumps({"maisons": [{"maison": h["name"], "adresse": h["address"], "fiche": h["notes"]}
                                   for h in (match or ([] if known else db.houses()))],
                       "procedures_tarifs_questions": [{"sujet": k["topic"], "texte": k["text"]} for k in known]},
                      ensure_ascii=False)


def _intervention(r: dict) -> dict:
    return {"date": r["at"], "maison": r["house"], "equipement": r["equipment"], "code": r["code"],
            "probleme": r["problem"], "solution": r["fix"], "par": r["by_name"]}


@function_tool
async def chercher_interventions(context: RunContext[Visio], maison: str, equipement: str = "", code: str = "") -> str:
    """Cherche dans l'historique : les interventions passées sur cette maison, et celles des autres maisons
    qui ont le même équipement. Affiche le résultat à l'équipe.

    Args:
        maison: le nom ou la référence de la maison (« REF415 »).
        equipement: l'équipement concerné (« pompe à chaleur », « chaudière »), si on le connaît.
        code: le code d'erreur, s'il y en a un.
    """
    wanted = db.find_houses(maison)
    names = {h["name"] for h in wanted}
    words = [w for w in db._plain(equipement).split() if len(w) > 3]
    rows = db.interventions()
    here = [r for r in rows if r["house"] in names]
    # Même équipement : celui qu'on cite, ou celui qui figure sur la fiche de la maison
    notes = " ".join(db._plain(h["notes"]) for h in wanted)
    elsewhere = [r for r in rows if r["house"] not in names
                 and (db._plain(r["equipment"]) in notes or any(w in db._plain(r["equipment"]) for w in words))]
    same_code = [r for r in elsewhere if code and db._plain(r["code"]) == db._plain(code)]
    await context.userdata.card("historique", title=maison, here=here, elsewhere=elsewhere)
    return json.dumps({"ici": [_intervention(r) for r in here],
                       "meme_equipement_ailleurs": [_intervention(r) for r in elsewhere],
                       "meme_code_ailleurs": [_intervention(r) for r in same_code],
                       "fiche": [h["notes"] for h in wanted]}, ensure_ascii=False)


@function_tool
async def noter_intervention(context: RunContext[Visio], maison: str, equipement: str, probleme: str, solution: str,
                             par: str, code: str = "") -> str:
    """Note une intervention dans l'historique de la maison, quand le problème est réglé, et met le
    tableau de la journée à jour.

    Args:
        maison: le nom ou la référence de la maison.
        equipement: l'équipement concerné.
        probleme: le problème, en quelques mots.
        solution: ce qui a été fait, en quelques mots.
        par: qui a fait quoi (« Sébastien, guidé par Patrice »).
        code: le code d'erreur, s'il y en a un.
    """
    wanted = db.find_houses(maison)
    if not wanted:
        return f"Je ne connais pas la maison « {maison} »."
    house = wanted[0]["name"]
    with db.connect() as conn:
        db.add_intervention(conn, date.today().isoformat(), house, equipement, code, probleme, solution, par)
    db.set_stay_status(house, "Réglé")
    v = context.userdata
    v.board_shown = True
    v.noted = True
    await v.send_board()
    await v.card("note", title=house, text=f"{probleme} : {solution} ({par})")
    return "C'est noté dans l'historique et le tableau est à jour."


@function_tool
async def changer_statut(context: RunContext[Visio], maison: str, statut: str) -> str:
    """Change l'état d'une maison dans le tableau de la journée, dès qu'il se passe quelque chose :
    « Panne de chauffage », « Patrice en visio », « Prêt ».

    Args:
        maison: le nom ou la référence de la maison.
        statut: le nouvel état, en deux ou trois mots.
    """
    wanted = db.find_houses(maison)
    if not wanted or not db.set_stay_status(wanted[0]["name"], statut.strip()[:40]):
        return f"La maison « {maison} » n'est pas dans le tableau du jour."
    v = context.userdata
    v.board_shown = True
    await v.send_board()
    return "Le tableau est à jour."


def _artisan_for(house: str, problem: str) -> dict | None:
    """L'artisan du métier qui répare. D'abord celui qui est déjà intervenu dans cette maison."""
    heard = db._plain(problem)
    trade = next((t for t, words in TRADES if words.search(heard)), None)
    if not trade:
        return None
    pool = [c for c in db.contacts() if c["role"] == "artisan" and db._plain(trade) in db._plain(c["trade"])]
    came = {db._plain(r["by_name"]) for r in db.interventions() if r["house"] == house}
    return next((c for c in pool if any(db._plain(c["name"]).split()[0] in by for by in came)), None) or (pool[0] if pool else None)


def _urgency(said: str, next_arrival: str | None) -> str:
    """L'urgence dite par l'équipe, relevée à « haute » si des voyageurs arrivent aujourd'hui."""
    level = db._plain(said).strip()
    level = level if level in URGENCIES else "normale"
    if next_arrival and next_arrival.startswith("aujourd'hui"):
        return "haute"
    return level


@function_tool
async def declarer_incident(context: RunContext[Visio], maison: str, probleme: str, urgence: str = "") -> str:
    """Crée la fiche d'un incident déclaré à la voix par l'équipe (« fuite sous l'évier à REF415 ») :
    maison, problème, urgence, prochaine arrivée, et l'artisan à proposer. Ne contacte personne.

    Args:
        maison: le nom ou la référence de la maison.
        probleme: le problème, en quelques mots, tel qu'il a été dit.
        urgence: « basse », « normale » ou « haute » si on te l'a dite. Sinon, laisse vide.
    """
    asker = _team_asker(context)
    if not asker:
        return "Seul un membre de l'équipe peut déclarer un incident. Dis-le simplement."
    probleme = probleme.strip()[:200]
    if not probleme:
        return "Il manque le problème. Demande en une phrase ce qui ne va pas."
    wanted = db.find_houses(maison)
    if not wanted:
        return f"Je ne connais pas la maison « {maison} ». Demande de quelle maison il s'agit."
    if len(wanted) > 1:
        return f"Plusieurs maisons correspondent : {', '.join(h['name'] for h in wanted)}. Demande laquelle."
    house = wanted[0]["name"]
    arrival = db.next_arrival(house)
    level = _urgency(urgence, arrival)
    artisan = _artisan_for(house, probleme)
    v = context.userdata
    db.add_incident(house, probleme, level, arrival, artisan["name"] if artisan else None, asker.name or "L'équipe")
    db.log(v.room.name, f"Incident à {house} : {probleme} (urgence {level}, déclaré par {asker.name})")
    if db.set_stay_status(house, "Incident signalé"):
        await v.send_board()
    await v.card("incident", title=house, problem=probleme, urgency=level, arrival=arrival,
                 artisan=f"{artisan['name']}, {artisan['trade']}" if artisan else None)
    a_dire = f"Fiche créée pour {house}, urgence {level}"
    a_dire += f", arrivée {arrival}." if arrival else "."
    # Pas de numéro : AIfred ne le lit jamais, et l'invitation passe par preparer_invitation puis le oui
    return json.dumps({"a_dire": a_dire,
                       "fiche": {"maison": house, "probleme": probleme, "urgence": level,
                                 "prochaine_arrivee": arrival or "inconnue", "declare_par": asker.name,
                                 "statut": "ouvert"},
                       "artisan_a_proposer": {"nom": artisan["name"], "metier": artisan["trade"]} if artisan else None,
                       "ensuite": ("Dis a_dire, puis propose de faire venir l'artisan. Rien n'est envoyé sans "
                                   "preparer_invitation et un oui clair.") if artisan else "Dis a_dire."},
                      ensure_ascii=False)


@function_tool
async def verifier_vol(context: RunContext[Visio], recherche: str) -> str:
    """Donne l'état du vol d'un voyageur du jour et l'affiche à l'équipe.

    Args:
        recherche: le numéro du vol ou le nom du voyageur.
    """
    wanted = db._plain(recherche).replace(" ", "")
    for s in db.stays():
        if s.get("flight") and (db._plain(s["flight"]) in wanted or wanted in db._plain(s["guest"]).replace(" ", "")):
            await context.userdata.card("vol", flight=s["flight"], guest=s["guest"], status=s["flight_status"])
            return json.dumps({"vol": s["flight"], "voyageur": s["guest"], "etat": s["flight_status"]}, ensure_ascii=False)
    return f"Aucun vol du jour pour « {recherche} »."


def _summary(stays: list[dict]) -> str:
    """Le résumé à dire : court, calculé ici pour qu'il tienne en une phrase."""
    def plural(n: int, word: str) -> str:
        return f"{n} {word}{'s' if n > 1 else ''}"
    arrivals = [s for s in stays if s["kind"] == "arrivee"]
    text = f"{plural(len(arrivals), 'arrivée')} et {plural(len(stays) - len(arrivals), 'départ')}."
    watch = next((s for s in arrivals if s["status"] not in ("Prêt", "Réglé")), None)
    if watch:
        text += f" À surveiller : {watch['guest']} à {int(watch['at'][:2])} heures en {watch['house']}, {watch['status'].lower()}."
    return text


@function_tool
async def ma_journee(context: RunContext[Visio]) -> str:
    """Affiche le tableau de la journée (arrivées, départs, transport, qui s'en occupe, état) et renvoie ce
    qu'il contient, pour le résumer à voix haute."""
    v = context.userdata
    v.board_shown = True
    await v.send_board()
    stays = db.stays()
    return json.dumps({"a_dire": _summary(stays),
                       "journee": [{"type": s["kind"], "heure": s["at"], "maison": s["house"], "voyageur": s["guest"],
                                    "transport": s["transport"], "vol": s.get("flight"), "responsable": s["staff"],
                                    "etat": s["status"]} for s in stays]}, ensure_ascii=False)


@function_tool
async def preparer_rapport(context: RunContext[Visio]) -> str:
    """Prépare l'envoi par email du rapport de la journée. N'envoie rien : il faut ensuite demander une
    confirmation à voix haute."""
    v = context.userdata
    asker = _team_asker(context)
    v.pending = {"kind": "rapport", "at": time.time(), "by": asker.identity if asker else ""}
    await v.card("mail_pending", by=asker.identity if asker else "", by_name=asker.name if asker else "")
    return "Rien n'est envoyé. Demande maintenant : « Je t'envoie le rapport par email ? » Puis attends un oui clair avant confirmer_envoi."


@function_tool
async def preparer_invitation(context: RunContext[Visio], qui: str, motif: str, avec_photo: bool = False) -> str:
    """Trouve la personne et prépare l'envoi par WhatsApp du lien de la visio : quelqu'un de l'équipe, un
    artisan ou un propriétaire. Sur le canal, prépare une visio à ouvrir ensuite. N'envoie rien : il faut
    ensuite demander une confirmation à voix haute.

    Args:
        qui: le nom ou le métier (« Sébastien », « chauffagiste », « Whitmore »).
        motif: la raison en quelques mots (« fuite du mitigeur de la douche, suite parentale »).
        avec_photo: vrai seulement si on demande d'envoyer la photo.
    """
    v = context.userdata
    found = db.search_contacts(qui)
    if not found:
        return f"Aucun contact pour « {qui} ». Dis-le simplement."
    if len(found) > 1:
        # Pas de numéro de téléphone : AIfred ne le lit jamais à voix haute
        names = ", ".join(f"{c['name']} ({c['trade']})" for c in found)
        return f"Plusieurs personnes correspondent : {names}. Demande laquelle."
    contact = found[0]
    # La photo seulement si on la demande, même si Claude la propose de lui-même
    avec_photo = avec_photo and "photo" in db._plain(_last_user_text(context))
    if avec_photo and not v.last_photo:
        await _take_photo(context)
    photo = v.last_photo if avec_photo else None
    asker = _team_asker(context)
    v.pending = {"kind": "invitation", "contact": contact, "reason": motif.strip()[:120], "photo": photo, "at": time.time(),
                 "by": asker.identity if asker else ""}
    await v.card("pending", name=contact["name"], trade=contact["trade"], reason=v.pending["reason"],
                 photo=f"/photos/{photo}" if photo else None,
                 by=asker.identity if asker else "", by_name=asker.name if asker else "")
    with_photo = " avec la photo" if photo else ""
    return (f"Rien n'est envoyé. Demande maintenant : « J'envoie à {contact['name']}, {contact['trade']}, "
            f"le lien de la visio{with_photo} ? » Puis attends un oui clair avant confirmer_envoi.")


@function_tool
async def confirmer_envoi(context: RunContext[Visio]) -> str:
    """Envoie ce qui a été préparé (l'invitation ou le rapport). Seulement juste après un « oui » clair d'un
    membre de l'équipe."""
    v = context.userdata
    speaker = _team_asker(context)
    if not speaker:
        return "Seul un membre de l'équipe peut confirmer un envoi. Dis-le simplement."
    # Comme le bouton Envoyer (agent.py, valider) : seule la personne qui a demandé l'envoi le confirme
    pending = v.pending
    if pending and pending.get("by") not in ("", speaker.identity):
        return "Seule la personne qui a demandé l'envoi peut le confirmer. Demande-lui son accord."
    if not is_clear_yes(_last_user_text(context)):
        return "Je n'ai pas entendu de oui clair. Repose la question et attends la réponse."
    return await send_pending(v, speaker.name or "L'équipe")


async def send_pending(v: Visio, invited_by: str) -> str:
    """Envoie ce qui attend : après le « oui » à voix haute, ou quand on touche Envoyer sur la carte."""
    pending = v.pending
    v.pending = None
    if not pending or time.time() - pending["at"] > PENDING_SECONDS:
        return "Il n'y a rien à envoyer, ou la demande est trop ancienne."
    if pending["kind"] == "rapport":
        return await _send_report(v)
    contact, reason, photo = pending["contact"], pending["reason"], pending["photo"]
    # Depuis le canal, la visio reste à ouvrir : la carte le propose
    result = await _invite(v, contact, reason, photo, invited_by, v.visio_room(), offer_room=v.is_canal)
    if not result["ok"]:
        return "L'envoi WhatsApp a échoué, on peut réessayer."
    if result["dry_run"]:
        return "Mode essai : WhatsApp n'est pas branché, le lien est affiché à l'écran."
    return f"C'est envoyé à {contact['name']}."


async def _invite(v: Visio, contact: dict, reason: str, photo: str | None, invited_by: str, room: str,
                  offer_room: bool = False) -> dict:
    code = db.create_invitation(room, contact["id"], reason, photo, invited_by)
    link = f"{config.PUBLIC_URL}/v/{code}"
    image_url = f"{config.PUBLIC_URL}/photos/{photo}" if photo else None
    first_name = contact["name"].split()[0]
    result = await whatsapp.send_invitation(contact["phone"], [first_name, invited_by, reason, link], image_url)
    if not result["ok"]:
        db.log(v.room.name, f"Échec de l'invitation à {contact['name']} : {result.get('error')}")
        await v.card("error", text=f"WhatsApp n'est pas parti vers {contact['name']} : {str(result.get('error'))[:150]}")
        return result
    trial = " (mode essai, rien n'est parti)" if result["dry_run"] else ""
    db.log(v.room.name, f"Invitation WhatsApp à {contact['name']} ({contact['trade']}) : {reason}{trial}")
    await v.card("sent", name=contact["name"], trade=contact["trade"], link=link, dry_run=result["dry_run"],
                 room=room if offer_room else None, reason=reason)
    return result


async def invite_many(v: Visio, contact_ids: list[int], reason: str, invited_by: str, room: str):
    """Les personnes cochées quand on lance une visio : toucher « Lancer la visio » vaut confirmation.
    Celles de l'équipe déjà sur le canal reçoivent une sonnerie, les autres un WhatsApp."""
    for contact_id in contact_ids:
        contact = db.contact(contact_id)
        if not contact:
            continue
        first_name = db._plain(contact["name"]).split()[0]
        here = next((p for p in v.team() if db._plain(p.name or " ").split()[0] == first_name), None)
        if here:
            payload = json.dumps({"type": "sonnerie", "room": room, "by_name": invited_by, "reason": reason}, ensure_ascii=False)
            await v.room.local_participant.publish_data(payload, topic="aifred", destination_identities=[here.identity])
        else:
            await _invite(v, contact, reason, None, invited_by, room)


async def _send_report(v: Visio) -> str:
    result = await mailer.send_report(db.stays())
    if not result["ok"]:
        db.log(v.room.name, f"Échec du rapport par email : {result.get('error')}")
        await v.card("error", text=f"L'email n'est pas parti : {str(result.get('error'))[:150]}")
        return "L'email a échoué, on peut réessayer."
    await v.card("mail_sent", dry_run=result["dry_run"])
    if result["dry_run"]:
        return "Mode essai : l'email n'est pas branché, rien n'est parti."
    db.log(v.room.name, "Rapport de la journée envoyé par email")
    return "C'est envoyé."


@function_tool
async def annuler_envoi(context: RunContext[Visio]) -> str:
    """Annule l'envoi préparé, quand l'équipe dit non ou change d'avis."""
    return await cancel_pending(context.userdata)


async def cancel_pending(v: Visio) -> str:
    v.pending = None
    await v.card("cancelled")
    return "Annulé, rien n'est parti."


FICHE_SYSTEM = """Tu remplis la fiche d'intervention d'une conciergerie à partir de ce qui a été dit pendant une visio.
Tu n'inventes rien : un champ que l'échange ne permet pas de remplir reste vide. « solution » est ce qui a réglé la panne, pas ce qui est prévu. « par » contient les prénoms des personnes qui ont fait l'intervention, jamais « équipe » ni AIfred : vide si l'échange ne le dit pas.
Réponds uniquement par un objet JSON avec ces champs, en français et en quelques mots chacun :
{"maison": "", "equipement": "", "code": "", "panne": "", "diagnostic": "", "solution": "", "par": ""}"""


async def write_report(v: Visio, history: llm.ChatContext) -> dict:
    """La fiche d'intervention proposée à la fin d'une visio. Rien n'est enregistré avant la validation."""
    if v.noted:
        return {"deja_notee": True}
    lines = [f"Sujet de la visio : {v.sujet}"] if v.sujet else []
    heard = False
    for item in history.items:
        role = getattr(item, "role", None)
        if role == "user" and item.text_content:
            # Le nom et le rôle sont déjà au début de la phrase
            lines.append(item.text_content)
            heard = True
        elif role == "assistant" and item.text_content:
            lines.append(f"AIfred : {item.text_content}")
    if not heard:
        return {"vide": True}
    response = await v.anthropic_client.messages.create(
        model=config.LLM_MODEL,
        max_tokens=1024,
        system=FICHE_SYSTEM + "\nMaisons gérées : " + ", ".join(h["name"] for h in db.houses()),
        messages=[{"role": "user", "content": "\n".join(lines)}],
    )
    text = "".join(b.text for b in response.content if b.type == "text")
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return {"vide": True}
    try:
        fiche = json.loads(match.group(0))
    except ValueError:
        return {"vide": True}
    return {**fiche, "photo": f"/photos/{v.last_photo}" if v.last_photo else None}


ALL = [regarder, ma_journee, preparer_rapport, chercher_dans_la_maison, chercher_interventions, verifier_vol,
       changer_statut, noter_intervention, declarer_incident, preparer_invitation, confirmer_envoi, annuler_envoi]
