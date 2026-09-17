"""
Blackboard Learn PDF Downloader
Supports any school running Blackboard Learn.
Usage: python3 bb_downloader.py

Requires:
  pip install requests beautifulsoup4 playwright rich
"""

import argparse
import json
import re
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from queue import Queue
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from rich.console import Console, Group
from rich.live import Live
from rich.progress import (
    BarColumn,
    DownloadColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.table import Column

CONTAINER_HANDLERS = {
    "resource/x-bb-folder",
    "resource/x-bb-lesson",
    "resource/x-bb-courselink",
    "resource/x-bb-blankpage",
}

# Nodes that should create a subfolder on disk when recursed into.
FOLDER_HANDLERS = {
    "resource/x-bb-folder",
    "resource/x-bb-lesson",
}


# ── Auth & Networking ─────────────────────────────────────────────────────────


class TrackedSession:
    """Wraps requests.Session to track the number of API calls made in a thread-safe way."""

    def __init__(self, session):
        self.session = session
        self.api_calls = 0
        self._lock = threading.Lock()

    def get(self, *args, **kwargs):
        with self._lock:
            self.api_calls += 1
        return self.session.get(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.session, name)


def wait_for_login(page, base_url):
    page.goto(f"{base_url}/ultra/course")
    print("\nA browser window has opened.")
    print("Please log in to Blackboard, then come back here and press Enter.")
    input()


def get_session_from_browser(context):
    session = requests.Session()
    for cookie in context.cookies():
        session.cookies.set(
            cookie["name"], cookie["value"], domain=cookie.get("domain")
        )
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        }
    )
    return TrackedSession(session)


# ── User & courses ────────────────────────────────────────────────────────────


def get_my_user_id(session, base_url):
    resp = session.get(f"{base_url}/learn/api/public/v1/users/me")
    if resp.status_code == 200:
        return resp.json().get("id")
    return None


def get_course_detail(session, base_url, course_id):
    resp = session.get(f"{base_url}/learn/api/public/v1/courses/{course_id}")
    if resp.status_code == 200:
        return resp.json()
    return None


def get_all_enrollments(session, base_url, user_id):
    """Fetch all course enrollments for the current user."""
    enrollments = []
    url = f"{base_url}/learn/api/public/v1/users/{user_id}/courses?limit=100"
    while url:
        resp = session.get(url)
        if resp.status_code != 200:
            print(f"  Failed to fetch enrollments: {resp.status_code}")
            break
        data = resp.json()
        enrollments.extend(data.get("results", []))
        next_page = data.get("paging", {}).get("nextPage")
        url = f"{base_url}{next_page}" if next_page else None
    return enrollments


def build_term_map(session, base_url, enrollments):
    """
    Returns a dict: { "Spring 2026 (26sprg)": [course, ...], ... }
    where each course = {"id": ..., "name": ..., "safe_name": ...}
    """
    print("  Fetching course details (this may take a moment)...")
    term_map = {}

    for enrollment in enrollments:
        raw_id = enrollment.get("courseId")
        if not raw_id:
            continue
        detail = get_course_detail(session, base_url, raw_id)
        if not detail:
            continue

        course_code = detail.get("courseId", "")
        name = detail.get("name", "Unknown")
        term_label = (
            detail.get("term", {}).get("name")
            or _infer_term(course_code)
            or "Unknown Term"
        )
        safe_name = _safe(name)

        term_map.setdefault(term_label, []).append(
            {
                "id": raw_id,
                "name": name,
                "safe_name": safe_name,
                "course_code": course_code,
            }
        )

    return term_map


def _infer_term(course_code):
    """Try to guess a human-readable term from the courseId string."""
    m = re.match(r"(\d{2})(sprg|fall|sum[123]?)", course_code, re.IGNORECASE)
    if not m:
        return None
    yy, sem = m.group(1), m.group(2).lower()
    year = f"20{yy}"
    names = {
        "sprg": "Spring",
        "fall": "Fall",
        "sum": "Summer",
        "sum1": "Summer 1",
        "sum2": "Summer 2",
        "sum3": "Summer 3",
    }
    return f"{names.get(sem, sem.title())} {year}"


def _safe(name):
    return re.sub(r'[<>:"/\\|?*]', "_", name).strip()


def pick_term(term_map):
    """Interactive term picker. Returns list of selected courses."""
    terms = sorted(term_map.keys())
    print("\nAvailable terms:")
    for i, t in enumerate(terms, 1):
        print(f"  [{i}] {t}  ({len(term_map[t])} courses)")
    print("  [0] All terms")

    while True:
        raw = input("\nSelect term number: ").strip()
        if raw == "0":
            all_courses = [c for courses in term_map.values() for c in courses]
            return all_courses
        if raw.isdigit() and 1 <= int(raw) <= len(terms):
            return term_map[terms[int(raw) - 1]]
        print("  Invalid input, try again.")


def pick_courses(courses):
    """
    Interactive multi-select course picker.
    Accepts: 'all' / blank, comma-separated indices, hyphen ranges, or a mix
    (e.g. '1,3,5', '1-3', '1-3,5,7-9'). Returns the chosen subset in original order.
    """
    print("\nCourses in selected term:")
    for i, c in enumerate(courses, 1):
        print(f"  [{i}] {c['name']}")
    print("\nEnter course numbers to download (e.g. 1,3,5 or 1-3,5).")
    print("Press Enter or type 'all' for every course.")

    while True:
        raw = input("  > ").strip().lower()
        if raw == "" or raw == "all":
            return courses

        chosen = set()
        ok = True
        for token in raw.split(","):
            token = token.strip()
            if not token:
                continue
            if "-" in token:
                parts = token.split("-")
                if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
                    ok = False
                    break
                a, b = int(parts[0]), int(parts[1])
                if a > b:
                    a, b = b, a
                for n in range(a, b + 1):
                    if not 1 <= n <= len(courses):
                        ok = False
                        break
                    chosen.add(n)
                if not ok:
                    break
            else:
                if not token.isdigit():
                    ok = False
                    break
                n = int(token)
                if not 1 <= n <= len(courses):
                    ok = False
                    break
                chosen.add(n)

        if not ok or not chosen:
            print("  Invalid input, try again.")
            continue

        selected = [courses[i - 1] for i in sorted(chosen)]
        print("\nSelected courses:")
        for c in selected:
            print(f"  - {c['name']}")
        return selected


def filter_courses_by_query(courses, queries):
    """Filter courses by case-insensitive substring match against name or course code."""
    queries_lc = [q.lower() for q in queries]
    matched = []
    seen_match = {q: False for q in queries_lc}
    for c in courses:
        name_lc = c["name"].lower()
        code_lc = c.get("course_code", "").lower()
        for q in queries_lc:
            if q in name_lc or q in code_lc:
                matched.append(c)
                seen_match[q] = True
                break
    for q, hit in seen_match.items():
        if not hit:
            print(f"  Warning: no course matched '{q}'")
    return matched


# ── Content traversal ─────────────────────────────────────────────────────────


def get_top_level_sections(session, base_url, course_id):
    resp = session.get(
        f"{base_url}/learn/api/public/v1/courses/{course_id}/contents?limit=100"
    )
    if resp.status_code != 200:
        return []
    sections = []
    for item in resp.json().get("results", []):
        sections.append(
            {
                "id": item.get("id"),
                "name": _safe(item.get("title", "Untitled")),
            }
        )
    return sections


def _html_to_markdown(html_content):
    """Convert Blackboard HTML body content into clean Markdown text."""
    if not html_content:
        return ""
    soup = BeautifulSoup(html_content, "html.parser")
    for script in soup(["script", "style"]):
        script.decompose()

    # Remove inline bbfile links since their attachments are downloaded separately
    for a in soup.find_all("a", attrs={"data-bbfile": True}):
        a.decompose()

    for a in soup.find_all("a"):
        href = a.get("href")
        text = a.get_text().strip()
        if isinstance(href, str):
            if href and text:
                a.replace_with(f"[{text}]({href})")
            elif href:
                a.replace_with(href)

    for p in soup.find_all(["p", "div", "br", "li"]):
        p.insert_after("\n")

    text = soup.get_text()
    lines = [line.strip() for line in text.splitlines()]
    cleaned_text = "\n".join([line for line in lines if line])

    if len(cleaned_text.strip()) < 3:
        return ""
    return cleaned_text


def collect_files_recursive(
    session, base_url, course_id, item_id, extensions, call_counter=None
):
    """
    Recursively collect all downloadable files and descriptive post bodies under item_id.
    Ties items and their attachments together in a dedicated subfolder named after the item title.
    """
    files = []

    def fetch(node_id, rel_path):
        if call_counter is not None:
            call_counter[0] += 1
        detail_resp = session.get(
            f"{base_url}/learn/api/public/v1/courses/{course_id}/contents/{node_id}"
        )

        item_rel_path = list(rel_path)
        attachments = []
        md_content = ""
        title = ""

        if detail_resp.status_code == 200:
            item_data = detail_resp.json()
            title = item_data.get("title", "")
            body = item_data.get("body", "")
            md_content = _html_to_markdown(body)

        if call_counter is not None:
            call_counter[0] += 1
        att_resp = session.get(
            f"{base_url}/learn/api/public/v1/courses/{course_id}/contents/{node_id}/attachments"
        )
        if att_resp.status_code == 200:
            for att in att_resp.json().get("results", []):
                filename = att.get("fileName", "")
                mime = att.get("mimeType", "")
                if _matches(filename, mime, extensions):
                    dl_url = (
                        f"{base_url}/learn/api/public/v1/courses/{course_id}"
                        f"/contents/{node_id}/attachments/{att['id']}/download"
                    )
                    attachments.append(
                        {
                            "type": "file",
                            "url": dl_url,
                            "filename": filename,
                        }
                    )

        # If this content item has a title and contains either text or attachments,
        # group them together in a subfolder named after the item title.
        if title and (md_content or attachments):
            item_rel_path = rel_path + [_safe(title)]
            if md_content:
                files.append(
                    {
                        "type": "text",
                        "content": md_content,
                        "filename": f"{_safe(title)}.md",
                        "rel_path": list(item_rel_path),
                    }
                )
        else:
            if md_content and title:
                files.append(
                    {
                        "type": "text",
                        "content": md_content,
                        "filename": f"{_safe(title)}.md",
                        "rel_path": list(rel_path),
                    }
                )

        for att_item in attachments:
            att_item["rel_path"] = list(item_rel_path)
            files.append(att_item)

        if detail_resp.status_code == 200:
            for f in _extract_from_body(
                item_data.get("body", ""), base_url, extensions
            ):
                f["rel_path"] = list(item_rel_path)
                files.append(f)

        if call_counter is not None:
            call_counter[0] += 1
        children_resp = session.get(
            f"{base_url}/learn/api/public/v1/courses/{course_id}/contents/{node_id}/children?limit=100"
        )
        if children_resp.status_code == 200:
            for child in children_resp.json().get("results", []):
                child_id = child.get("id")
                if not child_id:
                    continue
                handler = (child.get("contentHandler") or {}).get("id", "")
                child_name = _safe(child.get("title", "Untitled"))
                if handler in FOLDER_HANDLERS:
                    fetch(child_id, rel_path + [child_name])
                else:
                    fetch(child_id, rel_path)

    fetch(item_id, [])
    return files


def _extract_from_body(body, base_url, extensions):
    """Parse Blackboard body HTML for file links (data-bbfile and plain hrefs)."""
    if not body:
        return []
    files = []
    soup = BeautifulSoup(body, "html.parser")

    for a in soup.find_all("a"):
        bbfile = a.get("data-bbfile")
        if isinstance(bbfile, str):
            try:
                info = json.loads(bbfile)
                filename = info.get("displayName") or info.get("linkName", "")
                mime = info.get("mimeType", "")
                url = info.get("resourceUrl") or a.get("href", "")
                if url and _matches(filename, mime, extensions):
                    files.append({"type": "file", "url": url, "filename": filename})
            except json.JSONDecodeError:
                pass
            except AttributeError:
                pass
            continue

        href = a.get("href")
        if not isinstance(href, str) or not href:
            continue

        filename = unquote(urlparse(href).path.split("/")[-1])
        if _matches(filename, "", extensions):
            files.append(
                {"type": "file", "url": urljoin(base_url, href), "filename": filename}
            )

    return files


def _matches(filename, mime, extensions):
    """Check if a file matches the requested extensions."""
    ext = Path(filename).suffix.lower()
    if ext in extensions:
        return True
    mime_map = {
        "application/pdf": ".pdf",
        "application/vnd.ms-powerpoint": ".ppt",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
        "application/msword": ".doc",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    }
    return mime_map.get(mime, "") in extensions


# ── CLI ───────────────────────────────────────────────────────────────────────


def parse_args():
    parser = argparse.ArgumentParser(
        description="Download files from any Blackboard Learn instance.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 bb_downloader.py
  python3 bb_downloader.py --url learn.bu.edu --output ~/Desktop/BB --ext .pdf .pptx
  python3 bb_downloader.py --courses CSU33012 "Machine Learning"
        """,
    )
    parser.add_argument(
        "--url",
        default=None,
        help="Blackboard domain, e.g. learn.bu.edu (prompted if not provided)",
    )
    parser.add_argument(
        "--output",
        "-o",
        default=str(Path.home() / "Downloads" / "Blackboard"),
        help="Download destination folder (default: ~/Downloads/Blackboard)",
    )
    parser.add_argument(
        "--ext",
        nargs="+",
        default=[
            ".pdf",
            ".pptx",
            ".docx",
            ".zip",
            ".py",
            ".ipynb",
            ".cpp",
            ".h",
            ".c",
            ".m",
            ".tex",
        ],
        help=(
            "File extensions to download, e.g. --ext .pdf .pptx .docx "
            "(default: .pdf .pptx .docx .zip .py .ipynb .cpp .h .c .m .tex)"
        ),
    )
    parser.add_argument(
        "--courses",
        nargs="+",
        default=None,
        help=(
            "Course names or IDs to download (substring match, case-insensitive). "
            "Skips the interactive course picker. Example: --courses CSU33012 'Machine Learning'"
        ),
    )
    parser.add_argument(
        "--concurrent-downloads",
        type=int,
        default=5,
        help="Maximum number of concurrent downloads (default: 5)",
    )
    parser.add_argument(
        "--concurrent-collection",
        type=int,
        default=5,
        help="Maximum number of concurrent course scanners (default: 5)",
    )
    return parser.parse_args()


def prompt_url():
    print("Enter your Blackboard domain (e.g. learn.bu.edu):")
    raw = input("  > ").strip().rstrip("/")
    if not raw.startswith("http"):
        raw = f"https://{raw}"
    return raw


def main():
    args = parse_args()
    console = Console()

    base_url = args.url
    if base_url:
        if not base_url.startswith("http"):
            base_url = f"https://{base_url}"
        base_url = base_url.rstrip("/")
    else:
        base_url = prompt_url()

    extensions = set(args.ext)
    print(f"\nFile types: {', '.join(sorted(extensions))}")

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output folder: {output_dir}")

    # Launch Playwright browser context
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        try:
            wait_for_login(page, base_url)
            session = get_session_from_browser(context)
        finally:
            browser.close()

    print("\nFetching user info...")
    user_id = get_my_user_id(session, base_url)
    if not user_id:
        print("Could not retrieve user ID. Login may have failed.")
        return

    print("Fetching enrollments...")
    enrollments = get_all_enrollments(session, base_url, user_id)
    if not enrollments:
        print("No enrollments found.")
        return

    term_map = build_term_map(session, base_url, enrollments)
    if not term_map:
        print("No courses found.")
        return

    courses = pick_term(term_map)
    if args.courses:
        courses = filter_courses_by_query(courses, args.courses)
        if not courses:
            print("No courses matched --courses filter.")
            return
    else:
        courses = pick_courses(courses)

    # ── Phase 1: Concurrent Collection of Links ───────────────────────────────────

    num_scanners = min(args.concurrent_collection, len(courses))
    print(f"\nGathering file links for {len(courses)} course(s)\n")
    download_tasks = []
    total_collection_start = time.time()

    course_stats = []

    # Overall Progress
    overall_progress = Progress(
        TextColumn("[bold green]{task.description}"),
        BarColumn(bar_width=None),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%", justify="left"),
        TextColumn(
            "({task.completed}/{task.total} courses)",
            justify="right",
            table_column=Column(width=15),
        ),
    )

    # Worker Progress
    collection_progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(bar_width=None),
    )

    progress_group = Group(overall_progress, collection_progress)

    try:
        with Live(progress_group):
            master_task = overall_progress.add_task(
                "Overall Collection", total=len(courses)
            )

            worker_slots = Queue()
            for _ in range(num_scanners):
                task_id = collection_progress.add_task("", visible=False)
                worker_slots.put(task_id)

            def collect_course_worker(course):
                slot_id = worker_slots.get()
                display_name = course["name"]
                if len(display_name) > 64:
                    display_name = display_name[:59] + "..."

                collection_progress.update(
                    slot_id,
                    description=f"[cyan]{display_name}",
                    visible=True,
                )

                course_start = time.time()
                call_counter = [0]
                tasks_found = []

                try:
                    course_dir = output_dir / course["safe_name"]
                    call_counter[0] += 1
                    sections = get_top_level_sections(session, base_url, course["id"])

                    if sections:
                        for section in sections:
                            section_dir = course_dir / section["name"]
                            files = collect_files_recursive(
                                session,
                                base_url,
                                course["id"],
                                section["id"],
                                extensions,
                                call_counter=call_counter,
                            )
                            for f in files:
                                subdirs = [_safe(p) for p in f.get("rel_path", []) if p]
                                dest = section_dir.joinpath(
                                    *subdirs, _safe(f["filename"])
                                )
                                tasks_found.append(
                                    {
                                        "type": f.get("type", "file"),
                                        "url": f.get("url"),
                                        "content": f.get("content"),
                                        "dest": dest,
                                        "course": course["name"],
                                        "section": section["name"],
                                    }
                                )

                    duration = time.time() - course_start
                    calls = call_counter[0]
                    avg_load = (duration / calls) if calls > 0 else 0

                    stat = {
                        "name": course["name"],
                        "files": len(tasks_found),
                        "calls": calls,
                        "time": duration,
                        "avg_load": avg_load,
                    }

                    return tasks_found, stat

                finally:
                    collection_progress.update(slot_id, visible=False)
                    worker_slots.put(slot_id)
                    overall_progress.advance(master_task)

            with ThreadPoolExecutor(max_workers=num_scanners) as executor:
                futures = {
                    executor.submit(collect_course_worker, c): c for c in courses
                }
                for future in as_completed(futures):
                    tasks, stat = future.result()
                    download_tasks.extend(tasks)
                    course_stats.append(stat)

    except KeyboardInterrupt:
        print("\nCollection interrupted. Proceeding with what was found.")

    total_collection_time = time.time() - total_collection_start

    if course_stats:
        max_name_len = max((len(s["name"]) for s in course_stats), default=11)
        header = f"{'Course Name':<{max_name_len}} | {'Files Discovered':>16} | {'Links Visited':>13} | {'Time':>8} | {'Avg Load':>9}"
        print()
        console.print(header, style="bold", highlight=False)
        console.print("-" * len(header), highlight=False)
        for s in course_stats:
            row = f"{s['name']:<{max_name_len}} | {s['files']:>16} | {s['calls']:>13} | {s['time']:>7.1f}s | {s['avg_load']:>8.2f}s"
            console.print(row, highlight=False)

    # ── Phase 2: Confirmation & Verification ──────────────────────────────────

    new_files = []
    existing_files = []
    summary = defaultdict(lambda: defaultdict(lambda: {"new": 0, "existing": 0}))

    for task in download_tasks:
        if task["dest"].exists():
            existing_files.append(task)
            summary[task["course"]][task["section"]]["existing"] += 1
        else:
            new_files.append(task)
            summary[task["course"]][task["section"]]["new"] += 1

    if not download_tasks:
        print("\nNo files matching the extensions were found.")
        return

    print(
        f"\nTotal: {len(new_files)} new items discovered, {len(existing_files)} items have been downloaded previously."
    )

    print("\nOptions:")
    print("  [1] Download only NEW items")
    print("  [2] Download all items, and OVERWRITE existing files")
    print("  [3] Cancel")

    tasks_to_run = []
    overwrite = False

    while True:
        choice = input("\nSelect an option: ").strip()
        if choice == "1":
            tasks_to_run = new_files
            break
        elif choice == "2":
            tasks_to_run = new_files + existing_files
            overwrite = True
            break
        elif choice == "3":
            print("Cancelled.")
            return
        print("Invalid choice, try again.")

    if not tasks_to_run:
        print("No items to download. Exiting.")
        return

    # ── Phase 3: Concurrent Downloads ─────────────────────────

    print(
        f"\nStarting {len(tasks_to_run)} task(s) using {args.concurrent_downloads} workers...\n"
    )
    download_start = time.time()
    success_count = 0
    failure_count = 0

    overall_progress = Progress(
        TextColumn(
            "[bold green]{task.description}",
            justify="right",
            table_column=Column(width=30),
        ),
        BarColumn(bar_width=None),
        TextColumn("[progress.percentage]{task.percentage:>3.1f}%"),
        MofNCompleteColumn(table_column=Column(width=11, justify="right")),
        TextColumn("[dim]Items", justify="left"),
        TimeRemainingColumn(table_column=Column(width=8, justify="right")),
    )

    dl_progress = Progress(
        TextColumn(
            "[bold blue]{task.description}",
            justify="right",
            table_column=Column(width=30),
        ),
        BarColumn(bar_width=None),
        TextColumn("[progress.percentage]{task.percentage:>3.1f}%"),
        DownloadColumn(table_column=Column(width=19, justify="right")),
        TransferSpeedColumn(table_column=Column(width=12, justify="right")),
        TimeRemainingColumn(table_column=Column(width=8, justify="right")),
    )

    progress_group = Group(overall_progress, dl_progress)

    try:
        with Live(progress_group, console=console):
            master_task = overall_progress.add_task(
                "Overall Progress", total=len(tasks_to_run)
            )

            worker_slots = Queue()
            for _ in range(args.concurrent_downloads):
                task_id = dl_progress.add_task("", visible=False)
                worker_slots.put(task_id)

            def download_worker(task):
                dest_path = task["dest"]
                task_type = task.get("type", "file")

                if dest_path.exists() and not overwrite:
                    overall_progress.advance(master_task)
                    return False

                slot_id = worker_slots.get()
                display_name = dest_path.name
                if len(display_name) > 27:
                    display_name = display_name[:24] + "..."

                dl_progress.update(
                    slot_id,
                    description=f"[cyan]{display_name}",
                    visible=True,
                    completed=0,
                    total=None,
                )

                success = False
                try:
                    if task_type == "text":
                        dest_path.parent.mkdir(parents=True, exist_ok=True)
                        with open(dest_path, "w", encoding="utf-8") as f:
                            f.write(task["content"])
                        dl_progress.update(slot_id, completed=1, total=1)
                        success = True
                    else:
                        url = task["url"]
                        resp = session.get(url, stream=True, allow_redirects=True)
                        if resp.status_code == 200:
                            cd = resp.headers.get("Content-Disposition", "")
                            if cd and "filename=" in cd:
                                m = re.search(
                                    r'filename[^;=\n]*=([\'"]?)([^\'";\n]+)\1', cd
                                )
                                if m:
                                    real_name = m.group(2).strip()
                                    if Path(real_name).suffix:
                                        dest_path = dest_path.parent / _safe(real_name)

                            dest_path.parent.mkdir(parents=True, exist_ok=True)
                            total_size = (
                                int(resp.headers.get("Content-Length", 0)) or None
                            )
                            dl_progress.update(slot_id, total=total_size)

                            with open(dest_path, "wb") as f:
                                for chunk in resp.iter_content(chunk_size=8192):
                                    if chunk:
                                        f.write(chunk)
                                        dl_progress.advance(slot_id, len(chunk))
                            success = True
                        else:
                            console.print(
                                f"[red]Failed ({resp.status_code}): {display_name}[/red]",
                                highlight=False,
                            )

                except (requests.RequestException, OSError) as e:
                    console.print(
                        f"[red]Error processing {display_name}: {e}[/red]",
                        highlight=False,
                    )

                finally:
                    dl_progress.update(slot_id, visible=False)
                    worker_slots.put(slot_id)
                    overall_progress.advance(master_task)

                return success

            with ThreadPoolExecutor(max_workers=args.concurrent_downloads) as executor:
                futures = {executor.submit(download_worker, t): t for t in tasks_to_run}
                for future in as_completed(futures):
                    if future.result():
                        success_count += 1
                    else:
                        failure_count += 1

    except KeyboardInterrupt:
        print("\nDownloads interrupted.")

    download_time = time.time() - download_start
    avg_dl_time = (download_time / len(tasks_to_run)) if tasks_to_run else 0

    print("\n=== Final Run Statistics ===")
    print(f"Collection Time:      {total_collection_time:.1f}s")
    print(f"Download Time:        {download_time:.1f}s")
    print(f"Avg Time/Item:        {avg_dl_time:.2f}s")
    print(f"Successful Downloads: {success_count}")
    if failure_count > 0:
        print(f"Failed/Skipped:       {failure_count}")


if __name__ == "__main__":
    main()
