---
name: job-db
description: Job database skill — push selected jobs from a job-scout score_results JSON into the Notion database (with full JDs in page body), and pull tracked jobs back out for CV generation.
---

# job-db

The bridge between job-scout and cv_builder. Two modes:

- **push** — After reviewing a scout run, call `push.py` to upload selected jobs to Notion. Reads `score_results_latest.json` directly — no re-fetching, no LinkedIn scraping needed. The full JD is already in the JSON.
- **pull** — Read tracked jobs from Notion (including saved JDs) and hand off to cv_builder.

---

## CRITICAL: Where the job data lives

When the user says "add the OpenAI job to Notion" or "save these jobs", the data is **already on disk** in:

```
.claude/skills/job-scout/output/latest/score_results_latest.json
```

Do NOT re-scrape LinkedIn or re-fetch URLs. The `description` field in that JSON is the full JD. Use `push.py` to push it directly.

---

## Setup

No install needed — `push.py` uses stdlib only. Run with plain `python3`.

Requires `NOTION_API_KEY` at `~/.config/notion/api_key` or in the environment.

---

## Mode 1: Push — score_results → Notion

**Trigger:** "save these jobs to Notion", "add the [company] job to Notion", "log the jobs", "push selected jobs"

### Step 1: List jobs (no push yet)

Run from the repo root:

```bash
python3 .claude/skills/job-db/push.py
```

`push.py` auto-discovers `score_results_latest.json` — no path argument needed. Output:

```
  Using: .../output/latest/score_results_latest.json

Job Scout results — score_results_latest.json  (12 role(s))

  #    Score    Title                                        Company                  Location
  ──────────────────────────────────────────────────────────────────────────────────────────────
  1    🎯 9.0   Copilot / AI Transformation Lead             Cloud Decisions          United Kingdom
  2    🎯 9.0   Head of AI Engineering                       Acme Corp                London, UK
  3    💪 8.5   AI Solution Architect                        BigCo                    London, UK
  ...

  ℹ️  Listing only. To push, add --jobs 1,3 / --min-score 8 / --all / --company NAME / --title TEXT
```

### Step 2: Push selected jobs

```bash
# By company name (case-insensitive substring match) — use when user names a company
python3 .claude/skills/job-db/push.py --company openai
python3 .claude/skills/job-db/push.py --company "cloud decisions"

# By title substring
python3 .claude/skills/job-db/push.py --title "head of AI"

# By job number from the list
python3 .claude/skills/job-db/push.py --jobs 1,2,3

# All scoring 8+
python3 .claude/skills/job-db/push.py --min-score 8

# Everything in the file
python3 .claude/skills/job-db/push.py --all
```

If the user names specific companies or roles, prefer `--company` / `--title` over numbers — it's more robust and doesn't require listing first.

### What gets written to Notion

| Data | Notion location | Source field |
|------|----------------|--------------|
| Job title (hyperlinked) | `Name` property | `title` + `job_url` |
| Company | `Company` property | `company` |
| Location | `Location` property | `location` |
| Status | `Status` property | hardcoded `"To apply"` |
| Score + breakdown + summary | `comments` property (≤2000 chars) | `score`, `scoring_breakdown`, `summary` |
| Full job description | Page body (blocks) | `description` |

The JD goes in the **page body** — no character limit, readable when you open the entry.

---

## Mode 2: Pull — Notion → cv_builder

**Trigger:** "build a CV for the [company] job", "pull jobs from Notion", "generate CV from Notion"

Use `pull.py` — it queries Notion, reconstructs the full JD from the page body, and writes named input files ready for cv_builder.

### Commands

```bash
# List tracked jobs (To apply / Deciding / Networking), pick interactively
python3 .claude/skills/job-db/pull.py

# Filter by company or title — auto-selects if only one match
python3 .claude/skills/job-db/pull.py --company "cloud decisions"
python3 .claude/skills/job-db/pull.py --title "head of AI"

# Auto-pick job #1 from the filtered list (no interactive prompt — use in agent workflows)
python3 .claude/skills/job-db/pull.py --company openai --pick 1

# Show all statuses (including Applied, Rejected, etc.)
python3 .claude/skills/job-db/pull.py --all-statuses
```

### What it does

1. Queries Notion for tracked jobs (default: `To apply`, `Deciding`, `Networking`)
2. Reconstructs the full JD from the page body blocks
3. Writes two named files to `cv_builder/input/`:
   - `<Role_Company>.request.json` — full request for cv_builder (`job_title` + `job_description`)
   - `<Role_Company>.jd.txt` — plain JD text (for reference or manual use)
4. Prints the exact `cv_builder` command to run — **copy and run it directly**

### Output

```
  Querying Notion  (To apply, Deciding, Networking)...

  #    Status         Title                                        Company                  Location
  ──────────────────────────────────────────────────────────────────────────────────────────────────
  1    To apply       Head of AI Engineering                       Acme Corp                London, UK
  2    Deciding       AI Solution Architect                        BigCo                    London, UK

  Fetching JD from Notion page...
  ✅ JD fetched (3241 chars)

  📄 request.json : .../cv_builder/input/Head_of_AI_Engineering_Acme_Corp.request.json
  📄 jd.txt       : .../cv_builder/input/Head_of_AI_Engineering_Acme_Corp.jd.txt
  Job title: Head of AI Engineering - Acme Corp

  Run cv_builder with (pick either):

    # Option A — request file (recommended, job_title already embedded):
    cd ".../cv_builder" && uv run python generate_cv.py --request-file ".../Head_of_AI_Engineering_Acme_Corp.request.json" --additional-info-mode auto

    # Option B — plain JD file:
    cd ".../cv_builder" && uv run python generate_cv.py --job-description-file ".../Head_of_AI_Engineering_Acme_Corp.jd.txt" 'Head of AI Engineering - Acme Corp' --additional-info-mode auto
```

**Always use Option A** — the request file already contains the job title so you don't need to pass it separately.

---

## Updating status after applying

Use Notion MCP:

```
mcp__notion__notion-update-page:
  page_id: <page_id>
  command: update_properties
  properties:
    Status: Applied
```

Valid statuses: `To apply` | `Deciding` | `Networking` | `Applied` | `In progress` | `Interviewed` | `Done` | `Rejected` | `Given up` | `Missed Deadline`

---

## Error handling

**Rate limits:** `push.py` adds 0.4s between each job — no manual throttling needed.

**Missing description:** If a job's `description` field is empty in the JSON, push.py writes `⚠️ Description not available — visit URL.` to the page body. Re-run job-scout to get the JD, then re-push.

**Notion API error 400:** Usually a property name mismatch. Verify the Notion database still has `Company`, `Location`, `Status`, `comments` columns with the correct types.

---

## Related skills

- `../job-scout/SKILL.md` — produces `score_results_latest.json` that push reads
- `../cv_builder/SKILL.md` — consumes the JD pulled from Notion
