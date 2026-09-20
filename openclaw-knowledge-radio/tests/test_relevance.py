"""
Tests for the relevance gate and safe exclusion matching.

Run:  python -m unittest discover -s tests -v      (from openclaw-knowledge-radio/)

NOTE on tests/data/relevance_labels.json: the 30 owner-reported "missed" papers come from
state/missed_papers.json; the negatives were hand-labelled from the Sep 3/6/15/19 digests.
The vocabulary in src/processing/relevance.py was tuned while looking at this set, so the
recall/precision numbers below are IN-SAMPLE.  Treat them as regression protection, not as an
estimate of accuracy on unseen papers.
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.processing import relevance as R  # noqa: E402
from src.processing.rank import _title_has_keyword, apply_relevance_gate, rank_and_limit  # noqa: E402

LABELS = json.loads((ROOT / "tests" / "data" / "relevance_labels.json").read_text(encoding="utf-8"))
LABELS = LABELS.get("rows", LABELS) if isinstance(LABELS, dict) else LABELS

EXCLUDED = ["rat", "mouse", "mice", "murine", "zebrafish", "superconductiv*"]


def item(title, snippet="", **kw):
    d = {"title": title, "snippet": snippet, "source": "test"}
    d.update(kw)
    return d


class TestExclusion(unittest.TestCase):
    def test_rat_does_not_match_inside_words(self):
        for t in [
            "Accurate prediction of protein complexes",
            "Generative design of de novo binders",
            "Rational design of an enzyme",
            "Accelerated directed evolution",
            "Maturation of antibodies in silico",
            "Integration of language models for protein engineering",
        ]:
            self.assertIsNone(R.should_exclude(item(t), EXCLUDED), t)

    def test_rat_still_matches_the_animal(self):
        self.assertEqual(R.should_exclude(item("Behaviour of the rat hippocampus"), EXCLUDED), "rat")
        self.assertEqual(R.should_exclude(item("Rats learn faster"), EXCLUDED), "rat")

    def test_url_is_never_searched(self):
        it = item("Some neutral title", url="https://example.org/rat/mouse/mice")
        self.assertIsNone(R.should_exclude(it, EXCLUDED))

    def test_prefix_star(self):
        self.assertEqual(R.should_exclude(item("Superconductivity in twisted bilayers"), EXCLUDED), "superconductiv*")

    def test_topical_exclusion_overridden_by_strong_protein_design_signal(self):
        t = "Humanization of the mouse immunoglobulin loci enables therapeutic antibody discovery"
        self.assertIsNone(R.should_exclude(item(t), EXCLUDED))

    def test_hard_exclusions_always_win(self):
        t = "Author Correction: AlphaFold-based antibody design"
        self.assertEqual(R.should_exclude(item(t), EXCLUDED), "author correction")


class TestTermMatching(unittest.TestCase):
    def test_multiword_terms_match_space_and_hyphen(self):
        self.assertTrue(R._hits("Protein diffusion models for design", ["protein diffusion model*"]))
        self.assertTrue(R._hits("The CDR-H3 loop", ["cdr-h3"]))
        self.assertTrue(R._hits("The CDR H3 loop", ["cdr-h3"]))
        self.assertTrue(R._hits("co-folding of complexes", ["co-folding"]))

    def test_infix_wildcard(self):
        self.assertTrue(R._hits("Protein optimisation", ["protein optimi*ation"]))
        self.assertTrue(R._hits("Protein optimization", ["protein optimi*ation"]))

    def test_whole_word_not_substring(self):
        self.assertFalse(R._hits("Deep Boltzmann machines", ["boltz"]))
        self.assertTrue(R._hits("Boltz-2 predicts affinity", ["boltz"]))
        self.assertFalse(R._hits("AGI and the future of work", ["ai"]))

    def test_landmark_title_keyword(self):
        self.assertFalse(_title_has_keyword("Deep Boltzmann Machines", "Boltz"))
        self.assertFalse(_title_has_keyword("3D chromatin remodeling", "Chroma"))
        self.assertTrue(_title_has_keyword("AlphaFold3 and beyond", "AlphaFold"))
        self.assertTrue(_title_has_keyword("Boltz-2 predicts affinity", "Boltz"))
        self.assertTrue(_title_has_keyword("Advances in structure predictions", "structure prediction"))


class TestScoring(unittest.TestCase):
    def test_off_topic_examples_fall_below_gate(self):
        for t in [
            "Machine learning for functional outcome prediction after vestibular schwannoma surgery",
            "Tetrix: A novel Tetris-based paradigm for neuroimaging research",
            "3D chromatin remodeling during domestication defines novel targets for crop improvement",
            "Cross-Block Conditioning in Deep Boltzmann Machines for Statistical Data Fusion",
        ]:
            self.assertLess(R.score_text(t)["score"], R.thresholds({})["min_score"], t)

    def test_on_topic_examples_pass_gate(self):
        for t in [
            "De Novo Design of Miniprotein Inhibitors of Bacterial Adhesins",
            "PHASE: encoding global protein ensembles with local Hamiltonians",
            "Predicting non-specific binding of VHHs using machine learning models",
        ]:
            self.assertGreaterEqual(R.score_text(t)["score"], R.thresholds({})["min_score"], t)

    def test_disabled_gate_passes_everything(self):
        cfg = {"relevance": {"enabled": False}}
        self.assertTrue(R.passes_gate(item("Tetris neuroimaging"), cfg))

    def test_protected_items_bypass_gate(self):
        self.assertTrue(R.passes_gate(item("Tetris neuroimaging"), None, protected=True))


class TestLabelledSet(unittest.TestCase):
    """In-sample regression checks (see module docstring)."""

    def _scores(self):
        th = R.thresholds({})["min_score"]
        return [(r, R.score_text(r["title"], r.get("snippet", ""))["score"] >= th) for r in LABELS]

    def test_recall_on_positives(self):
        pos = [(r, ok) for r, ok in self._scores() if r["label"]]
        hit = sum(1 for _, ok in pos if ok)
        # 50/51 today; the one miss is a generic "molecular ML" essay that has no protein wording at all.
        self.assertGreaterEqual(hit / len(pos), 0.95, [r["title"] for r, ok in pos if not ok])

    def test_precision_on_negatives(self):
        neg = [(r, ok) for r, ok in self._scores() if not r["label"]]
        false_pos = [r["title"] for r, ok in neg if ok]
        self.assertLessEqual(len(false_pos), 1, false_pos)


class TestRankIntegration(unittest.TestCase):
    CFG = {"limits": {"max_items_total": 20, "max_items_protein": 18}, "ranking": {}}

    def test_gate_drops_off_topic_and_keeps_order_by_relevance(self):
        items = [
            item("Tetrix: a Tetris paradigm for neuroimaging", bucket="protein", tags=["journal"]),
            item("De novo binder design with RFdiffusion and ProteinMPNN", bucket="protein", tags=["journal"]),
            item("A study of protein language model embeddings", bucket="protein", tags=["journal"]),
        ]
        out = rank_and_limit(items, self.CFG)
        titles = [i["title"] for i in out]
        self.assertNotIn("Tetrix: a Tetris paradigm for neuroimaging", titles)
        self.assertEqual(titles[0], "De novo binder design with RFdiffusion and ProteinMPNN")

    def test_blog_off_topic_is_dropped_but_researcher_feed_is_protected(self):
        blog = item("AGI is coming and it changes everything", bucket="protein", tags=["author"], source="Some Blog")
        feed = item("A note on notation", bucket="protein", tags=["author"], source="Jane Doe (arXiv)")
        kept, dropped = apply_relevance_gate([blog, feed], self.CFG)
        self.assertEqual([k["title"] for k in kept], ["A note on notation"])
        self.assertEqual([d["title"] for d in dropped], ["AGI is coming and it changes everything"])

    def test_quiet_input_returns_fewer_than_quota(self):
        items = [item(f"Irrelevant paper {i} on soil pollution", bucket="protein") for i in range(30)]
        self.assertEqual(rank_and_limit(items, self.CFG), [])


if __name__ == "__main__":
    unittest.main()
