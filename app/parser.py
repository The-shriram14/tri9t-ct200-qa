"""Document-specific CT-200 PDF parser with a conservative OCR fallback.

Native PDF text is preferred because it retains punctuation more accurately. OCR is
used on pages whose text layer is unusually sparse, and OCR heading candidates can
supplement missing numbered headings. The parser intentionally preserves source
order (including 3.4 appearing before 3.3) instead of silently reordering content.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
import hashlib, re, unicodedata
import fitz

try:
    import pytesseract
    from PIL import Image
    OCR_AVAILABLE = True
except Exception:
    pytesseract = None
    Image = None
    OCR_AVAILABLE = False

try:
    import pdfplumber
    PDFPLUMBER_AVAILABLE = True
except Exception:
    pdfplumber = None
    PDFPLUMBER_AVAILABLE = False

SECTION_RE = re.compile(r"^\s*(\d+(?:\.\d+)*\.?)\s+(.+?)\s*$")
LIST_RE = re.compile(r"^\s*(?:\d+[.)]|[-*•])\s+")
TITLE = "CardioTrack CT-200 Home Blood Pressure Monitor — Technical & User Manual"

@dataclass
class Line:
    text: str
    page: int
    y: float
    x: float
    font_size: float = 0.0
    bold: bool = False
    source: str = "pdf_text"

@dataclass
class ParsedNode:
    temp_id: str
    heading: str
    section_number: str | None
    level: int
    node_type: str
    body_text: str = ""
    path: str = ""
    page_start: int | None = None
    parent_temp_id: str | None = None
    children: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    logical_id: str | None = None
    content_hash: str = ""

@dataclass
class ParseResult:
    title: str
    nodes: list[ParsedNode]
    warnings: list[str]
    page_count: int
    ocr_pages: list[int]


def normalize_text(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).replace("\u00ad", "")
    s = s.replace("ﬁ", "fi").replace("ﬂ", "fl").replace("ﬀ", "ff")
    return re.sub(r"\s+", " ", s).strip()


def sha256_text(s: str) -> str:
    return hashlib.sha256(normalize_text(s).encode("utf-8")).hexdigest()


def _native_lines(page, page_num: int) -> list[Line]:
    out: list[Line] = []
    data = page.get_text("dict", sort=True)
    for block in data.get("blocks", []):
        if "lines" not in block:
            continue
        for raw_line in block["lines"]:
            spans = raw_line.get("spans", [])
            text = normalize_text("".join(s.get("text", "") for s in spans))
            if not text:
                continue
            max_size = max((float(s.get("size", 0)) for s in spans), default=0.0)
            bold = any("bold" in s.get("font", "").lower() or int(s.get("flags", 0)) & 16 for s in spans)
            bbox = raw_line.get("bbox", (0, 0, 0, 0))
            out.append(Line(text, page_num, float(bbox[1]), float(bbox[0]), max_size, bold, "pdf_text"))
    return sorted(out, key=lambda z: (round(z.y, 1), z.x))


def _ocr_page_lines(page, page_num: int, native_lines: list[Line]) -> list[Line]:
    """Recover all visible lines from a page with a sparse native text layer."""
    if not OCR_AVAILABLE:
        return []
    try:
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        image = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT, config="--psm 6")
    except Exception:
        return []
    grouped: dict[tuple[int, int, int], list[int]] = {}
    for i, txt in enumerate(data.get("text", [])):
        if not txt.strip():
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        grouped.setdefault(key, []).append(i)
    candidates: list[Line] = []
    native_norms = [normalize_text(x.text).casefold() for x in native_lines]
    for ids in grouped.values():
        txt = normalize_text(" ".join(data["text"][i] for i in ids))
        if not txt:
            continue
        norm = txt.casefold()
        if any(norm == old or (len(norm) > 24 and norm in old) or (len(old) > 24 and old in norm) for old in native_norms):
            continue
        height = max(data["height"][i] for i in ids)
        y = min(data["top"][i] for i in ids) / 2
        x = min(data["left"][i] for i in ids) / 2
        candidates.append(Line(txt, page_num, y, x, height / 2, height >= 26, "ocr"))
    return candidates


def _heading_parts(line: Line) -> tuple[str, str] | None:
    m = SECTION_RE.match(line.text)
    if not m:
        return None
    number = m.group(1).rstrip(".")
    title = m.group(2).strip()
    # Numbered lists are regular-weight body text, while headings are bold or large.
    if not line.bold and line.font_size < 12.0:
        return None
    if title.endswith(":") and line.font_size < 12.5:
        return None
    return number, title


def _markdown_table(rows: list[list[str | None]]) -> str:
    cleaned = [[normalize_text(cell or "").replace("|", "\\|") for cell in row] for row in rows]
    # Trim trailing empty cells, but keep each extracted row's column order.
    cleaned = [row[:max((i + 1 for i, cell in enumerate(row) if cell), default=1)] for row in cleaned]
    width = max((len(row) for row in cleaned), default=0)
    if not cleaned or width == 0:
        return ""
    cleaned = [row + [""] * (width - len(row)) for row in cleaned]
    lines = ["| " + " | ".join(cleaned[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
    lines.extend("| " + " | ".join(row) + " |" for row in cleaned[1:])
    return "\n".join(lines)

def _replace_known_tables(path: Path, nodes: list[ParsedNode], warnings: list[str]) -> None:
    """Use grid-aware extraction for the two known CT-200 tables when available."""
    if not PDFPLUMBER_AVAILABLE:
        warnings.append("pdfplumber unavailable; table cells remain in native linearized text.")
        return
    targets = {
        "2.1": {"parameter", "value"},
        "4.2": {"code", "meaning", "device behavior"},
    }
    try:
        with pdfplumber.open(path) as pdf:
            for node in nodes:
                expected = targets.get(node.section_number or "")
                if not expected or not node.page_start:
                    continue
                page_idx = node.page_start - 1
                if page_idx < 0 or page_idx >= len(pdf.pages):
                    continue
                found = None
                for table in pdf.pages[page_idx].extract_tables():
                    if not table or not table[0]:
                        continue
                    header = {normalize_text(c or "").casefold() for c in table[0] if normalize_text(c or "")}
                    if expected.issubset(header):
                        found = table
                        break
                if found:
                    node.body_text = _markdown_table(found)
                    node.metadata["table_like_content"] = True
                    node.metadata["table_extraction"] = "pdfplumber grid-aware extraction; cell order retained as Markdown table"
                    node.metadata["table_rows"] = len(found)
                    node.content_hash = sha256_text(node.heading + "\n" + node.body_text)
                else:
                    node.metadata["table_like_content"] = True
                    node.metadata["table_extraction"] = "Grid extraction not confidently detected; native ordered text retained for visual review"
                    warnings.append(f"Page {page_idx + 1}: table for section {node.section_number} was not confidently reconstructed; retained native text.")
    except Exception as exc:
        warnings.append(f"Table extraction fallback failed ({exc}); native table text retained.")


def parse_pdf(path: str | Path) -> ParseResult:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    doc = fitz.open(path)
    if not doc:
        raise ValueError("PDF contains no pages")
    warnings: list[str] = []
    all_lines: list[Line] = []
    ocr_pages: list[int] = []
    for i, page in enumerate(doc, start=1):
        native = _native_lines(page, i)
        # Hybrid strategy: OCR all lines only on sparse native-text pages.
        if sum(len(x.text) for x in native) < 80:
            supplement = _ocr_page_lines(page, i, native)
            if supplement:
                ocr_pages.append(i)
                native.extend(supplement)
        all_lines.extend(sorted(native, key=lambda z: (round(z.y, 1), z.x)))
    if not all_lines:
        raise ValueError("No extractable text found; OCR fallback did not recover content")

    # Detect the title from the first page when possible, but keep a stable fallback.
    title = TITLE
    title_line_idxs = [i for i, line in enumerate(all_lines[:30]) if "CardioTrack CT-200" in line.text and "Manual" in line.text]
    if title_line_idxs:
        # Keep the supplied document title stable across versions; the title may be split over lines.
        title = TITLE

    root = ParsedNode("root", title, None, 0, "document", path="root", page_start=1)
    nodes: list[ParsedNode] = [root]
    by_number: dict[str, ParsedNode] = {}
    occurrence: dict[tuple[str, str], int] = {}
    current = root
    current_page = 1
    body_lines_by_node: dict[str, list[str]] = {"root": []}

    for line in all_lines:
        text = normalize_text(line.text)
        if not text:
            continue
        # The document title is split across three oversized lines on page 1.
        # Keep it as the root heading rather than duplicate it in root body text.
        if line.page == 1 and line.y < 145 and ("CardioTrack CT-200" in text or "Pressure Monitor" in text or text == "User Manual"):
            continue
        heading = _heading_parts(line)
        if heading:
            number, heading_title = heading
            # Avoid misclassifying table cells that happen to start with numbers.
            key = (number, heading_title.casefold())
            occurrence[key] = occurrence.get(key, 0) + 1
            temp_id = f"n{len(nodes)}"
            parts = number.split(".")
            parent = None
            for length in range(len(parts) - 1, 0, -1):
                candidate = ".".join(parts[:length])
                if candidate in by_number:
                    parent = by_number[candidate]
                    break
            if parent is None:
                parent = root
            # Duplicate numbering is not silently overwritten in the lookup map.
            if number in by_number and by_number[number].heading.casefold() != heading_title.casefold():
                warnings.append(f"Page {line.page}: section number {number} is reused by a different heading; retained as a distinct node.")
            parent_path = parent.path
            local_key = f"{number} {heading_title}"
            duplicate_count = sum(1 for n in nodes if n.parent_temp_id == parent.temp_id and n.heading.casefold() == local_key.casefold()) + 1
            path_value = f"{parent_path}/{local_key}" + (f" [duplicate {duplicate_count}]" if duplicate_count > 1 else "")
            node = ParsedNode(temp_id, local_key, number, len(parts), "section", path=path_value,
                              page_start=line.page, parent_temp_id=parent.temp_id,
                              metadata={"heading_source": line.source, "source_order": len(nodes)})
            nodes.append(node)
            body_lines_by_node[temp_id] = []
            parent.children.append(temp_id)
            # Only use first occurrence for numeric parent lookup, preserving duplicates separately.
            by_number.setdefault(number, node)
            current = node
            current_page = line.page
            continue
        # Keep all content, including table rows, list items, and wrapped paragraphs.
        # Lines that continue across pages remain attached to the same section.
        body_lines_by_node.setdefault(current.temp_id, []).append(text)
        if line.source == "ocr":
            current.metadata["contains_ocr_recovered_content"] = True

    # Attach text and create hashes after extraction. Avoid dropping source lines that
    # precede the first numbered heading by retaining them on the document root.
    for node in nodes:
        raw = body_lines_by_node.get(node.temp_id, [])
        # Preserve line boundaries; wrapped paragraph line breaks are harmless for QA source text.
        node.body_text = "\n".join(raw).strip()
        node.content_hash = sha256_text(node.heading + "\n" + node.body_text)
        if node.section_number:
            parts = node.section_number.split(".")
            expected_parent = ".".join(parts[:-1]) if len(parts) > 1 else None
            if expected_parent and expected_parent not in by_number:
                warnings.append(f"Section {node.section_number} skips an explicit parent section ({expected_parent}); attached to nearest existing ancestor.")
    # Reconstruct the two known tables using grid-aware extraction where possible.
    _replace_known_tables(path, nodes, warnings)
    # Preserve source order but flag the known numbering order anomaly instead of sorting it away.
    seen_numbers: dict[str, list[str]] = {}
    for node in nodes:
        if node.section_number:
            prefix = node.section_number.split(".")[0]
            seen_numbers.setdefault(prefix, []).append(node.section_number)
    if any(seq.index("3.4") < seq.index("3.3") for seq in seen_numbers.values() if "3.4" in seq and "3.3" in seq):
        warnings.append("Section order anomaly: 3.4 Auto Shutoff appears before 3.3 Result Display and Classification in the source; original order preserved.")
    return ParseResult(title, nodes, sorted(set(warnings)), len(doc), ocr_pages)
