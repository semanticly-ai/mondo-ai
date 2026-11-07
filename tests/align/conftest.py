"""Fixtures for the alignment tests.

The fixtures are deliberately small *real* artefacts — an actual OBO file, an actual RDF/XML
file — rather than mocks of the loaders. A mock adapter would test that the test knows what
the adapter does, which is the one thing never in doubt.

The object-side fixture encodes the cases the pipeline exists to get right:

* ``Q87.4 Marfan's syndrome`` — the possessive rewrite (``EV-004``)
* ``E80.29 Other porphyria`` — a residual rubric (``SC-001``)
* ``E76.21`` and children — a rubric coextensive with its subject (``SC-007``)
* ``G71.01 Duchenne or Becker muscular dystrophy`` — a disjunction (``SC-004``)
* ``E00-E90`` — a chapter range, not a code (``ST-005``)
* ``R48.1`` — an off-category chapter (``CM-002``)
* ``A00`` / ``A01`` — a clean pair, and a homonym trap for the ambiguity path
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from mondo_ai.align.adapters import load_obo, load_owl
from mondo_ai.align.index import build_index
from mondo_ai.align.models import ResourceSet

SUBJECT_OBO = """\
format-version: 1.2
data-version: releases/2026-09-01

[Term]
id: MONDO:0000001
name: disease or disorder

[Term]
id: MONDO:0000100
name: cholera
synonym: "Vibrio cholerae infectious disease" EXACT []
is_a: MONDO:0000001
xref: ICD10CM:A00 {source="MONDO:equivalentTo"}

[Term]
id: MONDO:0007947
name: Marfan syndrome
def: "A disorder of connective tissue." [PMID:1]
is_a: MONDO:0000001
xref: ICD10CM:Q87.4 {source="MONDO:equivalentTo"}

[Term]
id: MONDO:0009654
name: mucopolysaccharidosis type IV
is_a: MONDO:0000001
xref: ICD10CM:E76.21 {source="MONDO:equivalentTo"}

[Term]
id: MONDO:0008755
name: hereditary coproporphyria
is_a: MONDO:0000001

[Term]
id: MONDO:0010679
name: Duchenne muscular dystrophy
is_a: MONDO:0000001
xref: ICD10CM:G71.01 {source="MONDO:mondoIsNarrowerThanSource"}

[Term]
id: MONDO:0005066
name: metabolic disease
is_a: MONDO:0000001
xref: ICD10CM:E00-E90 {source="MONDO:equivalentTo"}

[Term]
id: MONDO:0002169
name: astereognosia
is_a: MONDO:0000001

[Term]
id: MONDO:0011111
name: immunodeficiency 112
is_a: MONDO:0000001

[Term]
id: MONDO:0022222
name: malignant hyperthermia, susceptibility to
is_a: MONDO:0000001

[Term]
id: MONDO:0033333
name: obsolete thing
is_obsolete: true
"""

OBJECT_OWL = """\
<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
  xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
  xmlns:owl="http://www.w3.org/2002/07/owl#"
  xmlns:oio="http://www.geneontology.org/formats/oboInOwl#">
  <owl:Ontology rdf:about="http://example.org/icd10cm">
    <owl:versionIRI rdf:resource="http://example.org/icd10cm/2024ab"/>
  </owl:Ontology>
{classes}
</rdf:RDF>
"""

_CLASS = """\
  <owl:Class rdf:about="http://purl.bioontology.org/ontology/ICD10CM/{code}">
    <rdfs:label>{label}</rdfs:label>
{extra}  </owl:Class>
"""

#: (code, label, parent, exact synonyms)
OBJECT_CLASSES = [
    ("E00-E90", "Endocrine, nutritional and metabolic diseases", "", []),
    ("A00", "Cholera", "", ["Vibrio cholerae infection"]),
    ("A01", "Typhoid and paratyphoid fevers", "", []),
    ("Q87", "Other specified congenital malformation syndromes", "", []),
    ("Q87.4", "Marfan's syndrome", "Q87", []),
    ("E76.2", "Other mucopolysaccharidoses", "", []),
    ("E76.21", "Morquio mucopolysaccharidoses", "E76.2", []),
    ("E76.210", "Morquio A mucopolysaccharidoses", "E76.21", []),
    ("E76.211", "Morquio B mucopolysaccharidoses", "E76.21", []),
    ("E76.219", "Morquio mucopolysaccharidoses, unspecified", "E76.21", []),
    ("E76.22", "Sly syndrome", "E76.2", []),
    ("E80.2", "Porphyria", "", []),
    ("E80.29", "Other porphyria", "E80.2", ["Hereditary coproporphyria"]),
    ("G71.01", "Duchenne or Becker muscular dystrophy", "", []),
    ("R48.1", "Astereognosia", "", []),
    ("M08.011", "Juvenile rheumatoid arthritis, right shoulder", "", []),
    ("M08.012", "Juvenile rheumatoid arthritis, left shoulder", "", []),
    ("M08.019", "Juvenile rheumatoid arthritis, unspecified shoulder", "", []),
    ("M08", "Juvenile arthritis", "", []),
]


def _object_owl_text() -> str:
    parts = []
    for code, label, parent, synonyms in OBJECT_CLASSES:
        extra = ""
        if parent:
            extra += (
                f'    <rdfs:subClassOf rdf:resource='
                f'"http://purl.bioontology.org/ontology/ICD10CM/{parent}"/>\n'
            )
        for syn in synonyms:
            extra += f"    <oio:hasExactSynonym>{syn}</oio:hasExactSynonym>\n"
        parts.append(_CLASS.format(code=code, label=label, extra=extra))
    return OBJECT_OWL.format(classes="".join(parts))


@pytest.fixture(scope="session")
def fixture_dir(tmp_path_factory) -> Path:
    """Write the fixture files once per session."""
    d = tmp_path_factory.mktemp("align-fixtures")
    (d / "subject.obo").write_text(textwrap.dedent(SUBJECT_OBO))
    (d / "object.owl").write_text(_object_owl_text())
    return d


@pytest.fixture(scope="session")
def subjects(fixture_dir: Path) -> ResourceSet:
    """The subject resource, loaded from the real OBO fixture."""
    return load_obo(
        fixture_dir / "subject.obo", "MONDO", keep_xref_prefixes=("ICD10CM",)
    )


@pytest.fixture(scope="session")
def objects(fixture_dir: Path) -> ResourceSet:
    """The object resource, loaded from the real RDF/XML fixture."""
    return load_owl(
        fixture_dir / "object.owl",
        "ICD10CM",
        "http://purl.bioontology.org/ontology/ICD10CM/",
    )


@pytest.fixture(scope="session")
def object_index(objects: ResourceSet):
    """A finalised index over the object resource."""
    return build_index(objects.active())
