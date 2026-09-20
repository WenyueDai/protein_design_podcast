"""Tests for tools/auto_deepdive.select_items (no network)."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import auto_deepdive as A  # noqa: E402


def it(title, hl=False):
    return {"title": title, "url": "http://x/" + title[:8], "one_liner": "", "highlighted": hl}


ITEMS = [
    it("Tetrix: a Tetris paradigm for neuroimaging", True),          # old pipeline would have added this
    it("De novo design of miniprotein binders with RFdiffusion"),
    it("ProteinMPNN improves antibody sequence design"),
    it("Soil pollution and crop yields"),
    it("AlphaFold3 protein structure prediction of antibody complexes"),
]


class TestSelectItems(unittest.TestCase):
    def test_only_relevant_papers_are_added(self):
        titles = [i["title"] for i in A.select_items(ITEMS, {})]
        self.assertNotIn("Tetrix: a Tetris paradigm for neuroimaging", titles)
        self.assertNotIn("Soil pollution and crop yields", titles)
        self.assertEqual(len(titles), 3)

    def test_cap_per_day(self):
        cfg = {"auto_deepdive": {"max_per_day": 2}}
        self.assertEqual(len(A.select_items(ITEMS, cfg)), 2)

    def test_legacy_behaviour_can_be_restored(self):
        cfg = {"auto_deepdive": {"only_relevant": False}}
        self.assertEqual(len(A.select_items(ITEMS, cfg)), len(ITEMS))


if __name__ == "__main__":
    unittest.main()
