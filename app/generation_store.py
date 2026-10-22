"""Small JSON-backed store for generated LLM output, as permitted by the brief."""
import json, os, tempfile, threading
from pathlib import Path
from uuid import uuid4
from datetime import datetime, timezone
from .config import GENERATION_STORE

_lock = threading.Lock()

def _read() -> list[dict]:
    path = Path(GENERATION_STORE)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        raise RuntimeError(f"Generation store {path} is unreadable; refusing to overwrite it.")

def list_generations() -> list[dict]:
    with _lock:
        return _read()

def get_generation(generation_id: str) -> dict | None:
    return next((x for x in list_generations() if x.get("id") == generation_id), None)

def add_generation(record: dict) -> dict:
    with _lock:
        records = _read()
        record = {"id": str(uuid4()), "created_at": datetime.now(timezone.utc).isoformat(), **record}
        records.append(record)
        path = Path(GENERATION_STORE)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix="generations-", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(records, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        return record
