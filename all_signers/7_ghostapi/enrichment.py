from __future__ import annotations

import html
import json
import re
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


@dataclass
class EnrichmentResult:
    domain: str
    company_name: str | None = None
    emails: list[str] = field(default_factory=list)
    phone_numbers: list[str] = field(default_factory=list)
    contact_page_url: str | None = None
    linkedin_url: str | None = None
    confidence: float = 0.0
    sources: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict) -> "EnrichmentResult":
        return cls(
            domain=str(payload.get("domain", "")),
            company_name=payload.get("company_name"),
            emails=[str(v) for v in payload.get("emails", [])],
            phone_numbers=[str(v) for v in payload.get("phone_numbers", [])],
            contact_page_url=payload.get("contact_page_url"),
            linkedin_url=payload.get("linkedin_url"),
            confidence=float(payload.get("confidence", 0.0)),
            sources=[str(v) for v in payload.get("sources", [])],
            created_at=str(payload.get("created_at", "")),
        )


class EnrichmentStore:
    def __init__(self, db_path: str = "ghostapi_enrichment.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.db_path))

    def _init_db(self) -> None:
        with closing(self._connect()) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS enrichments (
                    domain TEXT PRIMARY KEY,
                    result_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            conn.commit()

    def upsert(self, result: EnrichmentResult) -> None:
        payload = json.dumps(result.to_dict())
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO enrichments(domain, result_json, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(domain) DO UPDATE SET
                    result_json=excluded.result_json,
                    updated_at=excluded.updated_at
                """,
                (result.domain, payload, result.created_at),
            )
            conn.commit()

    def get(self, domain: str) -> EnrichmentResult | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT result_json FROM enrichments WHERE domain = ?", (domain,)).fetchone()
        if not row:
            return None
        return EnrichmentResult.from_dict(json.loads(row[0]))

    def stats(self) -> dict[str, float]:
        with closing(self._connect()) as conn:
            total = conn.execute("SELECT COUNT(*) FROM enrichments").fetchone()[0]
            rows = conn.execute("SELECT result_json FROM enrichments").fetchall()
        if not rows:
            return {"total_domains": float(total), "avg_confidence": 0.0}

        confidence_values: list[float] = []
        for row in rows:
            try:
                payload = json.loads(row[0])
                confidence_values.append(float(payload.get("confidence", 0.0)))
            except Exception:
                continue

        avg = sum(confidence_values) / len(confidence_values) if confidence_values else 0.0
        return {"total_domains": float(total), "avg_confidence": round(float(avg), 4)}


@dataclass
class PageContent:
    url: str
    body: str


USER_AGENT = "ghostapi/1.0 (+authorized-contact-enrichment)"
EMAIL_RE = re.compile(r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b")
PHONE_RE = re.compile(r"(?:\+?\d{1,2}[\s\-.]?)?(?:\(?\d{3}\)?[\s\-.]?)\d{3}[\s\-.]?\d{4}")
TITLE_RE = re.compile(r"(?is)<title>(.*?)</title>")
H1_RE = re.compile(r"(?is)<h1[^>]*>(.*?)</h1>")
LINKEDIN_RE = re.compile(r"https?://(?:[\w\-]+\.)?linkedin\.com/[^\s\"'<>]+", re.IGNORECASE)


def normalize_domain(value: str) -> str:
    raw = value.strip().lower()
    if "://" in raw:
        parsed = urlparse(raw)
        host = (parsed.hostname or "").strip().lower()
        if not host:
            raise ValueError(f"Invalid domain or URL: {value}")
        return host
    if not raw or " " in raw:
        raise ValueError(f"Invalid domain or URL: {value}")
    return raw


def enrich_domain(
    domain: str,
    *,
    store: EnrichmentStore,
    refresh: bool = False,
    timeout_seconds: int = 12,
) -> tuple[EnrichmentResult, bool]:
    if not refresh:
        cached = store.get(domain)
        if cached:
            return cached, True

    pages = _fetch_candidate_pages(domain, timeout_seconds=timeout_seconds)
    result = _extract_contact_enrichment(domain, pages)
    store.upsert(result)
    return result, False


def result_to_csv_header() -> list[str]:
    return [
        "domain",
        "company_name",
        "emails",
        "phone_numbers",
        "contact_page_url",
        "linkedin_url",
        "confidence",
        "sources",
        "created_at",
    ]


def result_to_csv_row(result: EnrichmentResult) -> list[str]:
    return [
        result.domain,
        result.company_name or "",
        ";".join(result.emails),
        ";".join(result.phone_numbers),
        result.contact_page_url or "",
        result.linkedin_url or "",
        str(result.confidence),
        ";".join(result.sources),
        result.created_at,
    ]


def _fetch_url(url: str, timeout_seconds: int = 12) -> str:
    req = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(req, timeout=timeout_seconds) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def _fetch_candidate_pages(domain: str, timeout_seconds: int = 12) -> list[PageContent]:
    roots = [f"https://{domain}", f"http://{domain}"]
    first_ok = None
    for root in roots:
        try:
            body = _fetch_url(root, timeout_seconds=timeout_seconds)
            first_ok = PageContent(url=root, body=body)
            break
        except Exception:
            continue
    if not first_ok:
        raise RuntimeError(f"Unable to fetch domain: {domain}")

    pages = [first_ok]
    for suffix in ("/contact", "/contact-us", "/about", "/about-us"):
        try:
            url = urljoin(first_ok.url, suffix)
            body = _fetch_url(url, timeout_seconds=timeout_seconds)
            pages.append(PageContent(url=url, body=body))
        except Exception:
            continue
    return pages


def _strip_html_to_text(raw_html: str) -> str:
    text = re.sub(r"(?is)<script.*?>.*?</script>", " ", raw_html)
    text = re.sub(r"(?is)<style.*?>.*?</style>", " ", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        norm = value.strip()
        if not norm:
            continue
        key = norm.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(norm)
    return out


def _extract_company_name(raw_html: str) -> str | None:
    title = TITLE_RE.search(raw_html)
    if title:
        val = re.sub(r"\s+", " ", title.group(1)).strip()
        if val:
            return val[:120]
    h1 = H1_RE.search(raw_html)
    if h1:
        val = re.sub(r"\s+", " ", h1.group(1)).strip()
        if val:
            return val[:120]
    return None


def _confidence(
    emails: list[str],
    phones: list[str],
    contact_page_url: str | None,
    linkedin_url: str | None,
    company_name: str | None,
) -> float:
    score = 0.0
    if emails:
        score += 0.35
    if phones:
        score += 0.2
    if contact_page_url:
        score += 0.2
    if linkedin_url:
        score += 0.15
    if company_name:
        score += 0.1
    return min(1.0, round(score, 3))


def _extract_contact_enrichment(domain: str, pages: list[PageContent]) -> EnrichmentResult:
    all_emails: list[str] = []
    all_phones: list[str] = []
    linkedin_urls: list[str] = []
    sources: list[str] = []

    company_name = None
    contact_page_url = None

    for page in pages:
        sources.append(page.url)
        text = _strip_html_to_text(page.body)
        all_emails.extend(EMAIL_RE.findall(text))
        all_phones.extend(PHONE_RE.findall(text))
        linkedin_urls.extend(LINKEDIN_RE.findall(page.body))
        if company_name is None:
            company_name = _extract_company_name(page.body)
        if contact_page_url is None and "/contact" in page.url.lower():
            contact_page_url = page.url

    emails = _dedupe(all_emails)
    phones = _dedupe(all_phones)
    linkedin = _dedupe(linkedin_urls)

    result = EnrichmentResult(
        domain=domain,
        company_name=company_name,
        emails=emails,
        phone_numbers=phones,
        contact_page_url=contact_page_url,
        linkedin_url=linkedin[0] if linkedin else None,
        sources=_dedupe(sources),
    )
    result.confidence = _confidence(
        result.emails,
        result.phone_numbers,
        result.contact_page_url,
        result.linkedin_url,
        result.company_name,
    )
    return result
