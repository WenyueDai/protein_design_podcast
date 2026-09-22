"""
Topical relevance scoring and safe exclusion matching.

Why this exists
---------------
Before this module the pipeline had two quality problems that were invisible
in the logs:

1. `excluded_terms` were matched as raw substrings against "title + source + URL".
   The term "rat" (the animal) therefore silently dropped any paper whose title
   contained "accurate", "generative", "rational", "integration", "literature",
   "accelerated", "maturation", "concentration" ... i.e. core protein-design
   papers.  6 of the 30 papers the owner manually reported as "missed" were lost
   this way.

2. Nothing checked whether a paper was on-topic.  Source tier (tracked blogs,
   researcher feeds) outranked relevance, and the ranker always filled its quota,
   so quiet days (weekends) were padded with loose PubMed keyword hits
   (radiograph classifiers, tunnel-dust models, Mendelian randomisation ...).

This module is deterministic (no LLM call, no network), so it is cheap, testable
and cannot be rate-limited.  All vocabularies can be overridden in config.yaml
under `relevance:`.

Scoring model
-------------
score = 4*min(strong, 2) + 2*min(protein_ctx, 2) + 1*min(method_ctx, 2) - 3*min(offtopic, 2)
         (+1 when the title has both a protein-context and a method-context word)

where each *_ctx / strong value is the sum of per-term hit weights: a term found
in the title counts 1.0, a term found only in the abstract snippet counts 0.6.

`strong` also picks up +1.0 when the title opens with a coined "Name: ..." tool
announcement alongside a protein-family word (protein/antibody/peptide/...), even when
`Name` isn't in `strong_terms` yet - see the "Unnamed-tool bonus" note below.
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Default vocabularies.  Term syntax:
#   "alphafold"      whole-word match (plural -s/-es allowed)
#   "antibod*"       prefix match (antibody, antibodies, antibody-antigen ...)
#   spaces / hyphens in a term match either a space or a hyphen in the text
# ---------------------------------------------------------------------------

DEFAULT_STRONG: List[str] = [
    # model / tool families
    "alphafold*", "rosettafold*", "esmfold", "esm-2", "esm2", "esm3", "rfdiffusion*",
    "proteinmpnn", "ligandmpnn", "openfold*", "omegafold", "boltz", "chai-1", "evodiff",
    "colabfold", "rosetta", "esm", "esmc", "saprot", "prot-t5", "prott5", "plm", "interplm",
    # core tasks
    "structure prediction", "protein structure prediction", "co-folding", "cofolding",
    "protein design*", "de novo design*", "de novo protein*", "de novo binder*",
    "binder design*", "protein binder*", "miniprotein*", "mini-protein*",
    "inverse folding", "sequence design*", "backbone generation", "backbone design*",
    "protein generat*", "generative protein*", "protein diffusion model*", "protein language model*",
    "protein foundation model*", "protein engineering", "directed evolution",
    "enzyme design*", "enzyme engineering", "peptide design*", "protein optimi*ation",
    "fitness landscape*", "deep mutational scanning", "variant effect*", "mutation effect*",
    "protein fitness", "protein stability", "circular permutation*", "protein universe", "thermostability", "thermal stability", "conformational ensemble*", "protein ensemble*",
    "disordered protein*", "intrinsically disordered", "conformational landscape*",
    "protein-protein interaction*", "protein complex*", "protein docking", "protein-peptide", "peptide-protein",
    "protein-ligand", "protein-rna", "protein-dna", "biomolecular complex*", "binding interface*",
    "conformational change*", "allosteric*", "allostery", "metalloenzyme*",
    # antibody / immune-protein design
    "antibody design*", "antibody engineering", "antibody discovery", "antibody-antigen",
    "antigen-antibody", "antibody optimi*ation", "antibody structure*", "antibody language model*",
    "monoclonal antibod*", "therapeutic antibod*", "developability", "polyreactiv*", "fab",
    "nanobod*", "vhh", "epitope*", "paratope*", "cdr", "cdrh3", "cdr-h3", "immunoglobulin*",
    "tcr-pmhc", "pmhc", "peptide-mhc",
]

DEFAULT_PROTEIN_CTX: List[str] = [
    "protein*", "peptide*", "antibod*", "enzyme*", "antigen*", "binder*", "residue*",
    "amino acid*", "conformation*", "cryo-em", "crystal structure*", "structural biology",
    "folding", "metalloprotein*", "receptor*", "codon*",
]

DEFAULT_METHOD_CTX: List[str] = [
    "design*", "engineer*", "bioengineer*", "machine learning", "deep learning", "language model*",
    "diffusion", "generative", "neural network*", "graph neural", "transformer*",
    "artificial intelligence", "ai", "computational", "in silico", "benchmark*", "predict*",
    "simulation*", "molecular dynamics", "docking", "foundation model*", "embedding*",
    "flow matching", "energy landscape*", "sampling", "optimi*ation", "sparse autoencoder*", "interpretab*",
]

DEFAULT_OFFTOPIC: List[str] = [
    # clinical / epidemiology / imaging
    "patient*", "cohort*", "clinical trial*", "case report*", "radiograph*", "radiolog*",
    "computed tomography", "ct reconstruction", "mri", "ultrasound", "surgery", "surgical",
    "mendelian randomi*ation", "randomi*ed", "meta-analysis", "epidemiolog*", "quality of life",
    "diagnostic accuracy", "prognos*", "dry eye", "ophthalm*", "mucoadhesive", "hyaluronic acid",
    "formulation*", "drug delivery", "infection*", "outbreak*", "surveillance",
    # unrelated life-science / physical-science domains
    "arabidopsis", "crop*", "soil", "wastewater", "pollut*", "tunnel", "dust",
    "photosynthe*", "marine", "livestock",
    # small-molecule drug discovery (adjacent, but not protein design)
    "small molecule*", "small-molecule*", "inhibitor*", "drug repurposing", "kinase inhibitor*",
    "pharmacokinetic*", "admet", "adme",
    # general-interest / non-research content
    "agi", "socialis*", "geopolitic*", "election*", "stock market", "cryptocurrenc*",
]

# Exclusion terms that must always win (administrative / non-research items).
DEFAULT_HARD_EXCLUDE: List[str] = ["author correction", "in this issue", "retraction notice"]

_TITLE_W = 1.0
_SNIPPET_W = 0.6

# ---------------------------------------------------------------------------
# Unnamed-tool bonus: new models/tools are coined faster than `strong_terms` can be
# updated by hand (see tools/process_missed_papers.py). Papers introducing one almost
# always use the "Name: what it does" title convention ("AlphaFast: High-throughput
# AlphaFold 3 via ...", "AbEpiTope-1.0: Improved antibody target prediction ..."). That
# convention alone is not distinctive (gene/protein symbols like "PrkA/YeaG" and
# unrelated tools like "GraphPert: ... drug repurposing ..." also read this way), so the
# bonus only fires when the title *also* names a protein-family object (protein,
# antibody, peptide, enzyme, ...) - checked against the full labelled set in
# tests/test_relevance.py with zero pass/fail flips and zero new false positives.
_GENERIC_LEAD_WORDS = {
    "abstract", "background", "note", "comment", "commentary", "editorial", "update",
    "review", "perspective", "correction", "erratum", "introduction", "summary",
    "highlight", "highlights", "preprint", "letter", "reply", "response", "viewpoint",
}
_LEADING_NAME_RX = re.compile(r"^\s*([A-Za-z][A-Za-z0-9]*(?:[\-.][A-Za-z0-9]+)*)\s*:\s*\S")


def _coined_tool_name(title: str) -> Optional[str]:
    """Leading 'Name:' / 'Name-1.0:' token, or None if absent / a generic section header."""
    m = _LEADING_NAME_RX.match(title or "")
    if not m:
        return None
    tok = m.group(1)
    return None if tok.lower() in _GENERIC_LEAD_WORDS else tok


# ---------------------------------------------------------------------------
# Term compilation
# ---------------------------------------------------------------------------

def _term_to_regex(term: str) -> "re.Pattern[str]":
    t = term.strip().lower()
    prefix = t.endswith("*")
    if prefix:
        t = t[:-1]
    # allow "optimi*ation" style infix wildcards: 'optimi*ation' -> optimi\w*ation
    def _seg(part: str) -> str:
        # spaces/hyphens are interchangeable: split on them and rejoin with one class
        words = [w for w in re.split(r"[\s\-]+", part) if w]
        return r"[\s\-]+".join(re.escape(w) for w in words)

    body = r"\w*".join(_seg(p) for p in t.split("*"))
    if prefix:
        return re.compile(r"(?<!\w)" + body + r"\w*", re.I)
    return re.compile(r"(?<!\w)" + body + r"(?:s|es)?(?!\w)", re.I)


@lru_cache(maxsize=4096)
def _compiled(terms: Tuple[str, ...]) -> Tuple["re.Pattern[str]", ...]:
    return tuple(_term_to_regex(t) for t in terms)


def _hits(text: str, terms: Sequence[str]) -> List[str]:
    """Distinct terms from `terms` that occur in `text`."""
    if not text:
        return []
    out: List[str] = []
    for term, rx in zip(terms, _compiled(tuple(terms))):
        if rx.search(text):
            out.append(term)
    return out


# ---------------------------------------------------------------------------
# Config access
# ---------------------------------------------------------------------------

def _rel_cfg(cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return ((cfg or {}).get("relevance") or {}) if isinstance(cfg, dict) else {}


def vocab(cfg: Optional[Dict[str, Any]]) -> Dict[str, List[str]]:
    r = _rel_cfg(cfg)
    return {
        "strong": list(r.get("strong_terms") or DEFAULT_STRONG),
        "protein": list(r.get("protein_context_terms") or DEFAULT_PROTEIN_CTX),
        "method": list(r.get("method_context_terms") or DEFAULT_METHOD_CTX),
        "offtopic": list(r.get("offtopic_terms") or DEFAULT_OFFTOPIC),
    }


def thresholds(cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    r = _rel_cfg(cfg)
    return {
        "enabled": bool(r.get("enabled", True)),
        "min_score": float(r.get("min_score", 4)),
        "featured_min_score": float(r.get("featured_min_score", 5)),
        "min_featured_for_episode": int(r.get("min_featured_for_episode", 3)),
        "max_candidates_to_analyze": int(r.get("max_candidates_to_analyze", 60)),
    }


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _weighted(title_hits: Iterable[str], snippet_hits: Iterable[str]) -> float:
    th, sh = set(title_hits), set(snippet_hits)
    return _TITLE_W * len(th) + _SNIPPET_W * len(sh - th)


def score_text(title: str, snippet: str = "", cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return {'score', 'strong', 'protein', 'method', 'offtopic', 'matched'}."""
    v = vocab(cfg)
    title = title or ""
    snippet = (snippet or "")[:900]
    parts: Dict[str, float] = {}
    matched: Dict[str, List[str]] = {}
    for key in ("strong", "protein", "method", "offtopic"):
        th = _hits(title, v[key])
        sh = _hits(snippet, v[key])
        parts[key] = _weighted(th, sh)
        matched[key] = sorted(set(th) | set(sh))
    # Unnamed-tool bonus: "Name: ..." title introducing a protein-family object, even
    # when `Name` itself isn't in `strong_terms` yet. See module notes above.
    coined = _coined_tool_name(title)
    if coined and _hits(title, v["protein"]):
        parts["strong"] += 1.0
        matched["strong"] = sorted(set(matched["strong"]) | {f"(coined:{coined})"})
    score = (
        4.0 * min(parts["strong"], 2.0)
        + 2.0 * min(parts["protein"], 2.0)
        + 1.0 * min(parts["method"], 2.0)
        - 3.0 * min(parts["offtopic"], 2.0)
    )
    # Combo bonus: a protein-context word *and* a computational/engineering word both
    # in the title ("... engineering of heme enzymes", "... transformer ... codon ...")
    # is a much stronger signal than either alone.
    if _hits(title, v["protein"]) and _hits(title, v["method"]):
        score += 1.0
    return {"score": round(score, 2), **parts, "matched": matched}


def item_text(it: Dict[str, Any]) -> Tuple[str, str]:
    title = (it.get("title") or "").strip()
    snippet = " ".join(
        str(x) for x in ((it.get("one_liner") or ""), (it.get("snippet") or ""), (it.get("summary") or ""))
        if x
    ).strip()
    # Strip crude HTML that RSS snippets sometimes carry.
    snippet = re.sub(r"<[^>]+>", " ", snippet)
    return title, snippet


def score_item(it: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    title, snippet = item_text(it)
    return score_text(title, snippet, cfg)


def has_strong_signal(it: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None) -> bool:
    return score_item(it, cfg)["strong"] > 0


# ---------------------------------------------------------------------------
# Exclusion (replaces raw-substring matching)
# ---------------------------------------------------------------------------

def matches_excluded(text: str, terms: Sequence[str]) -> Optional[str]:
    """
    Return the first excluded term found in `text`, or None.

    Terms match as WHOLE WORDS (so "rat" no longer matches "accurate"); a trailing
    "*" makes a term a prefix ("superconductiv*").  Matching is case-insensitive.
    """
    if not text:
        return None
    for term in terms:
        if not term or not term.strip():
            continue
        if _compiled((term,))[0].search(text):
            return term
    return None


def should_exclude(
    it: Dict[str, Any],
    terms: Sequence[str],
    cfg: Optional[Dict[str, Any]] = None,
    hard_terms: Optional[Sequence[str]] = None,
) -> Optional[str]:
    """
    Decide whether an item should be dropped by `excluded_terms`.

    - Only title + source are checked (never the URL: URLs contain arbitrary substrings).
    - Whole-word matching.
    - "Topical" exclusions (mouse, rat, zebrafish ...) are overridden when the item
      carries a strong protein-design signal (e.g. "humanization of the mouse
      immunoglobulin loci enables therapeutic antibody discovery" is on-topic).
      Hard (administrative) exclusions always apply.
    Returns the matched term when the item should be excluded, else None.
    """
    hard = list(hard_terms if hard_terms is not None else DEFAULT_HARD_EXCLUDE)
    title = (it.get("title") or "")
    source = (it.get("source") or "")
    hay = f"{title} {source}"

    h = matches_excluded(hay, hard)
    if h:
        return h
    m = matches_excluded(hay, terms)
    if not m:
        return None
    if has_strong_signal(it, cfg):
        return None
    return m


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------

def passes_gate(
    it: Dict[str, Any],
    cfg: Optional[Dict[str, Any]] = None,
    protected: bool = False,
) -> bool:
    th = thresholds(cfg)
    if not th["enabled"]:
        return True
    r = score_item(it, cfg)
    it["relevance"] = r["score"]
    if protected:
        it["relevance_protected"] = True
        return True
    return r["score"] >= th["min_score"]
