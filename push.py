#!/usr/bin/env python3
"""
job-db push — Upload selected jobs from a job-scout score_results JSON to Notion.

Usage:
    python push.py <json_file>                    # list jobs only
    python push.py <json_file> --jobs 1,3,5       # push specific jobs by number
    python push.py <json_file> --min-score 8      # push all jobs with score >= 8
    python push.py <json_file> --all              # push all jobs in the file
    python push.py <json_file> --company openai   # push jobs matching company name (case-insensitive)
    python push.py <json_file> --title "head of"  # push jobs matching title substring

Reads NOTION_API_KEY from:
    1. NOTION_API_KEY environment variable
    2. NOTION_KEY environment variable
    3. ~/.config/notion/api_key file

No external dependencies — stdlib only.
"""

import argparse
import io
import json
import re
import sys
import time
from pathlib import Path

from common import get_data_source_id, get_notion_key, notion_request

# ── Force UTF-8 stdout/stderr on Windows ──────────────────────────────────────
if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding != "utf-8":
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

RATE_LIMIT_DELAY = 0.4
MAX_BLOCKS_PER_REQUEST = 100


# ── Text / block helpers ──────────────────────────────────────────────────────

# Patterns that look like section headers in a plain-text JD
_HEADER_RE = re.compile(
    r"^(?:"
    r"about\b|the (?:role|company|team|opportunity|position)|"
    r"responsibilit|requirements?|qualifications?|"
    r"what (?:you|we|they)|your (?:skills|experience|background)|"
    r"key (?:skills|requirements|responsibilities)|"
    r"who (?:you|we)|why (?:join|work|us)|"
    r"benefits?|perks|compensation|"
    r"nice.to.have|preferred|minimum|essential|desirable|"
    r"how to apply|next steps|overview|summary|"
    r"our (?:mission|values|culture|team|stack|technology)"
    r")",
    re.IGNORECASE,
)

_BULLET_RE = re.compile(r"^(?:[-*•·–—]|\d{1,2}[.)]\s)")


def _is_header(line: str) -> bool:
    """Heuristic: short line that looks like a section title."""
    if len(line) > 80 or len(line) < 3:
        return False
    # Matches known header patterns
    if _HEADER_RE.search(line):
        return True
    # Short line ending with colon (e.g. "Responsibilities:")
    if line.endswith(":") and len(line) < 60:
        return True
    return False


def _is_bullet(line: str) -> bool:
    return bool(_BULLET_RE.match(line.strip()))


def _strip_bullet(line: str) -> str:
    return _BULLET_RE.sub("", line.strip()).strip()


def _make_rich_text(text: str) -> list[dict]:
    """Build a rich_text array, chunking if text exceeds the 2000-char Notion limit."""
    chunks = []
    while len(text) > 2000:
        split_at = text.rfind(" ", 0, 2000)
        if split_at <= 0:
            split_at = 2000
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip()
    if text:
        chunks.append(text)
    return [{"text": {"content": c}} for c in chunks]


def _para_block(text: str) -> dict:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": _make_rich_text(text)},
    }


def _heading_block(text: str) -> dict:
    # Strip trailing colon for cleaner headings
    display = text.rstrip(":").strip()
    return {
        "object": "block",
        "type": "heading_3",
        "heading_3": {"rich_text": [{"text": {"content": display[:2000]}}]},
    }


def _bullet_block(text: str) -> dict:
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": _make_rich_text(text)},
    }


def build_jd_blocks(description: str) -> list[dict]:
    blocks: list[dict] = [
        {
            "object": "block",
            "type": "heading_2",
            "heading_2": {"rich_text": [{"text": {"content": "Job Description"}}]},
        }
    ]
    if not description or description.startswith("⚠️"):
        blocks.append(_para_block(description or "No description available."))
        return blocks

    # Split on blank lines to get natural paragraphs
    paragraphs = re.split(r"\n\s*\n", description.strip())

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        lines = para.split("\n")

        # Single-line paragraph: check if it's a header or bullet
        if len(lines) == 1:
            line = lines[0].strip()
            if _is_header(line):
                blocks.append(_heading_block(line))
            elif _is_bullet(line):
                blocks.append(_bullet_block(_strip_bullet(line)))
            else:
                blocks.append(_para_block(line))
            continue

        # Multi-line paragraph: could be a mix of header + content, or a list
        all_bullets = all(_is_bullet(l.strip()) for l in lines if l.strip())

        if all_bullets:
            # Entire paragraph is a list
            for l in lines:
                l = l.strip()
                if l:
                    blocks.append(_bullet_block(_strip_bullet(l)))
        elif _is_header(lines[0].strip()) and len(lines) > 1:
            # First line is a header, rest is content
            blocks.append(_heading_block(lines[0].strip()))
            remaining = "\n".join(lines[1:]).strip()
            # Check if the remaining lines are all bullets
            rem_lines = [l for l in lines[1:] if l.strip()]
            if rem_lines and all(_is_bullet(l.strip()) for l in rem_lines):
                for l in rem_lines:
                    blocks.append(_bullet_block(_strip_bullet(l.strip())))
            else:
                blocks.append(_para_block(remaining))
        else:
            # Regular multi-line paragraph — render line by line,
            # promoting bullets where detected
            buf = []
            for l in lines:
                l_stripped = l.strip()
                if not l_stripped:
                    continue
                if _is_bullet(l_stripped):
                    # Flush any accumulated text first
                    if buf:
                        blocks.append(_para_block(" ".join(buf)))
                        buf = []
                    blocks.append(_bullet_block(_strip_bullet(l_stripped)))
                else:
                    buf.append(l_stripped)
            if buf:
                blocks.append(_para_block(" ".join(buf)))

    return blocks


# ── Score field extraction ────────────────────────────────────────────────────


def get_score(job: dict) -> float:
    """Supports both schema versions: 'score' (current) and 'overall_score'/'fit_score' (legacy)."""
    return float(
        job.get("score") or job.get("overall_score") or job.get("fit_score") or 0
    )


def build_comments(job: dict) -> str:
    """Build the comments property value from scoring fields (max 2000 chars)."""
    score = get_score(job)
    summary = job.get("summary") or job.get("reasoning") or ""

    # Current schema: scoring_breakdown with bonuses/penalties lists
    breakdown = job.get("scoring_breakdown")
    if breakdown:
        bonuses = breakdown.get("bonuses") or []
        penalties = breakdown.get("penalties") or []
        parts = [f"Score: {score}/10"]
        if bonuses:
            parts.append(
                "+"
                + "; +".join(
                    b.split(": +", 1)[-1] if ": +" in b else b for b in bonuses[:3]
                )
            )
        if penalties:
            parts.append("-" + "; -".join(p for p in penalties[:2]))
        header = " | ".join(parts)
    else:
        # Legacy schema: primary_track, track_a_score, track_b_score
        track = job.get("primary_track", "")
        track_a = job.get("track_a_score")
        track_b = job.get("track_b_score")
        if track_a is not None and track_b is not None:
            header = f"Score: {score}/10 (Track {track}) | A: {track_a} B: {track_b}"
        else:
            header = f"Score: {score}/10"

    comments = f"{header}. {summary}".strip()
    if len(comments) > 2000:
        comments = comments[:1997] + "..."
    return comments


# ── Notion push ───────────────────────────────────────────────────────────────


def push_job(job: dict, key: str) -> str | None:
    """Create a Notion page for one job. Returns the page URL on success."""
    title = job.get("title") or "Unknown Role"
    company = job.get("company") or ""
    location = job.get("location") or ""
    job_url = job.get("job_url") or ""
    description = job.get("description") or "⚠️ Description not available — visit URL."
    comments = build_comments(job)
    salary = job.get("salary") or ""
    workplace_type = job.get("workplace_type") or ""
    posted_time = job.get("posted_time") or ""
    applicants = job.get("applicants") or ""

    name_text: dict = {"content": title}
    if job_url:
        name_text["link"] = {"url": job_url}

    page_payload = {
        "parent": {"type": "data_source_id", "data_source_id": get_data_source_id()},
        "properties": {
            "Name": {"title": [{"text": name_text}]},
            "Company": {"rich_text": [{"text": {"content": company}}]},
            "Location": {"rich_text": [{"text": {"content": location}}]},
            "Status": {"status": {"name": "To apply"}},
            "comments": {"rich_text": [{"text": {"content": comments}}]},
        },
    }

    # Add salary if available
    if salary:
        page_payload["properties"]["Salary"] = {
            "rich_text": [{"text": {"content": salary}}]
        }

    # Add workplace type if available
    if workplace_type:
        page_payload["properties"]["Workplace Type"] = {
            "rich_text": [{"text": {"content": workplace_type}}]
        }

    # Add posted time if available
    if posted_time:
        page_payload["properties"]["Posted"] = {
            "rich_text": [{"text": {"content": posted_time}}]
        }

    # Add applicant count if available
    if applicants:
        page_payload["properties"]["Applicants"] = {
            "rich_text": [{"text": {"content": applicants}}]
        }

    page = notion_request("POST", "/pages", key, page_payload)
    if not page:
        return None

    page_id = page["id"]
    time.sleep(RATE_LIMIT_DELAY)

    blocks = build_jd_blocks(description)
    notion_request(
        "PATCH",
        f"/blocks/{page_id}/children",
        key,
        {"children": blocks[:MAX_BLOCKS_PER_REQUEST]},
    )

    return page.get("url") or f"https://notion.so/{page_id.replace('-', '')}"


# ── CLI ───────────────────────────────────────────────────────────────────────


def score_label(job: dict) -> str:
    score = get_score(job)
    if score >= 9:
        return f"(*) {score}"
    if score >= 8:
        return f"(+) {score}"
    if score >= 7:
        return f"(/) {score}"
    return f"(?) {score}"


def print_job_list(jobs: list[dict]) -> None:
    print(f"\n  {'#':<4} {'Score':<8} {'Title':<44} {'Company':<24} Location")
    print("  " + "-" * 106)
    for i, job in enumerate(jobs, 1):
        print(
            f"  {i:<4} {score_label(job):<8} "
            f"{(job.get('title') or '')[:43]:<44} "
            f"{(job.get('company') or '')[:23]:<24} "
            f"{(job.get('location') or '')[:30]}"
        )
    print()


def find_latest_score_results() -> Path | None:
    """Find the most recent score_results.json in dated archive directories."""
    import glob
    from datetime import datetime

    # Look for score_results.json in dated archive directories
    candidates = [
        Path("skills/job-scout/output/archive/*/score_results.json"),
        Path(".claude/skills/job-scout/output/archive/*/score_results.json"),
        Path("skills/job-scout/output/runs/*/score_results.json"),
        Path(".claude/skills/job-scout/output/runs/*/score_results.json"),
    ]

    # Also search relative to this script's location
    script_dir = Path(__file__).parent
    candidates += [
        script_dir / "../../job-scout/output/archive/*/score_results.json",
        script_dir / "../job-scout/output/archive/*/score_results.json",
        script_dir / "../../job-scout/output/runs/*/score_results.json",
        script_dir / "../job-scout/output/runs/*/score_results.json",
    ]

    all_found = []
    for pattern in candidates:
        matches = glob.glob(str(pattern))
        for match in matches:
            path = Path(match)
            if path.exists():
                # Extract date from path (assuming YYYYMMDD format)
                parts = str(path.parent).split("/")
                date_part = [
                    p
                    for p in parts
                    if len(p) == 8 and p.isdigit() and p.startswith("20")
                ][-1:]  # Take last 8-digit sequence that starts with '20'
                if date_part:
                    try:
                        date_obj = datetime.strptime(date_part[0], "%Y%m%d")
                        all_found.append((date_obj, path))
                    except ValueError:
                        # If parsing fails, just use the modification time
                        all_found.append(
                            (datetime.fromtimestamp(path.stat().st_mtime), path)
                        )
                else:
                    # If no date part found, use modification time
                    all_found.append(
                        (datetime.fromtimestamp(path.stat().st_mtime), path)
                    )

    if all_found:
        # Return the most recent one
        all_found.sort(key=lambda x: x[0], reverse=True)
        return all_found[0][1]

    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Push job-scout scored results to Notion database",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "json_file",
        nargs="?",
        help="Path to score_results JSON. Defaults to most recent dated archive in skills/job-scout/output/archive/",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--jobs", metavar="N,N,...", help="Comma-separated job numbers to push"
    )
    group.add_argument(
        "--min-score", type=float, metavar="N", help="Push all jobs with score >= N"
    )
    group.add_argument(
        "--all", action="store_true", dest="push_all", help="Push all jobs in the file"
    )
    group.add_argument(
        "--company",
        metavar="NAME",
        help="Push jobs where company name contains NAME (case-insensitive)",
    )
    group.add_argument(
        "--title",
        metavar="TEXT",
        help="Push jobs where title contains TEXT (case-insensitive)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.json_file:
        json_path = Path(args.json_file)
    else:
        json_path = find_latest_score_results()
        if not json_path:
            sys.exit(
                "❌ Could not find score_results.json in dated archives.\n"
                "   Pass the path explicitly or run from the repo root."
            )
        print(f"  Using: {json_path}")

    if not json_path.exists():
        sys.exit(f"❌ File not found: {json_path}")

    raw = json.loads(json_path.read_text(encoding="utf-8"))
    jobs: list[dict] = raw.get("jobs", raw) if isinstance(raw, dict) else raw
    jobs = sorted(jobs, key=lambda j: get_score(j), reverse=True)

    print(f"\nJob Scout results — {json_path.name}  ({len(jobs)} role(s))")
    print_job_list(jobs)

    # Determine which to push
    if args.push_all:
        indices = list(range(len(jobs)))
    elif args.min_score is not None:
        indices = [i for i, j in enumerate(jobs) if get_score(j) >= args.min_score]
        print(f"  Matched {len(indices)} job(s) with score ≥ {args.min_score}.\n")
    elif args.jobs:
        try:
            indices = [int(n.strip()) - 1 for n in args.jobs.split(",")]
        except ValueError:
            sys.exit("❌ --jobs expects comma-separated integers, e.g. --jobs 1,3,5")
    elif args.company:
        needle = args.company.lower()
        indices = [
            i for i, j in enumerate(jobs) if needle in (j.get("company") or "").lower()
        ]
        print(
            f"  Matched {len(indices)} job(s) with company containing '{args.company}'.\n"
        )
    elif args.title:
        needle = args.title.lower()
        indices = [
            i for i, j in enumerate(jobs) if needle in (j.get("title") or "").lower()
        ]
        print(
            f"  Matched {len(indices)} job(s) with title containing '{args.title}'.\n"
        )
    else:
        print(
            "  ℹ️  Listing only. To push, add --jobs 1,3 / --min-score 8 / --all / --company NAME / --title TEXT\n"
        )
        return

    if not indices:
        print("  ⚠️  No jobs matched the selection.\n")
        return

    out_of_range = [i + 1 for i in indices if i < 0 or i >= len(jobs)]
    if out_of_range:
        sys.exit(
            f"❌ Job number(s) out of range: {out_of_range}  (file has {len(jobs)} jobs)"
        )

    key = get_notion_key()
    selected = [(i, jobs[i]) for i in indices]

    print(f"  Pushing {len(selected)} job(s) to Notion...\n")
    pushed, failed = [], []

    for i, job in selected:
        label = f"[{i + 1}] {job.get('title')} — {job.get('company')}"
        print(f"  {label[:72]:<72} ", end="", flush=True)
        page_url = push_job(job, key)
        if page_url:
            print("✅")
            pushed.append((job, page_url))
        else:
            print("❌")
            failed.append(job)
        time.sleep(RATE_LIMIT_DELAY)

    print(f"\n  {'─' * 60}")
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
