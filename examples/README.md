# Examples of using mondo-ai

This folder contains examples using the datamodel.

The source of the data used in the example is [tests/data](../tests/data/).

The command `just test` creates different representations of the data in [tests/data](../tests/data/) and writes them to the subfolder `output`.
It also generates a markdown documentation of the examples which is not very useful in its current form.
Hence, the `output` sub-folder is git-ignored.

[packets](packets/) has three merged evidence packets from `packet_store_merged/`, one per
claim type, each supported by more than one source:

- `MappingStatement_0008e99cabbb.yaml`: MONDO:0008045 skos:exactMatch OMIM:159950, 2 sources
- `SubclassStatement_02b02f03c22c.yaml`: MONDO:0009290 subClassOf MONDO:0002412, 3 sources
- `SynonymStatement_05b667e04d79.yaml`: MONDO:0008667 exact synonym "Von Hippel-Lindau
  syndrome", 4 sources in 3 independent clusters
