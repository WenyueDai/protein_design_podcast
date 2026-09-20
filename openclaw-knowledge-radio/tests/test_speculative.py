"""Tests for tools/speculative_ideas.py (no network, LLM is mocked)."""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import speculative_ideas as S  # noqa: E402

POOL_TITLES = [
    "De novo design of miniprotein binders with RFdiffusion",
    "ProteinMPNN improves antibody sequence design",
    "Protein language model predicts variant effects",
    "Directed evolution with machine learning for enzyme engineering",
    "AlphaFold3 complexes for antibody-antigen docking",
    "Inverse folding of de novo enzymes",
]


def pool():
    return [
        {"id": f"P{i:02d}", "date": "2026-09-1%d" % (i % 9), "title": t, "one_liner": "summary " + t,
         "tags": ["journal"], "highlighted": i < 3, "source": "x", "url": ""}
        for i, t in enumerate(POOL_TITLES, 1)
    ]


def idea(n, ids, weird, title=None):
    return f"""## Idea {n}: {title or 'A specific evocative idea number ' + str(n)}

**Inspired by:** {ids}

**The speculative question:** What if we could design X?

**What the papers showed:** Something stated in the summary.

**The leap:** Where it goes.

**First real experiment:** Do a small thing.

**Weirdness:** {weird}/5

---
"""


def good_answer():
    ws = [1, 2, 3, 4, 5, 2]
    ids = ["P01", "P02, P03", "P04", "P05", "P06", "P01, P06"]
    return "\n".join(idea(i + 1, ids[i], ws[i]) for i in range(6)) + "\n## Meta-observation\n\nA paragraph.\n"


class TestValidation(unittest.TestCase):
    def test_good_answer_passes(self):
        valid, problems, meta = S.validate(good_answer(), pool())
        self.assertEqual(problems, [])
        self.assertEqual(len(valid), 6)
        self.assertTrue(meta)

    def test_unknown_paper_id_is_rejected(self):
        ans = good_answer().replace("P04", "P77")
        valid, problems, _ = S.validate(ans, pool())
        self.assertEqual(len(valid), 5)
        self.assertTrue(any("P77" in p for p in problems))

    def test_titles_instead_of_ids_are_rejected(self):
        ans = good_answer().replace("**Inspired by:** P01", "**Inspired by:** Some invented paper title")
        _, problems, _ = S.validate(ans, pool())
        self.assertTrue(any("cites no paper IDs" in p for p in problems))

    def test_weirdness_spread_required_for_six_or_more(self):
        ans = "\n".join(idea(i + 1, "P01", 3) for i in range(6)) + "\n## Meta-observation\n\nx\n"
        _, problems, _ = S.validate(ans, pool())
        self.assertTrue(any("1-2 weirdness" in p for p in problems))
        self.assertTrue(any("4-5 weirdness" in p for p in problems))

    def test_fewer_than_ten_ideas_is_fine(self):
        valid, problems, _ = S.validate(good_answer(), pool())
        self.assertLess(len(valid), 10)
        self.assertEqual(problems, [])

    def test_too_few_ideas_flagged(self):
        ans = idea(1, "P01", 2) + idea(2, "P02", 4) + "\n## Meta-observation\n\nx\n"
        _, problems, _ = S.validate(ans, pool())
        self.assertTrue(any("at least 4" in p for p in problems))


class TestRender(unittest.TestCase):
    def test_ids_replaced_by_real_titles_and_renumbered(self):
        valid, _, meta = S.validate(good_answer(), pool())
        md = S.render_markdown(valid[1:], meta, pool())      # drop the first idea -> renumber from 1
        self.assertIn("## Idea 1:", md)
        self.assertNotRegex(md, r"\bP0\d\b")
        self.assertIn("ProteinMPNN improves antibody sequence design", md)
        self.assertIn("★★☆☆☆ (2/5)", md)


class TestGenerateWithRetry(unittest.TestCase):
    def test_retries_with_feedback_then_succeeds(self):
        calls = []
        bad = good_answer().replace("P04", "P77")

        def fake_llm(system, user, cfg, **kw):
            calls.append(user)
            return bad if len(calls) == 1 else good_answer()

        md, report = S.generate_ideas(pool(), "2026-09-14", "2026-09-20", {}, llm=fake_llm)
        self.assertIsNotNone(md)
        self.assertEqual(report["attempts"], 2)
        self.assertIn("REJECTED", calls[1])
        self.assertIn("P77", calls[1])

    def test_gives_up_when_nothing_valid(self):
        md, report = S.generate_ideas(pool(), "a", "b", {}, llm=lambda *a, **k: "rambling with no structure")
        self.assertIsNone(md)
        self.assertEqual(report["attempts"], 3)   # 1 + 2 retries

    def test_keeps_best_attempt_and_drops_invalid_ideas(self):
        partial = good_answer().replace("P04", "P77")   # 5 valid ideas; still >=3, so publishable
        md, report = S.generate_ideas(pool(), "a", "b", {}, llm=lambda *a, **k: partial)
        self.assertIsNotNone(md)
        self.assertEqual(report["n_valid"], 5)


class TestPool(unittest.TestCase):
    def test_off_topic_papers_excluded_and_ids_assigned(self):
        papers = [
            {"date": "2026-09-15", "title": "De novo design of miniprotein binders with RFdiffusion", "one_liner": "", "tags": [], "highlighted": True, "source": ""},
            {"date": "2026-09-15", "title": "Directed evolution of enzyme variants with protein language model", "one_liner": "", "tags": [], "highlighted": False, "source": ""},
            {"date": "2026-09-16", "title": "Inverse folding for antibody design", "one_liner": "", "tags": [], "highlighted": False, "source": ""},
            {"date": "2026-09-16", "title": "AlphaFold3 protein structure prediction of complexes", "one_liner": "", "tags": [], "highlighted": False, "source": ""},
            {"date": "2026-09-17", "title": "ProteinMPNN sequence design of de novo proteins", "one_liner": "", "tags": [], "highlighted": False, "source": ""},
            {"date": "2026-09-17", "title": "Machine learning for outcome prediction after vestibular schwannoma surgery", "one_liner": "", "tags": [], "highlighted": True, "source": ""},
        ]
        out = S.select_pool(papers, {})
        titles = [p["title"] for p in out]
        self.assertNotIn("Machine learning for outcome prediction after vestibular schwannoma surgery", titles)
        self.assertEqual(out[0]["id"], "P01")
        self.assertTrue(out[0]["highlighted"])

    def test_returns_empty_when_too_little_on_topic_material(self):
        papers = [{"date": "d", "title": "Soil pollution study", "one_liner": "", "tags": [], "highlighted": False, "source": ""}] * 10
        self.assertEqual(S.select_pool(papers, {}), [])


class TestNotionBlocks(unittest.TestCase):
    def test_bold_becomes_annotation_not_asterisks(self):
        blocks = S.markdown_to_blocks("**Inspired by:** “Paper” (2026-09-17)")
        rt = blocks[0]["paragraph"]["rich_text"]
        self.assertEqual(rt[0]["text"]["content"], "Inspired by:")
        self.assertTrue(rt[0]["annotations"]["bold"])
        self.assertNotIn("*", "".join(r["text"]["content"] for r in rt))

    def test_long_line_is_chunked_under_notion_limit(self):
        blocks = S.markdown_to_blocks("x" * 5000)
        for b in blocks:
            for r in b["paragraph"]["rich_text"]:
                self.assertLessEqual(len(r["text"]["content"]), 2000)

    def test_headings_and_dividers(self):
        types = [b["type"] for b in S.markdown_to_blocks("# T\n\n## H\n\n---\n\ntext")]
        self.assertEqual(types, ["heading_1", "heading_2", "divider", "paragraph"])


if __name__ == "__main__":
    unittest.main()
