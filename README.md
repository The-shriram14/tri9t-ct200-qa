# Tri9T AI — CT-200 Document QA Traceability API

A focused FastAPI backend for the supplied CardioTrack CT-200 manuals. It extracts a hierarchical document tree, stores immutable document versions, saves version-pinned selections, generates structured QA test ideas with an LLM, and reports when generated tests may be stale after re-ingestion.

## What is implemented

- PDF text/layout extraction with PyMuPDF, grid-aware table extraction with pdfplumber, and a Tesseract OCR fallback for sparse text-layer pages (when available).
- Heading hierarchy based on section numbering and PDF typography; source order is preserved.
- SHA-256 content hashes per node and stable logical IDs matched across versions.
- SQLite + SQLAlchemy for versions, nodes, parent/child relationships, and selections.
- JSON-file storage for generated LLM output (atomic replace on write; intended for this single-process assignment demo).
- FastAPI browse/search/compare/selection/generation/retrieval endpoints.
- Pydantic structured-output validation, one retry (two attempts total), and clear failure if the response remains invalid.
- Retrieval-time staleness status and a lightweight text diff.
- Parser and end-to-end version/staleness tests.

## Setup

Python 3.11+ is recommended.

```bash
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# macOS/Linux:
# source .venv/bin/activate
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and set `LLM_API_KEY` to a valid Groq API key to use the live generation endpoint. The rest of the API and tests do not need an LLM key. The application loads `.env` automatically through `python-dotenv`. Never commit the real key.

Run the API from this directory:

```bash
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000/docs` for interactive Swagger API documentation.

## Run tests

```bash
python -m pytest -q
```

The tests use the two supplied PDFs. They check the duplicate “Error Codes” heading, the skipped `2.1.1` level, the `3.4`/`3.3` source order, preservation of table-like text, version matching, and stale-generation retrieval.

## Demonstrate v1 → generate → v2 → stale retrieval

The order matters: generate test cases from v1 **before** uploading v2. The included script demonstrates the complete flow with a configured LLM key. Start the API in one terminal:

```bash
uvicorn app.main:app --reload
```

In a second terminal, run:

```bash
python scripts/demo_staleness.py
```

The script uploads v1 as a new version, finds `2.1.1.1 Battery Life Under Typical Use`, creates a version-pinned selection, generates 3–5 QA ideas, uploads v2, then retrieves the original generation. It expects the v1 battery-life source to report `stale_review_required`. The script prints parser warnings and IDs so the flow is auditable. Running it again creates later sequential versions; it does not overwrite earlier versions.

You can also run the steps manually. Upload v1 first:

```bash
curl -X POST http://127.0.0.1:8000/documents/ct200/ingest \
  -F "file=@data/ct200_manual.pdf"
```

Browse/search v1, create a selection, and call `POST /selections/{selection_id}/generations` before uploading v2. Save the returned generation ID. Then upload v2:

```bash
curl -X POST http://127.0.0.1:8000/documents/ct200/ingest \
  -F "file=@data/ct200_manual_v2.pdf"
```

Confirm both versions exist and retrieve the old generation:

```bash
curl http://127.0.0.1:8000/documents/ct200/versions
curl http://127.0.0.1:8000/generations/GENERATION_ID
```

To inspect the section diff independently, take a v1 node ID from the search results and call:

```bash
curl "http://127.0.0.1:8000/nodes/NODE_ID/changes"
```

### Generate and retrieve test cases

1. In `GET /documents/ct200/sections`, browse to the desired section. Use `GET /search` or `GET /nodes/{node_id}` to locate the exact node ID.
2. Create a selection with the exact node ID(s):

```bash
curl -X POST http://127.0.0.1:8000/selections \
  -H "Content-Type: application/json" \
  -d '{"name":"Battery safety tests","node_ids":["NODE_ID_FROM_API"]}'
```

3. Generate test-case ideas (requires `LLM_API_KEY`):

```bash
curl -X POST http://127.0.0.1:8000/selections/SELECTION_ID/generations
```

4. Save the returned generation ID and retrieve it later, especially after ingesting a new manual version:

```bash
curl http://127.0.0.1:8000/generations/GENERATION_ID
curl http://127.0.0.1:8000/selections/SELECTION_ID/generations
curl http://127.0.0.1:8000/nodes/NODE_ID/generations
```

The same selection can be submitted repeatedly; each successful request creates a new generation record and does not overwrite earlier output. If the source text changes after v2 ingestion, retrieval reports `stale_review_required` and includes a text diff. A changed source is a review signal, not proof that a test case is incorrect.

## API overview

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/health` | Health check |
| `POST` | `/documents/{document_key}/ingest` | Ingest the next version of a PDF |
| `GET` | `/documents/{document_key}/versions` | List preserved versions |
| `GET` | `/documents/{document_key}/sections?version=` | List top-level sections; defaults to latest |
| `GET` | `/nodes/{node_id}` | Node, hash, full subtree text and children |
| `GET` | `/search?q=&version=` | Search headings and body text |
| `GET` | `/nodes/{node_id}/changes` | Compare a node with latest or selected version |
| `POST` | `/selections` | Create a version-pinned selection |
| `GET` | `/selections/{selection_id}` | Retrieve exact selected node/version text |
| `POST` | `/selections/{selection_id}/generations` | Generate and store 3–5 QA ideas |
| `GET` | `/generations/{generation_id}` | Retrieve generated cases with current staleness status |
| `GET` | `/selections/{selection_id}/generations` | Retrieve generations by selection |
| `GET` | `/nodes/{node_id}/generations` | Retrieve generations linked to a logical node |

## Known limitations

- This is a document-specific prototype, not a generic parser for arbitrary PDFs.
- The two known CT-200 tables are reconstructed as Markdown rows; complex or merged-cell tables may fall back to linearized text and should be visually reviewed.
- Logical matching can be ambiguous if section numbering, parent structure, and heading text all change; ambiguous matches may be reported as removed/unmatched.
- Staleness is conservative: any change in a selected node's subtree marks the generated cases for review. It does not understand clinical severity or decide whether a wording change is material.
- The JSON generation store is appropriate for a single-process assignment demo; multi-process concurrent writers and larger production workloads should use a transactional store such as MongoDB.
- The OCR fallback is used only for sparse text-layer pages. The supplied PDFs have usable text layers, so native text/layout extraction does most of the work; visual comparison remains necessary.
