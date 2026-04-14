#!/usr/bin/env python3
"""
Filter job results against the Notion database to remove duplicates.
Usage: python filter_notion_duplicates.py <jobs_json> [--output <filtered_json>]
"""

from __future__ import annotations

import json
import io
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

from common import get_data_source_id, get_notion_key, notion_request


if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding != "utf-8":
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


def normalize_url(url: str | None) -> str | None:
    """Normalize URL for comparison by removing query params and trailing slash."""
    if not url:
        return None
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")


def normalize_title(title: str | None) -> str | None:
    """Normalize job title for comparison."""
    if not title:
        return None
    normalized = title.lower().strip()
    normalized = re.sub(r"[^\w\s]", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized


def extract_title_words(title: str | None) -> set[str]:
    """Extract significant words from a title for fuzzy matching."""
    normalized = normalize_title(title)
    if not normalized:
        return set()

    normalized = re.sub(
        r",?\s*(united kingdom|uk|london|cambridge|remote|hybrid|onsite).*$",
        "",
        normalized,
    )

    stopwords = {"the", "a", "an", "and", "or", "at", "in", "for", "of", "to", "with"}
    seniority_words = {
        "senior",
        "junior",
        "lead",
        "principal",
        "staff",
        "associate",
        "chief",
    }
    return {
        word
        for word in normalized.split()
        if word not in stopwords and word not in seniority_words and len(word) > 2
    }


def titles_match_fuzzy(
    title1: str | None,
    title2: str | None,
    company1: str | None = None,
    company2: str | None = None,
) -> bool:
    """Check if two titles are likely the same role using word overlap."""
    words1 = extract_title_words(title1)
    words2 = extract_title_words(title2)
    if not words1 or not words2:
        return False

    if company1:
        words1 = words1 - set((normalize_title(company1) or "").split())
    if company2:
        words2 = words2 - set((normalize_title(company2) or "").split())

    overlap = words1 & words2
    smaller_set = min(len(words1), len(words2))
    larger_set = max(len(words1), len(words2))
    if smaller_set == 0 or len(overlap) < 2:
        return False

    word_count_diff = larger_set - smaller_set
    if word_count_diff > 1:
        return len(overlap) == smaller_set
    if smaller_set >= 3:
        return (len(overlap) / smaller_set) >= 0.8
    return len(overlap) == smaller_set


def get_notion_jobs(data_source_id: str) -> list[dict]:
    """Query all jobs from the Notion database."""
    key = get_notion_key(required=False)
    if not key:
        print(
            "Error: NOTION_API_KEY not set. Set it in .claude/skills/job-db/.env",
            file=sys.stderr,
        )
        return []

    jobs: list[dict] = []
    payload: dict = {
        "sorts": [{"timestamp": "created_time", "direction": "descending"}]
    }
    cursor = None

    while True:
        if cursor:
            payload["start_cursor"] = cursor
        result = notion_request(
            "POST", f"/data_sources/{data_source_id}/query", key, payload
        )
        if not result:
            break

        for page in result.get("results", []):
            props = page.get("properties", {})

            title = None
            title_items = props.get("Name", {}).get("title", [])
            if title_items:
                title = "".join(item.get("plain_text", "") for item in title_items)

            company = None
            company_items = props.get("Company", {}).get("rich_text", [])
            if company_items:
                company = "".join(item.get("plain_text", "") for item in company_items)

            url = None
            if title_items and title_items[0].get("text", {}).get("link"):
                url = title_items[0]["text"]["link"].get("url")
            if not url:
                for prop_name in ("URL", "Job URL", "job_url"):
                    url_prop = props.get(prop_name)
                    if url_prop and url_prop.get("url"):
                        url = url_prop["url"]
                        break

            jobs.append(
                {
                    "url": normalize_url(url),
                    "title": title,
                    "company": company.lower().strip() if company else None,
                    "page_id": page["id"],
                }
            )

        if result.get("has_more"):
            cursor = result.get("next_cursor")
        else:
            break

    return jobs


def filter_duplicates(
    jobs: list[dict], notion_jobs: list[dict]
) -> tuple[list[dict], list[dict]]:
    """Filter out jobs that already exist in Notion."""
    notion_urls = {job["url"] for job in notion_jobs if job.get("url")}

    filtered: list[dict] = []
    duplicates: list[dict] = []
    for job in jobs:
        job_url = normalize_url(
            job.get("job_url") or job.get("apply_url") or job.get("url")
        )
        is_duplicate = bool(job_url and job_url in notion_urls)

        if not is_duplicate:
            job_company = (
                job.get("company", "").lower().strip() if job.get("company") else None
            )
            job_title = job.get("title")
            if job_company and job_title:
                for notion_job in notion_jobs:
                    if notion_job.get("company") != job_company:
                        continue
                    if titles_match_fuzzy(
                        job_title,
                        notion_job.get("title"),
                        job_company,
                        notion_job.get("company"),
                    ):
                        is_duplicate = True
                        break

        if is_duplicate:
            duplicates.append(job)
        else:
            filtered.append(job)

    return filtered, duplicates


def main() -> int:
    if len(sys.argv) < 2:
        print(
            "Usage: python filter_notion_duplicates.py <jobs_json> [--output <filtered_json>]",
            file=sys.stderr,
        )
        return 1

    jobs_file = Path(sys.argv[1])
    output_file = None
    if "--output" in sys.argv:
        output_idx = sys.argv.index("--output")
        if output_idx + 1 < len(sys.argv):
            output_file = Path(sys.argv[output_idx + 1])

    if not output_file:
        output_file = jobs_file.parent / f"{jobs_file.stem}_filtered.json"

    jobs_data = json.loads(jobs_file.read_text(encoding="utf-8"))
    jobs = jobs_data if isinstance(jobs_data, list) else jobs_data.get("jobs", [])

    print(f"Loaded {len(jobs)} jobs", file=sys.stderr)
    print("Querying Notion database...", file=sys.stderr)
    notion_jobs = get_notion_jobs(get_data_source_id())
    print(f"Found {len(notion_jobs)} jobs in Notion", file=sys.stderr)

    filtered_jobs, duplicates = filter_duplicates(jobs, notion_jobs)
    print(f"Filtered out {len(duplicates)} duplicates", file=sys.stderr)
    print(f"Remaining: {len(filtered_jobs)} new jobs", file=sys.stderr)

    if duplicates:
        print("\nDuplicates found:", file=sys.stderr)
        for duplicate in duplicates[:5]:
            print(
                f"  - {duplicate.get('title', 'Unknown')} at {duplicate.get('company', 'Unknown')}",
                file=sys.stderr,
            )
        if len(duplicates) > 5:
            print(f"  ... and {len(duplicates) - 5} more", file=sys.stderr)

    output_file.write_text(
        json.dumps(filtered_jobs, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps(filtered_jobs, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
