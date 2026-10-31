"""
Envoi WhatsApp par Zernio, repris de Plat du Jour (convex-flows, ZernioClient.startConversation).

L'artisan ou le propriétaire ne nous a jamais écrit : la fenêtre de 24 h est fermée, donc WhatsApp
n'accepte qu'un modèle approuvé par Meta. Modèle attendu (AIFRED_WHATSAPP_TEMPLATE, catégorie
« Utilité », langue fr) :
    en-tête : image (la photo prise dans la visio)
    corps   : « Bonjour {{1}}, {{2}} vous demande de rejoindre une visio pour : {{3}}.
               Rejoindre : {{4}} »
Sans ZERNIO_API_KEY ou sans compte, rien ne part : send_invitation renvoie dry_run et la visio
affiche le lien, à ouvrir à la main sur un autre téléphone.
"""

import re

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


async def send_invitation(phone: str, params: list[str], image_url: str | None) -> dict:
    """Ouvre la conversation avec le modèle. Renvoie {ok, dry_run, error?}."""
    if not configured():
        return {"ok": True, "dry_run": True}
    body = {
        "accountId": config.ZERNIO_WHATSAPP_ACCOUNT_ID,
        "participantId": phone_digits(phone),
        "templateName": config.WHATSAPP_TEMPLATE,
        "templateLanguage": config.WHATSAPP_TEMPLATE_LANGUAGE,
        "templateParams": params,
    }
    if image_url:
        body["headerMedia"] = {"type": "image", "link": image_url}
    headers = {"Authorization": f"Bearer {config.ZERNIO_API_KEY}"}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as http:
            async with http.post(f"{config.ZERNIO_API_URL}/v1/inbox/conversations", json=body, headers=headers) as r:
                data = await r.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError) as e:
        return {"ok": False, "dry_run": False, "error": str(e)}
    # Zernio : 2xx et pas de success: false (TEMPLATE_REQUIRED, recipient_opted_out… dans code)
    if r.status >= 300 or not isinstance(data, dict) or data.get("success") is False:
        return {"ok": False, "dry_run": False, "error": str(data)[:300]}
    return {"ok": True, "dry_run": False}
