"""
Déclarer un incident à la voix (retour de Simon, entretien n° 3) : la fiche se crée avec la maison, le
problème, l'urgence et la prochaine arrivée, et l'artisan est proposé sans être contacté. Sans réseau.

    python -m unittest discover -s tests -t .
"""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from tests import stubs  # noqa: F401  (avant tools : faux livekit, anthropic, aiohttp)

import db  # noqa: E402
import tools  # noqa: E402
from tests.test_garde_fous import PATRICE, SEBASTIEN, context, visio  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class Incident(unittest.TestCase):
    def setUp(self):
        db.seed()
        self.whatsapp = patch.object(tools.whatsapp, "send_invitation", AsyncMock(return_value={"ok": True, "dry_run": False})).start()
        self.addCleanup(patch.stopall)
        self.v = visio()

    def declare(self, maison, probleme, urgence="", by=SEBASTIEN):
        said = f"Il y a {probleme} à {maison}"
        return run(tools.declarer_incident(context(self.v, by, said), maison=maison, probleme=probleme, urgence=urgence))

    def test_fiche_creee_avec_arrivee_du_jour(self):
        data = json.loads(self.declare("REF415", "fuite sous l'évier"))
        fiche = data["fiche"]
        self.assertEqual(fiche["maison"], "REF415")
        self.assertEqual(fiche["prochaine_arrivee"], "aujourd'hui à 11:00")
        self.assertEqual(fiche["declare_par"], "Sébastien")
        self.assertEqual(fiche["statut"], "ouvert")
        self.assertTrue(data["a_dire"].startswith("Fiche créée pour REF415"))
        self.assertLessEqual(len(data["a_dire"].split()), 15)
        saved = db.incidents()[0]
        self.assertEqual((saved["house"], saved["problem"], saved["status"]), ("REF415", "fuite sous l'évier", "ouvert"))

    def test_urgence_haute_avant_une_arrivee_du_jour(self):
        # Même dite « basse » : des voyageurs arrivent aujourd'hui
        self.assertEqual(json.loads(self.declare("REF415", "ampoule grillée", urgence="basse"))["fiche"]["urgence"], "haute")

    def test_urgence_dite_sans_arrivee_connue(self):
        data = json.loads(self.declare("Villa Les Oyats", "portail bloqué", urgence="basse"))
        self.assertEqual(data["fiche"]["urgence"], "basse")
        self.assertEqual(data["fiche"]["prochaine_arrivee"], "inconnue")
        self.assertEqual(json.loads(self.declare("REF127", "volet cassé"))["fiche"]["urgence"], "normale")

    def test_artisan_propose_sans_contact(self):
        data = json.loads(self.declare("REF415", "fuite du mitigeur de la douche"))
        self.assertEqual(data["artisan_a_proposer"], {"nom": "Marc Dubois", "metier": "plombier"})
        self.assertNotIn("phone", json.dumps(data))
        self.whatsapp.assert_not_awaited()
        self.assertIsNone(self.v.pending)

    def test_artisan_deja_venu_dans_la_maison(self):
        data = json.loads(self.declare("REF208", "le chauffage ne marche plus"))
        self.assertEqual(data["artisan_a_proposer"]["nom"], "Patrice")

    def test_pas_d_artisan_si_le_metier_est_inconnu(self):
        data = json.loads(self.declare("REF127", "volet cassé"))
        self.assertIsNone(data["artisan_a_proposer"])

    def test_tableau_du_jour_mis_a_jour(self):
        self.declare("REF415", "fuite sous l'évier")
        self.assertEqual(next(s["status"] for s in db.stays() if s["house"] == "REF415"), "Incident signalé")

    def test_maison_inconnue(self):
        before = len(db.incidents())
        self.assertIn("Je ne connais pas la maison", self.declare("Atlantide", "fuite"))
        self.assertEqual(len(db.incidents()), before)

    def test_probleme_manquant(self):
        before = len(db.incidents())
        self.assertIn("Il manque le problème", self.declare("REF415", "   "))
        self.assertEqual(len(db.incidents()), before)

    def test_un_invite_ne_declare_pas(self):
        before = len(db.incidents())
        self.assertIn("Seul un membre de l'équipe", self.declare("REF415", "fuite", by=PATRICE))
        self.assertEqual(len(db.incidents()), before)


if __name__ == "__main__":
    unittest.main()
