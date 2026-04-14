from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parent
NOTION_VERSION = "2026-03-11"
DATA_SOURCE_ID = os.environ.get(
    "NOTION_DATA_SOURCE_ID", "32148a58-db8b-804d-8a3e-000bc86acd54"
)


def load_env(env_path: Path | None = None) -> None:
    """Minimal .env loader using stdlib only."""
    target = env_path or (SKILL_DIR / ".env")
    if not target.exists():
        return
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


load_env()


def get_data_source_id() -> str:
    return os.environ.get("NOTION_DATA_SOURCE_ID", DATA_SOURCE_ID)


def get_notion_key(required: bool = True) -> str | None:
    key = os.environ.get("NOTION_API_KEY") or os.environ.get("NOTION_KEY")
    if key or not required:
        return key
    sys.exit(
        "❌ Notion API key not found.\n"
        "   Set NOTION_API_KEY in .claude/skills/job-db/.env"
    )


def notion_request(
    method: str, path: str, key: str, data: dict | None = None
) -> dict | None:
    url = f"https://api.notion.com/v1{path}"
    headers = {
        "Authorization": f"Bearer {key}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }
    body = json.dumps(data).encode() if data is not None else None
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as err:
        print(
            f"  ❌ Notion API error {err.code}: {err.read().decode()}", file=sys.stderr
        )
        return None
