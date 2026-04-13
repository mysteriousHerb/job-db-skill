#!/usr/bin/env python3
"""
job-db pull — Fetch a tracked job from Notion and write a cv_builder request.json.

Usage:
    python pull.py                        # list tracked jobs, pick interactively
    python pull.py --company openai       # filter by company name (case-insensitive)
    python pull.py --title "head of"      # filter by title substring
    python pull.py --status "To apply"    # filter by status (default: To apply,Deciding,Networking)
    python pull.py --all-statuses         # show all statuses
    python pull.py --out path/to/req.json # write request.json to custom path

Output:
    Writes a request.json for cv_builder, then prints the command to run it.

Reads NOTION_API_KEY from:
    1. NOTION_API_KEY environment variable
    2. NOTION_KEY environment variable
    3. ~/.config/notion/api_key file

No external dependencies — stdlib only.
"""

import argparse
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# ── Force UTF-8 stdout/stderr on Windows ──────────────────────────────────────
if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding != "utf-8":
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ── Load .env from skill directory ────────────────────────────────────────────
SKILL_DIR = Path(__file__).resolve().parent

def _load_env(env_path: Path) -> None:
    """Minimal .env loader (stdlib only). Reads KEY=VALUE lines into os.environ."""
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:  # don't override existing env vars
            os.environ[key] = value

_load_env(SKILL_DIR / ".env")

# ── Notion config ─────────────────────────────────────────────────────────────
NOTION_VERSION = "2026-03-11"
DATA_SOURCE_ID = os.environ.get("NOTION_DATA_SOURCE_ID", "32148a58-db8b-804d-8a3e-000bc86acd54")
RATE_LIMIT_DELAY = 0.3

DEFAULT_STATUSES = ["To apply", "Deciding", "Networking"]

CV_BUILDER_ROOT = Path(__file__).parent.parent / "cv_builder"


# ── Notion API ────────────────────────────────────────────────────────────────

def get_notion_key() -> str:
    key = os.environ.get("NOTION_API_KEY") or os.environ.get("NOTION_KEY")
    if key:
        return key
    sys.exit(
        "❌ Notion API key not found.\n"
        "   Set NOTION_API_KEY in .claude/skills/job-db/.env"
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


# ── Notion helpers ────────────────────────────────────────────────────────────

def prop_text(prop: dict) -> str:
    """Extract plain text from a Notion property value."""
    ptype = prop.get("type", "")
    if ptype == "title":
        return "".join(r.get("plain_text", "") for r in prop.get("title", []))
    if ptype == "rich_text":
        return "".join(r.get("plain_text", "") for r in prop.get("rich_text", []))
    if ptype == "status":
        return (prop.get("status") or {}).get("name", "")
    if ptype == "url":
        return prop.get("url") or ""
    return ""


def query_jobs(key: str, statuses: list[str] | None) -> list[dict]:
    """Query the Notion database for tracked jobs."""
    if statuses:
        status_filters = [{"property": "Status", "status": {"equals": s}} for s in statuses]
        filt = {"or": status_filters} if len(status_filters) > 1 else status_filters[0]
    else:
        filt = {}

    payload: dict = {
        "sorts": [{"timestamp": "created_time", "direction": "descending"}],
    }
    if filt:
        payload["filter"] = filt

    result = notion_request(
        "POST",
        f"/data_sources/{DATA_SOURCE_ID}/query",
        key,
        payload,
    )
    if not result:
        return []
    return result.get("results", [])


def fetch_page_jd(page_id: str, key: str) -> str:
    """Reconstruct the full JD from a Notion page's block children."""
    result = notion_request("GET", f"/blocks/{page_id}/children?page_size=100", key)
    if not result:
        return ""

    lines = []
    for block in result.get("results", []):
        btype = block.get("type", "")
        content = block.get(btype, {})
        rich = content.get("rich_text", [])
        text = "".join(r.get("plain_text", "") for r in rich)
        if text.strip():
            lines.append(text)

    # Handle pagination (unlikely for JDs but safe)
    while result.get("has_more"):
        cursor = result.get("next_cursor")
        time.sleep(RATE_LIMIT_DELAY)
        result = notion_request("GET", f"/blocks/{page_id}/children?page_size=100&start_cursor={cursor}", key)
        if not result:
            break
        for block in result.get("results", []):
            btype = block.get("type", "")
            content = block.get(btype, {})
            rich = content.get("rich_text", [])
            text = "".join(r.get("plain_text", "") for r in rich)
            if text.strip():
                lines.append(text)

    return "\n\n".join(lines)


def parse_job(page: dict) -> dict:
    props = page.get("properties", {})
    return {
        "page_id": page["id"],
        "title": prop_text(props.get("Name", {})),
        "company": prop_text(props.get("Company", {})),
        "location": prop_text(props.get("Location", {})),
        "status": prop_text(props.get("Status", {})),
        "url": page.get("url", ""),
    }


# ── CLI helpers ───────────────────────────────────────────────────────────────

def print_job_list(jobs: list[dict]) -> None:
    print(f"\n  {'#':<4} {'Status':<14} {'Title':<44} {'Company':<24} Location")
    print("  " + "─" * 106)
    for i, job in enumerate(jobs, 1):
        print(
            f"  {i:<4} {job['status']:<14} "
            f"{job['title'][:43]:<44} "
            f"{job['company'][:23]:<24} "
            f"{job['location'][:30]}"
        )
    print()


def pick_job(jobs: list[dict]) -> dict:
    """Prompt user to pick a job by number."""
    while True:
        try:
            raw = input("  Enter job number: ").strip()
            n = int(raw)
            if 1 <= n <= len(jobs):
                return jobs[n - 1]
            print(f"  ⚠️  Enter a number between 1 and {len(jobs)}.")
        except (ValueError, EOFError):
            sys.exit("\n  Aborted.")


def make_job_title(job: dict) -> str:
    """Format as 'Role - Company' for cv_builder."""
    title = job["title"].strip()
    company = job["company"].strip()
    if company and company.lower() not in title.lower():
        return f"{title} - {company}"
    return title


def make_file_stem(job_title: str) -> str:
    """Convert 'Head of AI - Acme Corp' → 'Head_of_AI_Acme_Corp'."""
    import re
    stem = re.sub(r"[^\w\s-]", "", job_title)   # strip punctuation except hyphen/space
    stem = re.sub(r"[\s\-]+", "_", stem.strip()) # spaces and hyphens → underscore
    return stem[:80]                              # cap length


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Pull a tracked job from Notion and write a cv_builder request.json",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--company", metavar="NAME", help="Filter by company name (case-insensitive substring)")
    parser.add_argument("--title", metavar="TEXT", help="Filter by title substring (case-insensitive)")
    parser.add_argument("--status", metavar="STATUS", help="Filter by a single status (default: To apply, Deciding, Networking)")
    parser.add_argument("--all-statuses", action="store_true", help="Show jobs of all statuses")
    parser.add_argument("--out", metavar="PATH", help="Output path for request.json (default: cv_builder/input/request.json)")
    parser.add_argument("--pick", metavar="N", type=int, help="Auto-pick job number N (skip interactive prompt)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    key = get_notion_key()

    # Determine status filter
    if args.all_statuses:
        statuses = None
    elif args.status:
        statuses = [args.status]
    else:
        statuses = DEFAULT_STATUSES

    print(f"\n  Querying Notion{'  (all statuses)' if not statuses else '  (' + ', '.join(statuses) + ')'}...")
    pages = query_jobs(key, statuses)

    if not pages:
        sys.exit("  ⚠️  No jobs found. Try --all-statuses or check your Notion database.")

    jobs = [parse_job(p) for p in pages]

    # Apply local filters
    if args.company:
        needle = args.company.lower()
        jobs = [j for j in jobs if needle in j["company"].lower()]
        if not jobs:
            sys.exit(f"  ⚠️  No jobs found with company containing '{args.company}'.")

    if args.title:
        needle = args.title.lower()
        jobs = [j for j in jobs if needle in j["title"].lower()]
        if not jobs:
            sys.exit(f"  ⚠️  No jobs found with title containing '{args.title}'.")

    print_job_list(jobs)

    # Pick job
    if len(jobs) == 1:
        job = jobs[0]
        print(f"  Auto-selected: {job['title']} — {job['company']}\n")
    elif args.pick:
        if not (1 <= args.pick <= len(jobs)):
            sys.exit(f"❌ --pick {args.pick} out of range (1–{len(jobs)})")
        job = jobs[args.pick - 1]
    else:
        job = pick_job(jobs)

    # Fetch JD from page body
    print(f"\n  Fetching JD from Notion page...")
    jd = fetch_page_jd(job["page_id"], key)

    # Strip the "Job Description" heading block if present
    if jd.startswith("Job Description"):
        jd = jd[len("Job Description"):].lstrip()

    if not jd or jd.startswith("⚠️"):
        print(f"  ⚠️  JD is empty or unavailable in Notion. The job may not have been pushed with a description.")
        sys.exit(1)

    print(f"  ✅ JD fetched ({len(jd)} chars)\n")

    # Determine output paths
    job_title = make_job_title(job)
    stem = make_file_stem(job_title)

    if args.out:
        out_path = Path(args.out)
    else:
        out_path = CV_BUILDER_ROOT / "input" / f"{stem}.request.json"

    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Build request.json
    request = {
        "job_title": job_title,
        "job_description": jd,
        "additional_instructions": "",
    }
    out_path.write_text(json.dumps(request, indent=2, ensure_ascii=False))

    # Also write a plain jd.txt alongside
    jd_path = out_path.with_name(f"{stem}.jd.txt")
    jd_path.write_text(jd, encoding="utf-8")

    print(f"  📄 request.json : {out_path}")
    print(f"  📄 jd.txt       : {jd_path}")
    print(f"  Job title: {job_title}")
    print(f"  Status:    {job['status']}")
    print(f"  Notion:    {job['url']}")
    print()

    # Print both cv_builder invocation options
    cv_root = CV_BUILDER_ROOT.resolve()
    req_path = out_path.resolve()
    jd_abs = jd_path.resolve()
    print("  Run cv_builder with (pick either):")
    print(f"\n    # Option A — request file (includes job_title):")
    print(f"    cd \"{cv_root}\" && uv run python generate_cv.py --request-file \"{req_path}\" --additional-info-mode auto")
    print(f"\n    # Option B — plain JD file:")
    print(f"    cd \"{cv_root}\" && uv run python generate_cv.py --job-description-file \"{jd_abs}\" '{job_title}' --additional-info-mode auto\n")


if __name__ == "__main__":
    main()
