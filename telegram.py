"""
Envoi des invitations par un bot Telegram (remplace WhatsApp par Zernio pour la démo).

Pourquoi Telegram : pas de bac à sable ni de modèle à faire approuver par Meta, pas de session qui expire,
gratuit, et la photo, le texte et un bouton « Rejoindre la visio » partent dans un seul message.

La limite : un bot ne peut pas écrire en premier, ni à un numéro de téléphone. Chaque destinataire doit
avoir ouvert le bot une fois (bouton Démarrer). Son identifiant Telegram (chat_id) va dans le champ
« telegram » du contact (AIFRED_DEMO_JSON). Pour le trouver : python telegram.py ids

Le cerveau ne connaît pas le canal : tools.py appelle seulement send_invitation, comme avant.
Sans TELEGRAM_BOT_TOKEN : mode essai, rien ne part, la visio affiche le lien.
"""

import asyncio
import sys

import aiohttp

import config

API_URL = "https://api.telegram.org"


def configured() -> bool:
    return bool(config.TELEGRAM_BOT_TOKEN)


async def _call(method: str, body: dict | None = None) -> tuple[bool, dict | str]:
    """Appel à l'API des bots Telegram. Succès : « ok »: true. Échec : la réponse, pour lire error_code."""
    url = f"{API_URL}/bot{config.TELEGRAM_BOT_TOKEN}/{method}"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as http:
            async with http.post(url, json=body or {}) as r:
                data = await r.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError) as e:
        # Jamais l'adresse complète dans l'erreur : elle contient le jeton
        return False, type(e).__name__
    if not isinstance(data, dict) or not data.get("ok"):
        return False, data if isinstance(data, dict) else str(data)[:300]
    return True, data


def _error_text(name: str, data: dict | str) -> str:
    """Une erreur lisible. 403 (bot bloqué) ou « chat not found » : la personne n'a pas ouvert le bot."""
    if isinstance(data, dict):
        description = str(data.get("description", ""))
        if data.get("error_code") == 403 or "chat not found" in description.lower():
            return f"{name} n'a pas encore ouvert le bot AIfred"
        return description[:300] or str(data)[:300]
    return str(data)[:300]


async def send_invitation(chat_id: str, params: list[str], image_url: str | None) -> dict:
    """Envoie l'invitation. params = [prénom de l'invité, prénom de l'employé, motif, lien].
    Renvoie {ok, dry_run, error?}."""
    if not configured():
        return {"ok": True, "dry_run": True}
    name, invited_by, reason, link = params
    if not str(chat_id or "").strip():
        return {"ok": False, "dry_run": False, "error": f"{name} n'a pas d'identifiant Telegram dans le carnet"}
    # Telegram va chercher la photo à cette adresse, et refuse un bouton vers localhost
    public = not link.startswith("http://localhost")
    if image_url and image_url.startswith("http://localhost"):
        image_url = None

    # Règle du hackathon : face à un tiers, AIfred se présente comme une IA
    text = (f"Bonjour {name}, {invited_by} vous demande de rejoindre une visio pour : {reason}.\n"
            f"Rejoindre : {link}\n"
            f"Ce message vient d'AIfred, un assistant IA.")
    body = {"chat_id": str(chat_id).strip()}
    if public:
        body["reply_markup"] = {"inline_keyboard": [[{"text": "Rejoindre la visio", "url": link}]]}
    if image_url:
        ok, data = await _call("sendPhoto", body | {"photo": image_url, "caption": text})
    else:
        ok, data = await _call("sendMessage", body | {"text": text})

    if not ok:
        return {"ok": False, "dry_run": False, "error": _error_text(name, data)}
    return {"ok": True, "dry_run": False}


async def _ids_cli():
    """Affiche les personnes qui ont écrit au bot récemment, avec leur identifiant à mettre dans le carnet."""
    if not configured():
        print("TELEGRAM_BOT_TOKEN manque dans .env (le jeton donné par @BotFather).")
        return
    ok, data = await _call("getUpdates", {"allowed_updates": ["message"]})
    if not ok:
        print("Échec :", _error_text("?", data))
        return
    seen = {}
    for update in data.get("result", []):
        chat = (update.get("message") or {}).get("chat") or {}
        if chat.get("id") is not None:
            seen[chat["id"]] = " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")])) or chat.get("title", "")
    if not seen:
        print("Personne n'a encore écrit au bot. Ouvre-le dans Telegram, touche Démarrer, puis relance.")
    for chat_id, name in seen.items():
        print(f"  {name} : \"telegram\": \"{chat_id}\"")


if __name__ == "__main__":
    # uv run python telegram.py ids
    if len(sys.argv) >= 2 and sys.argv[1] == "ids":
        asyncio.run(_ids_cli())
    else:
        print("Usage : uv run python telegram.py ids")
