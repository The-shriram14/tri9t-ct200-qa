"""Run a complete v1 -> generate -> v2 -> stale-retrieval demonstration.

Start the API with LLM_API_KEY configured, then run:
    python scripts/demo_staleness.py
The script uploads v1 as a fresh version, generates cases from the battery-life
section, uploads v2, and retrieves the original generation to display its status.
"""
from pathlib import Path
import sys
import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "http://127.0.0.1:8000"
V1 = ROOT / "data" / "ct200_manual.pdf"
V2 = ROOT / "data" / "ct200_manual_v2.pdf"


def upload(client: httpx.Client, pdf: Path) -> dict:
    with pdf.open("rb") as f:
        response = client.post(f"/documents/ct200/ingest", files={"file": (pdf.name, f, "application/pdf")})
    response.raise_for_status()
    result = response.json()
    print(f"Ingested {pdf.name} as version {result['version_number']} ({result['node_count']} nodes)")
    for warning in result.get("warnings", []):
        print("  Parser warning:", warning)
    return result


def main():
    with httpx.Client(base_url=BASE_URL, timeout=90) as client:
        health = client.get("/health")
        health.raise_for_status()
        v1 = upload(client, V1)
        search = client.get("/search", params={"q": "measurement cycles", "version": v1["version_number"]})
        search.raise_for_status()
        matches = [r for r in search.json()["results"] if r.get("section_number") == "2.1.1.1"]
        if not matches:
            raise RuntimeError("Could not find section 2.1.1.1 in the just-ingested v1")
        node_id = matches[0]["id"]
        selection_resp = client.post("/selections", json={"name": "Battery-life QA ideas", "node_ids": [node_id]})
        selection_resp.raise_for_status()
        selection = selection_resp.json()
        print("Created version-pinned selection:", selection["id"])
        generation_resp = client.post(f"/selections/{selection['id']}/generations")
        if generation_resp.status_code >= 400:
            print(generation_resp.text, file=sys.stderr)
            generation_resp.raise_for_status()
        generation = generation_resp.json()
        print("Generated cases:", len(generation["test_cases"]), "generation ID:", generation["id"])
        v2 = upload(client, V2)
        retrieved_resp = client.get(f"/generations/{generation['id']}")
        retrieved_resp.raise_for_status()
        retrieved = retrieved_resp.json()
        print("Latest version:", v2["version_number"])
        print("Generation staleness:", retrieved["staleness"])
        for source in retrieved.get("sources", []):
            print("Source status:", source.get("current_status"), "—", source.get("diff", {}).get("summary"))
        if retrieved["staleness"] != "stale_review_required":
            raise SystemExit("Expected the v1 battery-life generation to be flagged stale after v2 ingestion.")
        print("SUCCESS: the old generation remains retrievable and is flagged for review.")

if __name__ == "__main__":
    main()
