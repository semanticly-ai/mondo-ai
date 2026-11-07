"""String normalisation and bounded, named lexical rewrites.

Two levels, kept strictly apart:

* :func:`base_normalize` is *non-semantic*. It removes differences no reader would call a
  difference in meaning: case, diacritics, dash and quote variants, bracketed segments,
  whitespace. Two strings equal after base normalisation are the same string.
* :func:`exact_variants` applies *one* rewrite from a small named table and returns the
  result together with the rule that produced it. Each rewrite is meaning-preserving by
  construction ("Marfan's syndrome" and "Marfan syndrome" denote the same disease), which
  is what licenses ``EV-004`` rather than ``EV-006``.

:func:`broad_variants` is the third, separate level: rewrites that *change* scope
(stripping a qualifier, dropping a leading "other"). They exist only to widen the candidate
pool for adjudication and must never be emitted as evidence of an exact match.

The rule table and its certainties descend from MeDIC's lexical grounder and the
``map_icd10cm`` port of it; the Mondo/ICD-10-CM-specific additions (possessives, comma
inversion, deficiency inversion) are marked in :data:`RULE_CERTAINTY`.

Example:
    >>> base_normalize("  Marfan's  Syndrome [obsolete] ")
    "marfan's syndrome"
    >>> sorted(r for _, r in exact_variants("marfan's syndrome"))
    ['strip_possessive', 'syndrome_to_disease']
    >>> dict(exact_variants("marfan's syndrome"))["marfan syndrome"]
    'strip_possessive'
"""

from __future__ import annotations

import re
import unicodedata
from typing import Callable, Iterable

_BRACKETS = re.compile(r"\[[^\]]*\]")
_WS = re.compile(r"\s+")
_DASHES = {"‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-"}
_QUOTES = {"’": "'", "‘": "'", "“": '"', "”": '"'}


def base_normalize(s: str) -> str:
    """Apply non-semantic normalisation only.

    Args:
        s: Any label or synonym.

    Returns:
        The normalised string; the empty string for input that normalises away.

    Example:
        >>> base_normalize("Beh\\u00e7et\\u2019s Disease")
        "behcet's disease"
        >>> base_normalize("T\\u2013cell   leukaemia")
        't-cell leukaemia'
        >>> base_normalize("   ")
        ''
    """
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    for a, b in {**_DASHES, **_QUOTES}.items():
        s = s.replace(a, b)
    s = _BRACKETS.sub("", s)
    s = s.casefold()
    return _WS.sub(" ", s).strip()


_ARABIC_TO_ROMAN = {1: "i", 2: "ii", 3: "iii", 4: "iv", 5: "v", 6: "vi",
                    7: "vii", 8: "viii", 9: "ix", 10: "x", 11: "xi", 12: "xii"}
_ROMAN_TO_ARABIC = {v: k for k, v in _ARABIC_TO_ROMAN.items()}
_INDICATORS = "type|stage|grade|class|group|factor"
_BRIT_AM = {
    "tumour": "tumor", "oesophag": "esophag", "haemat": "hemat", "haemo": "hemo",
    "anaemia": "anemia", "leukaemia": "leukemia", "coeliac": "celiac",
    "oedema": "edema", "paediatric": "pediatric", "colour": "color",
    "diarrhoea": "diarrhea", "gynaecolog": "gynecolog", "fibre": "fiber",
    "ischaemi": "ischemi", "hypercalcaemi": "hypercalcemi", "glycaemi": "glycemi",
    "hypoglycaemi": "hypoglycemi", "caecum": "cecum", "foetal": "fetal",
}
_CELL_TOKENS = ("t", "b", "nk")

Rewrite = tuple[str, str]


def _r_disease_disorder(s: str) -> list[Rewrite]:
    out = []
    if "disease" in s:
        out.append((s.replace("disease", "disorder"), "disease_to_disorder"))
    if "disorder" in s:
        out.append((s.replace("disorder", "disease"), "disorder_to_disease"))
    return out


def _r_syndrome_disease(s: str) -> list[Rewrite]:
    out = []
    if "syndrome" in s:
        out.append((s.replace("syndrome", "disease"), "syndrome_to_disease"))
    if "disease" in s:
        out.append((s.replace("disease", "syndrome"), "disease_to_syndrome"))
    return out


def _r_arabic_roman(s: str) -> list[Rewrite]:
    out = []
    m = re.search(rf"\b({_INDICATORS})\s+(\d{{1,2}})\b", s)
    if m and int(m.group(2)) in _ARABIC_TO_ROMAN:
        roman = _ARABIC_TO_ROMAN[int(m.group(2))]
        out.append((s[: m.start(2)] + roman + s[m.end(2):], "arabic_to_roman"))
    m = re.search(rf"\b({_INDICATORS})\s+([ivx]{{1,4}})\b", s)
    if m and m.group(2) in _ROMAN_TO_ARABIC:
        arabic = str(_ROMAN_TO_ARABIC[m.group(2)])
        out.append((s[: m.start(2)] + arabic + s[m.end(2):], "roman_to_arabic"))
    return out


def _r_comma_drop_type(s: str) -> list[Rewrite]:
    m = re.search(r",\s+(type\s+\w+)$", s)
    return [(s[: m.start()] + " " + m.group(1), "comma_drop_type")] if m else []


def _r_hyphen_type(s: str) -> list[Rewrite]:
    new = re.sub(r"\btype-(\w+)", r"type \1", s)
    return [(new, "hyphen_type")] if new != s else []


def _r_brit_am(s: str) -> list[Rewrite]:
    out = []
    for brit, am in _BRIT_AM.items():
        if brit in s:
            out.append((s.replace(brit, am), "brit_to_am"))
        elif am in s:
            out.append((s.replace(am, brit), "am_to_brit"))
    return out


def _r_cell_hyphen(s: str) -> list[Rewrite]:
    out = []
    for tok in _CELL_TOKENS:
        if re.search(rf"\b{tok}-cell\b", s):
            out.append((re.sub(rf"\b{tok}-cell\b", f"{tok} cell", s), "cell_hyphen_to_space"))
        elif re.search(rf"\b{tok} cell\b", s):
            out.append((re.sub(rf"\b{tok} cell\b", f"{tok}-cell", s), "cell_space_to_hyphen"))
    return out


#: Head nouns that follow an eponym in a disease name. ``add_possessive`` only fires in
#: front of one of these, so that "deficiency of X" does not sprout "deficiency's of X".
_EPONYM_HEADS = (
    "syndrome", "disease", "disorder", "anemia", "anaemia", "sign", "palsy", "ataxia",
    "dystrophy", "sarcoma", "lymphoma", "tumor", "tumour", "encephalopathy", "chorea",
    "contracture", "arthritis", "neuralgia", "granulomatosis", "thyroiditis", "cyst",
    "cirrhosis", "nephritis", "fracture", "deformity", "phenomenon", "reaction",
)


def _r_possessive(s: str) -> list[Rewrite]:
    """``X's Y`` <-> ``X Y``. ICD-10-CM keeps eponym possessives that Mondo drops."""
    out = []
    if "'s " in s or s.endswith("'s"):
        out.append((re.sub(r"'s\b", "", s), "strip_possessive"))
    else:
        m = re.match(r"^(\w+)\s+(.+)$", s)
        if m and m.group(2).split()[0] in _EPONYM_HEADS:
            out.append((f"{m.group(1)}'s {m.group(2)}", "add_possessive"))
    return out


def _r_comma_invert(s: str) -> list[Rewrite]:
    """``X, congenital`` -> ``congenital X``. ICD-10-CM inverts far more than Mondo does."""
    if s.count(",") != 1:
        return []
    head, _, tail = s.partition(",")
    head, tail = head.strip(), tail.strip()
    if not head or not tail or len(tail.split()) > 3:
        return []
    return [(f"{tail} {head}", "comma_inversion")]


def _r_deficiency_of(s: str) -> list[Rewrite]:
    """``deficiency of X`` <-> ``X deficiency``."""
    m = re.match(r"^deficiency of (.+)$", s)
    if m:
        return [(f"{m.group(1)} deficiency", "deficiency_inversion")]
    m = re.match(r"^(.+) deficiency$", s)
    if m:
        return [(f"deficiency of {m.group(1)}", "deficiency_inversion")]
    return []


# --- scope-changing rewrites: candidate generation only, never emitted as evidence ------

_QUALIFIERS = (
    "severe", "moderate", "mild", "acute", "chronic", "advanced", "metastatic",
    "recurrent", "refractory", "relapsed", "relapsing", "newly diagnosed", "primary",
    "secondary", "malignant", "active", "persistent", "resistant", "unresectable",
    "locally advanced", "inoperable", "early", "late", "progressive", "generalized",
    "generalised", "localized", "localised", "familial", "hereditary", "congenital",
    "idiopathic", "classic", "classical", "juvenile", "infantile", "adult", "neonatal",
)
_QUAL_RE = re.compile(
    r"^((?:" + "|".join(_QUALIFIERS) + r")(?:[\s,/]+(?:to|and|or)?[\s,/]*)?)+", re.I
)


def _r_qualifier_strip(s: str) -> list[Rewrite]:
    m = _QUAL_RE.match(s)
    if m and m.end() < len(s):
        rest = s[m.end():].strip()
        if rest and rest != s:
            return [(rest, "qualifier_strip")]
    return []


def _r_strip_other(s: str) -> list[Rewrite]:
    return [(s[len("other "):], "strip_leading_other")] if s.startswith("other ") else []


#: Mapping predicate each rewrite is compatible with. Only ``skos:exactMatch`` rewrites
#: may back an ``EV-004`` citation.
RULE_PREDICATE = {
    "base_normalization": "skos:exactMatch",
    "comma_drop_type": "skos:exactMatch", "hyphen_type": "skos:exactMatch",
    "cell_hyphen_to_space": "skos:exactMatch", "cell_space_to_hyphen": "skos:exactMatch",
    "disease_to_disorder": "skos:exactMatch", "disorder_to_disease": "skos:exactMatch",
    "arabic_to_roman": "skos:exactMatch", "roman_to_arabic": "skos:exactMatch",
    "brit_to_am": "skos:exactMatch", "am_to_brit": "skos:exactMatch",
    "strip_possessive": "skos:exactMatch", "add_possessive": "skos:exactMatch",
    "comma_inversion": "skos:exactMatch", "deficiency_inversion": "skos:exactMatch",
    "syndrome_to_disease": "skos:exactMatch", "disease_to_syndrome": "skos:exactMatch",
    "strip_leading_other": "skos:broadMatch", "qualifier_strip": "skos:broadMatch",
    "fuzzy_edit1_unique": "skos:closeMatch",
}

#: Per-rewrite certainty, used as the deterministic confidence of a tier-4 hit.
RULE_CERTAINTY = {
    "base_normalization": 1.0,
    "comma_drop_type": 0.98, "hyphen_type": 0.98,
    "cell_hyphen_to_space": 0.97, "cell_space_to_hyphen": 0.97,
    "disease_to_disorder": 0.95, "disorder_to_disease": 0.95,
    "arabic_to_roman": 0.95, "roman_to_arabic": 0.95,
    "brit_to_am": 0.97, "am_to_brit": 0.97,
    # additions beyond MeDIC's set, for Mondo <-> ICD-10-CM label conventions
    "strip_possessive": 0.97, "add_possessive": 0.97,
    "comma_inversion": 0.95, "deficiency_inversion": 0.96,
    "syndrome_to_disease": 0.88, "disease_to_syndrome": 0.88,
    # scope-changing: pool widening only
    "strip_leading_other": 0.70, "qualifier_strip": 0.75,
    "fuzzy_edit1_unique": 0.60,
}

_EXACT_RULES: tuple[Callable[[str], list[Rewrite]], ...] = (
    _r_possessive, _r_disease_disorder, _r_syndrome_disease, _r_arabic_roman,
    _r_comma_drop_type, _r_hyphen_type, _r_brit_am, _r_cell_hyphen,
    _r_comma_invert, _r_deficiency_of,
)
_BROAD_RULES: tuple[Callable[[str], list[Rewrite]], ...] = (_r_qualifier_strip, _r_strip_other)


def _apply(rules: Iterable[Callable[[str], list[Rewrite]]], normalized: str) -> list[Rewrite]:
    seen: set[str] = set()
    out: list[Rewrite] = []
    for rule_fn in rules:
        for produced, rule_id in rule_fn(normalized):
            produced = _WS.sub(" ", produced).strip()
            if produced and produced != normalized and produced not in seen:
                seen.add(produced)
                out.append((produced, rule_id))
    return out


def exact_variants(normalized: str) -> list[Rewrite]:
    """Return meaning-preserving rewrites of an already-normalised string.

    Single pass, deterministic and bounded: each rule sees the original string, so no
    rewrite chains build up and the output size stays small.

    Args:
        normalized: A string that has already been through :func:`base_normalize`.

    Returns:
        ``(rewritten, rule_id)`` pairs, first occurrence wins on collision.

    Example:
        >>> dict(exact_variants("hemophilia a"))["haemophilia a"]
        'am_to_brit'
        >>> dict(exact_variants("mucopolysaccharidosis type 4"))["mucopolysaccharidosis type iv"]
        'arabic_to_roman'
        >>> dict(exact_variants("deficiency of biotinidase"))
        {'biotinidase deficiency': 'deficiency_inversion'}
        >>> exact_variants("cholera")
        []
    """
    return _apply(_EXACT_RULES, normalized)


def broad_variants(normalized: str) -> list[Rewrite]:
    """Return scope-changing rewrites, for candidate generation only.

    Example:
        >>> broad_variants("other porphyria")
        [('porphyria', 'strip_leading_other')]
        >>> broad_variants("severe combined immunodeficiency")[0][1]
        'qualifier_strip'
    """
    return _apply(_BROAD_RULES, normalized)


_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789 -',./"


def edits1(word: str) -> set[str]:
    """Return every edit-distance-1 neighbour of ``word`` over the label alphabet.

    Only used to widen the candidate pool: an edit-1 neighbour is never evidence of
    concept equivalence.

    Example:
        >>> "cholea" in edits1("cholera")
        True
        >>> "cholera" in edits1("cholera")
        False
    """
    splits = [(word[:i], word[i:]) for i in range(len(word) + 1)]
    deletes = [a + b[1:] for a, b in splits if b]
    transposes = [a + b[1] + b[0] + b[2:] for a, b in splits if len(b) > 1]
    replaces = [a + c + b[1:] for a, b in splits if b for c in _ALPHABET]
    inserts = [a + c + b for a, b in splits for c in _ALPHABET]
    return set(deletes + transposes + replaces + inserts) - {word}


_TOKEN_RE = re.compile(r"[a-z0-9]+")

#: Tokens with no discriminative power for retrieval. ``disease``/``disorder``/``syndrome``
#: are in here because almost every label carries one; ``other``/``unspecified``/``nos``
#: because they mark a rubric's residual status rather than its subject matter.
STOPWORDS = frozenset({
    "the", "of", "and", "or", "with", "without", "in", "to", "a", "an", "for", "due",
    "unspecified", "other", "nos", "nec", "disease", "diseases", "disorder", "disorders",
    "syndrome", "syndromes", "specified", "elsewhere", "classified", "not",
})


def stem_token(token: str) -> str:
    """Collapse the plural forms that separate Mondo's labels from ICD-10-CM's.

    ICD-10-CM pluralises rubric heads where Mondo names one disease:
    "mucopolysaccharidoses" against "mucopolysaccharidosis", "sphingolipidoses" against
    "sphingolipidosis". Without this, token comparison between the two vocabularies misses
    the head noun in exactly the cases where the head noun is the point.

    Deliberately crude — four suffix rules, no dictionary. A stemmer that is wrong in
    unpredictable ways is worse than one that is wrong in ways the table shows.

    Example:
        >>> stem_token("mucopolysaccharidoses")
        'mucopolysaccharidosis'
        >>> stem_token("anomalies")
        'anomaly'
        >>> stem_token("tumors")
        'tumor'
        >>> stem_token("diabetes"), stem_token("mellitus"), stem_token("fibrosis")
        ('diabetes', 'mellitus', 'fibrosis')
        >>> stem_token("iv")
        'iv'
    """
    if len(token) < 4:
        return token
    if token.endswith("oses"):
        return token[:-4] + "osis"
    if token.endswith("ies"):
        return token[:-3] + "y"
    if token.endswith("ae"):
        return token[:-1]
    if token.endswith("s") and not token.endswith(("ss", "us", "is", "es")):
        return token[:-1]
    return token


def content_tokens(s: str) -> frozenset[str]:
    """Return discriminative, plural-collapsed content tokens of a normalised string.

    Example:
        >>> sorted(content_tokens("other specified disorders of the pancreas"))
        ['pancrea']
        >>> sorted(content_tokens("marfan syndrome"))
        ['marfan']
        >>> content_tokens("Morquio mucopolysaccharidoses") & content_tokens(
        ...     "mucopolysaccharidosis type IV")
        frozenset({'mucopolysaccharidosis'})
    """
    return frozenset(
        stem_token(t)
        for t in _TOKEN_RE.findall(s)
        if t not in STOPWORDS and len(t) > 1
    )


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """Return the Jaccard similarity of two token sets.

    Example:
        >>> jaccard(frozenset("ab"), frozenset("ab"))
        1.0
        >>> jaccard(frozenset(), frozenset("a"))
        0.0
    """
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def containment(a: frozenset[str], b: frozenset[str]) -> float:
    """Return the fraction of ``a`` covered by ``b``.

    Asymmetric on purpose: ``containment(subject, object)`` near 1 with
    ``containment(object, subject)`` well below 1 is the signature of the object being the
    more specific concept.

    Example:
        >>> containment(frozenset("ab"), frozenset("abc"))
        1.0
        >>> round(containment(frozenset("abc"), frozenset("ab")), 3)
        0.667
        >>> containment(frozenset(), frozenset("a"))
        0.0
    """
    if not a:
        return 0.0
    return len(a & b) / len(a)
