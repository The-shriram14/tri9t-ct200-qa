from pathlib import Path
import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app import main
from app.models import DocumentVersion, DocumentNode
from app.parser import sha256_text

DATA = Path(__file__).resolve().parents[1] / "data"
V1 = DATA / "ct200_manual.pdf"
V2 = DATA / "ct200_manual_v2.pdf"

@pytest.fixture
def client_and_db(monkeypatch):
    test_engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(test_engine)
    TestSession = sessionmaker(bind=test_engine, autoflush=False, autocommit=False, expire_on_commit=False)
    def override_get_db():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()
    main.app.dependency_overrides[get_db] = override_get_db
    with TestClient(main.app) as client:
        yield client, TestSession
    main.app.dependency_overrides.clear()
    Base.metadata.drop_all(test_engine)

def upload(client, path):
    with open(path, "rb") as f:
        response = client.post("/documents/ct200/ingest", files={"file": (Path(path).name, f, "application/pdf")})
    assert response.status_code == 200, response.text
    return response.json()

def test_ingest_v1_then_v2_preserves_versions_and_matches_logical_nodes(client_and_db):
    client, Session = client_and_db
    first = upload(client, V1)
    second = upload(client, V2)
    assert first["version_number"] == 1
    assert second["version_number"] == 2
    versions = client.get("/documents/ct200/versions").json()
    assert [v["version_number"] for v in versions] == [1, 2]
    with Session() as db:
        v1 = db.scalar(select(DocumentVersion).where(DocumentVersion.version_number == 1))
        v2 = db.scalar(select(DocumentVersion).where(DocumentVersion.version_number == 2))
        old = db.scalar(select(DocumentNode).where(DocumentNode.version_id == v1.id, DocumentNode.section_number == "2.1.1.1"))
        new = db.scalar(select(DocumentNode).where(DocumentNode.version_id == v2.id, DocumentNode.section_number == "2.1.1.1"))
        assert old.logical_id == new.logical_id
        assert old.id != new.id
        assert old.content_hash != new.content_hash

def test_retrieval_api_reports_stale_generation_after_v2_ingest(client_and_db, monkeypatch):
    client, Session = client_and_db
    upload(client, V1)
    upload(client, V2)
    with Session() as db:
        v1 = db.scalar(select(DocumentVersion).where(DocumentVersion.version_number == 1))
        old = db.scalar(select(DocumentNode).where(DocumentNode.version_id == v1.id, DocumentNode.section_number == "2.1.1.1"))
        old_snapshot = main._subtree_text(old)
        record = {
            "id": "generation-demo", "selection_id": "selection-demo", "document_key": "ct200",
            "sources": [{"node_id": old.id, "logical_id": old.logical_id, "version_id": old.version_id,
                         "version_number": 1, "heading": old.heading, "content_hash": sha256_text(old_snapshot),
                         "node_content_hash": old.content_hash, "text_snapshot": old_snapshot}],
            "test_cases": [{"title": "Battery life test", "steps": ["Measure cycles"], "expected_result": "Matches v1 specification", "source_node_ids": [old.id]}]
        }
    monkeypatch.setattr(main, "get_generation", lambda generation_id: record if generation_id == "generation-demo" else None)
    response = client.get("/generations/generation-demo")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["staleness"] == "stale_review_required"
    assert data["sources"][0]["current_status"] == "stale_review_required"
    assert data["test_cases"][0]["staleness"] == "stale_review_required"

def test_selection_remains_pinned_to_original_version_after_reingestion(client_and_db):
    client, Session = client_and_db
    first = upload(client, V1)
    search = client.get("/search", params={"q": "measurement cycles", "version": first["version_number"]})
    assert search.status_code == 200
    node = next(r for r in search.json()["results"] if r["section_number"] == "2.1.1.1")
    created = client.post("/selections", json={"name": "Original battery spec", "node_ids": [node["id"]]})
    assert created.status_code == 201, created.text
    selection_id = created.json()["id"]
    upload(client, V2)
    saved = client.get(f"/selections/{selection_id}")
    assert saved.status_code == 200
    item = saved.json()["items"][0]
    assert item["version_number"] == 1
    assert "300 measurement cycles" in item["body_text"]
    assert "250 measurement cycles" not in item["body_text"]
