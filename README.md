# mondo-ai

AI-powered tools for Mondo disease ontology curation and quality assurance.

Two subsystems, independent of each other:

- **Evidence pipeline** — extracts candidate assertions (synonyms, subclass relations,
  definitions) from source ontologies, packages each with typed evidence and provenance,
  scores it, and stores it for curator review. See [`SPEC.md`](SPEC.md).
- **Ontology alignment** — turns two resources into an SSSOM mapping set where every row
  names the curation rules its verdict rests on, plus a triage table that says which rows
  need a human. See [`docs/alignment.md`](docs/alignment.md) and
  [the design spec](specs/2026-09-25-agentic-ontology-alignment-design.md).

```bash
just align-download     # Mondo + ICD-10-CM, with checksums
just align-smoke 300    # 300 subjects end to end, in seconds
just align-icd10cm      # the full reference run
just align-queue        # the top of the review queue
```

## Installation

```bash
pip install mondo-ai
```

## Development Setup

1. Clone the repository:
```bash
git clone https://github.com/monarch-initiative/mondo-ai.git
cd mondo-ai
```

2. Initialize the project:
```bash
just setup
```

3. Run tests:
```bash
just test
```

4. Run linting:
```bash
just lint
```

## Documentation

See the [documentation](https://monarch-initiative.github.io/mondo-ai/) for more details.

## License

This project is licensed under the Apache-2.0 License - see the [LICENSE](LICENSE) file for details.

## Acknowledgements

This project uses the [Monarch Project Copier](https://github.com/monarch-initiative/monarch-project-copier) template.
