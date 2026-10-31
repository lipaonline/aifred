"""
Les outils d'AIfred : ce qu'il peut faire quand on lui parle. Le prompt est dans prompt.md.

Fonctionnalité 1, chercher et montrer : chercher_dans_la_maison (version simple, à enrichir).
Fonctionnalité 2, faire venir dans la visio : regarder, chercher_contact, preparer_invitation,
    confirmer_invitation, annuler_invitation.
Fonctionnalité 3, replanifier après le bon « oui » : pas encore là.

Règle du hackathon : toute action qu'on ne peut pas annuler passe par une confirmation humaine.
Ici, preparer_invitation n'envoie rien ; confirmer_invitation vérifie dans le code que la dernière
phrase entendue est un « oui » et qu'elle vient d'un membre de l'équipe.
"""

import base64
import json
import re
import secrets
import time
from dataclasses import dataclass, field

import anthropic
from livekit import rtc
from livekit.agents import RunContext, function_tool

import config
import db
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
    anthropic_client: anthropic.AsyncAnthropic = field(default_factory=anthropic.AsyncAnthropic)

    def team(self) -> list[rtc.RemoteParticipant]:
        return [p for p in self.room.remote_participants.values() if p.attributes.get("role") == "equipe"]

    async def card(self, kind: str, **data):
        """Affiche une carte sur les téléphones de l'équipe (pas des invités : liens et numéros restent internes)."""
        team = [p.identity for p in self.team()]
        if team:
            payload = json.dumps({"type": kind, **data}, ensure_ascii=False)
            await self.room.local_participant.publish_data(payload, topic="aifred", destination_identities=team)


def _speaker(context: RunContext[Visio]) -> rtc.RemoteParticipant | None:
    """La personne qu'AIfred écoute (le participant lié à la session)."""
    return context.session.room_io.linked_participant


def _last_user_text(context: RunContext[Visio]) -> str:
    for item in reversed(context.session.history.items):
        if getattr(item, "role", None) == "user":
            return item.text_content or ""
    return ""


async def _take_photo(context: RunContext[Visio]) -> tuple[bytes, str] | None:
    v = context.userdata
    speaker = _speaker(context)
    jpeg = v.cameras.snapshot(prefer=speaker.identity if speaker else None)
    if not jpeg:
        return None
    PHOTOS.mkdir(parents=True, exist_ok=True)
    name = f"{secrets.token_hex(12)}.jpg"
    (PHOTOS / name).write_bytes(jpeg)
    v.last_photo = name
    await v.card("photo", url=f"/photos/{name}")
    return jpeg, name


@function_tool
async def regarder(context: RunContext[Visio], question: str) -> str:
    """Regarde l'image de la caméra de la personne sur place et répond à une question sur ce qu'on voit
    (« c'est quelle marque, ce mitigeur ? », « est-ce que ça fuit encore ? »). Garde aussi la photo, qui
    sera jointe à la prochaine invitation.

    Args:
        question: ce qu'il faut regarder sur l'image, en français.
    """
    shot = await _take_photo(context)
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
    # Version simple : les fiches entières, Claude y trouve la réponse. À remplacer par une vraie
    # base de connaissances (photos des équipements, notices, historique des interventions).
    houses = db.houses()
    words = db._plain(question)
    match = [h for h in houses if any(w in words for w in db._plain(h["name"]).split() if len(w) > 3)]
    for h in match or houses[:1]:
        await context.userdata.card("fiche", title=h["name"], text=h["notes"])
    # Un objet, jamais une liste : le plugin Anthropic prend une liste JSON pour des blocs de contenu (erreur 400)
    return json.dumps({"maisons": [{"maison": h["name"], "adresse": h["address"], "fiche": h["notes"]}
                                   for h in (match or houses)]}, ensure_ascii=False)


@function_tool
async def chercher_contact(context: RunContext[Visio], recherche: str) -> str:
    """Trouve un artisan ou un propriétaire par métier ou par nom (« plombier », « Whitmore »).

    Args:
        recherche: le métier ou le nom.
    """
    found = db.search_contacts(recherche)
    if not found:
        return f"Aucun contact pour « {recherche} »."
    # Pas de numéro de téléphone : AIfred ne le lit jamais à voix haute
    return json.dumps({"contacts": [{"id": c["id"], "nom": c["name"], "metier": c["trade"]} for c in found]},
                      ensure_ascii=False)


@function_tool
async def preparer_invitation(context: RunContext[Visio], contact_id: int, motif: str) -> str:
    """Prépare l'envoi par WhatsApp du lien de cette visio, avec la dernière photo, à un artisan ou un
    propriétaire. N'envoie rien : il faut ensuite demander une confirmation à voix haute.

    Args:
        contact_id: l'id renvoyé par chercher_contact.
        motif: la raison en quelques mots (« fuite du mitigeur de la douche, suite parentale »).
    """
    v = context.userdata
    contact = db.contact(contact_id)
    if not contact:
        return "Ce contact n'existe pas. Cherche-le d'abord avec chercher_contact."
    if not v.last_photo:
        await _take_photo(context)
    v.pending = {"contact": contact, "reason": motif.strip()[:120], "photo": v.last_photo, "at": time.time()}
    await v.card("pending", name=contact["name"], trade=contact["trade"], reason=v.pending["reason"],
                 photo=f"/photos/{v.last_photo}" if v.last_photo else None)
    with_photo = " avec la photo" if v.last_photo else ""
    return (f"Rien n'est envoyé. Demande maintenant : « J'envoie à {contact['name']}, {contact['trade']}, "
            f"le lien de la visio{with_photo} ? » Puis attends un oui clair avant confirmer_invitation.")


@function_tool
async def confirmer_invitation(context: RunContext[Visio]) -> str:
    """Envoie l'invitation préparée. Seulement juste après un « oui » clair d'un membre de l'équipe."""
    v = context.userdata
    pending = v.pending
    if not pending or time.time() - pending["at"] > PENDING_SECONDS:
        v.pending = None
        return "Il n'y a rien à envoyer (ou la demande est trop ancienne). Prépare d'abord l'invitation."
    speaker = _speaker(context)
    if not speaker or speaker.attributes.get("role") != "equipe":
        return "Seul un membre de l'équipe peut confirmer un envoi."
    heard = db._plain(_last_user_text(context))
    if not YES_RE.search(heard) or NO_RE.search(heard):
        return "Je n'ai pas entendu de oui clair. Repose la question et attends la réponse."

    contact, reason, photo = pending["contact"], pending["reason"], pending["photo"]
    v.pending = None
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


@function_tool
async def annuler_invitation(context: RunContext[Visio]) -> str:
    """Annule l'invitation préparée, quand l'équipe dit non ou change d'avis."""
    context.userdata.pending = None
    await context.userdata.card("cancelled")
    return "Annulé, rien n'est parti."


ALL = [regarder, chercher_dans_la_maison, chercher_contact, preparer_invitation, confirmer_invitation,
       annuler_invitation]
