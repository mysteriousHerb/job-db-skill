---
name: job-db
description: Job database skill — push selected jobs from a job-scout JSON into the Notion database (with full JDs in page body), and pull tracked jobs back out for CV generation.
---

# job-db

The bridge between job-scout and cv_builder. Two modes:

- **push** — After reviewing a scout report, run `push.py` to upload selected jobs to Notion. Deterministic: reads the scout JSON, maps fields to Notion properties, writes full JD to page body. No AI needed.
- **pull** — Read tracked jobs from Notion (including saved JDs) to feed directly into cv_builder.

---

## Setup

Requires `NOTION_API_KEY` in the environment or at `~/.config/notion/api_key`.

`push.py` uses stdlib only — no install needed. Run with plain `python3` or `uv run python`.

### Database IDs (hardcoded in push.py — do not change)

```
DATABASE_ID      = 32148a58-db8b-8070-a094-ea8d8d13d4df   # for creating pages
DATA_SOURCE_ID   = 32148a58-db8b-804d-8a3e-000bc86acd54   # for querying
```

---

## Mode 1: Push — Scout JSON → Notion

**Trigger:** "save these jobs to Notion", "log the jobs to the database", "push selected jobs"

### Workflow

**Step 1:** After job-scout runs, the output directory contains:
```
.claude/skills/job-scout/output/job-scout-YYYY-MM-DD.json   ← use this
.claude/skills/job-scout/output/job-scout-YYYY-MM-DD.md     ← human review
```

**Step 2:** Show the user the job list (run with no push flag to list only):
```bash
python3 .claude/skills/job-db/push.py .claude/skills/job-scout/output/job-scout-YYYY-MM-DD.json
```

Output example:
```
Job Scout results — 2026-03-19  (14 role(s) ≥ 6/10)

  #    Score     Title                                        Company                  Location
  ─────────────────────────────────────────────────────────────────────────────────────────────
  1    🎯 9/10   AI Engagement Architect                      Thrive IT Systems        Sheffield, UK
  2    🎯 9/10   Principal Architect - Gen AI                 Sky UK                   Coulsdon, UK
  3    💪 8/10   Forward Deployed AI Software Architect       Adobe                    London, UK
  ...

  ℹ️  Listing only. To push, add --jobs 1,3 / --min-score 8 / --all
```

**Step 3:** User decides which to save. Then push the selected jobs:

```bash
# Push specific jobs by number
python3 .claude/skills/job-db/push.py .claude/skills/job-scout/output/job-scout-YYYY-MM-DD.json --jobs 1,2,3

# Push all scoring 8+
python3 .claude/skills/job-db/push.py .claude/skills/job-scout/output/job-scout-YYYY-MM-DD.json --min-score 8

# Push everything
python3 .claude/skills/job-db/push.py .claude/skills/job-scout/output/job-scout-YYYY-MM-DD.json --all
```

### What push.py does (deterministic, no agent inference)

For each selected job, it:
1. Creates a Notion page with these **properties** mapped directly from the JSON:
   - `Name` (title): job title, hyperlinked to `job_url`
   - `Company`: `company` field
   - `Location`: `location` field
   - `Status`: hardcoded `"To apply"`
   - `comments`: `"Fit: {fit_score}/10. {reasoning}"` (truncated to 2000 chars)
2. Appends the **page body** with the full JD from the `description` field, split into ≤1900-char paragraph blocks

### What's stored where

| Data | Notion location | Why |
|------|----------------|-----|
| Title, company, location, status | Properties (columns) | Filterable, sortable |
| Fit score + reasoning | `comments` property | Quick scan in database view |
| Full job description | Page body (blocks) | No char limit, readable when opened |

---

## Mode 2: Pull — Notion → cv_builder

**Trigger:** "pull jobs from Notion", "what jobs am I tracking", "show tracked jobs", "build CV for a tracked job"

### Step 1: Query the database

```bash
NOTION_KEY=$(cat ~/.config/notion/api_key)

curl -s -X POST "https://api.notion.com/v1/data_sources/32148a58-db8b-804d-8a3e-000bc86acd54/query" \
  -H "Authorization: Bearer $NOTION_KEY" \
  -H "Notion-Version: 2025-09-03" \
  -H "Content-Type: application/json" \
  -d '{
    "filter": {
      "or": [
        {"property": "Status", "status": {"equals": "To apply"}},
        {"property": "Status", "status": {"equals": "Deciding"}},
        {"property": "Status", "status": {"equals": "Networking"}}
      ]
    },
    "sorts": [{"timestamp": "created_time", "direction": "descending"}]
  }'
```

Drop the filter to see all statuses.

### Step 2: Extract and present

For each result, read:
- `page_id` — the UUID
- `Name.title[0].text.content` + `.link.url` → title and job URL
- `Company.rich_text[0].text.content`
- `Location.rich_text[0].text.content`
- `Status.status.name`
- `comments.rich_text[0].text.content` → fit score + notes

Present as numbered list:
```
Tracked jobs (To apply / Deciding / Networking):

[1] Senior AI Architect - Acme Corp
    London, UK | To apply | Fit: 9/10 — Strong match for agentic AI + RAG...

[2] Head of AI - BigCo
    London, UK | Deciding | Fit: 8/10 — Excellent comp...
```

### Step 3: Fetch JD from page body

For the selected job:

```bash
NOTION_KEY=$(cat ~/.config/notion/api_key)

curl -s "https://api.notion.com/v1/blocks/<page_id>/children" \
  -H "Authorization: Bearer $NOTION_KEY" \
  -H "Notion-Version: 2025-09-03"
```

Concatenate all paragraph block `rich_text[].text.content` values to reconstruct the full JD.

### Step 4: Hand off to cv_builder

Output a structured context:

```
📋 Job Context for CV Generation:

Title: Senior AI Architect
Company: Acme Corp
Location: London, UK
Job URL: https://linkedin.com/jobs/view/...
Scout Notes: Fit 9/10. Strong match for agentic AI + RAG. Gap: Azure vs GCP (transferable).

--- FULL JOB DESCRIPTION ---
<reconstructed JD text>
--- END JOB DESCRIPTION ---
```

Then invoke cv_builder with this context as the JD input.

---

## Updating an existing entry's status

```bash
NOTION_KEY=$(cat ~/.config/notion/api_key)

curl -s -X PATCH "https://api.notion.com/v1/pages/<page_id>" \
  -H "Authorization: Bearer $NOTION_KEY" \
  -H "Notion-Version: 2025-09-03" \
  -H "Content-Type: application/json" \
  -d '{"properties": {"Status": {"status": {"name": "Applied"}}}}'
```

Valid statuses: `To apply` | `Deciding` | `Networking` | `Applied` | `In progress` | `Interviewed` | `Done` | `Rejected` | `Given up` | `Missed Deadline`

---

## Error handling

**Rate limits:** push.py adds 0.4s delay between calls. No manual throttling needed.

**LinkedIn auth expired:** If a job's JD shows `⚠️ Description not available` in Notion after push, re-run job-scout with a refreshed LinkedIn session — the JSON will have the real description. Then re-push that job.

**Page not found on pull:** If a `page_id` returns 404, the entry was deleted in Notion. Re-push from the original scout JSON.

---

## Related skills

- [job-scout](../job-scout/SKILL.md) — produces the JSON file that `push` reads
- [cv_builder](../cv_builder/SKILL.md) — consumes the JD pulled from Notion
- [notion skill](../../../../openclaw/skills/notion/SKILL.md) — Notion API reference
