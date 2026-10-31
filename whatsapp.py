"""
Envoi WhatsApp par Zernio, repris de Plat du Jour (convex-flows, ZernioClient).

Deux façons d'envoyer :

1. Numéro d'AIfred (production). L'artisan ne nous a jamais écrit : la fenêtre de 24 h est fermée,
   donc WhatsApp n'accepte qu'un modèle approuvé par Meta (AIFRED_WHATSAPP_TEMPLATE, catégorie
   « Utilité », langue fr) :
       en-tête : image (la photo prise dans la visio)
       corps   : « Bonjour {{1}}, {{2}} vous demande de rejoindre une visio pour : {{3}}.
                  Rejoindre : {{4}} »

2. Bac à sable Zernio (ZERNIO_SANDBOX=1), gratuit, pour la démo. Un seul téléphone de test à la fois,
   activé d'avance (python whatsapp.py sandbox +336…), qui doit avoir écrit au numéro du bac à sable
   dans les 24 dernières heures. On lui envoie un message libre : texte + photo.

Sans ZERNIO_API_KEY ou sans compte : mode essai, rien ne part, la visio affiche le lien.
"""

import asyncio
import re
import sys

import aiohttp

import config


def configured() -> bool:
    return bool(config.ZERNIO_API_KEY and config.ZERNIO_WHATSAPP_ACCOUNT_ID)


def phone_digits(phone: str) -> str:
    """« +33 6 12 34 56 78 », « 0033… » ou « 06 12… » → « 33612345678 »."""
    digits = re.sub(r"\D", "", phone)
    if digits.startswith("00"):
        digits = digits[2:]
    elif len(digits) == 10 and digits.startswith("0"):
        digits = "33" + digits[1:]
    return digits


async def _call(method: str, path: str, body: dict | None = None) -> tuple[bool, dict | str]:
    """Appel à l'API Zernio. Succès : 2xx et pas de success: false (TEMPLATE_REQUIRED, rate_limited…)."""
    headers = {"Authorization": f"Bearer {config.ZERNIO_API_KEY}"}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as http:
            async with http.request(method, f"{config.ZERNIO_API_URL}{path}", json=body, headers=headers) as r:
                data = await r.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError) as e:
        return False, str(e)
    if r.status >= 300 or not isinstance(data, dict) or data.get("success") is False:
        return False, str(data)[:300]
    return True, data


async def _sandbox_conversation(digits: str) -> str | None:
    """La conversation du bac à sable avec ce téléphone (il a écrit, donc elle existe)."""
    ok, data = await _call(
        "GET", f"/v1/inbox/conversations?accountId={config.ZERNIO_WHATSAPP_ACCOUNT_ID}&platform=whatsapp&limit=100")
    if not ok:
        return None
    for conv in data.get("data", []):
        if phone_digits(str(conv.get("participantId", ""))) == digits:
            return conv["id"]
    return None


async def send_invitation(phone: str, params: list[str], image_url: str | None) -> dict:
    """Envoie l'invitation. params = [prénom de l'invité, prénom de l'employé, motif, lien].
    Renvoie {ok, dry_run, error?}."""
    if not configured():
        return {"ok": True, "dry_run": True}
    digits = phone_digits(phone)
    # Zernio va chercher la photo à cette adresse : impossible depuis localhost
    if image_url and image_url.startswith("http://localhost"):
        image_url = None

    if config.ZERNIO_SANDBOX:
        conversation = await _sandbox_conversation(digits)
        if not conversation:
            return {"ok": False, "dry_run": False,
                    "error": "ce téléphone n'a pas de conversation avec le bac à sable (activation à faire)"}
        name, invited_by, reason, link = params
        body = {
            "accountId": config.ZERNIO_WHATSAPP_ACCOUNT_ID,
            "message": f"Bonjour {name}, {invited_by} vous demande de rejoindre une visio pour : {reason}.\n"
                       f"Rejoindre : {link}",
        }
        if image_url:
            body |= {"attachmentUrl": image_url, "attachmentType": "image"}
        ok, data = await _call("POST", f"/v1/inbox/conversations/{conversation}/messages", body)
    else:
        body = {
            "accountId": config.ZERNIO_WHATSAPP_ACCOUNT_ID,
            "participantId": digits,
            "templateName": config.WHATSAPP_TEMPLATE,
            "templateLanguage": config.WHATSAPP_TEMPLATE_LANGUAGE,
            "templateParams": params,
        }
        if image_url:
            body["headerMedia"] = {"type": "image", "link": image_url}
        ok, data = await _call("POST", "/v1/inbox/conversations", body)

    if not ok:
        return {"ok": False, "dry_run": False, "error": data}
    return {"ok": True, "dry_run": False}


async def _sandbox_cli(phone: str | None):
    """Affiche le bac à sable et ses sessions ; avec un numéro, lance son activation."""
    ok, data = await _call("GET", "/v1/phone-numbers")
    sandbox = data.get("sandbox") if ok else None
    if not sandbox:
        print("Pas de bac à sable sur ce compte Zernio :", data)
        return
    print(f"Numéro du bac à sable : {sandbox['phoneNumber']}")
    print(f"ZERNIO_WHATSAPP_ACCOUNT_ID={sandbox['accountId']}")
    if phone:
        ok, data = await _call("POST", "/v1/whatsapp/sandbox/sessions", {"phone": "+" + phone_digits(phone)})
        print("Activation lancée : réponds au message WhatsApp reçu sur ce téléphone." if ok else f"Échec : {data}")
    ok, data = await _call("GET", "/v1/whatsapp/sandbox/sessions")
    for s in data.get("sessions", []) if ok else []:
        print(f"  {s['phoneE164']} : {s['status']}, expire le {s.get('expiresAt')}")


if __name__ == "__main__":
    # uv run python whatsapp.py sandbox [+33612345678]
    if len(sys.argv) >= 2 and sys.argv[1] == "sandbox":
        asyncio.run(_sandbox_cli(sys.argv[2] if len(sys.argv) > 2 else None))
    else:
        print("Usage : uv run python whatsapp.py sandbox [+33612345678]")
