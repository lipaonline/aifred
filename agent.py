"""
L'agent AIfred : il entre dans une salle LiveKit, écoute, répond avec la voix Gradium et agit.

    uv run python agent.py dev      # en local, se recharge à chaque modification
    uv run python agent.py start    # en production (start.sh)

Le serveur web demande l'agent « aifred » dans le jeton de l'équipe : LiveKit Cloud l'envoie dans la
salle à sa création. L'agent tourne chez nous (Bunny), pas chez LiveKit.

Deux sortes de salles, où tout le monde parle librement :
- le canal de l'équipe, sans caméra ;
- une visio, où on se voit.

Dans les deux, AIfred dort tant que personne ne l'active. Un clic sur son bouton pour lui parler, un
second clic quand on a fini : c'est là qu'il répond. Il n'écoute que la personne qui a cliqué, et tout le
monde entend la question et la réponse. Gradium ne transcrit qu'entre les deux clics : c'est ce qui permet
de rester connecté sans payer l'écoute.
"""

import asyncio
import json
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
from livekit.plugins import anthropic, gradium, silero

import config
import db
import tools

logger = logging.getLogger("aifred")
# Si on oublie le second clic : il ne reste pas à écouter pour rien
AUTO_OFF_SECONDS = 30
# Gradium rend parfois le dernier mot, surtout un nombre, après avoir annoncé la fin de la phrase
TAIL_SECONDS = 0.8
# Dit dès la fin de la phrase, sans attendre Claude : jamais de gros blanc
ACK_QUESTION = ("Une seconde.", "Un instant.")
ACK_YES = "Je m'en occupe."


class Cameras:
    """AIfred ne reçoit pas de vidéo : il demande une image au téléphone de la personne quand il regarde."""

    def __init__(self, room: rtc.Room):
        self._room = room
        self._waiting: dict[str, asyncio.Future[bytes]] = {}
        self._tasks: set[asyncio.Task] = set()
        room.register_byte_stream_handler(tools.PHOTO_TOPIC, self._on_photo)

    def _on_photo(self, reader: rtc.ByteStreamReader, identity: str):
        task = asyncio.create_task(self._read(reader, identity))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _read(self, reader: rtc.ByteStreamReader, identity: str):
        data = bytearray()
        async for chunk in reader:
            data.extend(chunk)
        waiting = self._waiting.get(identity)
        if waiting and not waiting.done():
            waiting.set_result(bytes(data))

    async def capture(self, identity: str) -> bytes | None:
        waiting = self._waiting[identity] = asyncio.get_running_loop().create_future()
        try:
            await self._room.local_participant.perform_rpc(
                destination_identity=identity, method="photo", payload="", response_timeout=6)
            return await asyncio.wait_for(waiting, timeout=6)
        except (rtc.RpcError, TimeoutError) as e:
            logger.info("pas d'image de %s : %s", identity, e)
            return None
        finally:
            self._waiting.pop(identity, None)


class Aifred(Agent):
    def __init__(self, sujet: str):
        instructions = (config.ROOT / "prompt.md").read_text("utf-8")
        if sujet:
            instructions += f"\nLe sujet de cette visio : {sujet}. Quand on te parle de « cet appartement », c'est de lui qu'il s'agit."
        super().__init__(instructions=instructions, tools=tools.ALL)
        self._acks = 0
        self._finals: list[str] = []
        self._words: list[str] = []

    def new_turn(self):
        self._finals.clear()
        self._words.clear()

    def on_transcript(self, text: str, final: bool):
        if final:
            self._finals.append(text)
            self._words.clear()
        else:
            self._words.append(text)

    async def on_user_turn_completed(self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage) -> None:
        text = new_message.text_content or ""
        # Gradium rend parfois la ponctuation seule : « ? » n'est pas une question
        if not any(c.isalnum() for c in text):
            raise StopResponse()
        if tools.YES_RE.search(db._plain(text)):
            phrase = ACK_YES
        else:
            phrase = ACK_QUESTION[self._acks % len(ACK_QUESTION)]
            self._acks += 1
        self.session.say(phrase, add_to_chat_ctx=False)
        await asyncio.sleep(TAIL_SECONDS)
        # Tout ce que Gradium a rendu entre les deux clics, y compris après la fin annoncée
        whole = " ".join(self._finals + self._words).strip()
        text = whole if len(whole) > len(text.strip()) else text.strip()
        # L'équipe et les invités cliquent tous sur le même bouton : Claude doit savoir à qui il répond
        speaker = self.session.room_io.linked_participant
        if speaker:
            text = f"{speaker.name} ({speaker.attributes.get('role', 'invité')}) : {text}"
        new_message.content = [text]
        logger.debug("entendu : %s", text)


class Salle:
    """Qui a activé AIfred : une seule personne à la fois, la seule qu'il écoute."""

    def __init__(self, room: rtc.Room, session: AgentSession, agent: Aifred):
        self.room = room
        self.session = session
        self.agent = agent
        self.active: str | None = None
        self.last_asker: str | None = None
        self.last_activity = 0.0
        self.heard_speech = False
        self._finishing: asyncio.Task | None = None

    def _name(self, identity: str | None) -> str:
        p = self.room.remote_participants.get(identity or "")
        return (p.name or "Quelqu'un") if p else ""

    def publish(self):
        """L'état est sur les attributs de l'agent : même celui qui arrive après le voit."""
        return asyncio.create_task(self.room.local_participant.set_attributes({
            "pret": "1", "aifred": self.active or "", "aifred_nom": self._name(self.active),
            "dernier": self.last_asker or "", "dernier_nom": self._name(self.last_asker),
        }))

    def activate(self, identity: str) -> dict:
        """Le premier clic. Pendant qu'il répond, seule la personne à qui il répond peut le couper."""
        if self.active and self.active != identity:
            return {"ok": False, "nom": self._name(self.active)}
        if self.session.agent_state in ("thinking", "speaking"):
            # S'il a quitté la salle, plus personne n'attend cette réponse
            if self.last_asker not in (None, identity):
                return {"ok": False, "raison": "AIfred répond, attends un instant."}
            self.session.interrupt()
        self.session.clear_user_turn()
        self.agent.new_turn()
        self.session.room_io.set_participant(identity)
        self.session.input.set_audio_enabled(True)
        self.active = self.last_asker = identity
        self.last_activity = time.time()
        self.heard_speech = False
        self.publish()
        return {"ok": True}

    def deactivate(self, answer: bool = True):
        """Le second clic : c'est lui qui fait répondre AIfred. Sans lui, la phrase reste sans réponse."""
        if not self.active:
            return
        self.active = None
        self.publish()
        if answer:
            self._finishing = asyncio.create_task(self._answer())
        else:
            self.session.input.set_audio_enabled(False)
            self.session.clear_user_turn()

    async def _answer(self):
        # Les derniers mots sont encore en route au moment du clic
        await asyncio.sleep(0.35)
        if self.active:
            return
        self.session.input.set_audio_enabled(False)
        try:
            # Gradium ne rend les derniers mots qu'après un peu de silence : on lui en envoie
            self.session.commit_user_turn(transcript_timeout=2.5, stt_flush_duration=3.0)
        except RuntimeError as e:
            logger.info("phrase non envoyée : %s", e)

    def left(self, identity: str):
        if self.active == identity:
            self.deactivate(answer=False)
        # S'il revient avec la même identité, AIfred doit rouvrir son micro à neuf
        if self.last_asker == identity:
            self.session.room_io.unset_participant()
            self.last_asker = None

    async def watch(self):
        """Coupe AIfred après un silence : il ne reste pas à écouter pour rien."""
        while True:
            await asyncio.sleep(1)
            if not self.active:
                continue
            now = time.time()
            if self.session.user_state == "speaking":
                self.last_activity = now
            if now - self.last_activity > AUTO_OFF_SECONDS:
                # Il a parlé puis oublié le second clic : on répond quand même
                self.deactivate(answer=self.heard_speech)


class Brain(anthropic.LLM):
    """Claude Haiku réfléchit par défaut, et cette réflexion compte dans max_tokens : une limite trop basse
    coupe l'appel d'outil sans erreur. En effort low, il répond plus vite et choisit aussi bien ses outils."""

    def chat(self, **kwargs):
        return super().chat(**{"extra_kwargs": {"output_config": {"effort": "low"}}, **kwargs})


# C'est le second clic qui termine la phrase, pas un silence : on peut prendre le temps de réfléchir.
# On n'interroge pas Claude avant : ça faisait perdre des réponses, et ne gagnait presque rien ici
TURNS = {"turn_detection": "manual", "preemptive_generation": {"enabled": False}}


def ear() -> gradium.STT:
    # Le second clic termine la phrase. Sur 1 s de silence, Gradium la coupait trop tôt et perdait le dernier mot
    return gradium.STT(language="fr", vad_threshold=0.7)


def prewarm(proc: JobProcess):
    proc.userdata["vad"] = silero.VAD.load()


# Le canal et une visio tournent en même temps : deux processus prêts d'avance
server = AgentServer(setup_fnc=prewarm, num_idle_processes=2)


@server.rtc_session(agent_name=config.AGENT_NAME)
async def entrypoint(ctx: JobContext):
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    first = await ctx.wait_for_participant()
    sujet = first.attributes.get("sujet", "")
    agent = Aifred(sujet)
    userdata = tools.Visio(room=ctx.room, cameras=Cameras(ctx.room), sujet=sujet)

    session = AgentSession[tools.Visio](
        stt=ear(),
        llm=Brain(model=config.LLM_MODEL, max_tokens=1024, caching="ephemeral"),
        tts=gradium.TTS(voice_id=config.VOICE_ID),
        vad=ctx.proc.userdata["vad"],
        userdata=userdata,
        # regarder puis preparer_invitation dans la même réponse
        max_tool_steps=5,
        turn_handling=TURNS,
        # Si Claude ne répond pas vite, on réessaie vite : l'attente par défaut est de 10 s par essai
        conn_options=SessionConnectOptions(llm_conn_options=APIConnectOptions(max_retry=2, retry_interval=0.2, timeout=6.0)),
    )
    salle = Salle(ctx.room, session, agent)

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

    @session.on("user_input_transcribed")
    def _on_transcript(ev):
        agent.on_transcript(ev.transcript, ev.is_final)

    @session.on("user_state_changed")
    def _on_user(ev):
        if ev.new_state == "speaking":
            salle.heard_speech = True

    tasks: set[asyncio.Task] = set()

    def _run(coro):
        task = asyncio.create_task(coro)
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    # ---------- Ce que demandent les téléphones ----------

    def _payload(data: rtc.RpcInvocationData) -> dict:
        return json.loads(data.payload) if data.payload else {}

    @ctx.room.local_participant.register_rpc_method("activer")
    async def _activer(data: rtc.RpcInvocationData) -> str:
        if _payload(data).get("on"):
            return json.dumps(salle.activate(data.caller_identity), ensure_ascii=False)
        if salle.active == data.caller_identity:
            salle.deactivate()
        return json.dumps({"ok": True})

    @ctx.room.local_participant.register_rpc_method("valider")
    async def _valider(data: rtc.RpcInvocationData) -> str:
        # La carte Envoyer / Annuler : le toucher vaut le « oui » à voix haute. Jamais celui d'un invité
        caller = ctx.room.remote_participants.get(data.caller_identity)
        pending = userdata.pending
        if (not caller or not pending or caller.attributes.get("role") != "equipe"
                or pending.get("by") not in ("", data.caller_identity)):
            return json.dumps({"ok": False, "raison": "Seule la personne de l'équipe qui a demandé l'envoi peut le valider."}, ensure_ascii=False)
        if _payload(data).get("choix") == "envoyer":
            phrase = await tools.send_pending(userdata, caller.name or "L'équipe")
        else:
            phrase = await tools.cancel_pending(userdata)
        # Il le sait : sinon il redemanderait confirmation pour ce qui est déjà réglé
        memory = agent.chat_ctx.copy()
        memory.add_message(role="user", content=f"(Information de l'application : {caller.name} a répondu en touchant l'écran. "
                                                f"{phrase} Cet envoi est réglé : ne le repropose pas et ne demande plus de confirmation.)")
        await agent.update_chat_ctx(memory)
        session.say(phrase, add_to_chat_ctx=False)
        return json.dumps({"ok": True})

    @ctx.room.local_participant.register_rpc_method("lancer_visio")
    async def _lancer_visio(data: rtc.RpcInvocationData) -> str:
        caller = ctx.room.remote_participants.get(data.caller_identity)
        if not caller or caller.attributes.get("role") != "equipe":
            return json.dumps({"ok": False, "raison": "Seule l'équipe peut lancer une visio."}, ensure_ascii=False)
        body = _payload(data)
        room_name = tools.new_visio_name()
        reason = str(body.get("sujet") or "faire le point")[:120]
        # WhatsApp peut prendre plusieurs secondes : la visio s'ouvre sans l'attendre
        _run(tools.invite_many(userdata, [int(i) for i in body.get("invites", [])], reason, caller.name or "L'équipe", room_name))
        return json.dumps({"ok": True, "room": room_name})

    @ctx.room.local_participant.register_rpc_method("terminer")
    async def _terminer(data: rtc.RpcInvocationData) -> str:
        return json.dumps(await tools.write_report(userdata, session.history), ensure_ascii=False)

    # ---------- La salle ----------

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
        await userdata.send_board()

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

    @ctx.room.on("participant_disconnected")
    def _on_leave(participant: rtc.RemoteParticipant):
        salle.left(participant.identity)

    await session.start(
        agent=agent,
        room=ctx.room,
        room_options=room_io.RoomOptions(
            participant_identity=first.identity,
            text_input=False,
            # Une coupure réseau du membre de l'équipe ne doit pas faire partir AIfred
            close_on_disconnect=False,
        ),
    )
    session.input.set_audio_enabled(False)
    salle.publish()
    _run(_warm_up())
    _run(salle.watch())
    db.log(ctx.room.name, "AIfred est entré dans la salle")


if __name__ == "__main__":
    cli.run_app(server)
