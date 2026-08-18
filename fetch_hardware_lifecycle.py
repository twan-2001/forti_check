"""Fetch Fortinet Hardware RSS lifecycle feed and split entries into one JSON file per category.

Newly added entries (not previously present in the category JSON files) are
reported to a Discord webhook, one message per entry.

Usage:
    python fetch_hardware_lifecycle.py [--url URL] [--out-dir DIR] [--webhook-url URL]
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path

DEFAULT_URL = "https://support.fortinet.com/rss/Hardware.xml"
DEFAULT_OUT_DIR = "categories"
DEFAULT_STATE_DIR = "state"
DISCORD_COLOR = 0x3498DB
DISCORD_ORANGE = 0xE67E22
DISCORD_RED = 0xE74C3C
DATE_FIELDS = ("End of Order", "Last Service Extension", "End of Support")
UPCOMING_WINDOW_DAYS = 30
STATE_FILENAME = "notification_state.json"


def slugify(name: str) -> str:
    slug = re.sub(r"[^\w\-]+", "_", name.strip().lower())
    return re.sub(r"_+", "_", slug).strip("_") or "uncategorized"


def parse_description(description: str) -> dict:
    """Description is a comma-separated list of "Key: Value" pairs joined with <br/>."""
    fields = {}
    parts = re.split(r"<br\s*/?>", description)
    for part in parts:
        part = part.strip().strip(",").strip()
        if not part or ":" not in part:
            continue
        key, value = part.split(":", 1)
        fields[key.strip()] = value.strip()
    return fields


def fetch_items(url: str) -> list[dict]:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=30) as resp:
        content = resp.read()
    root = ET.fromstring(content)

    items = []
    for item in root.iterfind(".//item"):
        title = item.findtext("title", default="").strip()
        guid = item.findtext("guid", default="").strip()
        pub_date = item.findtext("pubDate", default="").strip()
        description = item.findtext("description", default="")

        fields = parse_description(description)
        category = fields.pop("Category", "Uncategorized")

        entry = {
            "title": title,
            "guid": guid,
            "pubDate": pub_date,
            "category": category,
            **fields,
        }
        items.append(entry)
    return items


def group_by_category(items: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for item in items:
        grouped.setdefault(item["category"], []).append(item)
    return grouped


def load_existing_guids(grouped: dict[str, list[dict]], out_dir: Path) -> set[str]:
    """Read guids already present on disk, before they get overwritten."""
    existing: set[str] = set()
    for category in grouped:
        file_path = out_dir / f"{slugify(category)}.json"
        if not file_path.exists():
            continue
        try:
            with file_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            existing.update(entry.get("guid") for entry in data)
        except (json.JSONDecodeError, OSError):
            pass
    return existing


def load_notification_state(state_path: Path) -> dict[str, str]:
    """Maps "guid:field:kind" -> last-notified date, so alerts fire once per date value."""
    if not state_path.exists():
        return {}
    try:
        with state_path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_notification_state(state_path: Path, state: dict[str, str]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with state_path.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def filter_unnotified(
    candidates: list[tuple[dict, str, date]], state: dict[str, str], kind: str
) -> list[tuple[dict, str, date]]:
    """Drop entries already notified for this exact date value."""
    result = []
    for entry, field, when in candidates:
        key = f"{entry.get('guid')}:{field}:{kind}"
        if state.get(key) == when.isoformat():
            continue
        result.append((entry, field, when))
    return result


def _post_discord_payload(webhook_url: str, payload: dict, entry: dict) -> None:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as resp:
            resp.read()
    except urllib.error.URLError as e:
        print(f"Failed to send webhook for {entry.get('title')}: {e}", file=sys.stderr)


def send_discord_notification(webhook_url: str, entry: dict) -> None:
    fields = [
        {"name": key, "value": str(value), "inline": True}
        for key, value in entry.items()
        if key not in ("title", "guid", "pubDate", "category")
    ]
    payload = {
        "embeds": [
            {
                "title": entry.get("title", "Unknown"),
                "description": f"Category: {entry.get('category', 'Uncategorized')}",
                "color": DISCORD_COLOR,
                "fields": fields,
            }
        ]
    }
    _post_discord_payload(webhook_url, payload, entry)


def notify_new_entries(new_entries: list[dict], webhook_url: str | None) -> None:
    if not new_entries:
        return
    if not webhook_url:
        print(f"{len(new_entries)} new entries found but no webhook URL provided; skipping notifications.")
        return
    for entry in new_entries:
        send_discord_notification(webhook_url, entry)
        time.sleep(5)  # stay well under Discord's rate limit
    print(f"Sent {len(new_entries)} Discord notifications.")


def find_upcoming_dates(items: list[dict], within_days: int = UPCOMING_WINDOW_DAYS) -> list[tuple[dict, str, date]]:
    today = date.today()
    threshold = today + timedelta(days=within_days)
    upcoming = []
    for entry in items:
        for field in DATE_FIELDS:
            value = entry.get(field)
            if not value or value == "N/A":
                continue
            try:
                parsed = datetime.strptime(value, "%Y-%m-%d").date()
            except ValueError:
                continue
            if today <= parsed <= threshold:
                upcoming.append((entry, field, parsed))
    return upcoming


def send_upcoming_notification(webhook_url: str, entry: dict, field: str, when: date) -> None:
    days_left = (when - date.today()).days
    payload = {
        "embeds": [
            {
                "title": entry.get("title", "Unknown"),
                "description": (
                    f"**{field}** is coming up on **{when.isoformat()}** "
                    f"({days_left} day(s) left)\nCategory: {entry.get('category', 'Uncategorized')}"
                ),
                "color": DISCORD_ORANGE,
            }
        ]
    }
    _post_discord_payload(webhook_url, payload, entry)


def notify_upcoming_dates(
    upcoming: list[tuple[dict, str, date]], webhook_url: str | None, state: dict[str, str]
) -> None:
    if not upcoming:
        return
    if not webhook_url:
        print(f"{len(upcoming)} upcoming dates found but no webhook URL provided; skipping notifications.")
        for entry, field, when in upcoming:
            state[f"{entry.get('guid')}:{field}:upcoming"] = when.isoformat()
        return
    for entry, field, when in upcoming:
        send_upcoming_notification(webhook_url, entry, field, when)
        state[f"{entry.get('guid')}:{field}:upcoming"] = when.isoformat()
        time.sleep(5)  # stay well under Discord's rate limit
    print(f"Sent {len(upcoming)} upcoming-date notifications.")


def find_expired_dates(items: list[dict]) -> list[tuple[dict, str, date]]:
    today = date.today()
    expired = []
    for entry in items:
        for field in DATE_FIELDS:
            value = entry.get(field)
            if not value or value == "N/A":
                continue
            try:
                parsed = datetime.strptime(value, "%Y-%m-%d").date()
            except ValueError:
                continue
            if parsed < today:
                expired.append((entry, field, parsed))
    return expired


def send_expired_notification(webhook_url: str, entry: dict, field: str, when: date) -> None:
    days_ago = (date.today() - when).days
    payload = {
        "embeds": [
            {
                "title": entry.get("title", "Unknown"),
                "description": (
                    f"**{field}** expired on **{when.isoformat()}** "
                    f"({days_ago} day(s) ago)\nCategory: {entry.get('category', 'Uncategorized')}"
                ),
                "color": DISCORD_RED,
            }
        ]
    }
    _post_discord_payload(webhook_url, payload, entry)


def notify_expired_dates(
    expired: list[tuple[dict, str, date]], webhook_url: str | None, state: dict[str, str]
) -> None:
    if not expired:
        return
    if not webhook_url:
        print(f"{len(expired)} expired dates found but no webhook URL provided; skipping notifications.")
        for entry, field, when in expired:
            state[f"{entry.get('guid')}:{field}:expired"] = when.isoformat()
        return
    for entry, field, when in expired:
        send_expired_notification(webhook_url, entry, field, when)
        state[f"{entry.get('guid')}:{field}:expired"] = when.isoformat()
        time.sleep(5)  # stay well under Discord's rate limit
    print(f"Sent {len(expired)} expired-date notifications.")


def write_category_files(grouped: dict[str, list[dict]], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for category, entries in grouped.items():
        file_path = out_dir / f"{slugify(category)}.json"
        with file_path.open("w", encoding="utf-8") as f:
            json.dump(entries, f, indent=2, ensure_ascii=False)
        print(f"Wrote {len(entries)} entries to {file_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL, help="RSS feed URL")
    parser.add_argument(
        "--out-dir", default=DEFAULT_OUT_DIR, help="Directory to write per-category JSON files"
    )
    parser.add_argument(
        "--webhook-url",
        default=os.environ.get("DISCORD_WEBHOOK_URL"),
        help="Discord webhook URL to notify for newly added entries (env: DISCORD_WEBHOOK_URL)",
    )
    parser.add_argument(
        "--state-dir", default=DEFAULT_STATE_DIR, help="Directory to store notification state"
    )
    args = parser.parse_args()

    items = fetch_items(args.url)
    if not items:
        print("No items found in feed.", file=sys.stderr)
        return 1

    out_dir = Path(args.out_dir)
    grouped = group_by_category(items)
    existing_guids = load_existing_guids(grouped, out_dir)
    new_entries = [item for item in items if item["guid"] not in existing_guids]

    state_path = Path(args.state_dir) / STATE_FILENAME
    state = load_notification_state(state_path)
    upcoming = filter_unnotified(find_upcoming_dates(items), state, "upcoming")
    expired = filter_unnotified(find_expired_dates(items), state, "expired")

    write_category_files(grouped, out_dir)
    notify_new_entries(new_entries, args.webhook_url)
    notify_upcoming_dates(upcoming, args.webhook_url, state)
    notify_expired_dates(expired, args.webhook_url, state)
    save_notification_state(state_path, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
