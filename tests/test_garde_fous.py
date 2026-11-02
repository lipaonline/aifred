"""
La règle absolue d'AIfred, vérifiée sans réseau : un envoi ne part jamais sans un oui clair de la
personne de l'équipe qui l'a demandé.

    python -m unittest discover -s tests -t .
"""

import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests import stubs  # noqa: F401  (avant tools : faux livekit, anthropic, aiohttp)

import db  # noqa: E402
import tools  # noqa: E402


class FakeLocal:
    def __init__(self):
        self.sent = []

    async def publish_data(self, payload, topic=None, destination_identities=None):
        self.sent.append(payload)


def person(identity: str, name: str, role: str):
    return SimpleNamespace(identity=identity, name=name, attributes={"role": role}, track_publications={})


SEBASTIEN = person("seb", "Sébastien", "equipe")
PATRICK = person("pat", "Patrick", "equipe")
PATRICE = person("patrice", "Patrice", "artisan")


def visio(room_name: str = "visio-test") -> tools.Visio:
    room = SimpleNamespace(name=room_name, local_participant=FakeLocal(),
                           remote_participants={p.identity: p for p in (SEBASTIEN, PATRICK, PATRICE)})
    return tools.Visio(room=room, cameras=None, anthropic_client=stubs._AsyncAnthropic())


def context(v: tools.Visio, speaker, said: str):
    """Ce qu'AIfred a entendu, avec le « Prénom (rôle) : » qu'ajoute agent.py."""
    turn = SimpleNamespace(role="user", text_content=f"{speaker.name} ({speaker.attributes['role']}) : {said}")
    session = SimpleNamespace(history=SimpleNamespace(items=[turn]), room_io=SimpleNamespace(linked_participant=speaker))
    return SimpleNamespace(userdata=v, session=session)


def run(coro):
    return asyncio.run(coro)


class OuiClair(unittest.TestCase):
    def test_les_oui_qui_comptent(self):
        for said in ["oui", "Oui, vas-y", "ok envoie", "D'accord", "ouais go", "Oui, pas de souci",
                     "confirme", "envoie-le"]:
            with self.subTest(said=said):
                self.assertTrue(tools.is_clear_yes(said))

    def test_les_phrases_qui_ne_font_rien_partir(self):
        for said in ["non", "N'envoie pas", "N’envoie pas encore", "pas d'accord", "oui mais attends",
                     "attendez", "surtout pas", "ce n'est pas ok", "ok, plus tard", "euh", "je ne sais pas, ok ?",
                     "annuler", "jamais", "arrête", "oui, enfin laisse tomber", ""]:
            with self.subTest(said=said):
                self.assertFalse(tools.is_clear_yes(said))


class Envois(unittest.TestCase):
    def setUp(self):
        db.seed()
        self.envoi = patch.object(tools.telegram, "send_invitation",
                                     AsyncMock(return_value={"ok": True, "dry_run": False})).start()
        self.mail = patch.object(tools.mailer, "send_report", AsyncMock(return_value={"ok": True, "dry_run": False})).start()
        self.addCleanup(patch.stopall)
        self.v = visio()

    def prepare(self, by=SEBASTIEN):
        return run(tools.preparer_invitation(context(self.v, by, "Fais venir le chauffagiste"),
                                             qui="chauffagiste", motif="panne de chauffage REF415"))

    def test_preparer_n_envoie_rien(self):
        answer = self.prepare()
        self.assertIn("Rien n'est envoyé", answer)
        self.assertIn("Patrice", answer)
        self.assertEqual(self.v.pending["by"], "seb")
        self.envoi.assert_not_called()

    def test_oui_de_celui_qui_a_demande(self):
        self.prepare()
        answer = run(tools.confirmer_envoi(context(self.v, SEBASTIEN, "Oui, vas-y")))
        self.assertEqual(answer, "C'est envoyé à Patrice.")
        self.envoi.assert_awaited_once()
        self.assertIsNone(self.v.pending)

    def test_un_autre_membre_de_l_equipe_ne_confirme_pas(self):
        self.prepare()
        answer = run(tools.confirmer_envoi(context(self.v, PATRICK, "oui")))
        self.assertIn("Seule la personne qui a demandé", answer)
        self.envoi.assert_not_called()
        self.assertIsNotNone(self.v.pending)

    def test_le_oui_de_l_artisan_ne_compte_pas(self):
        self.prepare()
        answer = run(tools.confirmer_envoi(context(self.v, PATRICE, "oui")))
        self.assertIn("Seul un membre de l'équipe", answer)
        self.envoi.assert_not_called()

    def test_n_envoie_pas(self):
        self.prepare()
        answer = run(tools.confirmer_envoi(context(self.v, SEBASTIEN, "Non, n'envoie pas encore")))
        self.assertIn("pas entendu de oui clair", answer)
        answer = run(tools.confirmer_envoi(context(self.v, SEBASTIEN, "N'envoie pas")))
        self.assertIn("pas entendu de oui clair", answer)
        self.envoi.assert_not_called()

    def test_demande_par_un_invite_tout_le_monde_de_l_equipe_confirme(self):
        self.prepare(by=PATRICE)
        self.assertEqual(self.v.pending["by"], "")
        run(tools.confirmer_envoi(context(self.v, PATRICK, "oui")))
        self.envoi.assert_awaited_once()

    def test_demande_trop_ancienne(self):
        self.prepare()
        self.v.pending["at"] = time.time() - tools.PENDING_SECONDS - 1
        answer = run(tools.confirmer_envoi(context(self.v, SEBASTIEN, "oui")))
        self.assertIn("trop ancienne", answer)
        self.envoi.assert_not_called()

    def test_rien_de_prepare(self):
        answer = run(tools.confirmer_envoi(context(self.v, SEBASTIEN, "oui")))
        self.assertIn("rien à envoyer", answer)
        self.envoi.assert_not_called()
        self.mail.assert_not_called()

    def test_annuler(self):
        self.prepare()
        run(tools.annuler_envoi(context(self.v, SEBASTIEN, "non")))
        answer = run(tools.confirmer_envoi(context(self.v, SEBASTIEN, "oui")))
        self.assertIn("rien à envoyer", answer)
        self.envoi.assert_not_called()

    def test_rapport_seulement_apres_le_oui(self):
        run(tools.preparer_rapport(context(self.v, SEBASTIEN, "C'est quoi ma journée ?")))
        self.mail.assert_not_called()
        run(tools.confirmer_envoi(context(self.v, SEBASTIEN, "pas maintenant")))
        self.mail.assert_not_called()
        self.assertEqual(run(tools.confirmer_envoi(context(self.v, SEBASTIEN, "oui"))), "C'est envoyé.")
        self.mail.assert_awaited_once()

    def test_les_numeros_ne_sont_jamais_dits(self):
        answer = run(tools.preparer_invitation(context(self.v, SEBASTIEN, "Fais venir quelqu'un"),
                                               qui="propriétaire", motif="accord devis"))
        self.assertIn("Plusieurs personnes", answer)
        self.assertNotIn("+33", answer)
        self.assertNotIn("+44", answer)


class Regarder(unittest.TestCase):
    def test_pas_de_photo_sans_demande(self):
        v = visio()
        answer = run(tools.regarder(context(v, SEBASTIEN, "Le chauffage est en panne"), question="panne"))
        self.assertIn("Aucune photo", answer)

    def test_pas_de_camera_sur_le_canal(self):
        v = visio("canal-equipe")
        answer = run(tools.regarder(context(v, SEBASTIEN, "Regarde le message"), question="erreur"))
        self.assertIn("pas de caméra", answer)


if __name__ == "__main__":
    unittest.main()
