#!/usr/bin/env bash
# Le conteneur fait tourner les deux processus d'AIfred : l'agent LiveKit et le serveur web.
# Si l'un des deux s'arrête, le conteneur s'arrête aussi, et Bunny le relance.
set -euo pipefail

python agent.py start &
uvicorn server:app --host 0.0.0.0 --port "${PORT:-8080}" --proxy-headers --forwarded-allow-ips='*' &

wait -n
echo "Un des processus s'est arrêté, arrêt du conteneur" >&2
exit 1
