"""
Blackboard Learn PDF Downloader
Supports any school running Blackboard Learn.
Usage: python3 bb_downloader.py
"""

import argparse
import json
import re
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from tqdm import tqdm

CONTAINER_HANDLERS = {
    "resource/x-bb-folder",
    "resource/x-bb-lesson",
    "resource/x-bb-courselink",
    "resource/x-bb-blankpage",
}

DEFAULT_EXTENSIONS = {".pdf"}


# ── Auth & Networking ─────────────────────────────────────────────────────────


class TrackedSession:
    """Wraps requests.Session to track the number of API calls made."""

    def __init__(self, session):
        self.session = session
        self.api_calls = 0

    def get(self, *args, **kwargs):
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
            selected = term_map[terms[int(raw) - 1]]
            print("\nCourses in selected term:")
            for c in selected:
                print(f"  - {c['name']}")
            return selected
        print("  Invalid input, try again.")


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


def collect_files_recursive(session, base_url, course_id, item_id, extensions):
    """
    Recursively collect all downloadable files under item_id.
    Returns list of {"url": ..., "filename": ...}
    """
    files = []

    def fetch(node_id):
        detail_resp = session.get(
            f"{base_url}/learn/api/public/v1/courses/{course_id}/contents/{node_id}"
        )
        if detail_resp.status_code == 200:
            body = detail_resp.json().get("body", "")
            files.extend(_extract_from_body(body, base_url, extensions))

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
                    files.append({"url": dl_url, "filename": filename})

        children_resp = session.get(
            f"{base_url}/learn/api/public/v1/courses/{course_id}/contents/{node_id}/children?limit=100"
        )
        if children_resp.status_code == 200:
            for child in children_resp.json().get("results", []):
                fetch(child.get("id"))

    fetch(item_id)
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
                    files.append({"url": url, "filename": filename})
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
            files.append({"url": urljoin(base_url, href), "filename": filename})

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


# ── Download ──────────────────────────────────────────────────────────────────


def download_file(session, url, dest_path: Path, overwrite=False):
    """
    Downloads a file. Returns True if downloaded, False if skipped/failed.
    """
    if dest_path.exists() and not overwrite:
        return False

    resp = session.get(url, stream=True, allow_redirects=True)
    if resp.status_code != 200:
        tqdm.write(f"Failed ({resp.status_code}): {dest_path.name}")
        return False

    cd = resp.headers.get("Content-Disposition", "")
    if cd and "filename=" in cd:
        m = re.search(r'filename[^;=\n]*=([\'"]?)([^\'";\n]+)\1', cd)
        if m:
            real_name = m.group(2).strip()
            if Path(real_name).suffix:
                dest_path = dest_path.parent / _safe(real_name)

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            if chunk:
                f.write(chunk)
    return True


# ── CLI ───────────────────────────────────────────────────────────────────────


def parse_args():
    parser = argparse.ArgumentParser(
        description="Download files from any Blackboard Learn instance.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 bb_downloader.py
  python3 bb_downloader.py --url learn.bu.edu --output ~/Desktop/BB --ext .pdf .pptx
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
            "File extensions to download, e.g. --ext .pdf .pptx .docx"
            "(default: .pdf .pptx .docx .zip .py .ipynb .cpp .h .c. .tex)"
        ),
    )
    parser.add_argument(
        "--concurrent-downloads",
        type=int,
        default=5,
        help="Maximum number of concurrent downloads (default: 5)",
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

    # ── Phase 1: Collect Links ────────────────────────────────────────────────

    print(f"\nGathering file links for {len(courses)} course(s)...\n")
    download_tasks = []
    total_collection_start = time.time()

    try:
        for course in tqdm(courses, desc="Collecting Courses", unit="course"):
            course_start_time = time.time()
            start_calls = session.api_calls

            course_dir = output_dir / course["safe_name"]
            sections = get_top_level_sections(session, base_url, course["id"])

            found_for_course = 0
            if sections:
                for section in sections:
                    section_dir = course_dir / section["name"]
                    files = collect_files_recursive(
                        session, base_url, course["id"], section["id"], extensions
                    )
                    for f in files:
                        dest = section_dir / _safe(f["filename"])
                        download_tasks.append(
                            {
                                "url": f["url"],
                                "dest": dest,
                                "course": course["name"],
                                "section": section["name"],
                            }
                        )
                        found_for_course += 1

            course_time = time.time() - course_start_time
            course_calls = session.api_calls - start_calls
            avg_load = (course_time / course_calls) if course_calls > 0 else 0

            # Print stats directly above the progress bar
            tqdm.write(
                f"  -> [{course['name']}] Found {found_for_course} files. "
                f"Visited {course_calls} links in {course_time:.1f}s "
                f"(Avg load: {avg_load:.2f}s/link)"
            )

    except KeyboardInterrupt:
        print("\nCollection interrupted. Proceeding with what was found.")

    total_collection_time = time.time() - total_collection_start

    # ── Phase 2: Confirmation & Verification ──────────────────────────────────

    new_files = []
    existing_files = []
    # summary structure: dict[course][section] = {"new": 0, "existing": 0}
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

    print("\n\n=== Download Summary ===")
    for course_name, sections in summary.items():
        print(f"\n[{course_name}]")
        for section_name, counts in sections.items():
            new_count = counts["new"]
            ext_count = counts["existing"]
            if new_count > 0 or ext_count > 0:
                print(f"  - {section_name}: {new_count} new, {ext_count} existing")

    print(f"\nTotal: {len(new_files)} new files, {len(existing_files)} existing files.")

    print("\nOptions:")
    print("  [1] Download ONLY new files")
    print("  [2] Download new AND overwrite existing files")
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
        print("No files to download. Exiting.")
        return

    # ── Phase 3: Download ─────────────────────────────────────────────────────

    print(
        f"\nStarting {len(tasks_to_run)} download(s) using {args.concurrent_downloads} workers..."
    )
    download_start = time.time()
    success_count = 0
    failure_count = 0

    try:
        with ThreadPoolExecutor(max_workers=args.concurrent_downloads) as executor:
            futures = {
                executor.submit(
                    download_file, session, task["url"], task["dest"], overwrite
                ): task
                for task in tasks_to_run
            }

            for future in tqdm(
                as_completed(futures),
                total=len(futures),
                desc="Downloading",
                unit="file",
            ):
                if future.result():
                    success_count += 1
                else:
                    failure_count += 1

    except KeyboardInterrupt:
        print("\nDownloads interrupted.")

    download_time = time.time() - download_start
    avg_dl_time = (download_time / len(tasks_to_run)) if tasks_to_run else 0

    # ── Final Report ──────────────────────────────────────────────────────────

    print("\n=== Final Run Statistics ===")
    print(
        f"Collection Time: {total_collection_time:.1f}s (Total API requests: {session.api_calls})"
    )
    print(f"Download Time:   {download_time:.1f}s")
    print(f"Avg DL Time/File:{avg_dl_time:.2f}s")
    print(f"Successfully DL: {success_count}")
    if failure_count > 0:
        print(f"Failed/Skipped:  {failure_count}")


if __name__ == "__main__":
    main()
