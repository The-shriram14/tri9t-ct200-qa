# Approach Document — Tri9T AI CT-200 Assignment

## 1. Summary

The backend ingests the supplied CT-200 PDF, reconstructs a document tree, stores each ingestion as an immutable version, and maintains stable logical node identities across versions where matching is sufficiently confident. Users can browse and search nodes, create selections pinned to exact node/version records, generate structured QA test ideas from a selection, and retrieve those generations with a current staleness status and a lightweight diff.

The implementation is intentionally scoped to the supplied CT-200 document family. It does not attempt to parse every possible PDF layout.

## 2. Parsing approach and why

I inspected the native text extraction and rendered pages of both PDFs before implementing the parser. The PDFs have usable text layers, so PyMuPDF text/layout extraction is the primary path. It preserves punctuation more accurately than OCR for these files and exposes line positions, font sizes, and bold-font information that help distinguish headings from body text. A Tesseract OCR fallback exists for pages with sparse native text; the provided files do not currently trigger that sparse-page fallback.

The parser processes lines in their visual/source order. Numbered headings are recognized using section-number patterns plus typography (bold font or larger font size). Body paragraphs, numbered lists, and table-like rows remain attached to the current section until the next recognized heading. The parser records warnings instead of silently normalizing unusual numbering or order.

The two known tables (`2.1 General Specifications` and `4.2 Error Codes`) are reconstructed with pdfplumber's grid-aware extraction and stored as Markdown table text inside their section nodes. This keeps cells grouped by row for these tables. If a table is not confidently detected, the parser retains native linearized text and emits a warning rather than pretending the cell layout is correct. More complex/unruled tables remain a known limitation.

## 3. Hierarchy reconstruction

Each parsed node stores a heading, section number, level, body text, page start, path, metadata, and SHA-256 content hash. A document root represents the manual title. For a numbered section, the parser searches for the longest existing numeric prefix as its parent. For example, `2.1.1.1` attaches to the closest existing `2.1` node because the PDF does not contain an explicit `2.1.1` heading. It retains the source numbering and adds a warning.

The parser does not sort headings numerically. This is deliberate: the source places `3.4 Auto Shutoff` before `3.3 Result Display and Classification`. Both remain children of `3 Device Operation` in the original order, and a warning makes the anomaly visible.

Repeated heading text is not used as a unique identifier. The same “Error Codes” title occurs in `4.2 Error Codes` and `7.1 Error Codes`; these are distinct nodes under different parents and have distinct version-specific IDs and paths.

## 4. Structural inconsistencies observed in the supplied PDFs

1. **Skipped hierarchy level:** `2.1.1.1 Battery Life Under Typical Use` follows `2.1 General Specifications`, with no explicit `2.1.1` node. The parser retains the literal section number, attaches it to the nearest existing ancestor, and emits a warning.
2. **Out-of-order section numbering:** `3.4 Auto Shutoff` appears before `3.3 Result Display and Classification`. Source order is preserved and a warning is emitted.
3. **Duplicate heading text:** “Error Codes” appears under section 4 (alarm/error behavior) and section 7 (troubleshooting). Tests assert that these nodes remain separate and have the correct parents.
4. **Tables:** Section `2.1 General Specifications` and section `4.2 Error Codes` contain tables. pdfplumber reconstructs these two tables as Markdown rows, and tests verify representative cells. More complex tables may still fall back to linearized text, with a warning and metadata rather than a silent guess.
5. **Paragraphs spanning page breaks:** In v2, the cleaning instructions in section 6.1 continue onto the following page, and section 8.1 continues onto the last page. The line stream remains attached to the current section until the next recognized heading.
6. **Version content changes:** v2 revises battery-life estimates and the low-battery threshold, changes cuff inflation increments, changes the E3 auto-deflation time, adds E6, and adds section 5.3 Data Export. These are useful real changes for the versioning and stale-generation demonstration.

## 5. Debugging and validation process

During parser iteration, the title's three visual lines were initially at risk of being retained as root body text. The parser now suppresses the title area and uses a stable document-root heading. A purely numeric heading sort would also have changed the source's `3.4`/`3.3` order; the implementation now retains source order and reports the anomaly. Finally, parent sections often have little or no direct body text because their content belongs to child sections. The generation path therefore reconstructs the selected node's entire subtree, not only its direct body, and snapshots that exact text.

Validation includes:

- Visual inspection of rendered pages containing the specifications and error-code tables.
- Unit tests for duplicate headings, the missing `2.1.1` level, the `3.4`/`3.3` order anomaly, and table-like text retention.
- An API-level test that ingests v1 and v2, checks version preservation and logical-node matching, then verifies retrieval flags a generation based on changed v1 text as stale.
- Manual comparison of changed requirement text in v1 and v2.

The tests do not prove the parser is perfect; they lock down the observed failure modes and make future regressions visible.

## 6. Data model

SQLite/SQLAlchemy stores:

- `DocumentVersion`: document key, sequential version number, filename, timestamp, and latest-version status.
- `DocumentNode`: version-specific node ID, stable logical ID, parent ID, heading, section number, level, body text, hash, path, page, and metadata.
- `Selection`: name and creation time.
- `SelectionItem`: exact node ID, which points to the node in its original document version.

Each version receives new version-specific node IDs. Matching nodes share a logical ID, while their per-version text and hashes remain immutable. This avoids overwriting the v1 source when v2 is ingested.

Generated output is stored in a separate JSON file, which is explicitly allowed by the assignment. Writes use a temporary file plus atomic replacement. This is sufficient for a local single-process demonstration but is not a substitute for a transactional database under concurrent production writes.

## 7. Version matching strategy and failure modes

Matching proceeds in this order:

1. Exact structural path match.
2. Match by section number and matched parent logical ID, preferring exact heading text.
3. If there is one plausible candidate, fuzzy heading similarity of at least 0.78 may retain the logical identity.
4. If no sufficiently confident match exists, create a new logical ID rather than force a match.

This handles the supplied v1/v2 changes because changed requirements generally retain the same section number and heading. Body changes do not change logical identity. A new section such as `5.3 Data Export` receives a new logical ID.

Known weaknesses: a renumbered section whose title and parent both change may not match; similar duplicate headings under the same parent can be ambiguous; a fuzzy threshold can still make a false match. A production implementation should store match confidence and potentially require review for ambiguous mappings. This prototype does not claim semantic identity detection.

## 8. LLM prompt, schema, retry, and repeated submissions

The prompt tells the model to use only the supplied source text, avoid invented thresholds/timing/error behavior, and return 3–5 QA test cases with title, preconditions, executable steps, expected result, source node IDs, and rationale. Pydantic validates the output structure. The implementation also verifies that every cited source node ID belongs to the submitted selection.

If JSON parsing or schema validation fails, the backend retries once with a corrective instruction. If the second attempt fails, the endpoint returns a clear error and does not store an invalid successful generation. A missing API key or provider failure also returns a clear API error. This is preferable to returning plausible-looking but invalid output.

Each successful submission creates a new generation record, even if the same selection was submitted previously. Earlier output is not overwritten, so users can inspect generation history. This policy avoids a hidden cache and makes repeated model output auditable.

## 9. Staleness and impact detection

A generation stores the exact selected subtree text, its source node/version, and a hash of that selected text. At retrieval time, the API finds the latest node with the same logical ID and reconstructs its subtree text. If the hash matches, the source is reported as `current`. If it differs, the status is `stale_review_required` and a lightweight text diff is returned. If no corresponding node can be found, the status is `removed_or_unmatched`.

Staleness is conservative. A one-word wording change is treated as a change just as a numeric threshold change is; the diff shows the changed text but does not classify clinical significance. Any change in a selected subtree can flag test cases sourced from that subtree. This is a review signal, not an assertion that a test case is definitely incorrect. Semantic impact analysis and automatic regeneration are out of scope.

The status is exposed through `GET /generations/{generation_id}`, `GET /selections/{selection_id}/generations`, and `GET /nodes/{node_id}/generations`; it is not hidden inside an unqueryable helper.

## 10. API and operational demonstration

FastAPI exposes ingestion, version listing, section browsing, node retrieval, search, version comparison, selection creation/retrieval, generation, and generation retrieval. The README includes commands for uploading v1 then v2 and checking version and stale-status results. Swagger UI at `/docs` is sufficient for manual exploration; no frontend is required.

## 11. Decision log (required)

**What is most likely to silently give wrong results, and how would you catch it?**
PDF hierarchy reconstruction is most likely to silently produce incorrect results because the CT-200 manual contains a skipped subsection level, duplicate headings, and sections out of numerical order. I added tests for these irregularities and would compare the extracted hierarchy against the original PDF to catch incorrect parent-child relationships.

**Where did you choose simplicity over correctness because of time, and what would break first in production?**
I used a JSON-file store for generated QA outputs instead of introducing another database service to keep the assignment implementation simple. Complex or merged-cell tables could be the first extraction weakness, while concurrent writes could cause reliability problems in production.

**Name one input you did not handle and what happens when it appears.**
I did not implement confident matching for a section whose numbering, heading, and parent all change between document versions. In this case, the system assigns a new logical ID instead of forcing a potentially incorrect match, and the older section can be reported as removed or unmatched.

## 12. What I would improve with more time

- Reconstruct table cells with layout-aware table extraction and compare each cell against rendered-page OCR.
- Store node bounding boxes and character-level source locations for better traceability.
- Add explicit match-confidence scores and review workflows for uncertain logical-node matches.
- Use a transactional document store for generations and add concurrency tests.
- Add semantic impact classification as a separate, clearly non-authoritative signal; retain conservative stale flags regardless.
- Test more corrupted PDFs, rotated pages, and pages where native text is present but badly ordered.
