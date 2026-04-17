"""
Deterministic text heuristics shared across ingest, normalize, and compile.
"""
from __future__ import annotations

import re

_TITLE_CONNECTORS = {
    "a",
    "an",
    "and",
    "as",
    "at",
    "by",
    "for",
    "from",
    "in",
    "of",
    "on",
    "the",
    "to",
    "using",
    "via",
    "with",
}

_AFFILIATION_WORDS = {
    "ai",
    "college",
    "corp",
    "department",
    "google",
    "institute",
    "lab",
    "labs",
    "microsoft",
    "openai",
    "school",
    "university",
}

_AFFILIATION_RE = re.compile(r"\b(?:ai|college|corp|department|google|institute|lab|labs|microsoft|openai|school|university)\b")


def collapse_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def clean_extracted_text(text: str) -> str:
    text = text.replace("\ufb01", "fi").replace("\ufb02", "fl")
    text = text.replace("ﬁ", "fi").replace("ﬂ", "fl")
    return _strip_frontmatter(text)


def _strip_frontmatter(text: str) -> str:
    if not text.startswith("---"):
        return text
    match = re.match(r"^---\s*\n.*?\n---\s*\n?", text, flags=re.S)
    return text[match.end():] if match else text


def is_placeholder_title(title: str | None) -> bool:
    if not title:
        return True
    clean = collapse_whitespace(title).strip("\"'")
    lower = clean.lower()
    if not clean:
        return True
    if lower.startswith("src_"):
        return True
    if lower.endswith(".pdf"):
        return True
    if re.fullmatch(r"\d{4}\.\d{5}(v\d+)?(?:_\d+)?", lower):
        return True
    if re.fullmatch(r"src_[a-z0-9]+_\d{4}\.\d{5}(v\d+)?(?:_\d+)?", lower):
        return True
    return False


def choose_document_title(current_title: str | None, text: str, fallback: str | None = None) -> str:
    clean_current = collapse_whitespace(current_title or "")
    if clean_current and not is_placeholder_title(clean_current):
        return clean_current

    inferred = infer_document_title_from_text(text)
    if inferred:
        return inferred

    trimmed = _strip_source_prefix(clean_current) or collapse_whitespace(fallback or "")
    return trimmed or "Untitled Source"


def infer_document_title_from_text(text: str) -> str | None:
    lines = _meaningful_lines(text)
    title_lines: list[str] = []

    for line in lines[:20]:
        if line.lower() == "abstract":
            break
        if _looks_like_section_heading(line) or line.lower().startswith("arxiv:"):
            break
        if _looks_like_metadata_line(line):
            if title_lines:
                break
            continue
        title_lines.append(line)
        joined = collapse_whitespace(" ".join(title_lines))
        if len(joined) >= 120 or len(title_lines) >= 3:
            break

    title = collapse_whitespace(" ".join(title_lines))
    if not title or is_placeholder_title(title):
        return None
    return title[:200]


def extract_abstract_or_excerpt(text: str, max_chars: int = 800) -> str:
    lines = _meaningful_lines(text)
    abstract = _extract_section(lines, "abstract", max_chars=max_chars)
    if abstract:
        return abstract
    return _extract_opening_excerpt(lines, max_chars=max_chars)


def derive_key_claims(summary: str, limit: int = 4) -> list[str]:
    clean = collapse_whitespace(summary)
    if not clean:
        return []

    claims: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+", clean):
        candidate = sentence.strip(" -•")
        if len(candidate) < 35:
            continue
        claims.append(candidate[:240].rstrip())
        if len(claims) >= limit:
            break

    if claims:
        return claims
    return [clean[:240].rstrip()]


def derive_key_concepts(title: str, summary: str = "", limit: int = 6) -> list[str]:
    concepts: list[str] = []

    def add(candidate: str) -> None:
        clean = collapse_whitespace(candidate.strip(" -:;,."))
        if not clean:
            return
        if is_placeholder_title(clean):
            return
        if len(clean) > 80:
            return
        if clean.lower() not in {c.lower() for c in concepts}:
            concepts.append(clean)

    clean_title = collapse_whitespace(title)
    if clean_title and not is_placeholder_title(clean_title):
        if ":" in clean_title:
            left, right = clean_title.split(":", 1)
            add(left)
            clean_title = right
        split_match = re.search(r"\b(for|with|using|via|from|in|on)\b", clean_title, flags=re.I)
        if split_match:
            left = clean_title[:split_match.start()]
            right = clean_title[split_match.end():]
            add(left)
            add(right)
        else:
            add(clean_title)

    for token in re.findall(r"\b[A-Z][A-Z0-9-]{1,}\b", summary):
        add(token)
        if len(concepts) >= limit:
            break

    return concepts[:limit]


def _meaningful_lines(text: str) -> list[str]:
    cleaned = clean_extracted_text(text)
    lines: list[str] = []
    for raw in cleaned.splitlines():
        line = collapse_whitespace(raw)
        if not line:
            continue
        if line.startswith("## Page "):
            continue
        lines.append(line)
    return lines


def _extract_section(lines: list[str], heading: str, max_chars: int) -> str | None:
    collecting = False
    parts: list[str] = []

    for line in lines:
        if not collecting:
            if line.lower() == heading.lower():
                collecting = True
            continue

        if _looks_like_section_heading(line) and len(collapse_whitespace(" ".join(parts))) >= 120:
            break
        if line.lower().startswith("arxiv:"):
            continue
        parts.append(line)
        if len(collapse_whitespace(" ".join(parts))) >= max_chars:
            break

    section = collapse_whitespace(" ".join(parts))
    return section[:max_chars] if section else None


def _extract_opening_excerpt(lines: list[str], max_chars: int) -> str:
    title = infer_document_title_from_text("\n".join(lines))
    excerpt_parts: list[str] = []
    body_started = False

    for line in lines:
        if title and line == title:
            continue
        if not body_started and (_looks_like_metadata_line(line) or _looks_like_section_heading(line)):
            continue
        if not body_started and len(line) < 40:
            continue
        body_started = True
        excerpt_parts.append(line)
        if len(collapse_whitespace(" ".join(excerpt_parts))) >= max_chars:
            break

    excerpt = collapse_whitespace(" ".join(excerpt_parts))
    return excerpt[:max_chars] if excerpt else ""


def _looks_like_metadata_line(line: str) -> bool:
    lower = line.lower()
    if "@" in line or "{" in line or "}" in line:
        return True
    if _AFFILIATION_RE.search(lower) and len(line.split()) <= 8:
        return True
    if _looks_like_author_line(line):
        return True
    if re.match(r"^(source_id|title|author|page_count|source_date|extracted_at)\s*:", lower):
        return True
    return False


def _looks_like_author_line(line: str) -> bool:
    tokens = re.findall(r"[A-Za-z][A-Za-z.'-]*", line)
    if len(tokens) < 4:
        return False
    lower_tokens = [token.lower() for token in tokens]
    if any(token in _TITLE_CONNECTORS for token in lower_tokens):
        return False
    uppercase_ratio = sum(token[0].isupper() for token in tokens) / len(tokens)
    short_ratio = sum(len(token) <= 12 for token in tokens) / len(tokens)
    return uppercase_ratio >= 0.8 and short_ratio >= 0.9


def _looks_like_section_heading(line: str) -> bool:
    if re.match(r"^\d+(?:\.\d+)*\s+[A-Z]", line):
        return True
    lower = line.lower()
    return lower in {
        "abstract",
        "background",
        "conclusion",
        "discussion",
        "evaluation",
        "experiments",
        "introduction",
        "methods",
        "results",
    }


def _strip_source_prefix(title: str) -> str:
    if not title:
        return ""
    cleaned = re.sub(r"^src_[A-Z0-9]+_", "", title, flags=re.I)
    cleaned = re.sub(r"\.pdf$", "", cleaned, flags=re.I)
    return collapse_whitespace(cleaned)
