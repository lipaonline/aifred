"""
Le rapport de la journée par email, avec Postmark.

Sans POSTMARK_API_TOKEN, POSTMARK_FROM ou AIFRED_REPORT_EMAIL : mode essai, rien ne part.
"""

from html import escape
from urllib.parse import quote_plus

import aiohttp

import config

POSTMARK_URL = "https://api.postmarkapp.com/email"


def configured() -> bool:
    return bool(config.POSTMARK_API_TOKEN and config.POSTMARK_FROM and config.REPORT_EMAIL)


def _rows(stays: list[dict]) -> str:
    html = ""
    for s in stays:
        place = escape(s["house"])
        if s.get("address"):
            maps = "https://www.google.com/maps/search/?api=1&query=" + quote_plus(s["address"])
            place = f'<a href="{maps}" style="color:#b07a12">{place}</a><br><span style="color:#667">{escape(s["address"])}</span>'
        details = s["transport"] + (f", vol {s['flight']}" if s.get("flight") else "")
        html += (
            '<tr style="border-top:1px solid #e3e6ea">'
            f'<td style="padding:8px 10px 8px 0;font-weight:600">{escape(s["at"])}</td>'
            f'<td style="padding:8px 10px 8px 0">{place}</td>'
            f'<td style="padding:8px 10px 8px 0">{escape(s["guest"])}<br><span style="color:#667">{escape(details)}</span></td>'
            f'<td style="padding:8px 10px 8px 0">{escape(s["staff"])}</td>'
            f'<td style="padding:8px 0">{escape(s["status"])}</td></tr>'
        )
    return html


def report_html(stays: list[dict]) -> str:
    html = '<div style="font:15px/1.4 -apple-system,Segoe UI,Roboto,sans-serif;color:#1b2128;max-width:680px">'
    html += '<h1 style="font-size:22px;margin:0 0 4px">Ta journée</h1>'
    html += '<p style="margin:0 0 16px;color:#667">Le rapport préparé par AIfred, assistant IA de la conciergerie.</p>'
    for kind, title in (("arrivee", "Arrivées"), ("depart", "Départs")):
        rows = [s for s in stays if s["kind"] == kind]
        if rows:
            html += f'<h2 style="font-size:15px;margin:16px 0 4px">{title}</h2>'
            html += f'<table style="border-collapse:collapse;width:100%;font-size:14px">{_rows(rows)}</table>'
    return html + "</div>"


async def send_report(stays: list[dict]) -> dict:
    """Envoie le rapport. Renvoie {ok, dry_run, error?}."""
    if not configured():
        return {"ok": True, "dry_run": True}
    arrivals = sum(s["kind"] == "arrivee" for s in stays)
    departures = len(stays) - arrivals
    body = {
        "From": config.POSTMARK_FROM,
        "To": config.REPORT_EMAIL,
        "Subject": f"Ta journée : {arrivals} arrivée(s), {departures} départ(s)",
        "HtmlBody": report_html(stays),
        "MessageStream": "outbound",
    }
    headers = {"X-Postmark-Server-Token": config.POSTMARK_API_TOKEN, "Accept": "application/json"}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as http:
            async with http.post(POSTMARK_URL, json=body, headers=headers) as r:
                data = await r.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError) as e:
        return {"ok": False, "dry_run": False, "error": str(e)}
    if r.status >= 300:
        return {"ok": False, "dry_run": False, "error": str(data)[:300]}
    return {"ok": True, "dry_run": False}
