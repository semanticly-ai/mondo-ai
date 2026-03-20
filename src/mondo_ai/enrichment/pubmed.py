"""PubMed enrichment module for fetching literature evidence.

This module queries NCBI's E-utilities API to find relevant papers
and creates LiteratureEvidence items for evidence packets.

Example:
    >>> from mondo_ai.enrichment.pubmed import PubMedClient
    >>> client = PubMedClient()
    >>> papers = client.search("asthma synonym", max_results=5)
    >>> for paper in papers:
    ...     print(paper.title)
"""

import hashlib
import os
import time
from dataclasses import dataclass
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
            "content_type": "abstract_only" if self.abstract else "metadata_only",
        }

        lines = ["---"]
        lines.append(yaml.dump(frontmatter, default_flow_style=False, sort_keys=False).strip())
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
                        if abstract_section and not abstract_section.startswith("*No abstract"):
                            abstract = abstract_section

                    return PubMedPaper(
                        pmid=pmid,
                        title=frontmatter.get("title", ""),
                        abstract=abstract,
                        authors=frontmatter.get("authors", []),
                        journal=frontmatter.get("journal"),
                        year=frontmatter.get("year"),
                        doi=frontmatter.get("doi"),
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

            # Parse XML response
            root = ElementTree.fromstring(response.content)
            article = root.find(".//PubmedArticle")
            if article is None:
                return None

            # Extract fields
            title_elem = article.find(".//ArticleTitle")
            title = title_elem.text if title_elem is not None else ""

            abstract_elem = article.find(".//AbstractText")
            abstract = abstract_elem.text if abstract_elem is not None else None

            # Authors
            authors = []
            for author in article.findall(".//Author"):
                lastname = author.find("LastName")
                forename = author.find("ForeName")
                if lastname is not None:
                    name = lastname.text or ""
                    if forename is not None and forename.text:
                        name = f"{forename.text} {name}"
                    authors.append(name)

            # Journal
            journal_elem = article.find(".//Journal/Title")
            journal = journal_elem.text if journal_elem is not None else None

            # Year
            year_elem = article.find(".//PubDate/Year")
            year = year_elem.text if year_elem is not None else None

            # DOI
            doi = None
            for article_id in article.findall(".//ArticleId"):
                if article_id.get("IdType") == "doi":
                    doi = article_id.text
                    break

            paper = PubMedPaper(
                pmid=pmid,
                title=title,
                abstract=abstract,
                authors=authors,
                journal=journal,
                year=year,
                doi=doi,
            )

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


def build_search_query(
    disease_label: str,
    synonym: str | None = None,
    additional_terms: list[str] | None = None,
) -> str:
    """Build a PubMed search query for a disease assertion.

    Args:
        disease_label: The disease name/label.
        synonym: Optional synonym to include.
        additional_terms: Additional search terms.

    Returns:
        PubMed search query string.
    """
    terms = [f'"{disease_label}"[Title/Abstract]']

    if synonym and synonym.lower() != disease_label.lower():
        terms.append(f'"{synonym}"[Title/Abstract]')

    if additional_terms:
        for term in additional_terms:
            terms.append(f'"{term}"[Title/Abstract]')

    # Combine with OR and add disease filter
    query = f"({' OR '.join(terms)})"
    return query
