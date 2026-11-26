"""Scope and conceptual-model detectors, each mapped to a citable rule.

These are what stop a lexical hit from becoming a wrong `exactMatch`. Every detector
returns the rule id it instantiates, so the flag is not a private heuristic label but a
citation the reviewer can look up.

Design commitment, stated once: a detector never *decides*. It moves a decision from the
deterministic ladder to a stage that can explain itself. Rejecting "Morquio
mucopolysaccharidoses, unspecified" out of hand costs a real mapping (``SC-007``); accepting
"Other porphyria" costs a wrong patient cohort (``SC-001``); and no regex separates them.
:func:`possibly_coextensive` exists to route exactly that pair to a human or a model with
the rubric's children in front of it.

Example:
    >>> residual_rubric("Other porphyria")
    'SC-001'
    >>> residual_rubric("Polyneuropathy, unspecified")
    'SC-002'
    >>> residual_rubric("Cholera") is None
    True
    >>> disjunctive("Duchenne or Becker muscular dystrophy")
    'SC-004'
    >>> numbered_subtype("immunodeficiency 112")
    'SC-006'
    >>> susceptibility("malignant hyperthermia susceptibility type 1")
    'CM-006'
"""

from __future__ import annotations

import re

from mondo_ai.align.models import Concept
from mondo_ai.align.normalize import base_normalize, content_tokens

# --- SC-001 / SC-002: residual rubrics --------------------------------------------------

_UNSPECIFIED_RE = re.compile(
    r"\b(unspecified|not otherwise specified|nos)\b|\bexcluding\b", re.I)
_OTHER_RE = re.compile(
    r"\b(other|not elsewhere classified|nec|unspecified sites?)\b", re.I)
_RELATED_BUCKET_RE = re.compile(
    r"\b(related (?:conditions?|disorders?|diseases?)|and related\b|"
    r"associated (?:conditions?|disorders?))\b", re.I)


def residual_rubric(label: str) -> str | None:
    """Return the residual-subset rule a label triggers, if any.

    ``SC-002`` (scoped by exclusion) is checked before ``SC-001`` (undefined remainder)
    because "Other specified X, unspecified" is both, and the exclusion is the stronger
    statement about the extension.

    Example:
        >>> residual_rubric("Other specified diabetes mellitus")
        'SC-001'
        >>> residual_rubric("Epilepsy, unspecified")
        'SC-002'
        >>> residual_rubric("Marfan syndrome") is None
        True
    """
    if _UNSPECIFIED_RE.search(label or ""):
        return "SC-002"
    if _OTHER_RE.search(label or ""):
        return "SC-001"
    return None


def related_bucket(label: str) -> str | None:
    """Return ``SC-003`` for an open-ended association grouping.

    Example:
        >>> related_bucket("Cancer and related conditions")
        'SC-003'
        >>> related_bucket("Cancer") is None
        True
    """
    return "SC-003" if _RELATED_BUCKET_RE.search(label or "") else None


# --- SC-004: disjunctive rubrics ---------------------------------------------------------

#: Heads that mark the second arm of a disjunction as a *disease* rather than a modifier.
#: "Duchenne or Becker muscular dystrophy" is disjunctive; "acute or chronic" is not,
#: because neither arm names a disease on its own.
_DISEASE_HEADS = (
    "dystrophy", "syndrome", "disease", "disorder", "anemia", "anaemia", "carcinoma",
    "sarcoma", "lymphoma", "leukemia", "leukaemia", "neoplasm", "tumor", "tumour",
    "deficiency", "gigantism", "acromegaly", "atrophy", "dysplasia", "palsy", "fibrosis",
    "myopathy", "neuropathy", "nephropathy", "retinopathy", "cirrhosis", "hepatitis",
    "colitis", "arthritis", "dermatitis", "porphyria", "thalassemia", "thalassaemia",
)
_MODIFIERS = frozenset({
    "acute", "chronic", "subacute", "severe", "moderate", "mild", "left", "right",
    "bilateral", "unilateral", "primary", "secondary", "unspecified", "other",
    "congenital", "acquired", "familial", "juvenile", "adult", "recurrent", "persistent",
    "with", "without", "and", "or",
})
_DISJUNCT_RE = re.compile(r"\b(?:or|and)\b", re.I)


def disjunctive(label: str) -> str | None:
    """Return ``SC-004`` when a label joins two distinct diseases.

    Two conditions, both necessary. *Every* arm must contribute something beyond a pure
    modifier — that is what keeps "acute or chronic pancreatitis" out, where the first arm
    is only a course qualifier of one disease. And the label must carry a disease head
    noun somewhere, which keeps coordinated symptom pairs ("nausea and vomiting") out,
    since those are not two diseases and would be rejected on category grounds anyway.

    Example:
        >>> disjunctive("Acromegaly and pituitary gigantism")
        'SC-004'
        >>> disjunctive("Duchenne or Becker muscular dystrophy")
        'SC-004'
        >>> disjunctive("Acute or chronic pancreatitis") is None
        True
        >>> disjunctive("Marfan syndrome") is None
        True
        >>> disjunctive("Nausea and vomiting") is None
        True
    """
    norm = base_normalize(label or "")
    if not norm or not _DISJUNCT_RE.search(norm):
        return None
    if not any(h in norm for h in _DISEASE_HEADS):
        return None
    parts = [p.strip() for p in _DISJUNCT_RE.split(norm) if p.strip()]
    if len(parts) < 2:
        return None
    for part in parts:
        if all(w in _MODIFIERS for w in part.split()):
            return None
    return "SC-004"


# --- SC-005: target finer than subject ---------------------------------------------------

_FINER_RE = re.compile(
    r",\s*(right|left|bilateral|unspecified side)\b"
    r"|\b(initial|subsequent|sequela) encounter\b"
    r"|\bwith (?:complication|systemic onset|coma|ketoacidosis|hyperglycemia)"
    r"|\bstage [0-9ivx]+\b"
    r"|\bin remission\b",
    re.I,
)


def finer_variant(label: str) -> str | None:
    """Return ``SC-005`` when a label is a site, laterality, stage or encounter variant.

    Example:
        >>> finer_variant("Juvenile arthritis, right shoulder")
        'SC-005'
        >>> finer_variant("Type 1 diabetes with ketoacidosis")
        'SC-005'
        >>> finer_variant("Juvenile arthritis") is None
        True
    """
    return "SC-005" if _FINER_RE.search(label or "") else None


# --- SC-006: numbered / gene-defined subtypes -------------------------------------------

_NUMBERED_RE = re.compile(
    r"\b(?:type|group|complementation group|subtype|form)\s+[0-9ivxlc]+[a-z]?\b"
    r"|\s\d{1,3}$"
    r"|\b[0-9]+[a-z]?$",
    re.I,
)


def numbered_subtype(label: str) -> str | None:
    """Return ``SC-006`` when a label individuates a numbered or lettered subtype.

    Example:
        >>> numbered_subtype("Fanconi anemia complementation group C")
        'SC-006'
        >>> numbered_subtype("common variable immunodeficiency 12")
        'SC-006'
        >>> numbered_subtype("mucopolysaccharidosis type IV")
        'SC-006'
        >>> numbered_subtype("Marfan syndrome") is None
        True
    """
    label = label or ""
    if re.search(r"\bcomplementation group\s+\w+\b", label, re.I):
        return "SC-006"
    return "SC-006" if _NUMBERED_RE.search(label) else None


# --- CM-006 / CM-007 / CM-009: conceptual model boundaries ------------------------------

_SUSCEPT_RE = re.compile(
    r"\b(susceptibility|predisposition|risk of|resistance to|protection against)\b", re.I)
_GENE_RE = re.compile(
    r"\b[A-Z][A-Z0-9]{1,8}-related\b"
    r"|\b[A-Z][A-Z0-9]{1,8}-associated\b"
    r"|\bcaused by mutation in\b"
    r"|\b[A-Z][A-Z0-9]{2,8} deficiency\b",
)
_EVENT_RE = re.compile(
    r"\b(injury|poisoning|crisis|complication of|adverse effect|"
    r"encounter for|underdosing|toxic effect)\b", re.I)


def susceptibility(label: str) -> str | None:
    """Return ``CM-006`` when a label denotes a susceptibility rather than a disease.

    Example:
        >>> susceptibility("breast cancer, susceptibility to")
        'CM-006'
        >>> susceptibility("breast cancer") is None
        True
    """
    return "CM-006" if _SUSCEPT_RE.search(label or "") else None


def gene_level(label: str) -> str | None:
    """Return ``CM-007`` when a label is gene- or molecule-centric.

    The pattern insists on an upper-case gene-shaped token, so a lower-cased label is not
    flagged — this detector runs on the *original* label, not the normalised form.

    Example:
        >>> gene_level("COL4A1-related disorder")
        'CM-007'
        >>> gene_level("biotinidase deficiency") is None
        True
    """
    return "CM-007" if _GENE_RE.search(label or "") else None


def injury_event(label: str) -> str | None:
    """Return ``CM-009`` when a label denotes an event or procedural complication.

    Example:
        >>> injury_event("Malignant hyperthermia crisis")
        'CM-009'
        >>> injury_event("Malignant hyperthermia") is None
        True
    """
    return "CM-009" if _EVENT_RE.search(label or "") else None


# --- ST-005: identifiers that are not codes ---------------------------------------------

_RANGE_RE = re.compile(r"^[A-Z]\d{2}(?:\.\d+)?-[A-Z]\d{2}(?:\.\d+)?$")
_ICD10CM_CODE_RE = re.compile(r"^[A-Z]\d{2}(?:\.[0-9A-Z]{1,4})?$")


def chapter_range(object_id: str) -> str | None:
    """Return ``ST-005`` when an ICD-shaped identifier is a chapter range, not a code.

    Chapter ranges are the single most common bad identifier in released disease mapping
    sets: they read like codes, they are not pullable, and they are invalid regardless of
    how plausible the label is.

    Example:
        >>> chapter_range("ICD10CM:E00-E90")
        'ST-005'
        >>> chapter_range("ICD10CM:E84.0") is None
        True
        >>> chapter_range("MONDO:0007947") is None
        True
    """
    local = (object_id or "").split(":", 1)[-1]
    return "ST-005" if _RANGE_RE.match(local) else None


def foreign_shaped(object_id: str, prefix: str) -> str | None:
    """Return ``ST-005`` when an id does not match its prefix's identifier shape.

    Only ICD-10-CM is shape-checked: it is the one vocabulary in this pipeline's remit with
    a strict, well-known code grammar, and released Mondo sets do contain ICD-11-shaped ids
    under an ``ICD10CM:`` prefix.

    Example:
        >>> foreign_shaped("ICD10CM:QA0.0142", "ICD10CM")
        'ST-005'
        >>> foreign_shaped("ICD10CM:E84.0", "ICD10CM") is None
        True
        >>> foreign_shaped("SNOMED:12345", "SNOMED") is None
        True
    """
    if prefix != "ICD10CM":
        return None
    local = (object_id or "").split(":", 1)[-1]
    if _RANGE_RE.match(local):
        return None
    return None if _ICD10CM_CODE_RE.match(local) else "ST-005"


# --- CM-002 / CM-003: off-category ICD chapters -----------------------------------------

#: ICD-10-CM chapter letters whose rubrics are not diseases in Mondo's sense. Mapped to
#: the conflation rule a curator would have to invoke to accept the row anyway.
OFF_CATEGORY_CHAPTERS = {
    "R": "CM-002",  # symptoms, signs and abnormal findings — murky by construction
    "S": "CM-009",  # injury
    "T": "CM-009",  # poisoning, adverse effects
    "V": "CM-009", "W": "CM-009", "X": "CM-009", "Y": "CM-009",  # external causes
    "Z": "CM-003",  # factors influencing health status, encounters — clear-cut non-disease
    "O": "CM-005",  # pregnancy, childbirth — individuated by episode, not by disease
    "P": "CM-005",  # perinatal period
}


def off_category(object_id: str, prefix: str = "ICD10CM") -> str | None:
    """Return the conflation rule implied by an ICD-10-CM chapter, if it is off-category.

    Example:
        >>> off_category("ICD10CM:R51")
        'CM-002'
        >>> off_category("ICD10CM:Z94.0")
        'CM-003'
        >>> off_category("ICD10CM:E84") is None
        True
        >>> off_category("SNOMED:R51", "SNOMED") is None
        True
    """
    if prefix != "ICD10CM":
        return None
    local = (object_id or "").split(":", 1)[-1]
    return OFF_CATEGORY_CHAPTERS.get(local[:1].upper()) if local else None


# --- SNOMED CT: concept class and laterality --------------------------------------------

#: SNOMED CT concept class (as OMOP names it) -> the rule a curator invokes to map a disease
#: onto it. ``Disorder`` carries nothing. ``Clinical Finding`` is SNOMED's finding/disorder
#: split, the conflation ``CM-004`` names. Anything else that the extraction keeps — in
#: practice ``Morph Abnormality`` — is not a condition at all (``RJ-005``).
SNOMED_CLASS_RULE = {
    "Disorder": None,
    "Clinical Finding": "CM-004",
}

_SNOMED_LATERALITY_RE = re.compile(
    r"\b(?:of|in|on) (?:the )?(?:left|right)\b|\b(?:left|right)[- ]sided\b", re.I)


def snomed_class(obj: Concept) -> str | None:
    """Return the rule a SNOMED concept's class implies for a disease mapping, if any.

    Example:
        >>> snomed_class(Concept(id="SCTID:1", label="Fever",
        ...                      extra={"concept_class_id": "Clinical Finding"}))
        'CM-004'
        >>> snomed_class(Concept(id="SCTID:2", label="Adenoma",
        ...                      extra={"concept_class_id": "Morph Abnormality"}))
        'RJ-005'
        >>> snomed_class(Concept(id="SCTID:3", label="Cholera",
        ...                      extra={"concept_class_id": "Disorder"})) is None
        True
        >>> snomed_class(Concept(id="SCTID:4", label="x")) is None
        True
    """
    cls = obj.extra.get("concept_class_id", "")
    if not cls:
        return None
    return SNOMED_CLASS_RULE.get(cls, "RJ-005")


def snomed_laterality(label: str) -> str | None:
    """Return ``SC-005`` when a SNOMED label pre-coordinates a side.

    SNOMED writes laterality into the name ("Osteoarthritis of left knee") rather than after
    a comma as ICD does, so :func:`finer_variant` does not see it. Anatomical names that
    contain a side ("hypoplastic left heart syndrome", "double outlet right ventricle") are
    not laterality and do not match: the pattern needs "of/in/on left" or "left-sided".

    Example:
        >>> snomed_laterality("Osteoarthritis of left knee")
        'SC-005'
        >>> snomed_laterality("Right-sided heart failure")
        'SC-005'
        >>> snomed_laterality("Hypoplastic left heart syndrome") is None
        True
    """
    return "SC-005" if _SNOMED_LATERALITY_RE.search(label or "") else None


# --- SC-007: the hardest judgement ------------------------------------------------------


def possibly_coextensive(
    subject_label: str,
    obj: Concept,
    concepts: dict[str, Concept],
) -> bool:
    """Return whether a residual-labelled object may in fact be coextensive with the subject.

    ``E76.21 Morquio mucopolysaccharidoses`` divides only into Morquio A, Morquio B and
    unspecified, so it *is* MPS IV — ``SC-007``, not ``SC-001``. ``A41 Other sepsis`` also
    subdivides into sepsis variants, and yet it is a genuine remainder, because its sibling
    ``A40 Streptococcal sepsis`` takes part of the same subject matter. Three conditions,
    all necessary, separate those two:

    1. **The subject is on the table.** The rubric's discriminating stem and the subject
       label must share at least one content token, or the ``SC-007`` reading is not even a
       candidate.
    2. **Internal coherence.** Every child carries all of the rubric's stem tokens, so the
       rubric subdivides by subtype rather than fanning out into other diseases. Token
       containment rather than substring, because the subtype letter is infixed: "Morquio
       **A** mucopolysaccharidoses".
    3. **No sibling competes for the stem.** If a sibling under the same parent also carries
       the full stem, the rubric is a remainder *within* that stem and ``SC-001`` stands.

    A leaf rubric cannot pass condition 2: with no children there is no evidence either
    way, so ``SC-001`` stands.

    A True here never decides anything. It routes the pair to adjudication with the
    children listed, and raises ``review_priority``.

    Example:
        >>> from mondo_ai.align.models import Concept
        >>> kids = {
        ...   "I:E76.210": Concept(id="I:E76.210", label="Morquio A mucopolysaccharidoses"),
        ...   "I:E76.211": Concept(id="I:E76.211", label="Morquio B mucopolysaccharidoses"),
        ...   "I:E76.219": Concept(id="I:E76.219",
        ...                        label="Morquio mucopolysaccharidoses, unspecified")}
        >>> obj = Concept(id="I:E76.21", label="Morquio mucopolysaccharidoses, unspecified",
        ...               children=list(kids), parents=["I:E76.2"])
        >>> sibling = Concept(id="I:E76.22", label="Sly mucopolysaccharidoses",
        ...                   parents=["I:E76.2"])
        >>> parent = Concept(id="I:E76.2", label="Other mucopolysaccharidoses",
        ...                  children=["I:E76.21", "I:E76.22"])
        >>> universe = {**kids, obj.id: obj, sibling.id: sibling, parent.id: parent}
        >>> possibly_coextensive("mucopolysaccharidosis type IV", obj, universe)
        True

        A remainder whose sibling holds part of the same subject matter fails condition 3:

        >>> a40 = Concept(id="I:A40", label="Streptococcal sepsis", parents=["I:A00-B99"])
        >>> a41 = Concept(id="I:A41", label="Other sepsis", parents=["I:A00-B99"],
        ...               children=["I:A41.0"])
        >>> chap = Concept(id="I:A00-B99", label="Infectious diseases",
        ...                children=["I:A40", "I:A41"])
        >>> kid = Concept(id="I:A41.0", label="Sepsis due to Staphylococcus aureus")
        >>> u2 = {c.id: c for c in (a40, a41, chap, kid)}
        >>> possibly_coextensive("sepsis", a41, u2)
        False

        A rubric that fans out into other diseases fails condition 2:

        >>> kids3 = {
        ...   "I:E88.41": Concept(id="I:E88.41", label="MELAS syndrome"),
        ...   "I:E88.42": Concept(id="I:E88.42", label="MERRF syndrome")}
        >>> obj3 = Concept(id="I:E88.4", label="Other mitochondrial metabolism disorders",
        ...                children=list(kids3))
        >>> possibly_coextensive("MELAS syndrome", obj3, {**kids3, obj3.id: obj3})
        False

        A leaf rubric cannot be shown coextensive by this test:

        >>> possibly_coextensive("x", Concept(id="I:E80.29", label="Other porphyria"), {})
        False
    """
    children = [concepts[c] for c in obj.children if c in concepts]
    if not children:
        return False
    stem_tokens = content_tokens(_discriminating_stem(obj.label))
    if not stem_tokens:
        return False
    if not (stem_tokens & content_tokens(base_normalize(subject_label))):
        return False
    if not all(
        stem_tokens <= content_tokens(base_normalize(child.label)) for child in children
    ):
        return False
    for parent_id in obj.parents:
        parent = concepts.get(parent_id)
        if parent is None:
            continue
        for sib_id in parent.children:
            if sib_id == obj.id:
                continue
            sib = concepts.get(sib_id)
            if sib is None:
                continue
            if stem_tokens <= content_tokens(base_normalize(sib.label)):
                return False
    return True


_STEM_STRIP_RE = re.compile(
    r"\b(other|specified|unspecified|not elsewhere classified|nec|nos)\b", re.I)


def _discriminating_stem(label: str) -> str:
    """Return the part of a rubric label that names its subject matter.

    Example:
        >>> _discriminating_stem("Other porphyria")
        'porphyria'
        >>> _discriminating_stem("Morquio mucopolysaccharidoses, unspecified")
        'morquio mucopolysaccharidoses'
        >>> _discriminating_stem("Unspecified")
        ''
    """
    norm = base_normalize(label or "")
    norm = norm.split(",")[0]
    norm = _STEM_STRIP_RE.sub("", norm)
    return " ".join(norm.split()).strip()


# --- assembly ---------------------------------------------------------------------------

#: Flags that forbid promoting a lexical hit straight to ``skos:exactMatch``.
BLOCKING_FLAGS = frozenset({
    "SC-001", "SC-002", "SC-003", "SC-004", "SC-005", "SC-006",
    "CM-003", "CM-006", "CM-007", "CM-009",
    "ST-004", "ST-005",
    "RJ-005",
})


def object_flags(obj: Concept, prefix: str) -> list[str]:
    """Return every flag the *object* concept triggers, as rule ids.

    Example:
        >>> from mondo_ai.align.models import Concept
        >>> object_flags(Concept(id="ICD10CM:E80.29", label="Other porphyria"), "ICD10CM")
        ['SC-001']
        >>> object_flags(Concept(id="ICD10CM:E00-E90", label="Chapter IV"), "ICD10CM")
        ['ST-005']
        >>> object_flags(Concept(id="ICD10CM:R51", label="Headache"), "ICD10CM")
        ['CM-002']
        >>> object_flags(Concept(id="ICD10CM:Q87.4", label="Marfan syndrome"), "ICD10CM")
        []
    """
    flags: list[str] = []
    for detector in (chapter_range, lambda i: foreign_shaped(i, prefix)):
        f = detector(obj.id)
        if f and f not in flags:
            flags.append(f)
    if flags:
        # A non-code identifier is terminal; no further scope analysis is meaningful.
        return flags
    if obj.obsolete:
        flags.append("ST-004")
    for detector in (residual_rubric, related_bucket, disjunctive, finer_variant,
                     injury_event):
        f = detector(obj.label)
        if f and f not in flags:
            flags.append(f)
    f = off_category(obj.id, prefix)
    if f and f not in flags:
        flags.append(f)
    if prefix == "SCTID":
        for f in (snomed_class(obj), snomed_laterality(obj.label)):
            if f and f not in flags:
                flags.append(f)
    return flags


def subject_flags(subject: Concept) -> list[str]:
    """Return every flag the *subject* concept triggers, as rule ids.

    Example:
        >>> from mondo_ai.align.models import Concept
        >>> subject_flags(Concept(id="MONDO:1", label="immunodeficiency 112"))
        ['SC-006']
        >>> subject_flags(Concept(id="MONDO:2",
        ...     label="malignant hyperthermia susceptibility type 1"))
        ['SC-006', 'CM-006']
        >>> subject_flags(Concept(id="MONDO:3", label="Marfan syndrome"))
        []
    """
    flags: list[str] = []
    for detector in (numbered_subtype, susceptibility, gene_level):
        f = detector(subject.label)
        if f and f not in flags:
            flags.append(f)
    return flags
