import socket
from datetime import datetime
from statistics import median

import requests
import urllib3.util.connection as urllib3_cn

# This environment's IPv6 routes are unreachable/very slow, and Python's
# resolver tries them before falling back to IPv4 (unlike curl's Happy
# Eyeballs), turning every request into a 20s+ stall. Force IPv4-only DNS.
urllib3_cn.allowed_gai_family = lambda: socket.AF_INET

WORKS_URL = "https://api.openalex.org/works"
AUTHORS_URL = "https://api.openalex.org/authors"

# "Recent" window used by both the sort and the rank-hint text below, so the
# two never drift apart. If the ranking logic changes, update _rank_reason
# to match. "Highly cited" is relative to this search's own results (see
# _rank_reason) rather than a fixed number, since a reasonable citation
# count varies a lot by field and by how niche the query is.
RECENT_YEARS_WINDOW = 3


def _short_id(openalex_url):
    """'https://openalex.org/A5023888391' -> 'A5023888391'."""
    if not openalex_url:
        return None
    return openalex_url.rstrip("/").split("/")[-1]


def normalize_author_id(value):
    """Accepts either a short id ('A5023888391') or a full OpenAlex URL
    (as a user might paste from their browser) and returns the short id,
    or None if blank."""
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    return _short_id(value) if value.startswith("http") else value


def _reconstruct_abstract(inverted_index):
    """OpenAlex stores abstracts as {word: [positions]} instead of plain text."""
    if not inverted_index:
        return ""

    positions = {}
    for word, idxs in inverted_index.items():
        for idx in idxs:
            positions[idx] = word

    return " ".join(positions[i] for i in sorted(positions))


def _format_number(value):
    return str(int(value)) if value == int(value) else f"{value:.1f}"


def _rank_reason(paper, current_year, median_citations):
    """Explain the (year, citations) sort key in plain language.

    "Highly cited" is relative to this search's own results, so the
    citation part of the message always states the median it's being
    compared against.
    """
    is_recent = (
        paper["year"] is not None
        and paper["year"] >= current_year - (RECENT_YEARS_WINDOW - 1)
    )
    is_highly_cited = paper["citations"] >= median_citations

    if paper["year"]:
        recency_desc = (
            f"published within the last {RECENT_YEARS_WINDOW} years"
            if is_recent
            else f"published in {paper['year']}"
        )
    else:
        recency_desc = "publication year unknown"

    median_desc = _format_number(median_citations)
    citation_desc = (
        f"cited {paper['citations']} times (at/above this search's median of {median_desc})"
        if is_highly_cited
        else f"cited {paper['citations']} times (below this search's median of {median_desc})"
    )

    if is_recent and is_highly_cited:
        return f"Higher ranking: {recency_desc} + {citation_desc}"
    if is_recent or is_highly_cited:
        return f"Moderate ranking: {recency_desc}, {citation_desc}"
    return f"Lower relevance: {recency_desc}, {citation_desc}"


def _extract_paper(raw):
    authors = []
    for authorship in raw.get("authorships", []):
        author = authorship.get("author") or {}
        name = author.get("display_name")
        if not name:
            continue
        authors.append({"id": _short_id(author.get("id")), "name": name})

    source = (raw.get("primary_location") or {}).get("source") or {}

    return {
        "id": _short_id(raw.get("id")),
        "title": raw.get("title") or "Untitled",
        "authors": authors,
        "year": raw.get("publication_year"),
        "journal": source.get("display_name") or "Unknown",
        "abstract": _reconstruct_abstract(raw.get("abstract_inverted_index")),
        "citations": raw.get("cited_by_count", 0),
        "doi": raw.get("doi"),
    }


def search_papers(query, per_page=25):
    response = requests.get(
        WORKS_URL,
        params={
            "search": query,
            "per-page": per_page,
        },
        timeout=20,
    )
    response.raise_for_status()

    results = response.json().get("results", [])
    papers = [_extract_paper(raw) for raw in results]

    # Basic sort: newer papers and highly cited papers first.
    papers.sort(key=lambda p: (p["year"] or 0, p["citations"]), reverse=True)

    median_citations = median(p["citations"] for p in papers) if papers else 0
    current_year = datetime.now().year
    for paper in papers:
        paper["rank_reason"] = _rank_reason(paper, current_year, median_citations)

    return papers


def _extract_work_summary(raw):
    source = (raw.get("primary_location") or {}).get("source") or {}
    return {
        "title": raw.get("title") or "Untitled",
        "year": raw.get("publication_year"),
        "journal": source.get("display_name") or "Unknown",
    }


def _extract_research_areas(raw_works, top_n=8):
    """Aggregate each work's concepts/topics into the author's top research areas."""
    counts = {}
    for raw in raw_works:
        tags = raw.get("concepts") or raw.get("topics") or []
        for tag in tags:
            name = tag.get("display_name")
            if not name:
                continue
            counts[name] = counts.get(name, 0) + 1

    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    return [name for name, _count in ranked[:top_n]]


def _extract_affiliation(raw_author):
    affiliations = raw_author.get("affiliations") or []
    if affiliations:
        institution = affiliations[0].get("institution") or {}
        if institution.get("display_name"):
            return institution["display_name"]

    last_known = raw_author.get("last_known_institutions") or []
    if last_known and last_known[0].get("display_name"):
        return last_known[0]["display_name"]

    return "Unknown"


def _extract_author_summary(raw):
    return {
        "id": _short_id(raw.get("id")),
        "name": raw.get("display_name") or "Unknown",
        "affiliation": _extract_affiliation(raw),
        "works_count": raw.get("works_count", 0),
    }


def search_authors(query, per_page=25):
    response = requests.get(
        AUTHORS_URL,
        params={
            "search": query,
            "per-page": per_page,
        },
        timeout=20,
    )
    response.raise_for_status()

    results = response.json().get("results", [])
    return [_extract_author_summary(raw) for raw in results]


def get_author_profile(author_id, per_page=50):
    author_response = requests.get(f"{AUTHORS_URL}/{author_id}", timeout=20)
    author_response.raise_for_status()
    raw_author = author_response.json()

    works_response = requests.get(
        WORKS_URL,
        params={
            "filter": f"author.id:{author_id}",
            "sort": "publication_year:desc",
            "per-page": per_page,
        },
        timeout=20,
    )
    works_response.raise_for_status()
    raw_works = works_response.json().get("results", [])

    works = [_extract_work_summary(raw) for raw in raw_works]
    works.sort(key=lambda w: w["year"] or 0, reverse=True)

    return {
        "id": _short_id(raw_author.get("id")) or author_id,
        "name": raw_author.get("display_name") or "Unknown",
        "affiliation": _extract_affiliation(raw_author),
        "topics": _extract_research_areas(raw_works),
        "works": works,
    }
