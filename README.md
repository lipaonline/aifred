# AIfred

AIfred (AI + Alfred, le majordome) est l'assistant IA des petites équipes de conciergerie de location
saisonnière. Un employé est dans une maison devant une panne : il ouvre une visio sur son téléphone,
AIfred regarde la caméra, trouve l'information dans la fiche de la maison et fait venir l'artisan ou le
propriétaire dans la visio par WhatsApp. Tout ça sans faire la route.

> « AIfred, c'est le talkie-walkie avec des yeux, qui répond et qui agit. »

Projet du hackathon Club IA VIP (26 octobre – 10 novembre 2026). Toutes les données de démo sont
fictives.

## Les 3 fonctionnalités

| | Fonctionnalité | État |
| --- | --- | --- |
| 1 | **Chercher et montrer** : AIfred trouve dans la fiche de la maison (vanne d'arrêt, référence du mitigeur, seuil du mandat) et l'affiche sur les téléphones de l'équipe | version simple (`chercher_dans_la_maison`) |
| 2 | **Faire venir dans la visio** : AIfred prend la photo, trouve l'artisan, demande « j'envoie ? », et après le « oui » envoie un WhatsApp avec la photo et le lien de la visio | fait, à tester avec les vraies clés |
| 3 | **Replanifier après le bon « oui »** | à faire |

## Comment ça marche

```
 Téléphone (page web)                      Notre serveur (Bunny Magic Containers)
 ┌──────────────────────┐   jeton          ┌─────────────────────────────────────────┐
 │ web/index.html       │ ◄──────────────  │ server.py  FastAPI : page, jetons,      │
 │ livekit-client       │                  │            invitations, photos          │
 └──────────┬───────────┘                  │ agent.py   AIfred : LiveKit Agents      │
            │ audio, vidéo                 │            + Gradium (voix) + Claude    │
            ▼                              │ tools.py   ce qu'AIfred sait faire      │
 ┌──────────────────────┐   audio, vidéo   │ db.py      SQLite (contacts, maisons…)  │
 │ LiveKit Cloud (salle)│ ◄──────────────► │ whatsapp.py  Zernio → WhatsApp          │
 └──────────────────────┘                  └─────────────────────────────────────────┘
```

- **La visio** : LiveKit Cloud. Le premier membre de l'équipe qui ouvre une visio reçoit un jeton qui
  demande l'agent « aifred » : LiveKit l'envoie dans la salle. L'agent tourne chez nous, pas chez
  LiveKit (facturé comme un participant, 0,0005 $ la minute).
- **La voix** : Gradium, obligatoire (plugin `livekit-plugins-gradium` : reconnaissance en français, voix
  Leo). La clé Gradium reste sur le serveur, jamais dans la page.
- **Le cerveau** : Claude Haiku 5.5 (`claude-haiku-5-5`), le plus rapide pour la voix. Il voit la caméra
  seulement quand on le lui demande (outil `regarder`), pour ne pas payer une image à chaque phrase.
- **WhatsApp** : par Zernio, comme Plat du Jour. Sans clé Zernio, AIfred est en **mode essai** : rien ne
  part, le lien d'invitation s'affiche dans la visio pour l'ouvrir sur un autre téléphone.
- **Les garde-fous** :
  - un envoi ne part qu'après un « oui » d'un membre de l'équipe, vérifié dans le code
    (`confirmer_invitation`), pas seulement demandé au modèle ;
  - AIfred se présente comme une IA à chaque invité ;
  - il ne lit jamais un numéro à voix haute ;
  - à plusieurs, il ne répond que si on l'appelle « Alfred » (ou dans les 20 s après sa réponse).

Qui travaille où : Sébastien sur l'intelligence (`prompt.md`, `tools.py`), Patrick sur la vidéo et la voix
(`agent.py`, `web/index.html`, `server.py`).

## Lancer en local

Il faut [uv](https://docs.astral.sh/uv/) et les clés : LiveKit Cloud (projet gratuit sur
[cloud.livekit.io](https://cloud.livekit.io)), Anthropic, Gradium.

```bash
git clone <ce dépôt> aifred && cd aifred
cp .env.example .env          # puis remplis LIVEKIT_*, ANTHROPIC_API_KEY, GRADIUM_API_KEY
uv sync
uv run python agent.py download-files

# deux terminaux
uv run python agent.py dev
uv run uvicorn server:app --port 8080 --reload
```

Ouvre <http://localhost:8080>, donne ton prénom, « Démarrer une visio ». AIfred dit bonjour. Essaie :
« Alfred, c'est quoi la marque de ce mitigeur ? », puis « fais venir le plombier ».

Sur un téléphone, la caméra exige une page en HTTPS : en local, passe par un tunnel
(`cloudflared tunnel --url http://localhost:8080`) et mets son adresse dans `AIFRED_PUBLIC_URL`.

## Mettre en ligne sur Bunny Magic Containers

Une seule image Docker fait tourner l'agent et le serveur web (`start.sh`).

1. Pousse ce dépôt sur GitHub.
2. Bunny → **Magic Containers** → **Create application** → depuis le dépôt GitHub (le `Dockerfile` est
   détecté).
3. **Une seule région (Paris), une seule instance** : la base SQLite est sur le disque du conteneur.
4. Ressources : 1 vCPU et 2 Go de mémoire pour commencer.
5. **Disque persistant** monté sur `/data` (base et photos). Bunny ne le sauvegarde pas : pour le
   hackathon, ça suffit.
6. Port public **8080**, puis note l'adresse donnée par Bunny (`https://….b-cdn.net`).
7. Variables : celles de `.env.example`, avec `AIFRED_PUBLIC_URL` = l'adresse Bunny et un
   `AIFRED_TEAM_CODE` (`openssl rand -hex 8`). Redéploie.
8. Donne à l'équipe le lien `https://….b-cdn.net/?equipe=<code>` : le code reste dans le téléphone.

À vérifier au premier déploiement : que l'agent rejoint bien la salle depuis Bunny (il sort vers LiveKit
Cloud en UDP, ou en TCP si l'UDP est bloqué).

## WhatsApp pour la démo : le bac à sable Zernio

Zernio prête gratuitement un numéro américain de test (+1 202 908 7457). Pas de modèle à faire approuver :
on envoie un message libre (texte, photo et lien). Limites :

- **un seul téléphone destinataire à la fois** (celui qui joue l'artisan), 50 messages par jour ;
- ce téléphone est activé d'avance, et l'activation dure 7 jours ;
- il doit **avoir écrit au numéro du bac à sable dans les 24 dernières heures** : avant chaque démo,
  envoie « bonjour » depuis ce téléphone ;
- le message arrive d'un numéro américain au nom de Zernio, pas d'AIfred.

Mise en place :

```bash
# dans .env : ZERNIO_API_KEY=… puis
uv run python whatsapp.py sandbox                 # affiche ZERNIO_WHATSAPP_ACCOUNT_ID à copier dans .env
uv run python whatsapp.py sandbox +33612345678    # active le téléphone de l'« artisan »
```

Le téléphone reçoit un message du bac à sable : il y répond. Puis dans `.env` : `ZERNIO_SANDBOX=1`, et
le numéro de ce téléphone dans le contact « plombier » (`AIFRED_DEMO_JSON`, ou `demo.example.json` en
local, puis supprime `data/aifred.db` pour recharger). La photo n'est jointe que si le serveur est en
ligne : Zernio va la chercher à l'adresse `AIFRED_PUBLIC_URL`.

## Le modèle WhatsApp, pour un vrai numéro (à faire approuver par Meta)

Un artisan qui ne nous a jamais écrit ne peut recevoir qu'un modèle approuvé. Dans Zernio (ou le
Gestionnaire WhatsApp de Meta), sur le numéro d'AIfred :

- **Nom** : `aifred_visio` · **Catégorie** : Utilité · **Langue** : français (`fr`)
- **En-tête** : image (exemple : une photo de mitigeur)
- **Corps** :
  `Bonjour {{1}}, {{2}} vous demande de rejoindre une visio pour : {{3}}. Rejoindre : {{4}}`
  (1 = prénom de l'artisan, 2 = prénom de l'employé, 3 = motif, 4 = lien de la visio)

Puis `ZERNIO_API_KEY` et `ZERNIO_WHATSAPP_ACCOUNT_ID` dans les variables.

## Fichiers

| Fichier | Rôle |
| --- | --- |
| `agent.py` | l'agent LiveKit : voix, écoute, caméras, présentation aux invités |
| `tools.py` | les outils d'AIfred (regarder, chercher, inviter, confirmer) |
| `prompt.md` | les instructions d'AIfred |
| `server.py` | page web, jetons LiveKit, liens d'invitation, photos |
| `db.py` | base SQLite ; `demo.example.json` : contacts et maisons fictifs |
| `whatsapp.py` | envoi du modèle WhatsApp par Zernio |
| `web/index.html` | la page du téléphone (équipe et invités) |
| `Dockerfile`, `start.sh` | l'image pour Bunny |

## Limites connues

- AIfred n'écoute qu'une personne : le premier membre de l'équipe entré dans la salle. Écouter chaque
  participant (et accepter le « oui » de n'importe quel membre de l'équipe) viendra ensuite.
- La reconnaissance vocale tourne tant que la visio est ouverte : à surveiller sur les crédits Gradium.
- Pas encore de réponse aux messages WhatsApp entrants.
