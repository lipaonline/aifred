"""
Les outils d'AIfred : ce qu'il peut faire quand on lui parle. Le prompt est dans prompt.md.

Les 3 fonctionnalités :
1. Préparer la journée : ma_journee, preparer_rapport.
2. Le carnet des maisons : chercher_dans_la_maison, chercher_interventions, verifier_vol,
   changer_statut, noter_intervention.
3. Faire venir en visio : preparer_invitation.

regarder est un sens, comme la voix. confirmer_envoi et annuler_envoi servent aux envois des
fonctionnalités 1 et 3.

Règle du hackathon : toute action qu'on ne peut pas annuler passe par une confirmation humaine.
Ici, preparer_rapport et preparer_invitation n'envoient rien ; confirmer_envoi vérifie dans le code
que la dernière phrase entendue est un « oui » et qu'elle vient d'un membre de l'équipe.
"""

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
NO_RE = re.compile(r"\b(non|attends|attend|annule|stop)\b")
PENDING_SECONDS = 120


@dataclass
class Visio:
    """Ce que les outils savent de la visio en cours (userdata de la session)."""
    room: rtc.Room
    cameras: "object"                       # agent.Cameras
    pending: dict | None = None             # invitation qui attend le « oui »
    last_photo: str | None = None           # fichier dans data/photos
    board_shown: bool = False               # le tableau de la journée est affiché
    anthropic_client: anthropic.AsyncAnthropic = field(default_factory=anthropic.AsyncAnthropic)

    def team(self) -> list[rtc.RemoteParticipant]:
        return [p for p in self.room.remote_participants.values() if p.attributes.get("role") == "equipe"]

    async def card(self, kind: str, **data):
        """Affiche une carte sur les téléphones de l'équipe (pas des invités : liens et numéros restent internes)."""
        team = [p.identity for p in self.team()]
        if team:
            payload = json.dumps({"type": kind, **data}, ensure_ascii=False)
            await self.room.local_participant.publish_data(payload, topic="aifred", destination_identities=team)

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
    """L'auteur de la dernière phrase : son identité est sur le message s'il vient d'une session d'écoute,
    sinon c'est le participant lié à la session principale."""
    turn = _last_user_turn(context)
    identity = turn.extra.get("identity") if turn else None
    if identity:
        return context.userdata.room.remote_participants.get(identity)
    return context.session.room_io.linked_participant


def _last_user_text(context: RunContext[Visio]) -> str:
    turn = _last_user_turn(context)
    return (turn.text_content or "") if turn else ""


def _on_site(context: RunContext[Visio], name: str = "") -> rtc.RemoteParticipant | None:
    """La personne à regarder : celle qu'on nomme, sinon le dernier de l'équipe entré dans la visio.
    Le gérant, à distance, ouvre la visio : ce n'est pas lui qui montre la panne."""
    v = context.userdata
    wanted = db._plain(name)
    if wanted:
        for p in v.room.remote_participants.values():
            if wanted in db._plain(p.name or ""):
                return p
    team = v.team()
    return team[-1] if team else None


async def _take_photo(context: RunContext[Visio], name: str = "") -> tuple[bytes, str] | None:
    v = context.userdata
    on_site = _on_site(context, name)
    jpeg = v.cameras.snapshot(prefer=on_site.identity if on_site else None)
    if not jpeg:
        return None
    PHOTOS.mkdir(parents=True, exist_ok=True)
    name = f"{secrets.token_hex(12)}.jpg"
    (PHOTOS / name).write_bytes(jpeg)
    v.last_photo = name
    await v.card("photo", url=f"/photos/{name}")
    return jpeg, name


@function_tool
async def regarder(context: RunContext[Visio], question: str, personne: str = "") -> str:
    """Regarde l'image de la caméra de la personne sur place et répond à une question sur ce qu'on voit
    (« c'est quelle marque, ce mitigeur ? », « c'est quoi ce message d'erreur ? »). Garde aussi la photo,
    pour l'envoyer si on le demande.

    Args:
        question: ce qu'il faut regarder sur l'image, en français.
        personne: le prénom de la personne à regarder, si on la nomme. Sinon, la personne sur place.
    """
    shot = await _take_photo(context, personne)
    if not shot:
        return "Aucune caméra n'est allumée. Demande à la personne sur place d'allumer sa caméra."
    jpeg, _name = shot
    response = await context.userdata.anthropic_client.messages.create(
        model=config.LLM_MODEL,
        max_tokens=300,
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
    v.pending = {"kind": "rapport", "at": time.time()}
    await v.card("mail_pending")
    return "Rien n'est envoyé. Demande maintenant : « Je t'envoie le rapport par email ? » Puis attends un oui clair avant confirmer_envoi."


@function_tool
async def preparer_invitation(context: RunContext[Visio], qui: str, motif: str, avec_photo: bool = False) -> str:
    """Trouve la personne et prépare l'envoi par WhatsApp du lien de cette visio : quelqu'un de l'équipe, un
    artisan ou un propriétaire. N'envoie rien : il faut ensuite demander une confirmation à voix haute.

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
    if avec_photo and not v.last_photo:
        await _take_photo(context)
    photo = v.last_photo if avec_photo else None
    v.pending = {"kind": "invitation", "contact": contact, "reason": motif.strip()[:120], "photo": photo, "at": time.time()}
    await v.card("pending", name=contact["name"], trade=contact["trade"], reason=v.pending["reason"],
                 photo=f"/photos/{photo}" if photo else None)
    with_photo = " avec la photo" if photo else ""
    return (f"Rien n'est envoyé. Demande maintenant : « J'envoie à {contact['name']}, {contact['trade']}, "
            f"le lien de la visio{with_photo} ? » Puis attends un oui clair avant confirmer_envoi.")


@function_tool
async def confirmer_envoi(context: RunContext[Visio]) -> str:
    """Envoie ce qui a été préparé (l'invitation ou le rapport). Seulement juste après un « oui » clair d'un
    membre de l'équipe."""
    v = context.userdata
    pending = v.pending
    if not pending or time.time() - pending["at"] > PENDING_SECONDS:
        v.pending = None
        return "Il n'y a rien à envoyer (ou la demande est trop ancienne). Prépare d'abord l'envoi."
    speaker = _speaker(context)
    if not speaker or speaker.attributes.get("role") != "equipe":
        return "Seul un membre de l'équipe peut confirmer un envoi."
    heard = db._plain(_last_user_text(context))
    if not YES_RE.search(heard) or NO_RE.search(heard):
        return "Je n'ai pas entendu de oui clair. Repose la question et attends la réponse."

    v.pending = None
    if pending["kind"] == "rapport":
        return await _send_report(context)
    contact, reason, photo = pending["contact"], pending["reason"], pending["photo"]
    invited_by = speaker.name or "L'équipe"
    code = db.create_invitation(v.room.name, contact["id"], reason, photo, invited_by)
    link = f"{config.PUBLIC_URL}/v/{code}"
    image_url = f"{config.PUBLIC_URL}/photos/{photo}" if photo else None
    first_name = contact["name"].split()[0]
    result = await whatsapp.send_invitation(contact["phone"], [first_name, invited_by, reason, link], image_url)
    if not result["ok"]:
        db.log(v.room.name, f"Échec de l'invitation à {contact['name']} : {result.get('error')}")
        await v.card("error", text=f"WhatsApp n'est pas parti vers {contact['name']} : {str(result.get('error'))[:150]}")
        return "L'envoi WhatsApp a échoué. Dis-le simplement et propose de réessayer."
    trial = " (mode essai, rien n'est parti)" if result["dry_run"] else ""
    db.log(v.room.name, f"Invitation WhatsApp à {contact['name']} ({contact['trade']}) : {reason}{trial}")
    await v.card("sent", name=contact["name"], trade=contact["trade"], link=link, dry_run=result["dry_run"])
    if result["dry_run"]:
        return "Mode essai : WhatsApp n'est pas branché, le lien est affiché sur l'écran de l'équipe."
    return f"C'est envoyé à {contact['name']}."


async def _send_report(context: RunContext[Visio]) -> str:
    v = context.userdata
    result = await mailer.send_report(db.stays())
    if not result["ok"]:
        db.log(v.room.name, f"Échec du rapport par email : {result.get('error')}")
        await v.card("error", text=f"L'email n'est pas parti : {str(result.get('error'))[:150]}")
        return "L'email a échoué. Dis-le simplement et propose de réessayer."
    await v.card("mail_sent", dry_run=result["dry_run"])
    if result["dry_run"]:
        return "Mode essai : l'email n'est pas branché, rien n'est parti. Dis-le simplement."
    db.log(v.room.name, "Rapport de la journée envoyé par email")
    return "C'est envoyé."


@function_tool
async def annuler_envoi(context: RunContext[Visio]) -> str:
    """Annule l'envoi préparé, quand l'équipe dit non ou change d'avis."""
    context.userdata.pending = None
    await context.userdata.card("cancelled")
    return "Annulé, rien n'est parti."


ALL = [regarder, ma_journee, preparer_rapport, chercher_dans_la_maison, chercher_interventions, verifier_vol,
       changer_statut, noter_intervention, preparer_invitation, confirmer_envoi, annuler_envoi]
