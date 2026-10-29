# Health knowledge corpus

This package is D3's bounded evidence boundary. `source_registry.json` records
publisher authority, license-review state, and whether a source may be used at
runtime. `corpus_manifest.json` allowlists frozen documents. `snapshots/`
contains concise paraphrased chunks with source URLs, retrieval dates, and
content hashes. `registry.py` rejects identity or hash mismatches and excludes
conditional sources such as NICE until approval is recorded.

The initial index is deliberately small and deterministic. It covers selected
NHS guidance for chest-pain safety, adult physical activity, and fatigue. It is
not an open-web search, a diagnostic knowledge base, or a substitute for
clinical governance. When no approved chunk matches, D3 returns insufficient
evidence instead of asking a language model to answer from memory.

To add a document, review source authority and redistribution terms, create a
dated paraphrased snapshot, compute its canonical chunk-content SHA-256 with
`registry.canonical_content_hash`, add the exact identity to the manifest, and
add offline retrieval, provenance, abstention, and safety tests.
