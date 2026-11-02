"""
Faux modules pour tester le cerveau sans rien installer ni appeler : livekit, anthropic et aiohttp
sont remplacés par des coquilles vides. Aucune clé, aucun réseau, aucun crédit consommé.
La base SQLite va dans un dossier temporaire, jamais dans data/.

À importer avant tools, db ou config.
"""

import os
import sys
import tempfile
import types
from pathlib import Path

DATA_DIR = Path(tempfile.mkdtemp(prefix="aifred-tests-"))
os.environ["AIFRED_DATA_DIR"] = str(DATA_DIR)
# Jamais les vrais numéros des testeurs : les données fictives du dépôt
os.environ.pop("AIFRED_DEMO_JSON", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _module(name: str, **attrs) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module
    return module


class _Generic:
    def __class_getitem__(cls, _item):
        return cls


class _Rtc:
    class Room: ...
    class RemoteParticipant: ...
    class RpcError(Exception): ...
    class TrackSource:
        SOURCE_CAMERA = "camera"


class _Llm:
    class ChatMessage: ...
    class ChatContext: ...


class _AsyncAnthropic:
    """Si un test appelait Claude par erreur, il échouerait ici au lieu de consommer des crédits."""
    def __init__(self, *args, **kwargs):
        self.messages = self

    async def create(self, *args, **kwargs):
        raise AssertionError("Appel à Claude interdit dans les tests")


def install():
    """Toujours les coquilles, même si les vraies dépendances sont installées : les outils restent de
    simples fonctions qu'on appelle, et rien ne peut partir sur le réseau."""
    livekit = _module("livekit")
    livekit.rtc = _module("livekit.rtc", **{k: v for k, v in vars(_Rtc).items() if not k.startswith("_")})
    livekit.agents = _module("livekit.agents", RunContext=_Generic, function_tool=lambda f: f,
                             llm=_module("livekit.agents.llm", ChatMessage=_Llm.ChatMessage,
                                         ChatContext=_Llm.ChatContext))
    _module("anthropic", AsyncAnthropic=_AsyncAnthropic, APIError=type("APIError", (Exception,), {}))
    _module("aiohttp")


install()
