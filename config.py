"""
Réglages d'AIfred, lus dans les variables d'environnement (fichier .env en local,
variables de l'application sur Bunny Magic Containers). Voir .env.example.
"""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load_env():
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()

# Adresse publique du serveur : liens envoyés par WhatsApp, photos jointes.
PUBLIC_URL = os.environ.get("AIFRED_PUBLIC_URL", "http://localhost:8080").rstrip("/")
# Code d'accès de l'équipe : sans lui, personne ne peut ouvrir une visio (le serveur est public).
TEAM_CODE = os.environ.get("AIFRED_TEAM_CODE", "")
# Base SQLite et photos. Sur Bunny : le disque persistant monté sur /data.
DATA_DIR = Path(os.environ.get("AIFRED_DATA_DIR", ROOT / "data"))

# LiveKit Cloud (projet AIfred, pas celui de LearnFloo).
LIVEKIT_URL = os.environ.get("LIVEKIT_URL", "")
LIVEKIT_API_KEY = os.environ.get("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.environ.get("LIVEKIT_API_SECRET", "")
AGENT_NAME = "aifred"

# Le cerveau et la voix. ANTHROPIC_API_KEY et GRADIUM_API_KEY sont lus par les SDK eux-mêmes.
LLM_MODEL = os.environ.get("AIFRED_LLM_MODEL", "claude-haiku-5-5")
VOICE_ID = os.environ.get("AIFRED_VOICE_ID", "axlOaUiFyOZhy4nv")  # Leo, voix masculine française

# WhatsApp par Zernio, comme Plat du Jour (convex-flows ZernioClient.startConversation).
# Sans clé ni compte, les messages ne partent pas : ils s'affichent dans la visio (mode essai).
ZERNIO_API_KEY = os.environ.get("ZERNIO_API_KEY", "")
ZERNIO_API_URL = os.environ.get("ZERNIO_API_URL", "https://zernio.com/api").rstrip("/")
ZERNIO_WHATSAPP_ACCOUNT_ID = os.environ.get("ZERNIO_WHATSAPP_ACCOUNT_ID", "")
# Bac à sable Zernio (numéro US gratuit de test) : message libre au lieu du modèle approuvé
ZERNIO_SANDBOX = os.environ.get("ZERNIO_SANDBOX", "") in ("1", "true", "oui")
WHATSAPP_TEMPLATE = os.environ.get("AIFRED_WHATSAPP_TEMPLATE", "aifred_visio")
WHATSAPP_TEMPLATE_LANGUAGE = os.environ.get("AIFRED_WHATSAPP_TEMPLATE_LANGUAGE", "fr")

# Le rapport de la journée par email. Sans ces trois réglages : mode essai, rien ne part.
POSTMARK_API_TOKEN = os.environ.get("POSTMARK_API_TOKEN", "")
POSTMARK_FROM = os.environ.get("POSTMARK_FROM", "")
REPORT_EMAIL = os.environ.get("AIFRED_REPORT_EMAIL", "")

# Durée de validité d'un lien d'invitation, en heures.
INVITATION_HOURS = int(os.environ.get("AIFRED_INVITATION_HOURS", "12"))
