from __future__ import annotations
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Query
from sqlalchemy.orm import Session, selectinload
from sqlalchemy import select, desc
from uuid import uuid4
from pathlib import Path
from tempfile import NamedTemporaryFile
from difflib import SequenceMatcher
import json

from .db import Base, engine, get_db
from .models import DocumentVersion, DocumentNode, Selection, SelectionItem
from .schemas import SelectionCreate
from .parser import parse_pdf, ParsedNode, sha256_text
from .versioning import diff_summary, heading_similarity
from .generation_store import add_generation, list_generations, get_generation
from .llm import generate_test_cases, LLMGenerationError
from .config import DOCUMENT_KEY

Base.metadata.create_all(bind=engine)
app = FastAPI(title="Tri9T CT-200 QA Traceability API", version="0.1.0",
              description="Parse CT-200 manuals, version their hierarchy, generate QA ideas, and report stale sources.")


def _latest_version(db: Session, key: str = DOCUMENT_KEY):
    return db.scalar(select(DocumentVersion).where(DocumentVersion.document_key == key).order_by(desc(DocumentVersion.version_number)))


def _get_version(db: Session, key: str, version_number: int | None):
    if version_number is None:
        version = _latest_version(db, key)
    else:
        version = db.scalar(select(DocumentVersion).where(DocumentVersion.document_key == key, DocumentVersion.version_number == version_number))
    if not version:
        raise HTTPException(404, f"No ingested version found for document '{key}'. Ingest a PDF first.")
    return version


def _node_by_id(db: Session, node_id: str) -> DocumentNode:
    node = db.get(DocumentNode, node_id)
    if not node:
        raise HTTPException(404, "Node not found")
    return node


def _node_order(node: DocumentNode):
    try:
        source_order = json.loads(node.metadata_json or "{}").get("source_order", 0)
    except (json.JSONDecodeError, TypeError):
        source_order = 0
    return (source_order, node.page_start or 0, node.id)

def _subtree_text(node: DocumentNode) -> str:
    parts = [node.heading]
    if node.body_text.strip():
        parts.append(node.body_text.strip())
    for child in sorted(node.children, key=_node_order):
        parts.append(_subtree_text(child))
    return "\n\n".join(parts)

def _node_json(node: DocumentNode, db: Session, recursive: bool = True, depth: int = 0) -> dict:
    children = []
    if recursive and depth < 12:
        children = [_node_json(x, db, True, depth + 1) for x in sorted(node.children, key=_node_order)]
    full_text = _subtree_text(node) if recursive else (node.heading + ("\n" + node.body_text if node.body_text.strip() else ""))
    return {
        "id": node.id, "logical_id": node.logical_id, "version_id": node.version_id,
        "version_number": node.version.version_number, "parent_id": node.parent_id,
        "heading": node.heading, "section_number": node.section_number, "level": node.level,
        "node_type": node.node_type, "body_text": node.body_text, "full_text": full_text,
        "content_hash": node.content_hash, "path": node.path, "page_start": node.page_start,
        "metadata": json.loads(node.metadata_json or "{}"), "children": children,
    }


def _assign_logical_ids(parsed_nodes: list[ParsedNode], old_nodes: list[DocumentNode], old_db: Session) -> dict[str, str]:
    """Return parsed temp_id -> stable logical ID, matching by path then section+parent+title."""
    old_by_id = {n.id: n for n in old_nodes}
    old_parent_logical = {n.id: (old_by_id[n.parent_id].logical_id if n.parent_id in old_by_id else None) for n in old_nodes}
    old_by_path = {n.path: n for n in old_nodes}
    used: set[str] = set()
    temp_to_logical: dict[str, str] = {}
    for p in parsed_nodes:
        if p.temp_id == "root":
            match = next((n for n in old_nodes if n.level == 0), None)
        else:
            match = old_by_path.get(p.path)
            parent_logical = temp_to_logical.get(p.parent_temp_id or "root")
            if match is None or match.id in used:
                candidates = [n for n in old_nodes if n.id not in used
                              and n.section_number == p.section_number
                              and old_parent_logical.get(n.id) == parent_logical]
                exact = [n for n in candidates if n.heading.casefold() == p.heading.casefold()]
                if exact:
                    match = exact[0]
                elif candidates:
                    best = max(candidates, key=lambda n: heading_similarity(n.heading, p.heading))
                    if heading_similarity(best.heading, p.heading) >= 0.78:
                        match = best
                    else:
                        match = None
        if match is not None and match.id in used:
            match = None
        if match is not None:
            used.add(match.id)
            temp_to_logical[p.temp_id] = match.logical_id
        else:
            temp_to_logical[p.temp_id] = str(uuid4())
    return temp_to_logical


@app.get("/health")
def health():
    return {"status": "ok", "service": "tri9t-ct200"}


@app.post("/documents/{document_key}/ingest")
def ingest_document(document_key: str, file: UploadFile = File(...), db: Session = Depends(get_db)):
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Upload a PDF file (.pdf).")
    content = file.file.read()
    if not content or not content.startswith(b"%PDF"):
        raise HTTPException(400, "Uploaded file is empty or is not a valid PDF.")
    tmp_name = None
    try:
        with NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(content)
            tmp_name = tmp.name
        parsed = parse_pdf(tmp_name)
    except Exception as exc:
        raise HTTPException(422, f"PDF parsing failed: {exc}") from exc
    finally:
        if tmp_name and Path(tmp_name).exists():
            Path(tmp_name).unlink()

    prior_version = _latest_version(db, document_key)
    prior_nodes = list(db.scalars(select(DocumentNode).where(DocumentNode.version_id == prior_version.id))) if prior_version else []
    version_number = prior_version.version_number + 1 if prior_version else 1
    logical_ids = _assign_logical_ids(parsed.nodes, prior_nodes, db)
    version = DocumentVersion(document_key=document_key, version_number=version_number, filename=file.filename, is_latest=True)
    if prior_version:
        prior_version.is_latest = False
    db.add(version)
    db.flush()

    ids_by_temp = {p.temp_id: str(uuid4()) for p in parsed.nodes}
    objects = []
    for p in parsed.nodes:
        parent_id = ids_by_temp.get(p.parent_temp_id) if p.parent_temp_id else None
        obj = DocumentNode(
            id=ids_by_temp[p.temp_id], logical_id=logical_ids[p.temp_id], version_id=version.id,
            parent_id=parent_id, heading=p.heading, section_number=p.section_number,
            level=p.level, node_type=p.node_type, body_text=p.body_text,
            content_hash=p.content_hash, path=p.path, page_start=p.page_start,
            metadata_json=json.dumps(p.metadata, ensure_ascii=False),
        )
        objects.append(obj)
    db.add_all(objects)
    db.commit()
    return {
        "document_key": document_key, "version_number": version_number, "version_id": version.id,
        "filename": file.filename, "page_count": parsed.page_count, "node_count": len(objects),
        "ocr_pages_used": parsed.ocr_pages, "warnings": parsed.warnings,
        "matching_strategy": "exact structural path, then section number + parent logical ID + fuzzy heading (>=0.78)",
    }


@app.get("/documents/{document_key}/versions")
def list_versions(document_key: str, db: Session = Depends(get_db)):
    versions = list(db.scalars(select(DocumentVersion).where(DocumentVersion.document_key == document_key).order_by(DocumentVersion.version_number)))
    return [{"id": v.id, "version_number": v.version_number, "filename": v.filename,
             "created_at": v.created_at.isoformat() if v.created_at else None,
             "is_latest": v.is_latest, "node_count": len(v.nodes)} for v in versions]


@app.get("/documents/{document_key}/sections")
def list_sections(document_key: str, version: int | None = Query(default=None), db: Session = Depends(get_db)):
    v = _get_version(db, document_key, version)
    nodes = list(db.scalars(select(DocumentNode).where(DocumentNode.version_id == v.id, DocumentNode.level == 1).order_by(DocumentNode.page_start, DocumentNode.id)))
    return {"document_key": document_key, "version_number": v.version_number,
            "sections": [_node_json(n, db, recursive=False) for n in nodes]}


@app.get("/nodes/{node_id}")
def get_node(node_id: str, db: Session = Depends(get_db)):
    return _node_json(_node_by_id(db, node_id), db, recursive=True)


@app.get("/search")
def search_nodes(q: str = Query(min_length=1), document_key: str = DOCUMENT_KEY,
                version: int | None = None, limit: int = Query(default=50, ge=1, le=200), db: Session = Depends(get_db)):
    v = _get_version(db, document_key, version)
    query = q.casefold()
    nodes = list(db.scalars(select(DocumentNode).where(DocumentNode.version_id == v.id)))
    hits = []
    for n in nodes:
        if query in n.heading.casefold() or query in n.body_text.casefold():
            hits.append({"id": n.id, "logical_id": n.logical_id, "heading": n.heading,
                         "section_number": n.section_number, "path": n.path, "version_number": v.version_number,
                         "content_hash": n.content_hash, "snippet": (n.body_text[:240] if query in n.body_text.casefold() else n.heading)})
    return {"query": q, "version_number": v.version_number, "count": min(len(hits), limit), "results": hits[:limit]}


@app.get("/nodes/{node_id}/changes")
def node_changes(node_id: str, compare_to_version: int | None = None, db: Session = Depends(get_db)):
    node = _node_by_id(db, node_id)
    source_version = node.version.version_number
    target_version = compare_to_version
    if target_version is None:
        latest = _latest_version(db, node.version.document_key)
        target_version = latest.version_number if latest else source_version
    target = db.scalar(select(DocumentVersion).where(DocumentVersion.document_key == node.version.document_key,
                                                       DocumentVersion.version_number == target_version))
    if not target:
        raise HTTPException(404, f"Version {target_version} does not exist")
    counterpart = db.scalar(select(DocumentNode).where(DocumentNode.version_id == target.id, DocumentNode.logical_id == node.logical_id))
    if not counterpart:
        return {"node_id": node.id, "logical_id": node.logical_id, "source_version": source_version,
                "compare_to_version": target_version, "status": "removed_or_unmatched",
                "changed": True, "diff": {"summary": "No confidently matched node exists in the comparison version."}}
    diff = diff_summary(node.heading + "\n" + node.body_text, counterpart.heading + "\n" + counterpart.body_text)
    return {"node_id": node.id, "logical_id": node.logical_id, "source_version": source_version,
            "compare_to_version": target_version, "counterpart_node_id": counterpart.id,
            "status": "changed" if diff["changed"] else "unchanged", "changed": diff["changed"], "diff": diff}


@app.post("/selections", status_code=201)
def create_selection(payload: SelectionCreate, db: Session = Depends(get_db)):
    nodes = []
    for node_id in payload.node_ids:
        nodes.append(_node_by_id(db, node_id))
    selection = Selection(id=str(uuid4()), name=payload.name)
    db.add(selection)
    db.flush()
    for node in nodes:
        db.add(SelectionItem(selection_id=selection.id, node_id=node.id))
    db.commit()
    return {"id": selection.id, "name": selection.name, "created_at": selection.created_at.isoformat() if selection.created_at else None,
            "items": [{"node_id": n.id, "logical_id": n.logical_id, "version_id": n.version_id,
                       "version_number": n.version.version_number, "heading": n.heading,
                       "content_hash": n.content_hash} for n in nodes]}


@app.get("/selections/{selection_id}")
def get_selection(selection_id: str, db: Session = Depends(get_db)):
    selection = db.get(Selection, selection_id)
    if not selection:
        raise HTTPException(404, "Selection not found")
    return {"id": selection.id, "name": selection.name,
            "items": [{"node_id": item.node.id, "logical_id": item.node.logical_id,
                       "version_number": item.node.version.version_number, "version_id": item.node.version_id,
                       "heading": item.node.heading, "body_text": item.node.body_text,
                       "content_hash": item.node.content_hash, "path": item.node.path} for item in selection.items]}


def _generation_with_staleness(record: dict, db: Session) -> dict:
    result = dict(record)
    overall = []
    for source in record.get("sources", []):
        latest_version = _latest_version(db, record.get("document_key", DOCUMENT_KEY))
        latest_node = None
        if latest_version:
            latest_node = db.scalar(select(DocumentNode).where(DocumentNode.version_id == latest_version.id,
                                                                  DocumentNode.logical_id == source["logical_id"]))
        if not latest_node:
            status = "removed_or_unmatched"
            diff = {"changed": True, "summary": "Source node is absent or could not be matched in the latest version."}
        elif sha256_text(_subtree_text(latest_node)) == source["content_hash"]:
            status = "current"
            diff = {"changed": False, "summary": "Selected section/subtree content hash matches the latest version."}
        else:
            status = "stale_review_required"
            diff = diff_summary(source.get("text_snapshot", ""), _subtree_text(latest_node))
        overall.append(status)
        source["current_status"] = status
        source["current_node_id"] = latest_node.id if latest_node else None
        source["diff"] = diff
    result["sources"] = record.get("sources", [])
    result["staleness"] = "stale_review_required" if "stale_review_required" in overall else ("removed_or_unmatched" if "removed_or_unmatched" in overall else "current")
    # Per-case status is based on the source nodes explicitly linked by the LLM.
    source_by_id = {s["node_id"]: s for s in result["sources"]}
    for case in result.get("test_cases", []):
        case_statuses = [source_by_id[sid]["current_status"] for sid in case.get("source_node_ids", []) if sid in source_by_id]
        case["staleness"] = "stale_review_required" if "stale_review_required" in case_statuses else ("removed_or_unmatched" if "removed_or_unmatched" in case_statuses else "current")
    return result


@app.post("/selections/{selection_id}/generations", status_code=201)
def generate_for_selection(selection_id: str, db: Session = Depends(get_db)):
    selection = db.get(Selection, selection_id)
    if not selection:
        raise HTTPException(404, "Selection not found")
    if not selection.items:
        raise HTTPException(400, "Selection has no nodes")
    sources = []
    llm_sources = []
    for item in selection.items:
        node = item.node
        snapshot = _subtree_text(node)
        selected_hash = sha256_text(snapshot)
        source = {"node_id": node.id, "logical_id": node.logical_id, "version_id": node.version_id,
                  "version_number": node.version.version_number, "heading": node.heading,
                  "content_hash": selected_hash, "node_content_hash": node.content_hash, "text_snapshot": snapshot}
        sources.append(source)
        llm_sources.append({"node_id": node.id, "heading": node.heading,
                            "version_number": node.version.version_number,
                            "text_snapshot": snapshot})
    try:
        generated = generate_test_cases(llm_sources)
    except LLMGenerationError as exc:
        raise HTTPException(502, str(exc)) from exc
    record = add_generation({"selection_id": selection.id, "selection_name": selection.name,
                             "document_key": selection.items[0].node.version.document_key,
                             "source_version_numbers": sorted({s["version_number"] for s in sources}),
                             "sources": sources, "test_cases": generated["test_cases"],
                             "llm_attempts": generated["attempts"], "policy": "new generation record per request"})
    return _generation_with_staleness(record, db)


@app.get("/generations/{generation_id}")
def retrieve_generation(generation_id: str, db: Session = Depends(get_db)):
    record = get_generation(generation_id)
    if not record:
        raise HTTPException(404, "Generation not found")
    return _generation_with_staleness(record, db)


@app.get("/selections/{selection_id}/generations")
def generations_for_selection(selection_id: str, db: Session = Depends(get_db)):
    if not db.get(Selection, selection_id):
        raise HTTPException(404, "Selection not found")
    records = [r for r in list_generations() if r.get("selection_id") == selection_id]
    return {"selection_id": selection_id, "count": len(records), "generations": [_generation_with_staleness(r, db) for r in records]}


@app.get("/nodes/{node_id}/generations")
def generations_for_node(node_id: str, db: Session = Depends(get_db)):
    node = _node_by_id(db, node_id)
    records = [r for r in list_generations() if any(s.get("logical_id") == node.logical_id for s in r.get("sources", []))]
    return {"node_id": node_id, "logical_id": node.logical_id, "count": len(records),
            "generations": [_generation_with_staleness(r, db) for r in records]}
