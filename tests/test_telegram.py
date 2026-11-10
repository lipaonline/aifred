"""
L'envoi par Telegram, sans réseau : l'appel à l'API des bots est remplacé par un faux. On vérifie le
message (photo, texte, bouton), le mode essai sans jeton, la personne qui n'a pas ouvert le bot, et que
le garde-fou du oui reste devant l'envoi.

    python -m unittest discover -s tests -t .
"""

import unittest
from unittest.mock import AsyncMock, patch

from tests import stubs  # noqa: F401  (avant tools : faux livekit, anthropic, aiohttp)

import config  # noqa: E402
import db  # noqa: E402
import telegram  # noqa: E402
import tools  # noqa: E402
from tests.test_garde_fous import PATRICK, SEBASTIEN, context, run, visio  # noqa: E402

LINK = "https://aifred.example/v/abc123"
PARAMS = ["Marc", "Sébastien", "fuite sous l'évier", LINK]


class Message(unittest.TestCase):
    def setUp(self):
        patch.object(config, "TELEGRAM_BOT_TOKEN", "jeton-de-test").start()
        self.call = patch.object(telegram, "_call", AsyncMock(return_value=(True, {"ok": True}))).start()
        self.addCleanup(patch.stopall)

    def sent(self):
        method, body = self.call.await_args.args
        return method, body

    def test_photo_texte_et_bouton_dans_un_seul_message(self):
        result = run(telegram.send_invitation("1000000001", PARAMS, "https://aifred.example/photos/p.jpg"))
        self.assertEqual(result, {"ok": True, "dry_run": False})
        self.call.assert_awaited_once()
        method, body = self.sent()
        self.assertEqual(method, "sendPhoto")
        self.assertEqual(body["chat_id"], "1000000001")
        self.assertEqual(body["photo"], "https://aifred.example/photos/p.jpg")
        self.assertIn("Bonjour Marc, Sébastien vous demande", body["caption"])
        self.assertIn("AIfred, un assistant IA", body["caption"])
        button = body["reply_markup"]["inline_keyboard"][0][0]
        self.assertEqual(button, {"text": "Rejoindre la visio", "url": LINK})

    def test_sans_photo_un_message_texte_avec_le_bouton(self):
        run(telegram.send_invitation("1000000001", PARAMS, None))
        method, body = self.sent()
        self.assertEqual(method, "sendMessage")
        self.assertIn(LINK, body["text"])
        self.assertIn("AIfred, un assistant IA", body["text"])
        self.assertIn("reply_markup", body)

    def test_en_local_pas_de_photo_ni_de_bouton_vers_localhost(self):
        params = PARAMS[:3] + ["http://localhost:8080/v/abc123"]
        run(telegram.send_invitation("1000000001", params, "http://localhost:8080/photos/p.jpg"))
        method, body = self.sent()
        self.assertEqual(method, "sendMessage")
        self.assertNotIn("reply_markup", body)
        self.assertIn("http://localhost:8080/v/abc123", body["text"])

    def test_bot_pas_encore_ouvert(self):
        for answer in [{"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked by the user"},
                       {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}]:
            with self.subTest(answer=answer["description"]):
                self.call.return_value = (False, answer)
                result = run(telegram.send_invitation("1000000001", PARAMS, None))
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"], "Marc n'a pas encore ouvert le bot AIfred")

    def test_contact_sans_identifiant_telegram(self):
        result = run(telegram.send_invitation("", PARAMS, None))
        self.assertFalse(result["ok"])
        self.assertIn("pas d'identifiant Telegram", result["error"])
        self.call.assert_not_called()


class ModeEssai(unittest.TestCase):
    def test_sans_jeton_rien_ne_part(self):
        with patch.object(config, "TELEGRAM_BOT_TOKEN", ""), patch.object(telegram, "_call", AsyncMock()) as call:
            result = run(telegram.send_invitation("1000000001", PARAMS, None))
        self.assertEqual(result, {"ok": True, "dry_run": True})
        call.assert_not_called()


class GardeFou(unittest.TestCase):
    """Le vrai send_invitation, seul l'appel réseau est faux : le oui reste devant l'envoi."""

    def setUp(self):
        db.seed()
        patch.object(config, "TELEGRAM_BOT_TOKEN", "jeton-de-test").start()
        self.call = patch.object(telegram, "_call", AsyncMock(return_value=(True, {"ok": True}))).start()
        self.addCleanup(patch.stopall)
        self.v = visio()
        run(tools.preparer_invitation(context(self.v, SEBASTIEN, "Fais venir le chauffagiste"),
                                      qui="chauffagiste", motif="panne de chauffage REF415"))

    def test_rien_ne_part_sans_oui(self):
        run(tools.confirmer_envoi(context(self.v, SEBASTIEN, "N'envoie pas")))
        run(tools.confirmer_envoi(context(self.v, PATRICK, "oui")))
        self.call.assert_not_called()

    def test_le_oui_envoie_au_bon_identifiant(self):
        answer = run(tools.confirmer_envoi(context(self.v, SEBASTIEN, "Oui, vas-y")))
        self.assertTrue(answer.startswith("C'est envoyé"))
        method, body = self.call.await_args.args
        contact = db.search_contacts("chauffagiste")[0]
        self.assertEqual(body["chat_id"], contact["telegram"])
        self.assertTrue(contact["telegram"])

    def test_bot_pas_ouvert_le_dit_clairement(self):
        self.call.return_value = (False, {"ok": False, "error_code": 403, "description": "Forbidden"})
        answer = run(tools.confirmer_envoi(context(self.v, SEBASTIEN, "oui")))
        self.assertIn("n'a pas encore ouvert le bot AIfred", answer)


if __name__ == "__main__":
    unittest.main()
