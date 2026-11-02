"""
Le carnet des maisons et la journée, sur les données fictives de demo.example.json, dans une base
temporaire. Sans réseau.

    python -m unittest discover -s tests -t .
"""

import asyncio
import json
import unittest

from tests import stubs  # noqa: F401  (avant tools : faux livekit, anthropic, aiohttp)

import db  # noqa: E402
import tools  # noqa: E402
from tests.test_garde_fous import SEBASTIEN, context, visio  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class Carnet(unittest.TestCase):
    def setUp(self):
        db.seed()
        self.v = visio()

    def test_seed_une_seule_fois(self):
        before = len(db.contacts())
        db.seed()
        self.assertEqual(len(db.contacts()), before)

    def test_trouver_la_maison_comme_on_la_dit(self):
        for said in ["REF415", "la réf 415", "l'appartement 415", "ref 415"]:
            with self.subTest(said=said):
                self.assertEqual([h["name"] for h in db.find_houses(said)], ["REF415"])
        self.assertEqual([h["name"] for h in db.find_houses("le chalet des Mélèzes")], ["Chalet Les Mélèzes"])
        self.assertEqual(db.find_houses("une maison inconnue"), [])

    def test_contacts_sans_accents(self):
        self.assertEqual([c["name"] for c in db.search_contacts("electricien")], ["Karim Benali"])
        self.assertEqual([c["name"] for c in db.search_contacts("le chauffagiste")], ["Patrice"])

    def test_fiche_et_procedure(self):
        data = json.loads(run(tools.chercher_dans_la_maison(context(self.v, SEBASTIEN, "chauffage REF415"),
                                                           question="REF415 panne de chauffage")))
        self.assertEqual([m["maison"] for m in data["maisons"]], ["REF415"])
        self.assertIn("Atlantic Alfea", data["maisons"][0]["fiche"])
        self.assertIn("Procédure : panne de chauffage", [k["sujet"] for k in data["procedures_tarifs_questions"]])

    def test_historique_du_meme_equipement_ailleurs(self):
        data = json.loads(run(tools.chercher_interventions(context(self.v, SEBASTIEN, "historique"),
                                                          maison="REF415", equipement="pompe à chaleur", code="E12")))
        self.assertEqual(data["ici"], [])
        self.assertEqual({r["maison"] for r in data["meme_equipement_ailleurs"]}, {"REF208", "REF330"})
        self.assertEqual([r["maison"] for r in data["meme_code_ailleurs"]], ["REF208"])
        self.assertTrue(all(r["par"] == "Patrice" for r in data["meme_equipement_ailleurs"]))

    def test_journee_et_resume(self):
        data = json.loads(run(tools.ma_journee(context(self.v, SEBASTIEN, "c'est quoi ma journée"))))
        self.assertTrue(data["a_dire"].startswith("2 arrivées et 2 départs."))
        self.assertLessEqual(len(data["a_dire"].split()), 25)

    def test_vol(self):
        data = json.loads(run(tools.verifier_vol(context(self.v, SEBASTIEN, "vol"), recherche="AF 66")))
        self.assertEqual(data["voyageur"], "Sarah Connor")

    def test_noter_puis_tableau_regle(self):
        answer = run(tools.noter_intervention(context(self.v, SEBASTIEN, "note-le"), maison="REF415",
                                              equipement="pompe à chaleur", probleme="pression basse",
                                              solution="circuit remis à 1,5 bar", par="Sébastien, guidé par Patrice"))
        self.assertIn("noté", answer)
        self.assertEqual(next(s["status"] for s in db.stays() if s["house"] == "REF415"), "Réglé")
        self.assertIn("REF415", [r["house"] for r in db.interventions()])

    def test_maison_inconnue(self):
        answer = run(tools.changer_statut(context(self.v, SEBASTIEN, "statut"), maison="Atlantide", statut="Prêt"))
        self.assertIn("n'est pas dans le tableau", answer)


if __name__ == "__main__":
    unittest.main()
