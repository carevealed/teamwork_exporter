"""
teamwork_export.py
===================
Exports everything from a Teamwork.com project to a permanent local archive:
  - A folder of Markdown files (one per task / message / notebook)
  - A single self-contained, searchable HTML report
  - A raw JSON backup of all data

Exports:
  - All task lists (active and completed) with tasks and comments
  - All messages (active and archived) with replies
  - All notebooks
  - Project files (downloaded)
  - Task and message attachments (downloaded)

Usage:
  1. Run teamwork_discover_completed_lists.py first to find your completed
     task list IDs, then paste them into COMPLETED_TASKLIST_IDS below.
  2. Fill in API_KEY, SITE_NAME, and PROJECT_ID.
  3. Run:  python teamwork_export.py
  4. Outputs are saved to a folder named  teamwork_export_YYYYMMDD_HHMMSS/

Optional:
  - Set MAX_FILE_MB to skip files above a certain size (0 = no limit).
  - File downloads are resumable — re-running skips already-downloaded files.

Requirements:
  pip install requests

Getting your API key:
  Teamwork → profile avatar → Edit Profile → API Keys

Notes:
  - Completed task lists are not discoverable via the Teamwork API and must
    be supplied manually in COMPLETED_TASKLIST_IDS (see above).
  - The script rate-limits itself to 4 requests/second to be polite to the API.
  - Large projects (1000+ tasks) may take 20-30 minutes to export fully.
"""

import requests
import json
import os
import re
import time
from datetime import datetime
from base64 import b64encode

# ─────────────────────────────────────────────
#  CONFIGURATION — fill these in before running
# ─────────────────────────────────────────────
API_KEY    = "YOUR_API_KEY_HERE"   # Teamwork API key
SITE_NAME  = "yoursite"            # subdomain: yoursite.teamwork.com
PROJECT_ID = "0000000"             # found in your project URL

# Completed/archived task lists are discovered automatically at runtime
# by sweeping all completed tasks project-wide. You can optionally hardcode
# them here to skip the discovery step on future runs (faster).
# Example:
#   COMPLETED_TASKLIST_IDS = {
#       1234567: "Sprint 1",
#       1234568: "Sprint 2",
#   }
# Leave as {} to always auto-discover.
COMPLETED_TASKLIST_IDS = {}

MAX_FILE_MB = 500   # skip files larger than this in MB (0 = no limit)
# ─────────────────────────────────────────────

BASE_URL = f"https://{SITE_NAME}.teamwork.com"

# Auth: Teamwork accepts  APIkey:anystring  as Basic auth
AUTH_HEADER = {
    "Authorization": "Basic " + b64encode(f"{API_KEY}:x".encode()).decode(),
    "Content-Type": "application/json",
}

RATE_LIMIT_PAUSE = 0.25   # seconds between requests (be polite to the API)


# ── Helpers ──────────────────────────────────

def get(endpoint, params=None):
    """GET with simple rate-limit pause and error reporting."""
    time.sleep(RATE_LIMIT_PAUSE)
    url = BASE_URL + endpoint
    resp = requests.get(url, headers=AUTH_HEADER, params=params, timeout=30)
    if resp.status_code == 200:
        return resp.json()
    else:
        print(f"  ⚠  {resp.status_code} on {endpoint}: {resp.text[:200]}")
        return None


def safe_filename(text, max_len=60):
    """Convert a string to a safe filename."""
    text = re.sub(r'[\\/*?:"<>|]', "", text)
    text = text.strip().replace(" ", "_")
    return text[:max_len] if text else "untitled"


def html_escape(text):
    """Minimal HTML escaping."""
    return (text or "").\
        replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fmt_date(iso_str):
    """Format an ISO date string to something readable."""
    if not iso_str:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return iso_str


def get_all_pages(endpoint, key, params=None):
    """Fetch all pages of a paginated endpoint and return combined list."""
    results = []
    page = 1
    while True:
        p = dict(params or {})
        p["page"] = page
        p["pageSize"] = 100
        data = get(endpoint, params=p)
        if not data:
            break
        items = data.get(key, [])
        results.extend(items)
        # Teamwork uses X-Pages header; approximate via item count
        if len(items) < 100:
            break
        page += 1
    return results


# ── Fetch functions ───────────────────────────

def discover_completed_task_lists():
    """Sweep all completed tasks project-wide to discover completed task list IDs.
    The Teamwork API does not expose completed task lists directly — this is the
    workaround: every task record contains its parent tasklist ID and name."""
    print("  Discovering completed task lists by sweeping completed tasks…")
    tasks = get_all_pages(
        f"/projects/{PROJECT_ID}/tasks.json",
        "todo-items",
        params={
            "completedOnly": "true",
            "includeCompletedTasks": "true",
        },
    )
    discovered = {}
    for t in tasks:
        tl_id   = t.get("todo-list-id") or t.get("taskListId")
        tl_name = t.get("todo-list-name") or t.get("taskListName", "Unknown")
        if tl_id and tl_id not in discovered:
            discovered[tl_id] = tl_name
    print(f"  ✓ Discovered {len(discovered)} completed task lists from {len(tasks)} completed tasks")
    return discovered


def fetch_task_lists():
    print("  Fetching active task lists…")
    active = get_all_pages(
        f"/projects/{PROJECT_ID}/tasklists.json",
        "tasklists",
    )
    active_ids = {tl["id"] for tl in active}

    # Use hardcoded IDs if provided, otherwise auto-discover
    if COMPLETED_TASKLIST_IDS:
        print(f"  Using {len(COMPLETED_TASKLIST_IDS)} hardcoded completed task lists…")
        completed_ids = COMPLETED_TASKLIST_IDS
    else:
        completed_ids = discover_completed_task_lists()

    # Build list entries, filtering any that already appear in active lists
    completed = []
    for tl_id, tl_name in completed_ids.items():
        if tl_id not in active_ids:
            completed.append({
                "id": tl_id,
                "name": tl_name,
                "_archived": True,
            })

    print(f"  ✓ {len(active)} active + {len(completed)} completed task lists ready")
    return active + completed


def fetch_tasks_for_list(tasklist_id, archived=False):
    # Diagnostic confirmed: /tasklists/{id}/tasks.json?includeCompletedTasks=true
    # works for both active and completed lists
    return get_all_pages(
        f"/tasklists/{tasklist_id}/tasks.json",
        "todo-items",
        params={
            "includeCompletedTasks": "true",
            "nestSubTasks": "true",
            "getAllTasks": "true",
        },
    )


def fetch_comments(resource_type, resource_id):
    """Fetch all comments for a resource, with fallback for message endpoints.
    resource_type: tasks | messages | posts | notebooks | fileversions
    Falls back silently from 'posts' to 'messages' if API returns 400."""
    time.sleep(RATE_LIMIT_PAUSE)
    url = BASE_URL + f"/{resource_type}/{resource_id}/comments.json"
    resp = requests.get(url, headers=AUTH_HEADER, params={"pageSize": 100}, timeout=30)
    if resp.status_code == 200:
        data = resp.json()
        comments = list(data.get("comments", []))
        page = 2
        while len(data.get("comments", [])) == 100:
            time.sleep(RATE_LIMIT_PAUSE)
            r2 = requests.get(url, headers=AUTH_HEADER,
                              params={"pageSize": 100, "page": page}, timeout=30)
            if r2.status_code != 200:
                break
            data = r2.json()
            comments.extend(data.get("comments", []))
            page += 1
        return comments
    elif resp.status_code == 400 and resource_type == "posts":
        # Teamwork sometimes requires /messages/ instead of /posts/ for comments
        return fetch_comments("messages", resource_id)
    else:
        return []


def fetch_messages():
    print("  Fetching messages…")
    # Active messages
    active = get_all_pages(
        f"/projects/{PROJECT_ID}/posts.json",
        "posts",
    )
    # Archived messages
    archived_data = get(f"/projects/{PROJECT_ID}/posts/archive.json")
    archived = archived_data.get("posts", []) if archived_data else []
    return active, archived


def fetch_message_detail(message_id):
    data = get(f"/posts/{message_id}.json")
    return data.get("post", {}) if data else {}


def fetch_notebooks():
    print("  Fetching notebooks…")
    return get_all_pages(
        f"/projects/{PROJECT_ID}/notebooks.json",
        "notebooks",
    )


def fetch_notebook_detail(notebook_id):
    data = get(f"/notebooks/{notebook_id}.json")
    return data.get("notebook", {}) if data else {}


def fetch_files():
    print("  Fetching files…")
    return get_all_pages(
        f"/projects/{PROJECT_ID}/files.json",
        "files",
    )


# ── Markdown writers ──────────────────────────

def task_to_markdown(task, comments):
    status = "✅ Completed" if task.get("completed") else "🔲 Open"
    priority = task.get("priority", "none") or "none"
    assigned = task.get("responsible-party-names", "—") or "—"
    due = fmt_date(task.get("due-date", ""))
    created = fmt_date(task.get("created-on", ""))
    description = (task.get("description") or "").strip()

    lines = [
        f"# {task.get('content', 'Untitled Task')}",
        "",
        f"**Status:** {status}  ",
        f"**Priority:** {priority}  ",
        f"**Assigned to:** {assigned}  ",
        f"**Due:** {due}  ",
        f"**Created:** {created}  ",
        f"**Task ID:** {task.get('id', '—')}  ",
        f"**Task List:** {task.get('todo-list-name', '—')}  ",
        "",
    ]

    if description:
        lines += ["## Description", "", description, ""]

    if comments:
        lines.append("## Comments")
        lines.append("")
        for c in comments:
            author = f"{c.get('author-firstname','')} {c.get('author-lastname','')}".strip()
            date = fmt_date(c.get("datetime", ""))
            body = (c.get("html-body") or c.get("body") or "").strip()
            # Strip HTML tags for markdown
            body = re.sub(r"<[^>]+>", "", body)
            lines += [
                f"---",
                f"**{author}** — {date}",
                "",
                body,
                "",
            ]

    return "\n".join(lines)


def message_to_markdown(msg, comments):
    author = f"{msg.get('author-first-name','')} {msg.get('author-last-name','')}".strip()
    posted = fmt_date(msg.get("posted-on", ""))
    body = (msg.get("html-body") or msg.get("body") or "").strip()
    body_text = re.sub(r"<[^>]+>", "", body)

    lines = [
        f"# {msg.get('title', 'Untitled Message')}",
        "",
        f"**Posted by:** {author}  ",
        f"**Posted on:** {posted}  ",
        f"**Message ID:** {msg.get('id', '—')}  ",
        "",
        "## Body",
        "",
        body_text,
        "",
    ]

    if comments:
        lines.append("## Replies / Comments")
        lines.append("")
        for c in comments:
            cauthor = f"{c.get('author-firstname','')} {c.get('author-lastname','')}".strip()
            date = fmt_date(c.get("datetime", ""))
            cbody = re.sub(r"<[^>]+>", "", (c.get("html-body") or c.get("body") or "").strip())
            lines += [
                "---",
                f"**{cauthor}** — {date}",
                "",
                cbody,
                "",
            ]

    return "\n".join(lines)


def notebook_to_markdown(nb):
    author = nb.get("author-first-name", "") + " " + nb.get("author-last-name", "")
    created = fmt_date(nb.get("created-on", ""))
    body = (nb.get("html-content") or nb.get("content") or "").strip()
    body_text = re.sub(r"<[^>]+>", "", body)

    return "\n".join([
        f"# {nb.get('name', 'Untitled Notebook')}",
        "",
        f"**Created by:** {author.strip()}  ",
        f"**Created on:** {created}  ",
        f"**Notebook ID:** {nb.get('id', '—')}  ",
        "",
        "## Content",
        "",
        body_text,
    ])


def files_to_markdown(files):
    lines = ["# Files", "", f"Total files: {len(files)}", ""]
    for f in files:
        uploader = f.get("uploader-firstname", "") + " " + f.get("uploader-lastname", "")
        lines += [
            f"## {f.get('name', 'Unnamed')}",
            f"- **Uploaded by:** {uploader.strip()}",
            f"- **Date:** {fmt_date(f.get('created-at', ''))}",
            f"- **Size:** {f.get('size', '—')} bytes",
            f"- **File ID:** {f.get('id', '—')}",
            f"- **Download URL:** {f.get('download-url', '—')}",
            "",
        ]
    return "\n".join(lines)


# ── HTML report builder ───────────────────────

def build_html_report(all_data, project_name):
    tasks_by_list = all_data["tasks_by_list"]
    messages      = all_data["messages"]
    archived_msgs = all_data["archived_messages"]
    notebooks     = all_data["notebooks"]
    files         = all_data["files"]
    export_date   = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    def render_comments(comments):
        if not comments:
            return "<p><em>No comments.</em></p>"
        html = ""
        for c in comments:
            author = f"{c.get('author-firstname','')} {c.get('author-lastname','')}".strip()
            date = fmt_date(c.get("datetime",""))
            body = c.get("html-body") or c.get("body") or ""
            if not c.get("html-body"):
                body = html_escape(body).replace("\n", "<br>")
            html += f"""
            <div class="comment">
              <div class="comment-meta">💬 <strong>{html_escape(author)}</strong> &mdash; {date}</div>
              <div class="comment-body">{body}</div>
            </div>"""
        return html

    # Build task sections
    task_html = ""
    for tl_name, tasks in tasks_by_list.items():
        task_html += f'<h3 class="tasklist-name">📋 {html_escape(tl_name)}</h3>\n'
        for t in tasks:
            status_icon = "✅" if t.get("completed") else "🔲"
            title = html_escape(t.get("content","Untitled"))
            assigned = html_escape(t.get("responsible-party-names","—") or "—")
            due = fmt_date(t.get("due-date",""))
            created = fmt_date(t.get("created-on",""))
            desc = t.get("description","") or ""
            desc_html = html_escape(desc).replace("\n","<br>") if desc else ""
            tid = t.get("id","")
            comments_html = render_comments(t.get("_comments", []))
            task_html += f"""
            <div class="task">
              <div class="task-title">{status_icon} {title}</div>
              <div class="task-meta">
                <span>👤 {assigned}</span>
                <span>📅 Due: {due}</span>
                <span>🕒 Created: {created}</span>
                <span class="task-id">ID: {tid}</span>
              </div>
              {"<div class='task-desc'>" + desc_html + "</div>" if desc_html else ""}
              <div class="comments-section">{comments_html}</div>
            </div>"""

    # Build message sections
    def render_messages(msgs, archived=False):
        html = ""
        label = "🗄 Archived" if archived else ""
        for m in msgs:
            title = html_escape(m.get("title","Untitled"))
            author = html_escape(f"{m.get('author-first-name','')} {m.get('author-last-name','')}".strip())
            posted = fmt_date(m.get("posted-on",""))
            body = m.get("html-body") or m.get("body") or ""
            if not m.get("html-body"):
                body = html_escape(body).replace("\n","<br>")
            comments_html = render_comments(m.get("_comments",[]))
            html += f"""
            <div class="message {'archived' if archived else ''}">
              <div class="message-title">{'🗄 ' if archived else '📨 '}{title}</div>
              <div class="task-meta">
                <span>👤 {author}</span>
                <span>🕒 {posted}</span>
                {f'<span class="archived-tag">Archived</span>' if archived else ''}
              </div>
              <div class="message-body">{body}</div>
              <div class="comments-section">{comments_html}</div>
            </div>"""
        return html

    msg_html = render_messages(messages) + render_messages(archived_msgs, archived=True)

    # Notebooks
    nb_html = ""
    for nb in notebooks:
        name = html_escape(nb.get("name","Untitled"))
        author = html_escape(f"{nb.get('author-first-name','')} {nb.get('author-last-name','')}".strip())
        created = fmt_date(nb.get("created-on",""))
        body = nb.get("html-content") or nb.get("content") or ""
        if not nb.get("html-content"):
            body = html_escape(body).replace("\n","<br>")
        nb_html += f"""
        <div class="notebook">
          <div class="message-title">📓 {name}</div>
          <div class="task-meta"><span>👤 {author}</span> <span>🕒 {created}</span></div>
          <div class="message-body">{body}</div>
        </div>"""

    # Files table
    files_html = '<table class="files-table"><thead><tr><th>Name</th><th>Uploaded By</th><th>Date</th><th>Size</th><th>Link</th></tr></thead><tbody>'
    for f in files:
        name = html_escape(f.get("name","—"))
        uploader = html_escape(f"{f.get('uploader-firstname','')} {f.get('uploader-lastname','')}".strip())
        date = fmt_date(f.get("created-at",""))
        size = f.get("size","—")
        url = f.get("download-url","")
        link = f'<a href="{url}" target="_blank">Download</a>' if url else "—"
        files_html += f"<tr><td>{name}</td><td>{uploader}</td><td>{date}</td><td>{size}</td><td>{link}</td></tr>"
    files_html += "</tbody></table>"

    total_tasks = sum(len(v) for v in tasks_by_list.values())

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Teamwork Export — {html_escape(project_name)}</title>
<style>
  :root {{
    --bg: #f8f9fa; --card: #ffffff; --border: #dee2e6;
    --primary: #2c5f8a; --accent: #e8f0fe; --text: #212529;
    --muted: #6c757d; --archived: #fff8e1;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
          background: var(--bg); color: var(--text); font-size: 14px; line-height: 1.6; }}
  header {{ background: var(--primary); color: white; padding: 20px 32px; }}
  header h1 {{ font-size: 1.6rem; font-weight: 600; }}
  header p {{ opacity: 0.8; font-size: 0.85rem; margin-top: 4px; }}
  .stats {{ display: flex; gap: 24px; padding: 16px 32px; background: white;
            border-bottom: 1px solid var(--border); flex-wrap: wrap; }}
  .stat {{ text-align: center; }}
  .stat .num {{ font-size: 1.8rem; font-weight: 700; color: var(--primary); }}
  .stat .lbl {{ font-size: 0.75rem; color: var(--muted); text-transform: uppercase; }}
  nav {{ display: flex; gap: 0; background: var(--primary); overflow-x: auto; }}
  nav a {{ color: rgba(255,255,255,0.8); text-decoration: none; padding: 10px 20px;
           font-size: 0.85rem; white-space: nowrap; border-bottom: 3px solid transparent; }}
  nav a:hover, nav a.active {{ color: white; border-bottom-color: #90caf9; background: rgba(255,255,255,0.1); }}
  .section {{ display: none; padding: 24px 32px; max-width: 1100px; }}
  .section.active {{ display: block; }}
  h2.section-title {{ font-size: 1.2rem; color: var(--primary); margin-bottom: 16px;
                      padding-bottom: 8px; border-bottom: 2px solid var(--accent); }}
  .tasklist-name {{ font-size: 1rem; color: var(--muted); margin: 20px 0 8px;
                    text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600; }}
  .task, .message, .notebook {{ background: var(--card); border: 1px solid var(--border);
    border-radius: 6px; padding: 14px 18px; margin-bottom: 12px; }}
  .task:hover, .message:hover {{ box-shadow: 0 2px 8px rgba(0,0,0,0.08); }}
  .task-title, .message-title {{ font-size: 1rem; font-weight: 600; margin-bottom: 6px; }}
  .task-meta {{ display: flex; gap: 16px; flex-wrap: wrap; font-size: 0.78rem;
               color: var(--muted); margin-bottom: 8px; }}
  .task-id {{ font-family: monospace; }}
  .task-desc {{ background: #f1f3f5; border-left: 3px solid var(--primary);
               padding: 8px 12px; border-radius: 4px; margin: 8px 0; font-size: 0.85rem; }}
  .comments-section {{ margin-top: 10px; }}
  .comment {{ background: var(--accent); border-radius: 4px; padding: 8px 12px; margin: 6px 0; }}
  .comment-meta {{ font-size: 0.78rem; color: var(--muted); margin-bottom: 4px; }}
  .comment-body {{ font-size: 0.88rem; }}
  .message-body {{ margin-top: 10px; font-size: 0.9rem; border-top: 1px solid var(--border); padding-top: 10px; }}
  .archived {{ background: var(--archived); }}
  .archived-tag {{ background: #f59e0b; color: white; padding: 1px 6px; border-radius: 10px; font-size: 0.7rem; }}
  .files-table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; }}
  .files-table th {{ background: var(--primary); color: white; padding: 8px 12px; text-align: left; }}
  .files-table td {{ padding: 8px 12px; border-bottom: 1px solid var(--border); }}
  .files-table tr:hover td {{ background: var(--accent); }}
  .search-box {{ width: 100%; padding: 10px 14px; border: 1px solid var(--border);
                border-radius: 6px; font-size: 0.9rem; margin-bottom: 16px; }}
  @media (max-width: 600px) {{ .section {{ padding: 16px; }} .stats {{ gap: 12px; padding: 12px; }} }}
</style>
</head>
<body>
<header>
  <h1>📦 {html_escape(project_name)}</h1>
  <p>Teamwork.com Export &mdash; Generated {export_date}</p>
</header>
<div class="stats">
  <div class="stat"><div class="num">{total_tasks}</div><div class="lbl">Tasks</div></div>
  <div class="stat"><div class="num">{len(messages)}</div><div class="lbl">Messages</div></div>
  <div class="stat"><div class="num">{len(archived_msgs)}</div><div class="lbl">Archived Msgs</div></div>
  <div class="stat"><div class="num">{len(notebooks)}</div><div class="lbl">Notebooks</div></div>
  <div class="stat"><div class="num">{len(files)}</div><div class="lbl">Files</div></div>
</div>
<nav>
  <a href="#" class="active" onclick="showSection('tasks',this)">📋 Tasks</a>
  <a href="#" onclick="showSection('messages',this)">📨 Messages</a>
  <a href="#" onclick="showSection('notebooks',this)">📓 Notebooks</a>
  <a href="#" onclick="showSection('files',this)">📎 Files</a>
</nav>

<div id="tasks" class="section active">
  <h2 class="section-title">Tasks &amp; Comments</h2>
  <input class="search-box" type="text" placeholder="🔍 Filter tasks by keyword…" oninput="filterItems(this,'task')">
  {task_html or '<p>No tasks found.</p>'}
</div>

<div id="messages" class="section">
  <h2 class="section-title">Messages &amp; Replies</h2>
  <input class="search-box" type="text" placeholder="🔍 Filter messages by keyword…" oninput="filterItems(this,'message')">
  {msg_html or '<p>No messages found.</p>'}
</div>

<div id="notebooks" class="section">
  <h2 class="section-title">Notebooks</h2>
  {nb_html or '<p>No notebooks found.</p>'}
</div>

<div id="files" class="section">
  <h2 class="section-title">Files</h2>
  {files_html}
</div>

<script>
function showSection(id, el) {{
  document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('nav a').forEach(a => a.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  el.classList.add('active');
  return false;
}}
function filterItems(input, cls) {{
  const q = input.value.toLowerCase();
  document.querySelectorAll('.' + cls).forEach(el => {{
    el.style.display = el.textContent.toLowerCase().includes(q) ? '' : 'none';
  }});
}}
</script>
</body>
</html>"""
    return html



# ── File download helpers ─────────────────────

def human_size(num_bytes):
    """Format bytes as a human-readable string."""
    try:
        num_bytes = int(num_bytes)
    except (ValueError, TypeError):
        return "unknown size"
    for unit in ["B", "KB", "MB", "GB"]:
        if num_bytes < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"


def download_file(file_version_id, filename, dest_dir):
    """Download a single file by its fileVersionId into dest_dir.
    Returns: 'downloaded' | 'skipped' | 'skipped_size' | 'not_found' | 'error'
    """
    os.makedirs(dest_dir, exist_ok=True)
    # Sanitise filename
    filename = re.sub(r'[\\/*?:"<>|]', "_", str(filename)).strip() or "unnamed_file"
    dest_path = os.path.join(dest_dir, filename)

    # Resumable — skip if already downloaded
    if os.path.exists(dest_path):
        return "skipped"

    url = f"{BASE_URL}/fileversions/{file_version_id}/download"
    # Use auth header without Content-Type for binary downloads
    headers = {"Authorization": AUTH_HEADER["Authorization"]}
    time.sleep(RATE_LIMIT_PAUSE)

    try:
        resp = requests.get(url, headers=headers, stream=True, timeout=60)
        # Fall back to /files/{id}/download if fileversions 404s
        if resp.status_code in (404, 400):
            url = f"{BASE_URL}/files/{file_version_id}/download"
            resp = requests.get(url, headers=headers, stream=True, timeout=60)

        if resp.status_code == 200:
            content_length = int(resp.headers.get("Content-Length", 0))
            if MAX_FILE_MB > 0 and content_length > MAX_FILE_MB * 1024 * 1024:
                print(f"      ⚠  Skipping {filename} — {human_size(content_length)} exceeds {MAX_FILE_MB}MB limit")
                return "skipped_size"
            with open(dest_path, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=8192):
                    fh.write(chunk)
            size = os.path.getsize(dest_path)
            print(f"      ✓  {filename} ({human_size(size)})")
            return "downloaded"
        elif resp.status_code == 404:
            print(f"      ✗  Not found (404): {filename}")
            return "not_found"
        else:
            print(f"      ✗  [{resp.status_code}] {filename}")
            return "error"
    except Exception as e:
        print(f"      ✗  Error downloading {filename}: {e}")
        return "error"


def download_attachments(attachments, dest_dir, stats):
    """Download a list of attachment records into dest_dir, updating stats."""
    for att in attachments:
        file_version_id = att.get("fileVersionId") or att.get("file-version-id")
        filename        = att.get("filename") or att.get("name") or f"file_{att.get('id','unknown')}"
        if not file_version_id:
            print(f"      ⚠  No fileVersionId for: {filename} — skipping")
            stats["skipped"] = stats.get("skipped", 0) + 1
            continue
        result = download_file(file_version_id, filename, dest_dir)
        stats[result] = stats.get(result, 0) + 1


def get_attachments(obj):
    """Extract all attachment records from a task, message, or comment."""
    atts = []
    if obj.get("attachments"):
        atts.extend(obj["attachments"])
    if obj.get("pendingFileAttachments"):
        atts.extend(obj["pendingFileAttachments"])
    return atts


# ── Main export orchestrator ──────────────────

def run_export():
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    out_dir = f"teamwork_export_{timestamp}"
    md_tasks_dir   = os.path.join(out_dir, "tasks")
    md_msgs_dir    = os.path.join(out_dir, "messages")
    md_nb_dir      = os.path.join(out_dir, "notebooks")
    dl_proj_dir    = os.path.join(out_dir, "downloads", "project_files")
    dl_tasks_dir   = os.path.join(out_dir, "downloads", "tasks")
    dl_msgs_dir    = os.path.join(out_dir, "downloads", "messages")
    for d in [out_dir, md_tasks_dir, md_msgs_dir, md_nb_dir,
              dl_proj_dir, dl_tasks_dir, dl_msgs_dir]:
        os.makedirs(d, exist_ok=True)

    download_stats = {"downloaded": 0, "skipped": 0, "skipped_size": 0,
                      "not_found": 0, "error": 0}

    print(f"\n🚀 Starting Teamwork export for project {PROJECT_ID}")
    print(f"   Site: {SITE_NAME}.teamwork.com")
    print(f"   Output: {out_dir}/\n")

    all_data = {
        "tasks_by_list": {},
        "messages": [],
        "archived_messages": [],
        "notebooks": [],
        "files": [],
    }

    # ── Tasks ──
    print("📋 Exporting tasks…")
    task_lists = fetch_task_lists()
    for tl in task_lists:
        tl_name = tl.get("name", "Unnamed List")
        if tl.get("_archived"):
            tl_name = f"[ARCHIVED] {tl_name}"
        tl_id      = tl.get("id")
        is_archived = tl.get("_archived", False)
        print(f"   Task list: {tl_name}")
        tasks = fetch_tasks_for_list(tl_id, archived=is_archived)
        all_data["tasks_by_list"][tl_name] = tasks
        for task in tasks:
            task_id = task.get("id")
            comments = fetch_comments("tasks", task_id)
            task["_comments"] = comments
            # Download task attachments + comment attachments
            task_atts = get_attachments(task)
            for c in comments:
                task_atts.extend(get_attachments(c))
            if task_atts:
                task_dl_dir = os.path.join(
                    dl_tasks_dir,
                    f"{task_id}_{safe_filename(task.get('content','task'))}"
                )
                print(f"      📎 {len(task_atts)} attachment(s)")
                download_attachments(task_atts, task_dl_dir, download_stats)
            # Write markdown
            md = task_to_markdown(task, comments)
            fname = f"{task_id}_{safe_filename(task.get('content','task'))}.md"
            with open(os.path.join(md_tasks_dir, fname), "w", encoding="utf-8") as f:
                f.write(md)
        print(f"   ✓ {len(tasks)} tasks exported")

    # ── Messages ──
    print("\n📨 Exporting messages…")
    active_msgs, archived_msgs = fetch_messages()

    for msg in active_msgs + archived_msgs:
        msg_id = msg.get("id")
        # Get full message detail (body may be truncated in list)
        detail = fetch_message_detail(msg_id)
        if detail:
            msg.update(detail)
        comments = fetch_comments("messages", msg_id)
        msg["_comments"] = comments
        # Download message attachments + reply attachments
        msg_atts = get_attachments(msg)
        for c in comments:
            msg_atts.extend(get_attachments(c))
        if msg_atts:
            msg_dl_dir = os.path.join(
                dl_msgs_dir,
                f"{msg_id}_{safe_filename(msg.get('title','message'))}"
            )
            print(f"      📎 {len(msg_atts)} attachment(s)")
            download_attachments(msg_atts, msg_dl_dir, download_stats)
        md = message_to_markdown(msg, comments)
        fname = f"{msg_id}_{safe_filename(msg.get('title','message'))}.md"
        with open(os.path.join(md_msgs_dir, fname), "w", encoding="utf-8") as f:
            f.write(md)

    all_data["messages"] = active_msgs
    all_data["archived_messages"] = archived_msgs
    print(f"   ✓ {len(active_msgs)} active, {len(archived_msgs)} archived messages exported")

    # ── Notebooks ──
    print("\n📓 Exporting notebooks…")
    notebooks = fetch_notebooks()
    for nb in notebooks:
        nb_id = nb.get("id")
        detail = fetch_notebook_detail(nb_id)
        if detail:
            nb.update(detail)
        md = notebook_to_markdown(nb)
        fname = f"{nb_id}_{safe_filename(nb.get('name','notebook'))}.md"
        with open(os.path.join(md_nb_dir, fname), "w", encoding="utf-8") as f:
            f.write(md)
    all_data["notebooks"] = notebooks
    print(f"   ✓ {len(notebooks)} notebooks exported")

    # ── Project files — catalog + download ──
    print("\n📎 Exporting project files…")
    files = fetch_files()
    all_data["files"] = files
    files_md = files_to_markdown(files)
    with open(os.path.join(out_dir, "files.md"), "w", encoding="utf-8") as f:
        f.write(files_md)
    print(f"   Catalogued {len(files)} project files — downloading…")
    for pf in files:
        fv_id    = pf.get("fileVersionId") or pf.get("file-version-id") or pf.get("id")
        filename = pf.get("name") or pf.get("filename") or f"file_{pf.get('id','unknown')}"
        if fv_id:
            result = download_file(fv_id, filename, dl_proj_dir)
            download_stats[result] = download_stats.get(result, 0) + 1
    print(f"   ✓ {len(files)} project files processed")

    # ── Raw JSON backup ──
    print("\n💾 Writing raw JSON backup…")
    raw_backup = {
        "export_date": datetime.utcnow().isoformat(),
        "project_id": PROJECT_ID,
        "tasks_by_list": {
            k: [{kk: vv for kk, vv in t.items() if kk != "_comments"} | {"_comments": t.get("_comments",[])}
                for t in v]
            for k, v in all_data["tasks_by_list"].items()
        },
        "messages": all_data["messages"],
        "archived_messages": all_data["archived_messages"],
        "notebooks": all_data["notebooks"],
        "files": all_data["files"],
    }
    with open(os.path.join(out_dir, "raw_export.json"), "w", encoding="utf-8") as f:
        json.dump(raw_backup, f, indent=2, ensure_ascii=False)

    # ── HTML report ──
    print("\n🌐 Building HTML report…")
    html = build_html_report(all_data, f"Teamwork Export — Project {PROJECT_ID}")
    html_path = os.path.join(out_dir, "report.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)

    total_tasks = sum(len(v) for v in all_data["tasks_by_list"].values())
    dl = download_stats
    print(f"""
✅ Export complete!
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Tasks:              {total_tasks}
  Messages:           {len(active_msgs)} active + {len(archived_msgs)} archived
  Notebooks:          {len(notebooks)}
  Files catalogued:   {len(files)}
  ─────────────────────────────
  Files downloaded:   {dl.get("downloaded", 0)}
  Already existed:    {dl.get("skipped", 0)}
  Skipped (size):     {dl.get("skipped_size", 0)}
  Not found (404):    {dl.get("not_found", 0)}
  Download errors:    {dl.get("error", 0)}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Output folder: {out_dir}/
  📄 report.html              ← open in any browser
  📁 tasks/                   ← one .md per task
  📁 messages/                ← one .md per message
  📁 notebooks/               ← one .md per notebook
  📄 files.md                 ← file catalog
  📄 raw_export.json          ← complete raw backup
  📁 downloads/
      project_files/          ← project Files section
      tasks/{{id}}_{{title}}/     ← attachments per task
      messages/{{id}}_{{title}}/ ← attachments per message
""")


if __name__ == "__main__":
    if API_KEY == "YOUR_API_KEY_HERE":
        print("❌ Please set your API_KEY in the configuration section at the top of this file.")
    else:
        run_export()
