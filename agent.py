"""
L'agent AIfred : il entre dans la visio LiveKit, écoute, répond avec la voix Gradium et agit.

    uv run python agent.py dev      # en local, se recharge à chaque modification
    uv run python agent.py start    # en production (start.sh)

Le serveur web demande l'agent « aifred » dans le jeton du premier membre de l'équipe : LiveKit
Cloud l'envoie dans la salle à sa création. L'agent tourne chez nous (Bunny), pas chez LiveKit.

AIfred écoute tout le monde. La première personne de l'équipe passe par la session principale ;
chaque autre personne a sa session d'écoute (Oreilles), qui lui transmet ce qu'elle entend en disant
qui a parlé. Quand plusieurs humains sont là, il ne répond que si on l'appelle « Alfred », ou dans
les 20 secondes qui suivent sa dernière réponse.
"""

import asyncio
import logging
import time

from livekit import rtc
from livekit.agents import (
    Agent,
    APIConnectOptions,
    AgentServer,
    AgentSession,
    AutoSubscribe,
    JobContext,
    JobProcess,
    StopResponse,
    cli,
    llm,
    SessionConnectOptions,
    room_io,
)
from livekit.agents.utils import images
from livekit.plugins import anthropic, gradium, silero

import config
import db
import tools

logger = logging.getLogger("aifred")
FOLLOW_UP_SECONDS = 20
# Dit dès la fin de la phrase, sans attendre Claude : jamais de gros blanc
ACK_QUESTION = ("Je regarde.", "Un instant.")
ACK_YES = "Je m'en occupe."


class Cameras:
    """Garde la dernière image de chaque caméra de la salle, pour les photos à la demande."""

    def __init__(self, room: rtc.Room):
        self._frames: dict[str, rtc.VideoFrame] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        room.on("track_subscribed", self._on_track)

    def _on_track(self, track: rtc.Track, publication: rtc.RemoteTrackPublication,
                  participant: rtc.RemoteParticipant):
        if track.kind != rtc.TrackKind.KIND_VIDEO or publication.source != rtc.TrackSource.SOURCE_CAMERA:
            return
        old = self._tasks.pop(participant.identity, None)
        if old:
            old.cancel()
        self._tasks[participant.identity] = asyncio.create_task(self._read(track, participant.identity))

    async def _read(self, track: rtc.Track, identity: str):
        # capacity=1 : on ne garde que l'image la plus récente
        stream = rtc.VideoStream(track, capacity=1)
        try:
            async for event in stream:
                self._frames[identity] = event.frame
        finally:
            self._frames.pop(identity, None)
            await stream.aclose()

    def snapshot(self, prefer: str | None = None) -> bytes | None:
        frame = self._frames.get(prefer) if prefer else None
        frame = frame or next(iter(self._frames.values()), None)
        if frame is None:
            return None
        return images.encode(frame, images.EncodeOptions(
            format="JPEG",
            quality=80,
            resize_options=images.ResizeOptions(width=1280, height=1280, strategy="scale_aspect_fit"),
        ))


class Aifred(Agent):
    def __init__(self, room: rtc.Room):
        super().__init__(instructions=(config.ROOT / "prompt.md").read_text("utf-8"), tools=tools.ALL)
        self._room = room
        self.last_reply = 0.0
        self.asker: str | None = None       # la personne à qui il vient de répondre
        self._acks = 0

    def _humans(self) -> list[rtc.RemoteParticipant]:
        return [p for p in self._room.remote_participants.values()
                if p.kind != rtc.ParticipantKind.PARTICIPANT_KIND_AGENT]

    def _should_answer(self, text: str, speaker: rtc.RemoteParticipant | None) -> bool:
        """Seul avec AIfred, on lui parle forcément. À plusieurs, on se parle entre humains : il répond si on
        l'appelle, ou s'il attend la suite de la personne à qui il vient de répondre."""
        heard = db._plain(text)
        identity = speaker.identity if speaker else None
        humans = self._humans()
        if len(humans) <= 1 or "alfred" in heard or "alfre" in heard:
            self.asker = identity
            return True
        words = heard.split()
        talks_to_someone = words and any(words[0].strip(",.!?") == db._plain(p.name or " ").split()[0] for p in humans)
        if talks_to_someone:
            return False
        still_talking = self.session.agent_state != "listening"
        follow_up = identity == self.asker and (still_talking or time.time() - self.last_reply < FOLLOW_UP_SECONDS)
        waiting_yes = (self.session.userdata.pending is not None and speaker is not None
                       and speaker.attributes.get("role") == "equipe" and bool(tools.YES_RE.search(heard) or tools.NO_RE.search(heard)))
        return follow_up or waiting_yes

    def _ack(self, text: str):
        if tools.YES_RE.search(db._plain(text)):
            phrase = ACK_YES
        else:
            phrase = ACK_QUESTION[self._acks % len(ACK_QUESTION)]
            self._acks += 1
        self.session.say(phrase, add_to_chat_ctx=False)

    async def on_user_turn_completed(self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage) -> None:
        text = new_message.text_content or ""
        if not self._should_answer(text, self.session.room_io.linked_participant):
            raise StopResponse()
        self._ack(text)

    async def heard(self, participant: rtc.RemoteParticipant, text: str) -> None:
        """Une autre personne a parlé. Son identité reste sur le message : confirmer_envoi la vérifie."""
        role = participant.attributes.get("role", "invité")
        message = llm.ChatMessage(role="user", content=[f"{participant.name} ({role}) : {text}"],
                                  extra={"identity": participant.identity})
        if self._should_answer(text, participant):
            self._ack(text)
            self.session.generate_reply(user_input=message)
        else:
            # Il ne répond pas, mais il retient : « qu'est-ce que Marc vient de dire ? »
            chat_ctx = self.chat_ctx.copy()
            chat_ctx.items.append(message)
            await self.update_chat_ctx(chat_ctx)


class Oreilles(Agent):
    """La session d'écoute d'une personne en plus de la première : ni voix ni cerveau."""

    def __init__(self, aifred: Aifred, participant: rtc.RemoteParticipant):
        super().__init__(instructions="Transcription seulement.")
        self._aifred = aifred
        self._participant = participant

    async def on_user_turn_completed(self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage) -> None:
        await self._aifred.heard(self._participant, new_message.text_content or "")
        raise StopResponse()


# La fin de phrase est le plus gros poste d'attente : on la décide sur la voix, sans le plafond par défaut de 3 s
class Brain(anthropic.LLM):
    """Claude Haiku réfléchit par défaut, et cette réflexion compte dans max_tokens : une limite trop basse
    coupe l'appel d'outil sans erreur. En effort low, il répond plus vite et choisit aussi bien ses outils."""

    def chat(self, **kwargs):
        return super().chat(**{"extra_kwargs": {"output_config": {"effort": "low"}}, **kwargs})


# On n'interroge pas Claude avant la fin de la phrase : ça faisait perdre des réponses, et ne gagnait presque rien ici
TURNS = {"turn_detection": "vad", "endpointing": {"min_delay": 0.2, "max_delay": 1.5},
         "preemptive_generation": {"enabled": False}}


def ear() -> gradium.STT:
    # Gradium juge la fin de phrase sur 1 s de silence probable au lieu de 2 s
    return gradium.STT(language="fr", vad_bucket=1, vad_threshold=0.7)


def prewarm(proc: JobProcess):
    proc.userdata["vad"] = silero.VAD.load()


# Un seul processus prêt d'avance : assez pour la démo, et la mémoire du conteneur reste petite
server = AgentServer(setup_fnc=prewarm, num_idle_processes=1)


@server.rtc_session(agent_name=config.AGENT_NAME)
async def entrypoint(ctx: JobContext):
    cameras = Cameras(ctx.room)
    await ctx.connect(auto_subscribe=AutoSubscribe.SUBSCRIBE_ALL)
    first = await ctx.wait_for_participant()
    agent = Aifred(ctx.room)

    session = AgentSession[tools.Visio](
        stt=ear(),
        llm=Brain(model=config.LLM_MODEL, max_tokens=1024, caching="ephemeral"),
        tts=gradium.TTS(voice_id=config.VOICE_ID),
        vad=ctx.proc.userdata["vad"],
        userdata=tools.Visio(room=ctx.room, cameras=cameras),
        # regarder puis preparer_invitation dans la même réponse
        max_tool_steps=5,
        turn_handling=TURNS,
        # Si Claude ne répond pas vite, on réessaie vite : l'attente par défaut est de 10 s par essai
        conn_options=SessionConnectOptions(llm_conn_options=APIConnectOptions(max_retry=2, retry_interval=0.2, timeout=6.0)),
    )

    @session.on("agent_state_changed")
    def _on_state(ev):
        if ev.old_state == "speaking":
            agent.last_reply = time.time()

    # Le délai entre la fin d'une phrase et le début de la réponse, pour la fiche technique
    @session.on("metrics_collected")
    def _on_metrics(ev):
        m = ev.metrics
        if m.type == "eou_metrics":
            logger.info("délai fin de phrase %.2f s, transcription %.2f s", m.end_of_utterance_delay, m.transcription_delay)
        elif m.type == "llm_metrics":
            logger.info("délai Claude %.2f s, durée %.2f s", m.ttft, m.duration)
        elif m.type == "tts_metrics":
            logger.info("délai voix %.2f s", m.ttfb)

    ears: dict[str, AgentSession] = {}
    tasks: set[asyncio.Task] = set()

    def _run(coro):
        task = asyncio.create_task(coro)
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    async def _listen(participant: rtc.RemoteParticipant):
        if (participant.identity == first.identity or participant.identity in ears
                or participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT):
            return
        session_ear = ears[participant.identity] = AgentSession(stt=ear(), vad=ctx.proc.userdata["vad"], turn_handling=TURNS)
        await session_ear.start(
            agent=Oreilles(agent, participant),
            room=ctx.room,
            room_options=room_io.RoomOptions(
                participant_identity=participant.identity,
                text_input=False,
                audio_output=False,
                close_on_disconnect=False,
            ),
        )

    async def _warm_up():
        # La première requête à Claude est la plus lente (plusieurs secondes si les outils ont changé) :
        # on la fait avant que quelqu'un parle, avec une longue marge
        warm = agent.chat_ctx.copy()
        warm.add_message(role="user", content="Test de connexion, réponds juste ok.")
        try:
            async with session.llm.chat(chat_ctx=warm, tools=agent.tools,
                                        conn_options=APIConnectOptions(max_retry=1, timeout=30.0)) as stream:
                async for _ in stream:
                    break
        except Exception as e:
            logger.warning("préchauffage de Claude raté : %s", e)

    async def _show_board():
        # Le temps que sa page soit prête à recevoir le tableau
        await asyncio.sleep(2)
        await session.userdata.send_board()

    @ctx.room.on("participant_connected")
    def _on_join(participant: rtc.RemoteParticipant):
        role = participant.attributes.get("role")
        if role in ("artisan", "proprietaire"):
            db.log(ctx.room.name, f"{participant.name} ({role}) est entré dans la visio")
            # Règle : face à un tiers, AIfred se présente comme une IA
            session.say(f"Bonjour {participant.name}. Je suis AIfred, une intelligence artificielle "
                        f"qui assiste l'équipe pendant cette visio.")
        elif role == "equipe":
            _run(_show_board())
        _run(_listen(participant))

    @ctx.room.on("participant_disconnected")
    def _on_leave(participant: rtc.RemoteParticipant):
        session_ear = ears.pop(participant.identity, None)
        if session_ear:
            _run(session_ear.aclose())

    await session.start(
        agent=agent,
        room=ctx.room,
        room_options=room_io.RoomOptions(
            participant_identity=first.identity,
            # Une coupure réseau du membre de l'équipe ne doit pas faire partir AIfred
            close_on_disconnect=False,
        ),
    )
    _run(_warm_up())
    for participant in ctx.room.remote_participants.values():
        _run(_listen(participant))
    db.log(ctx.room.name, "AIfred est entré dans la visio")
    session.say("Bonjour, c'est AIfred. Je vous écoute.")


if __name__ == "__main__":
    cli.run_app(server)
