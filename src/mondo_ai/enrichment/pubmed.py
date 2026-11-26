"""PubMed enrichment module for fetching literature evidence.

This module queries NCBI's E-utilities API to find relevant papers. The enricher
(``mondo_ai.enrichment.enricher``) turns them into Publication items of the SIEVE profile.

Example:
    >>> from mondo_ai.enrichment.pubmed import PubMedClient
    >>> client = PubMedClient()
    >>> papers = client.search("asthma synonym", max_results=5)  # doctest: +SKIP
    >>> for paper in papers:  # doctest: +SKIP
    ...     print(paper.title)
"""

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import httpx
import yaml


# NCBI E-utilities base URLs
ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"


@dataclass
class PubMedPaper:
    """A paper from PubMed."""

    pmid: str
    title: str
    abstract: str | None
    authors: list[str]
    journal: str | None
    year: str | None
    doi: str | None
    publication_date: str | None = None
    """Full ISO publication date (YYYY-MM-DD), only when PubMed gives year, month and day."""
    publication_types: list[tuple[str, str]] = field(default_factory=list)
    """PubMed publication types as (MeSH UI, name), e.g. ("D016428", "Journal Article")."""

    @property
    def reference_id(self) -> str:
        """Return the reference ID in CURIE format."""
        return f"PMID:{self.pmid}"

    def to_markdown(self) -> str:
        """Convert to markdown format for caching."""
        # YAML frontmatter
        frontmatter = {
            "reference_id": self.reference_id,
            "title": self.title,
            "authors": self.authors,
            "journal": self.journal,
            "year": self.year,
            "doi": self.doi,
            "publication_date": self.publication_date,
            "publication_types": [list(t) for t in self.publication_types],
            "content_type": "abstract_only" if self.abstract else "metadata_only",
        }

        lines = ["---"]
        lines.append(
            yaml.dump(frontmatter, default_flow_style=False, sort_keys=False).strip()
        )
        lines.append("---")
        lines.append("")
        lines.append(f"# {self.title}")
        lines.append(f"**Authors:** {', '.join(self.authors)}")
        if self.journal and self.year:
            lines.append(f"**Journal:** {self.journal} ({self.year})")
        if self.doi:
            lines.append(f"**DOI:** [{self.doi}](https://doi.org/{self.doi})")
        lines.append("")
        lines.append("## Abstract")
        lines.append("")
        lines.append(self.abstract or "*No abstract available*")

        return "\n".join(lines)


class ReferenceCache:
    """Cache for PubMed references as markdown files.

    Follows the dismech pattern of storing references as markdown files
    with YAML frontmatter.
    """

    def __init__(self, cache_dir: str | Path = "references_cache") -> None:
        """Initialize the cache.

        Args:
            cache_dir: Directory to store cached references.
        """
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _get_cache_path(self, pmid: str) -> Path:
        """Get the cache file path for a PMID."""
        return self.cache_dir / f"PMID_{pmid}.md"

    def get(self, pmid: str) -> PubMedPaper | None:
        """Get a paper from cache.

        Args:
            pmid: PubMed ID.

        Returns:
            PubMedPaper if cached, None otherwise.
        """
        cache_path = self._get_cache_path(pmid)
        if not cache_path.exists():
            return None

        content = cache_path.read_text()

        # Parse YAML frontmatter
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                try:
                    frontmatter = yaml.safe_load(parts[1])
                    # Extract abstract from markdown content
                    md_content = parts[2]
                    abstract = None
                    if "## Abstract" in md_content:
                        abstract_section = md_content.split("## Abstract", 1)[1].strip()
                        if abstract_section and not abstract_section.startswith(
                            "*No abstract"
                        ):
                            abstract = abstract_section

                    return PubMedPaper(
                        pmid=pmid,
                        title=frontmatter.get("title", ""),
                        abstract=abstract,
                        authors=frontmatter.get("authors", []),
                        journal=frontmatter.get("journal"),
                        year=frontmatter.get("year"),
                        doi=frontmatter.get("doi"),
                        publication_date=frontmatter.get("publication_date"),
                        publication_types=[
                            (ui, name)
                            for ui, name in frontmatter.get("publication_types") or []
                        ],
                    )
                except yaml.YAMLError:
                    pass

        return None

    def put(self, paper: PubMedPaper) -> None:
        """Store a paper in cache.

        Args:
            paper: The paper to cache.
        """
        cache_path = self._get_cache_path(paper.pmid)
        cache_path.write_text(paper.to_markdown())

    def has(self, pmid: str) -> bool:
        """Check if a PMID is cached."""
        return self._get_cache_path(pmid).exists()


class PubMedClient:
    """Client for querying PubMed via NCBI E-utilities.

    Uses the E-utilities API with optional API key for higher rate limits.
    Without API key: 3 requests/second
    With API key: 10 requests/second
    """

    def __init__(
        self,
        api_key: str | None = None,
        cache_dir: str | Path | None = "references_cache",
        email: str | None = None,
    ) -> None:
        """Initialize the PubMed client.

        Args:
            api_key: NCBI API key (optional, uses NCBI_API_KEY env var if not provided).
            cache_dir: Directory for caching references (None to disable caching).
            email: Email for NCBI (recommended for tracking).
        """
        self.api_key = api_key or os.environ.get("NCBI_API_KEY")
        self.email = email or os.environ.get("NCBI_EMAIL", "mondo-ai@example.com")
        self.cache = ReferenceCache(cache_dir) if cache_dir else None

        # Rate limiting
        self._last_request_time = 0.0
        self._min_interval = 0.1 if self.api_key else 0.34  # 10/sec or 3/sec

    def _rate_limit(self) -> None:
        """Enforce rate limiting."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request_time = time.time()

    def _build_params(self, **kwargs: Any) -> dict[str, Any]:
        """Build request parameters with API key and email."""
        params = dict(kwargs)
        if self.api_key:
            params["api_key"] = self.api_key
        if self.email:
            params["email"] = self.email
        return params

    def search(
        self,
        query: str,
        max_results: int = 10,
    ) -> list[str]:
        """Search PubMed for papers matching a query.

        Args:
            query: Search query string.
            max_results: Maximum number of results to return.

        Returns:
            List of PMIDs.
        """
        self._rate_limit()

        params = self._build_params(
            db="pubmed",
            term=query,
            retmax=max_results,
            retmode="json",
        )

        try:
            response = httpx.get(ESEARCH_URL, params=params, timeout=30.0)
            response.raise_for_status()
            data = response.json()
            return data.get("esearchresult", {}).get("idlist", [])
        except Exception as e:
            print(f"PubMed search error: {e}")
            return []

    def fetch(self, pmid: str) -> PubMedPaper | None:
        """Fetch paper details from PubMed.

        Args:
            pmid: PubMed ID.

        Returns:
            PubMedPaper or None if not found.
        """
        # Check cache first
        if self.cache and self.cache.has(pmid):
            return self.cache.get(pmid)

        self._rate_limit()

        params = self._build_params(
            db="pubmed",
            id=pmid,
            rettype="xml",
            retmode="xml",
        )

        try:
            response = httpx.get(EFETCH_URL, params=params, timeout=30.0)
            response.raise_for_status()

            paper = parse_article(pmid, response.content)
            if paper is None:
                return None

            # Cache the result
            if self.cache:
                self.cache.put(paper)

            return paper

        except Exception as e:
            print(f"PubMed fetch error for {pmid}: {e}")
            return None

    def fetch_many(self, pmids: list[str]) -> list[PubMedPaper]:
        """Fetch multiple papers.

        Args:
            pmids: List of PubMed IDs.

        Returns:
            List of PubMedPaper objects.
        """
        papers = []
        for pmid in pmids:
            paper = self.fetch(pmid)
            if paper:
                papers.append(paper)
        return papers

    def search_and_fetch(
        self,
        query: str,
        max_results: int = 10,
    ) -> list[PubMedPaper]:
        """Search PubMed and fetch full details for results.

        Args:
            query: Search query string.
            max_results: Maximum number of results.

        Returns:
            List of PubMedPaper objects.
        """
        pmids = self.search(query, max_results)
        return self.fetch_many(pmids)


MONTHS = {
    m: i
    for i, m in enumerate(
        [
            "Jan",
            "Feb",
            "Mar",
            "Apr",
            "May",
            "Jun",
            "Jul",
            "Aug",
            "Sep",
            "Oct",
            "Nov",
            "Dec",
        ],
        1,
    )
}


def _full_date(elem: ElementTree.Element | None) -> str | None:
    """An ISO date from a PubMed date element, only if year, month and day are all given.

    >>> _full_date(ElementTree.fromstring("<D><Year>2021</Year><Month>Feb</Month><Day>9</Day></D>"))
    '2021-02-09'
    >>> _full_date(ElementTree.fromstring("<D><Year>2021</Year><Month>02</Month></D>")) is None
    True
    """
    if elem is None:
        return None
    year, month, day = (elem.findtext(tag) for tag in ("Year", "Month", "Day"))
    if not (year and month and day):
        return None
    month_number = MONTHS[month[:3]] if month[:3] in MONTHS else int(month)
    return f"{int(year):04d}-{month_number:02d}-{int(day):02d}"


def parse_article(pmid: str, xml: bytes) -> PubMedPaper | None:
    """Parse an efetch XML response into a PubMedPaper (None if it holds no article).

    The publication date is the electronic article date, else the journal issue date, and
    only when it is a full date; ``year`` is the issue year.

    >>> parse_article("1", b"<PubmedArticleSet/>") is None
    True
    """
    root = ElementTree.fromstring(xml)
    article = root.find(".//PubmedArticle")
    if article is None:
        return None

    abstract_parts = [
        e.text for e in article.findall(".//Abstract/AbstractText") if e.text
    ]

    authors = []
    for author in article.findall(".//Author"):
        lastname = author.find("LastName")
        forename = author.find("ForeName")
        if lastname is not None:
            name = lastname.text or ""
            if forename is not None and forename.text:
                name = f"{forename.text} {name}"
            authors.append(name)

    doi = next(
        (a.text for a in article.findall(".//ArticleId") if a.get("IdType") == "doi"),
        None,
    )
    pub_date = article.find(".//JournalIssue/PubDate")
    if pub_date is None:
        pub_date = article.find(".//PubDate")

    return PubMedPaper(
        pmid=pmid,
        title=article.findtext(".//ArticleTitle") or "",
        abstract="\n".join(abstract_parts) or None,
        authors=authors,
        journal=article.findtext(".//Journal/Title"),
        year=pub_date.findtext("Year") if pub_date is not None else None,
        doi=doi,
        publication_date=_full_date(article.find(".//ArticleDate"))
        or _full_date(pub_date),
        publication_types=[
            (e.get("UI") or "", e.text or "")
            for e in article.findall(".//PublicationTypeList/PublicationType")
            if e.get("UI")
        ],
    )


def build_search_query(
    disease_label: str,
    synonym: str | None = None,
    additional_terms: list[str] | None = None,
) -> str:
    """Build a PubMed search query for a claim: its terms OR-ed, each in title or abstract.

    Empty terms are dropped and a term repeated in another case is used once.

    Args:
        disease_label: The disease name/label.
        synonym: Optional second term (synonym text, parent or mapped term label).
        additional_terms: Additional search terms.

    Returns:
        PubMed search query string.

    >>> build_search_query("asthma", "bronchial asthma")
    '("asthma"[Title/Abstract] OR "bronchial asthma"[Title/Abstract])'
    >>> build_search_query("", "Asthma", ["asthma"])
    '("Asthma"[Title/Abstract])'
    """
    terms: list[str] = []
    for term in [disease_label, synonym or "", *(additional_terms or [])]:
        if term and term.casefold() not in {t.casefold() for t in terms}:
            terms.append(term)
    return "(" + " OR ".join(f'"{t}"[Title/Abstract]' for t in terms) + ")"
