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
    AgentServer,
    AgentSession,
    AutoSubscribe,
    JobContext,
    JobProcess,
    StopResponse,
    cli,
    llm,
    room_io,
)
from livekit.agents.utils import images
from livekit.plugins import anthropic, gradium, silero

import config
import db
import tools

logger = logging.getLogger("aifred")
FOLLOW_UP_SECONDS = 20


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

    def _humans(self) -> list[rtc.RemoteParticipant]:
        return [p for p in self._room.remote_participants.values()
                if p.kind != rtc.ParticipantKind.PARTICIPANT_KIND_AGENT]

    def _should_answer(self, text: str) -> bool:
        heard = db._plain(text)
        addressed = "alfred" in heard or "alfre" in heard
        follow_up = time.time() - self.last_reply < FOLLOW_UP_SECONDS
        waiting_yes = self.session.userdata.pending is not None
        # Seul avec AIfred, on lui parle forcément. À plusieurs, on se parle entre humains.
        return len(self._humans()) <= 1 or addressed or follow_up or waiting_yes

    async def on_user_turn_completed(self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage) -> None:
        if not self._should_answer(new_message.text_content or ""):
            raise StopResponse()

    async def heard(self, participant: rtc.RemoteParticipant, text: str) -> None:
        """Une autre personne a parlé. Son identité reste sur le message : confirmer_invitation la vérifie."""
        role = participant.attributes.get("role", "invité")
        message = llm.ChatMessage(role="user", content=[f"{participant.name} ({role}) : {text}"],
                                  extra={"identity": participant.identity})
        if self._should_answer(text):
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
        stt=gradium.STT(language="fr"),
        llm=anthropic.LLM(model=config.LLM_MODEL, max_tokens=400, caching="ephemeral"),
        tts=gradium.TTS(voice_id=config.VOICE_ID),
        vad=ctx.proc.userdata["vad"],
        userdata=tools.Visio(room=ctx.room, cameras=cameras),
        # regarder, chercher_contact puis preparer_invitation dans la même réponse
        max_tool_steps=5,
    )

    @session.on("agent_state_changed")
    def _on_state(ev):
        if ev.old_state == "speaking":
            agent.last_reply = time.time()

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
        ear = ears[participant.identity] = AgentSession(stt=gradium.STT(language="fr"), vad=ctx.proc.userdata["vad"])
        await ear.start(
            agent=Oreilles(agent, participant),
            room=ctx.room,
            room_options=room_io.RoomOptions(
                participant_identity=participant.identity,
                text_input=False,
                audio_output=False,
                close_on_disconnect=False,
            ),
        )

    @ctx.room.on("participant_connected")
    def _on_join(participant: rtc.RemoteParticipant):
        role = participant.attributes.get("role")
        if role in ("artisan", "proprietaire"):
            db.log(ctx.room.name, f"{participant.name} ({role}) est entré dans la visio")
            # Règle : face à un tiers, AIfred se présente comme une IA
            session.say(f"Bonjour {participant.name}. Je suis AIfred, une intelligence artificielle "
                        f"qui assiste l'équipe pendant cette visio.")
        _run(_listen(participant))

    @ctx.room.on("participant_disconnected")
    def _on_leave(participant: rtc.RemoteParticipant):
        ear = ears.pop(participant.identity, None)
        if ear:
            _run(ear.aclose())

    await session.start(
        agent=agent,
        room=ctx.room,
        room_options=room_io.RoomOptions(
            participant_identity=first.identity,
            # Une coupure réseau du membre de l'équipe ne doit pas faire partir AIfred
            close_on_disconnect=False,
        ),
    )
    for participant in ctx.room.remote_participants.values():
        _run(_listen(participant))
    db.log(ctx.room.name, "AIfred est entré dans la visio")
    session.say("Bonjour, c'est AIfred. Je vous écoute.")


if __name__ == "__main__":
    cli.run_app(server)
