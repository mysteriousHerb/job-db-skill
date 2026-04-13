#!/usr/bin/env python3
"""
job-db push — Upload selected jobs from a job-scout JSON file to Notion.

Usage:
    python push.py <json_file>                  # list jobs only
    python push.py <json_file> --jobs 1,3,5     # push specific jobs by number
    python push.py <json_file> --min-score 8    # push all jobs with fit_score >= 8
    python push.py <json_file> --all            # push all jobs in the file

Reads NOTION_API_KEY from:
    1. NOTION_API_KEY environment variable
    2. NOTION_KEY environment variable
    3. ~/.config/notion/api_key file

No external dependencies — stdlib only.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# ── Notion config ────────────────────────────────────────────────────────────
NOTION_VERSION = "2025-09-03"
DATABASE_ID = "32148a58-db8b-8070-a094-ea8d8d13d4df"   # for creating pages
BLOCK_CHUNK_SIZE = 1900   # safe under Notion's 2000-char rich_text limit
RATE_LIMIT_DELAY = 0.4    # seconds between API calls (~2.5 req/s, under the 3/s limit)
MAX_BLOCKS_PER_REQUEST = 100


# ── Notion API ────────────────────────────────────────────────────────────────

def get_notion_key() -> str:
    key = os.environ.get("NOTION_API_KEY") or os.environ.get("NOTION_KEY")
    if key:
        return key
    keyfile = Path("~/.config/notion/api_key").expanduser()
    if keyfile.exists():
        return keyfile.read_text().strip()
    sys.exit(
        "❌ Notion API key not found.\n"
        "   Set NOTION_API_KEY env var, or write the key to ~/.config/notion/api_key"
    )


def notion_request(method: str, path: str, key: str, data: dict | None = None) -> dict | None:
    url = f"https://api.notion.com/v1{path}"
    headers = {
        "Authorization": f"Bearer {key}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode()
        print(f"  ❌ Notion API error {e.code}: {error_body}", file=sys.stderr)
        return None


# ── Text / block helpers ──────────────────────────────────────────────────────

def chunk_text(text: str, size: int = BLOCK_CHUNK_SIZE) -> list[str]:
    """Split text into chunks of at most `size` chars, preferring newline splits."""
    chunks = []
    while len(text) > size:
        split_at = text.rfind("\n", 0, size)
        if split_at <= 0:
            split_at = size
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    if text.strip():
        chunks.append(text)
    return chunks


def build_jd_blocks(description: str) -> list[dict]:
    """Convert a JD string into a list of Notion block objects."""
    blocks: list[dict] = [
        {
            "object": "block",
            "type": "heading_2",
            "heading_2": {"rich_text": [{"text": {"content": "Job Description"}}]},
        }
    ]
    if not description or description.startswith("⚠️"):
        blocks.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"text": {"content": description or "No description available."}}]
            },
        })
        return blocks

    for chunk in chunk_text(description):
        blocks.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": [{"text": {"content": chunk}}]},
        })
    return blocks


# ── Notion push ───────────────────────────────────────────────────────────────

def push_job(job: dict, key: str) -> str | None:
    """Create a Notion page for one job. Returns the page URL on success, None on failure."""
    title = job.get("title") or "Unknown Role"
    company = job.get("company") or ""
    location = job.get("location") or ""
    job_url = job.get("job_url") or ""
    fit_score = job.get("fit_score", "?")
    reasoning = job.get("reasoning") or ""
    description = job.get("description") or "⚠️ Description not available — visit URL."

    # comments property: fit score + reasoning, hard cap at 2000 chars
    comments = f"Fit: {fit_score}/10. {reasoning}".strip()
    if len(comments) > 2000:
        comments = comments[:1997] + "..."

    # Name property: title text with hyperlink to job URL
    name_text: dict = {"content": title}
    if job_url:
        name_text["link"] = {"url": job_url}

    page_payload = {
        "parent": {"database_id": DATABASE_ID},
        "properties": {
            "Name": {"title": [{"text": name_text}]},
            "Company": {"rich_text": [{"text": {"content": company}}]},
            "Location": {"rich_text": [{"text": {"content": location}}]},
            "Status": {"status": {"name": "To apply"}},
            "comments": {"rich_text": [{"text": {"content": comments}}]},
        },
    }

    page = notion_request("POST", "/pages", key, page_payload)
    if not page:
        return None

    page_id = page["id"]
    time.sleep(RATE_LIMIT_DELAY)

    # Append JD to page body (trimmed to 100 blocks max per request)
    blocks = build_jd_blocks(description)
    notion_request(
        "PATCH",
        f"/blocks/{page_id}/children",
        key,
        {"children": blocks[:MAX_BLOCKS_PER_REQUEST]},
    )

    return page.get("url") or f"https://notion.so/{page_id.replace('-', '')}"


# ── CLI ───────────────────────────────────────────────────────────────────────

TIER_EMOJI = {"HIGH": "🎯", "STRONG": "💪", "GOOD": "✅", "DECENT": "🤔"}


def print_job_list(jobs: list[dict]) -> None:
    print(f"\n  {'#':<4} {'Score':<9} {'Title':<44} {'Company':<24} Location")
    print("  " + "─" * 100)
    for i, job in enumerate(jobs, 1):
        score = job.get("fit_score", "?")
        tier = job.get("fit_tier") or ""
        emoji = TIER_EMOJI.get(tier, " ")
        label = f"{emoji} {score}/10"
        print(
            f"  {i:<4} {label:<9} "
            f"{(job.get('title') or '')[:43]:<44} "
            f"{(job.get('company') or '')[:23]:<24} "
            f"{(job.get('location') or '')[:30]}"
        )
    print()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Push job-scout results to Notion database",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("json_file", help="Path to job-scout JSON file")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--jobs",
        metavar="N,N,...",
        help="Comma-separated job numbers to push (as shown in the list)",
    )
    group.add_argument(
        "--min-score",
        type=int,
        metavar="N",
        help="Push all jobs with fit_score >= N",
    )
    group.add_argument(
        "--all",
        action="store_true",
        dest="push_all",
        help="Push all jobs in the file",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    json_path = Path(args.json_file)
    if not json_path.exists():
        sys.exit(f"❌ File not found: {json_path}")

    raw = json.loads(json_path.read_text())
    # Support both {"scout_date": ..., "jobs": [...]} and a bare list
    jobs: list[dict] = raw.get("jobs", raw) if isinstance(raw, dict) else raw
    scout_date = raw.get("scout_date", json_path.stem) if isinstance(raw, dict) else json_path.stem

    print(f"\nJob Scout results — {scout_date}  ({len(jobs)} role(s) ≥ 6/10)")
    print_job_list(jobs)

    # Determine which jobs to push
    if args.push_all:
        indices = list(range(len(jobs)))
    elif args.min_score is not None:
        indices = [i for i, j in enumerate(jobs) if (j.get("fit_score") or 0) >= args.min_score]
        print(f"  Matched {len(indices)} job(s) with fit_score ≥ {args.min_score}.")
    elif args.jobs:
        try:
            indices = [int(n.strip()) - 1 for n in args.jobs.split(",")]
        except ValueError:
            sys.exit("❌ --jobs expects comma-separated integers, e.g. --jobs 1,3,5")
    else:
        print("  ℹ️  Listing only. To push, add --jobs 1,3 / --min-score 8 / --all\n")
        return

    if not indices:
        print("  ⚠️  No jobs matched the selection.")
        return

    out_of_range = [i + 1 for i in indices if i < 0 or i >= len(jobs)]
    if out_of_range:
        sys.exit(f"❌ Job number(s) out of range: {out_of_range}  (file has {len(jobs)} jobs)")

    key = get_notion_key()
    selected = [(i, jobs[i]) for i in indices]

    print(f"  Pushing {len(selected)} job(s) to Notion...\n")
    pushed, failed = [], []

    for i, job in selected:
        label = f"[{i+1}] {job.get('title')} — {job.get('company')}"
        print(f"  {label[:70]:<70} ", end="", flush=True)
        page_url = push_job(job, key)
        if page_url:
            print(f"✅")
            pushed.append((job, page_url))
        else:
            print(f"❌")
            failed.append(job)
        time.sleep(RATE_LIMIT_DELAY)

    print(f"\n  {'─'*60}")
    print(f"  ✅ Pushed:  {len(pushed)}")
    if failed:
        print(f"  ❌ Failed:  {len(failed)}")
        for j in failed:
            print(f"     - {j.get('title')} — {j.get('company')}")
    print()

    for job, url in pushed:
        print(f"  🔗 {job.get('title')} — {job.get('company')}")
        print(f"     {url}")
    print()


if __name__ == "__main__":
    main()
