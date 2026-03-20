# Task: Investigate and Resolve MONDO-HP Phenotype-Disease Conflation

## Summary

Concepts that exist in both MONDO (as diseases) and HPO (as phenotypes) are causing equivalence clique issues downstream, notably in Translator. A strategy is needed to categorize, and where appropriate resolve, these overlapping terms — without disrupting existing users or creating unsustainable maintenance burden.

## Background

Certain terms (e.g., "diarrheal disease" — `MONDO:0001673` / `HP:0002014`) sit in a grey area between phenotype and disease. They appear in both MONDO and HPO for historical reasons, and there is no consensus on whether they should be classified as one or the other — in many cases, the answer is context-dependent.

An initial analysis has been drafted (`PHENO_CONFLATION.md`) proposing a decision tree for classifying these terms, and has been linked to the tracking issue: [monarch-initiative/mondo#7359](https://github.com/monarch-initiative/mondo/issues/7359).

## Problem

- **Downstream impact:** MONDO-HP equivalence cliques surface in Translator and potentially other consumers (e.g., Babel/NodeNormalizer).
- **No clean boundary:** Some terms are legitimately both phenotypes and diseases depending on context; a blanket rule won't work.
- **User reliance:** Clinicians and projects (e.g., ClinGen, OTAR) actively use many of these terms for data annotation. Mass obsoletion would break production workflows.
- **Maintenance burden:** Retaining ambiguous terms without any categorization increases long-term technical debt.

## Constraints

- Any obsoletion or renaming requires manual review per current Mondo SOP.
- Terms actively used by the rare disease community (e.g., ClinGen-referenced terms) must be preserved or handled with a higher barrier.
- Current Mondo team capacity is limited to urgent user requests, OMIM/Orphanet alignment, and paper writing.

## Proposed Investigation

1. **Categorize conflated terms** — Develop a typology for the overlapping concepts (e.g., "high-level etiology-neutral phenotype-shadowing grouping class") so each term can be tagged without necessarily being obsoleted.
2. **Refine the decision tree** — Iterate on the analysis in `PHENO_CONFLATION.md`, ideally through agentic/automated passes followed by manual review of edge cases.
3. **Assess user impact** — Identify which conflated terms are in active use (ClinGen, OTAR, clinical annotation workflows) and tier them by obsoletion risk.
4. **Determine resolution actions per category:**
   - Obsolete (low-use, clearly phenotype-only terms)
   - Retain but tag/categorize (actively used or genuinely dual-nature terms)
   - Rename or restructure (where the label is misleading)
5. **Downstream recommendations** — Provide guidance to Babel/NodeNormalizer on how to handle remaining overlaps.
6. **Consider a tracking dashboard** — Explore building a dedicated site or dashboard to monitor conflation status across terms.

## Open Questions

- Should the analysis and recommendations live in the existing GitHub issue (#7359), a new dedicated repo/document, or a standalone site?
- Can SOP barriers for obsoletion be adjusted based on term category/usage tier?
- How should Translator and other consumers handle terms that are intentionally kept in both ontologies?

## Next Steps

- [ ] Schedule a call with Mondo team to align on approach and priorities
- [ ] Iterate on `PHENO_CONFLATION.md` analysis (collaboratively or via agentic passes)
- [ ] Prototype term categorization on a subset of clear-cut cases
- [ ] Draft downstream guidance for Babel/NodeNormalizer