"""
teamwork_discover_completed_lists.py
=====================================
Teamwork.com does not expose completed/archived task lists via its API.
This script works around that limitation by sweeping all completed tasks
project-wide and extracting the unique tasklist IDs and names from them.

Run this FIRST, before teamwork_export.py, to discover your completed
task list IDs. Then paste the output into teamwork_export.py.

Usage:
    pip install requests
    python teamwork_discover_completed_lists.py

Configuration:
    Fill in API_KEY, SITE_NAME, and PROJECT_ID below.

Getting your API key:
    Teamwork → profile avatar → Edit Profile → API Keys
"""

import requests
import time
from base64 import b64encode

# ─────────────────────────────────────────────
#  CONFIGURATION
# ─────────────────────────────────────────────
API_KEY    = "YOUR_API_KEY_HERE"   # Teamwork API key
SITE_NAME  = "yoursite"            # subdomain: yoursite.teamwork.com
PROJECT_ID = "0000000"             # found in your project URL
# ─────────────────────────────────────────────

BASE_URL = f"https://{SITE_NAME}.teamwork.com"
AUTH_HEADER = {
    "Authorization": "Basic " + b64encode(f"{API_KEY}:x".encode()).decode(),
    "Content-Type": "application/json",
}


def get_all_pages(endpoint, key, params=None):
    results = []
    page = 1
    while True:
        time.sleep(0.25)
        p = dict(params or {})
        p["page"] = page
        p["pageSize"] = 100
        resp = requests.get(BASE_URL + endpoint, headers=AUTH_HEADER, params=p, timeout=30)
        if resp.status_code != 200:
            print(f"  [{resp.status_code}] {endpoint}")
            break
        data = resp.json()
        items = data.get(key, [])
        results.extend(items)
        print(f"  Page {page}: {len(items)} items (total so far: {len(results)})")
        if len(items) < 100:
            break
        page += 1
    return results


if __name__ == "__main__":
    if API_KEY == "YOUR_API_KEY_HERE":
        print("❌ Please set your API_KEY in the configuration section.")
        exit(1)

    print(f"Sweeping ALL completed tasks for project {PROJECT_ID}…")
    print("(This may take a moment depending on project size)\n")

    tasks = get_all_pages(
        f"/projects/{PROJECT_ID}/tasks.json",
        "todo-items",
        params={
            "completedOnly": "true",
            "includeCompletedTasks": "true",
        },
    )

    print(f"\nTotal completed tasks found: {len(tasks)}")

    # Extract unique tasklists from task records
    tasklists = {}
    for t in tasks:
        tl_id   = t.get("todo-list-id") or t.get("taskListId")
        tl_name = t.get("todo-list-name") or t.get("taskListName", "Unknown")
        if tl_id and tl_id not in tasklists:
            tasklists[tl_id] = tl_name

    print(f"Unique completed task lists discovered: {len(tasklists)}\n")
    print("Completed task list IDs and names:")
    print("-" * 50)
    for tl_id, tl_name in sorted(tasklists.items(), key=lambda x: x[1]):
        print(f"  {tl_id}: {tl_name}")

    print("\n-- Paste the following into teamwork_export.py --")
    print("COMPLETED_TASKLIST_IDS = {")
    for tl_id, tl_name in tasklists.items():
        print(f"    {tl_id}: \"{tl_name}\",")
    print("}")
