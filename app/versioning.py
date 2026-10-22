from difflib import SequenceMatcher
import re
from .parser import normalize_text


def heading_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize_text(a).casefold(), normalize_text(b).casefold()).ratio()


def diff_summary(old_text: str, new_text: str) -> dict:
    old = normalize_text(old_text)
    new = normalize_text(new_text)
    if old == new:
        return {"changed": False, "summary": "Content is unchanged.", "old_excerpt": None, "new_excerpt": None}
    matcher = SequenceMatcher(None, old, new)
    opcodes = matcher.get_opcodes()
    changed = [op for op in opcodes if op[0] != "equal"]
    old_chunks = [old[i1:i2] for tag, i1, i2, j1, j2 in changed if i1 != i2]
    new_chunks = [new[j1:j2] for tag, i1, i2, j1, j2 in changed if j1 != j2]
    old_excerpt = " … ".join(x.strip() for x in old_chunks if x.strip())[:240] or "(no prior text)"
    new_excerpt = " … ".join(x.strip() for x in new_chunks if x.strip())[:240] or "(no current text)"
    return {
        "changed": True,
        "summary": f"Text changed: {len(changed)} differing span(s); review linked test cases.",
        "old_excerpt": old_excerpt,
        "new_excerpt": new_excerpt,
    }
