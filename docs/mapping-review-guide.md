# Mondo Mapping Review — a guide for domain experts

**Who this is for.** Clinicians and domain experts asked to check disease mappings. It assumes
no knowledge of ontologies, identifiers or SKOS, and it does not teach any. It teaches one
judgement, which you already have: *do these two things mean the same disease?*

**Why you.** A machine proposes the mappings. Your verdicts are the standard its accuracy is
measured against — literally: the pipeline's reported precision is agreement with reviewers
like you. A careful "unsure" is worth more to us than a confident guess.

Module 1 is also a ~15-minute slide deck built around one running example, with three real pairs
the audience decides before the reasoning is revealed. This page is the long form behind it, and
the place new modules get added.

| Module | Covers | Status |
|---|---|---|
| **1** | Exact and narrow matches: concepts vs codes, the two-question test, value sets, the traps — worked end to end on cystic fibrosis | this page + deck |
| 2 | Broad, close and related matches; when to assert nothing | planned |
| 3 | Disease vs phenotype vs susceptibility vs gene — the category questions | planned |
| 4 | Groupings, subtype families and hierarchy-sensitive calls | planned |

---

## 1. The two sides are different kinds of thing

This is the one structural fact worth internalising, because it changes how you read every
pair.

**Concepts** — Mondo, SNOMED CT, DO. Each entry is *one disease*, defined by what it is.
Nothing is a leftover. No two entries are meant to denote the same thing.

**Codes** — ICD-10-CM, LOINC. Each entry is *a box you file a case into*, for counting and
billing. Every encounter has to land somewhere, and that requirement forces catch-all boxes
into existence: "other", "unspecified", "not elsewhere classified". **Those boxes are not
diseases.** They exist so the classification can be exhaustive.

ICD is not badly made. It is well made for a different job. Counting and billing need
exhaustive buckets; integrating knowledge needs defined concepts. The mismatch is structural,
not sloppiness — and it is the source of most of the hard calls you will see.

So the question is never *"do these two labels mean the same thing?"* It is:

> **Does this box hold exactly this disease — no more, no less?**

---

## 2. The method: which patients does the code cover?

Stop reading the labels. Picture the patients each side would include, and compare the two
groups. There are only two questions.

**Nothing extra** — does everyone the code covers have *this* disease?
If no, **the code is broader**.

**Nothing missing** — does everyone with this disease fall *somewhere under* the code?
If no, **the code is narrower**.

Both yes: **exact match**.

### One word does all the work

**"Covers" means the code and everything filed beneath it.** It does *not* mean "the code a
coder would key in".

Those come apart immediately. Nobody is ever assigned plain `E84` — a CF patient with pulmonary
manifestations is coded `E84.0`. Ask "would this patient get `E84`?" and you would reject a
correct exact match. Ask "is this patient somewhere under `E84`?" and the answer is yes.

> **If you have seen an earlier version of this guide.** An earlier draft phrased the second
> question as *"does everyone with this disease get this code?"*. That is wrong, for exactly the
> reason above: it asks about code assignment rather than extension, and it would make every
> heading-level code fail. Use "falls somewhere under".

Differences that are **not** differences: word order, possessives, British/American spelling,
`disease` vs `disorder`, singular vs plural, an eponym against a descriptive name for the same
entity.

### The one mechanical fact to memorise

Your answers get written down as `exactMatch`, `narrowMatch` and `broadMatch`. The word
**always describes the code**, never the Mondo term. `narrowMatch` means *the code is narrower*.

People reliably get this backwards, because English attaches the adjective to the subject. In
this notation it attaches to the object.

---

## 2a. Worked examples: cystic fibrosis, end to end

One disease, and every case you will meet. All of these are real, and so is what the machine
said about them.

Mondo has `cystic fibrosis` — an autosomal recessive disorder caused by pathogenic variants in
*CFTR*. ICD-10-CM has this neighbourhood:

```
E70-E88   Metabolic disorders            ← a RANGE, not a code
  E84       Cystic fibrosis
    E84.0     ... with pulmonary manifestations
    E84.1     ... with intestinal manifestations
      E84.11    Meconium ileus in cystic fibrosis
      E84.19    ... with other intestinal manifestations
    E84.8     ... with other manifestations
    E84.9     Cystic fibrosis, unspecified
```

**Why does `E84.9 Cystic fibrosis, unspecified` exist?** No clinician has "unspecified cystic
fibrosis". It exists because every encounter must be filed somewhere and sometimes the note does
not record the manifestation. The box is an artefact of needing to be exhaustive — which is the
concept/code difference, made concrete.

| Pair | Verdict | Why |
|---|---|---|
| cystic fibrosis / `E84` | **exact** | Its four children are all *manifestations of CF*, not other diseases. Nothing extra, nothing missing. **Heading level is not a disqualifier.** |
| cystic fibrosis / `E84.0` | code **narrower** | Nothing extra — but CF patients with only intestinal disease sit under `E84.1`. Something missing. |
| cystic fibrosis / `E84.9` | code **narrower** | A residual box, but a residual box *inside* CF. Everyone in it has CF; most CF patients are not in it. **"Unspecified" is not automatically broader.** |
| cystic fibrosis / `E70-E88` | **invalid target** | A range, not a code. Nobody is ever assigned it. Reject without considering patients at all. |
| cf-associated meconium ileus / `E84.11` | **exact** | Both questions yes. The machine returned *no match* here — the labels share no identical string ("cystic fibrosis associated meconium ileus" vs "Meconium ileus in cystic fibrosis"). A clinician sees one concept instantly. |
| cf-related diabetes / `E13 Other specified diabetes mellitus` | code **broader** | `E13` also holds post-pancreatectomy and drug-induced diabetes. It is the right place to *code* CFRD and the wrong thing to call equivalent. **Mondo asserts this one as equivalent — a human wrote it, and it is still wrong.** |

Three things that table is designed to teach:

- **Depth decides nothing.** `E84` at heading level is exact; `E84.11` five characters deep is
  exact. Only the patients decide.
- **"No match" proposals need reviewing as hard as confident ones.** A missed mapping is a gap
  nobody will notice — you are the only person who will look at that pair.
- **Existing mappings are reviewable.** "A human wrote it" is provenance, not proof. Curators
  work at speed and inherit assertions from other resources.

### Check the stated reason, not only the verdict

When a proposal tells you *why* it matched, read that too. Mondo lists
*"pseudomonas aeruginosa, susceptibility to chronic infection by, in cystic fibrosis"* as an
**exact synonym** of cystic fibrosis. It is not one — it is inherited from a gene-centric
source. A correct verdict resting on a bad synonym is worth flagging, because the same bad
synonym will produce wrong matches elsewhere.

---

## 3. Value sets answer a different question

Most domain experts arrive with a cohort-definition reflex: *which codes would I pull to find
these patients?* That reflex is correct for its own job and it produces confident wrong
answers on this one.

| | Finding patients | Mapping |
|---|---|---|
| The question | "Which codes should I pull to catch these patients?" | "Does this one code mean this one disease?" |
| The answer | A **set** of codes, deliberately wide or tight depending on whether you need sensitivity or specificity | **One code at a time**, and about meaning rather than retrieval |
| Tuned to | Your study's purpose | Nothing — it gets reused by people whose purpose you will never see |

To find CF patients you would pull `E84` and everything under it, and quite possibly screen
`P09.4 Abnormal findings on neonatal screening for cystic fibrosis` charts too. A perfectly good
value set — and `P09.4` plainly does not *mean* cystic fibrosis. It is a screening result, not a
diagnosis.

> **"I would use this code to find these patients" is never a reason to answer exact match.**

If the code you would actually use is not an exact match, say it is broader or narrower. That
is useful information, not a failure to answer.

---

## 4. The three traps

Nearly every machine error we see has one of these three shapes, and all three share a
feature: the labels overlap heavily and the extensions do not. That is exactly the case a
language model handles badly and a clinician handles well.

**The leftover box.** "Other", "unspecified", NEC, NOS. `E80.29 Other porphyria` is where
hereditary coproporphyria gets coded — the disease is filed there, and the box is still not the
disease.

**Two diseases in one code.** `G71.01 Duchenne or Becker muscular dystrophy` covers both, so it
is broader than either, by construction.

**A code above your subtype.** `E24 Cushing's syndrome` also holds Nelson's, drug-induced and
ectopic-ACTH Cushing's. Check whether a subtype-specific code exists before accepting the family
code — for Cushing disease due to pituitary adenoma, `E24.0 Pituitary-dependent Cushing's
disease` exists and is the right target. The machine asserted `E24` as an exact match at 0.88
confidence; it was wrong.

Compare `E24` with `E84`. **Same shape** — a heading with children — and opposite answers:
`E84`'s children are manifestations of one disease, `E24`'s children *are* different diseases.
Nothing about the label or the depth tells you which situation you are in. Only reading the
children does.

### The exception, and it is the hardest call in the task

**Wording alone never settles it.** `E76.21 Morquio mucopolysaccharidoses` divides only into
Morquio A, Morquio B and unspecified — its patients are exactly the MPS IV patients, so it *is*
an exact match, "unspecified" child and all.

Do not leave with the rule "unspecified means no". The instruction is: **look at what a code's
own subdivisions actually are**, not at what its label sounds like. If you cannot tell, that is
a legitimate "unsure" and we would rather have it flagged than guessed.

---

## 5. How to answer

For each pair, we want three things.

1. **A verdict**: exact match · the code is narrower · the code is broader · related but
   neither · no suitable code.
2. **One sentence**, naming the code and what makes it right or wrong. "No suitable match" on
   its own is not usable — say what the closest code was and why it fails.
3. **How sure you are.** Say "unsure" freely.

Two rules that matter more than they look:

- **Ambiguity is not a match.** Two codes equally defensible means *neither*. Record that, do
  not pick one. A recorded absence is honest; an arbitrary choice is a crosswalk nobody can
  trust.
- **A wrong exact match is worse than a gap.** It silently pulls the wrong patients into
  someone else's study, years later, with nothing to signal the error. A gap is visible and
  fixable.

---

## 6. What happens to your answer

Your verdict is recorded against the mapping with the reason you gave, and it is used three
ways: to correct that row; to measure the machine (reviewer agreement is the pipeline's
reported precision); and to find systematic errors — when several reviewers reject the same
*kind* of mapping, that becomes a rule the machine applies from then on.

Those rules are a published set with stable identifiers, so a mapping can cite the reasoning it
rests on rather than asserting a bare answer. You do not need to learn them to review, and the
[rule registry](maprule.md) is there if you ever want to see what your verdicts turn into.

For the machinery behind all of this, see [Ontology alignment](alignment.md).
