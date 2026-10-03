"""Bounded HTML extraction and reproducible document/chunk transformations."""

import re
from dataclasses import dataclass
from hashlib import sha256
from typing import Literal

from bs4 import BeautifulSoup


@dataclass(frozen=True, slots=True)
class CleanDocument:
    title: str
    content: str
    language: str
    content_hash: str


@dataclass(frozen=True, slots=True)
class KnowledgeChunk:
    index: int
    content: str
    section: str
    component: str
    content_hash: str


ChunkStrategy = Literal["fixed", "overlap", "paragraph", "section"]
_BOILERPLATE = re.compile(
    r"(^|[\s_-])(nav|menu|cookie|consent|advert|promo|sidebar|footer|header|breadcrumb)"
    r"([\s_-]|$)",
    re.IGNORECASE,
)
_HEADING = re.compile(r"^#{1,6}\s+(.+)$")
_COMPONENTS = {
    "engine": ("engine", "двигател", "мотор"),
    "transmission": ("transmission", "gearbox", "коробк", "трансмисс"),
    "suspension": ("suspension", "подвеск"),
    "electronics": ("electrical", "electronic", "электр"),
    "body": ("body", "кузов"),
    "recall": ("recall", "отзыв"),
}


def normalize_text(text: str) -> str:
    lines = []
    for raw in text.splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if line and (not lines or lines[-1] != line):
            lines.append(line)
    return "\n".join(lines)


def process_html(html: str | bytes, fallback_title: str = "") -> CleanDocument:
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else fallback_title
    for tag in list(
        soup.find_all(["script", "style", "noscript", "nav", "footer", "aside", "form"])
    ):
        tag.decompose()
    for tag in list(soup.find_all(True)):
        if tag.parent is None:
            continue
        if tag.name in {"html", "body", "main", "article"}:
            continue
        marker = " ".join(
            (str(tag.get("id", "")), " ".join(tag.get("class", [])), str(tag.get("role", "")))
        )
        if _BOILERPLATE.search(marker):
            tag.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup
    blocks: list[str] = []
    for tag in root.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "pre", "tr"]):
        if tag.name == "p" and tag.find_parent("li"):
            continue
        if tag.name == "li" and tag.find_parent("li"):
            continue
        value = re.sub(r"\s+", " ", tag.get_text(" ", strip=True)).strip()
        if not value:
            continue
        if tag.name.startswith("h"):
            value = f"## {value}"
        elif tag.name == "li":
            value = f"- {value}"
        if not blocks or blocks[-1] != value:
            blocks.append(value)
    if not blocks:
        blocks = [root.get_text(" ", strip=True)]
    content = normalize_text("\n".join(blocks))
    cyrillic = len(re.findall(r"[а-яё]", content, re.IGNORECASE))
    latin = len(re.findall(r"[a-z]", content, re.IGNORECASE))
    language = "ru" if cyrillic > latin else "en" if latin else "unknown"
    return CleanDocument(title, content, language, sha256(content.encode("utf-8")).hexdigest())


def process_pdf(data: bytes, fallback_title: str = "", max_pages: int = 25) -> CleanDocument:
    """Extract bounded selectable PDF text; image-only scans need manual OCR review."""
    from io import BytesIO

    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("Install the 'knowledge' extra for PDF ingestion") from exc
    reader = PdfReader(BytesIO(data), strict=False)
    if len(reader.pages) > max_pages:
        raise ValueError("PDF exceeds the configured page limit")
    content = normalize_text("\n".join(page.extract_text() or "" for page in reader.pages))
    cyrillic = len(re.findall(r"[а-яё]", content, re.IGNORECASE))
    latin = len(re.findall(r"[a-z]", content, re.IGNORECASE))
    language = "ru" if cyrillic > latin else "en" if latin else "unknown"
    return CleanDocument(
        fallback_title, content, language, sha256(content.encode("utf-8")).hexdigest()
    )


def _component(section: str) -> str:
    lowered = section.casefold()
    return next(
        (name for name, needles in _COMPONENTS.items() if any(n in lowered for n in needles)),
        "",
    )


def _sections(text: str) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    heading = ""
    lines: list[str] = []
    for line in text.splitlines():
        match = _HEADING.match(line)
        if match:
            if lines:
                result.append((heading, "\n".join(lines)))
            heading = match.group(1)
            lines = []
        elif line.strip():
            lines.append(line.strip())
    if lines:
        result.append((heading, "\n".join(lines)))
    return result


def chunk_document(
    document: CleanDocument,
    strategy: ChunkStrategy = "section",
    chunk_words: int = 220,
    overlap_words: int = 35,
) -> list[KnowledgeChunk]:
    if chunk_words < 20 or not 0 <= overlap_words < chunk_words:
        raise ValueError("Invalid chunk size or overlap")
    if strategy not in {"fixed", "overlap", "paragraph", "section"}:
        raise ValueError("Unsupported chunk strategy")
    if strategy == "fixed":
        groups = [("", document.content)]
        overlap = 0
    elif strategy == "overlap":
        groups = [("", document.content)]
        overlap = overlap_words
    elif strategy == "paragraph":
        groups = [("", line) for line in document.content.splitlines() if line.strip()]
        overlap = 0
    else:
        groups = _sections(document.content)
        overlap = overlap_words
    result: list[KnowledgeChunk] = []
    seen: set[str] = set()
    for section, content in groups:
        words = content.split()
        step = chunk_words - overlap
        for start in range(0, len(words), step):
            part = " ".join(words[start : start + chunk_words])
            if not part:
                continue
            digest = sha256(part.casefold().encode("utf-8")).hexdigest()
            if digest not in seen:
                seen.add(digest)
                result.append(
                    KnowledgeChunk(len(result), part, section, _component(section), digest)
                )
            if start + chunk_words >= len(words):
                break
    return result
