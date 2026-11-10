# AIfred

AIfred (AI + Alfred, le majordome) est l'assistant IA des petites équipes de conciergerie de location
saisonnière. L'équipe se parle sur un canal, comme au talkie-walkie. Devant une panne, un employé passe
en visio : AIfred regarde la caméra, trouve l'information dans la fiche de la maison et fait venir
l'artisan ou le propriétaire par Telegram. Tout ça sans faire la route.

> « AIfred, c'est le talkie-walkie avec des yeux, qui répond et qui agit. »

Projet du hackathon Jarvis × Gradium du Club IA VIP (26 octobre – 10 novembre 2026). Toutes les données de démo sont
fictives.

## Les 3 fonctionnalités

| | Fonctionnalité | État |
| --- | --- | --- |
| 1 | **Préparer la journée** : AIfred résume les arrivées et les départs, affiche le tableau du jour, et après le « oui » envoie le rapport par email | fait ; email en mode essai sans clé Postmark |
| 2 | **Le carnet des maisons** : fiches, historique des interventions, procédures, tarifs, questions fréquentes, vol du voyageur ; il met le tableau à jour et note chaque intervention | fait ; le statut du vol est simulé |
| 3 | **Faire venir en visio** : AIfred trouve la personne, demande « j'envoie ? », et après le « oui » envoie un message Telegram avec la photo si on la demande, le texte et un bouton « Rejoindre la visio » | fait ; chaque destinataire doit avoir ouvert le bot une fois |

AIfred voit aussi par la caméra (`regarder`) : c'est un sens, comme la voix.

## L'interface : deux boutons

- **Le sélecteur Talkie | Visio.** Talkie : toute l'équipe sur le même canal, sans caméra. Visio : on se voit, avec les personnes qu'on invite. Dans les deux, tout le monde parle librement.
- **Le bouton Équipe** : un toucher coupe ou rouvre son micro, quand c'est bruyant.
- **Le bouton AIfred** : un clic pour lui parler, un second clic quand on a fini, et c'est à ce moment qu'il répond. On peut donc faire une pause. L'équipe et les invités l'ont tous. Il n'écoute que la personne qui a cliqué, et tout le monde entend la question et la réponse. Pendant sa réponse, la même personne peut le couper d'un clic. Si on oublie le second clic, il se coupe seul après 30 s de silence.
- **Le bouton Photo**, en visio : un toucher prend une photo avec sa propre caméra, l'affiche à l'équipe, et AIfred dit ce qu'il voit. À la voix (« Regarde le message d'erreur »), AIfred choisit la caméra : celle qu'on nomme, sinon la caméra arrière, sinon pas celle de la personne qui a ouvert la visio.
- **Finir** : sur Talkie, le bouton Quitter revient à l'accueil. En visio, le bouton rouge raccroche et propose la fiche.
- **La carte à valider** : Envoyer, Annuler, ou un clic sur AIfred pour lui dire « oui ».
- **La fiche d'intervention** : à la fin d'une visio, AIfred la rédige à partir de ce qu'il a entendu. Rien n'est enregistré avant « Valider ».

Le canal dédié pour parler à AIfred en privé est une évolution prévue, pas dans cette version.

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
 │ LiveKit Cloud (salle)│ ◄──────────────► │ telegram.py  bot Telegram (invitations) │
 └──────────────────────┘                  └─────────────────────────────────────────┘
```

- **Les salles** : LiveKit Cloud. Le canal `canal-equipe` et une salle par visio (`visio-…`). Le jeton de
  l'équipe demande l'agent « aifred » : LiveKit l'envoie dans la salle. L'agent tourne chez nous, pas chez
  LiveKit (facturé comme un participant, 0,0005 $ la minute).
- **Qui a activé AIfred** : c'est sur les attributs de l'agent, visibles par tous. Les boutons l'appellent
  par RPC : `activer`, `valider`, `lancer_visio`, `terminer`.
- **La voix** : Gradium, obligatoire (plugin `livekit-plugins-gradium` : reconnaissance en français, voix
  Leo). La clé Gradium reste sur le serveur, jamais dans la page.
- **Le cerveau** : Claude Haiku 5.5 (`claude-haiku-5-5`) en effort `low`, le plus rapide pour la voix.
- **Les yeux** : l'agent ne reçoit aucune vidéo. Quand on le lui demande (outil `regarder`), il demande une
  image au téléphone de la personne : une image, pas un flux.
- **L'écoute** : Gradium ne transcrit qu'entre les deux clics. Le reste du temps, rester connecté ne consomme
  aucun crédit d'écoute.
- **Les invitations** : par un bot Telegram (`telegram.py`, une seule fonction `send_invitation` : le
  cerveau ne connaît pas le canal). Sans `TELEGRAM_BOT_TOKEN`, AIfred est en **mode essai** : rien ne
  part, le lien d'invitation s'affiche dans la visio pour l'ouvrir sur un autre téléphone.
- **Les garde-fous** :
  - un envoi ne part qu'après un « oui » de la personne qui a demandé l'envoi, vérifié dans le code
    (`confirmer_envoi`), ou quand cette personne touche Envoyer ;
  - un artisan ou un propriétaire peut parler à AIfred, mais son « oui » ne fait rien partir ;
  - AIfred ne prend une photo que si on lui demande de regarder, vérifié dans le code (`regarder`), ou si la
    personne touche le bouton Photo de sa visio : c'est alors sa propre caméra ;
  - AIfred se présente comme une IA à chaque invité ;
  - il ne lit jamais un numéro à voix haute.

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

Ouvre <http://localhost:8080>, donne ton prénom, « Entrer sur le canal ». Touche AIfred, dis « C'est quoi
ma journée ? » et retouche-le. Puis essaie « Fais venir le plombier ».

Sur un téléphone, la caméra exige une page en HTTPS : en local, passe par un tunnel
(`cloudflared tunnel --url http://localhost:8080`) et mets son adresse dans `AIFRED_PUBLIC_URL`.

## Mettre en ligne sur Bunny Magic Containers

Adresse : <https://aifred.byfloo.com>. Une seule image Docker fait tourner l'agent et le serveur web
(`start.sh`). Bunny ne construit pas l'image : GitHub Actions le fait à chaque push et la range sur
`ghcr.io/lipaonline/aifred` (`.github/workflows/deploy.yml`). Un push sur `main` la met en ligne.

Mise en place, une seule fois :

1. Bunny → **Magic Containers** → **Image Registries** → GitHub : `lipaonline` et un
   [jeton `read:packages`](https://github.com/settings/tokens/new?scopes=read:packages&description=bunny-aifred)
   (le dépôt est privé, l'image aussi).
2. **Add App**, conteneur nommé `app`, image `lipaonline/aifred`, tag : le dernier commit construit.
3. **Une seule région, une seule instance** : la base SQLite est sur le disque du conteneur.
4. **Disque persistant** monté sur `/data` (base et photos). Bunny ne le sauvegarde pas : pour le
   hackathon, ça suffit.
5. **Endpoint CDN** sur le port **8080**, puis le domaine `aifred.byfloo.com` : un CNAME dans la zone
   Bunny DNS vers l'adresse de la pull zone, et le certificat gratuit.
6. Variables : celles de `.env.example`, avec `AIFRED_PUBLIC_URL=https://aifred.byfloo.com` et un
   `AIFRED_TEAM_CODE` (`openssl rand -hex 8`).
7. Dans les réglages du dépôt GitHub : la variable `APP_ID` (id de l'application) et le secret
   `BUNNYNET_API_KEY`, pour que les push sur `main` déploient.
8. Donne à l'équipe le lien `https://aifred.byfloo.com/?equipe=<code>` : le code reste dans le téléphone.

Testé le 31 octobre 2026 : depuis Bunny (région Paris), l'agent rejoint la salle, écoute, cherche dans les
fiches et répond à voix haute. Pas encore essayé depuis un vrai téléphone, ni avec la caméra.

## Telegram pour la démo : le bot AIfred

Pourquoi Telegram plutôt que WhatsApp pour la démo : pas de bac à sable ni de modèle à faire approuver par
Meta, pas de session qui expire, gratuit, et la photo, le texte et le bouton « Rejoindre la visio » partent
dans un seul message.

La limite : **un bot ne peut pas écrire en premier, ni à un numéro de téléphone.** Chaque destinataire doit
avoir Telegram et avoir ouvert le bot une fois (bouton Démarrer). Sinon, AIfred le dit : « Marc n'a pas
encore ouvert le bot AIfred ». Pour de vrais clients, un vrai artisan ne connaîtra pas le bot : voir le
business plan (SMS par Brevo, WhatsApp avec son propre numéro en option), même fonction `send_invitation`.

Mise en place :

1. Dans Telegram, ouvre **@BotFather**, envoie `/newbot`, choisis un nom (« AIfred démo ») et un
   identifiant qui finit par `bot`. BotFather donne un jeton : il va dans `.env` (`TELEGRAM_BOT_TOKEN=`)
   et dans les variables de Bunny, **jamais dans le dépôt ni dans le chat de l'équipe**.
2. Chaque testeur ouvre le bot et touche **Démarrer** (ou écrit « bonjour »).
3. Les identifiants :

   ```bash
   uv run python telegram.py ids    # liste les personnes qui ont écrit au bot, avec leur identifiant
   ```

4. Chaque identifiant va dans le champ `"telegram"` du contact (`AIFRED_DEMO_JSON` sur Bunny, ou
   `demo.example.json` en local avec des identifiants fictifs), puis on repart d'un dossier de données
   vide (`data/aifred.db` supprimé en local, nouveau dossier sur Bunny) pour que le carnet soit rechargé.

La photo et le bouton ne sont joints que si le serveur est en ligne : Telegram va chercher la photo à
l'adresse `AIFRED_PUBLIC_URL` et refuse un bouton vers `localhost` (le lien reste dans le texte).

## Fichiers

| Fichier | Rôle |
| --- | --- |
| `agent.py` | l'agent LiveKit : voix, écoute, caméras, présentation aux invités |
| `tools.py` | les outils d'AIfred, rangés par fonctionnalité |
| `prompt.md` | les instructions d'AIfred |
| `server.py` | page web, jetons LiveKit, liens d'invitation, photos |
| `db.py` | base SQLite ; `demo.example.json` : contacts et maisons fictifs |
| `telegram.py` | envoi de l'invitation par le bot Telegram ; `python telegram.py ids` donne les identifiants |
| `mailer.py` | le rapport de la journée par email, avec Postmark |
| `web/index.html` | la page du téléphone (équipe et invités) |
| `tests/` | les garde-fous et le carnet, testés sans clé ni réseau : `python -m unittest discover -s tests -t .` |
| `Dockerfile`, `start.sh` | l'image pour Bunny |
| `.github/workflows/deploy.yml` | construit l'image, et la met en ligne depuis `main` |

## Limites connues

- AIfred ne sert qu'une personne à la fois.
- Sur le canal, les micros sont ouverts : tout s'entend, à chacun de couper le sien quand c'est bruyant.
- Il n'entend que la personne qui clique : la fiche d'intervention ne contient que ce qu'on lui a dit ainsi.
- Rester sur le canal toute la journée suppose l'écran allumé : sur iPhone, une page web s'endort avec l'écran.
- Le seuil du mandat et les voyageurs sont cachés à l'artisan par une consigne au modèle, pas par le code :
  la fiche est un seul bloc de texte.
- Pas encore de réponse aux messages Telegram entrants.
- Délai mesuré en local avec des voix de synthèse : AIfred dit « Une seconde » environ 1 s après le second clic.
  Claude met ensuite 1,3 s par appel en moyenne.
