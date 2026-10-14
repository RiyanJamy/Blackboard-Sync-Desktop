"""Walk course content trees and mirror files into the destination folder."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .client import BBClient, BBError, Forbidden, RateLimited, AuthExpired
from .config import Config, CourseEntry
from .manifest import Manifest
from .safe_files import FileTooLarge
from .manual import enqueue, clear, adopt_new_arrival

log = logging.getLogger("bbsync")

_FOLDER_TYPES = {"resource/x-bb-folder", "resource/x-bb-lesson"}
_FILE_TYPES = {"resource/x-bb-file", "resource/x-bb-document", "resource/x-bb-assignment", "resource/x-bb-assessment"}
_LINK_TYPES = {"resource/x-bb-externallink", "resource/x-bb-blti-link"}

_VIDEO_PAT = re.compile(
    r"panopto|echo360|mediasite|kaltura|lecture ?capture|recording|youtube|vimeo|stream",
    re.IGNORECASE,
)

_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# names Windows refuses regardless of extension (CON.pdf is as invalid as CON)
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL",
                 *(f"COM{i}" for i in range(1, 10)),
                 *(f"LPT{i}" for i in range(1, 10))}


def sanitize(name: str) -> str:
    s = _ILLEGAL_CHARS.sub("-", name)
    s = re.sub(r"\s+", " ", s).strip().strip(".")
    if s.split(".")[0].upper() in _WIN_RESERVED:
        s = "_" + s
    return s[:150] or "untitled"


@dataclass
class Stats:
    courses: int = 0
    downloaded: int = 0
    updated: int = 0
    skipped: int = 0
    links_added: int = 0
    errors: int = 0
    excluded: int = 0

    def summary(self) -> str:
        return (
            f"{self.courses} courses: {self.downloaded} new, {self.updated} updated, "
            f"{self.skipped} unchanged, {self.excluded} size exclusions, {self.links_added} video links, {self.errors} errors"
        )


def update_course_list(client: BBClient, config: Config, user_id: str) -> None:
    """Add newly discovered enrolments to the config (enabled by default)."""
    for m in client.memberships(user_id):
        course = m.get("course") or {}
        cid = course.get("id") or m.get("courseId")
        if not cid:
            continue
        name = course.get("name") or cid
        if cid not in config.courses:
            config.courses[cid] = CourseEntry(name=name)
            log.info("new course: %s", name)
        else:
            config.courses[cid].name = name
    config.save()


def run_sync(client: BBClient, config: Config, manifest: Manifest, user_id: str) -> Stats:
    stats = Stats()
    update_course_list(client, config, user_id)

    enabled = [(cid, entry) for cid, entry in config.courses.items() if entry.enabled]
    for i, (cid, entry) in enumerate(enabled, 1):
        log.info("[%d/%d] checking %s", i, len(enabled), entry.name)
        try:
            items = client.contents(cid)
        except Forbidden:
            log.warning("no access to %s (unavailable course?) — skipping", entry.name)
            continue
        except (RateLimited, AuthExpired):
            raise
        except BBError as exc:
            log.error("failed to list %s: %s", entry.name, exc)
            stats.errors += 1
            enqueue(manifest, config, cid, title=entry.name, reason=str(exc), kind="course")
            continue
        clear(manifest, cid)
        stats.courses += 1
        if not entry.destination:
            raise ValueError(f"No mapped destination for {entry.name}")
        course_dir = Path(entry.destination)
        _recover_renames(course_dir, manifest)
        _walk(client, cid, entry.name, items, course_dir, course_dir, manifest, stats, set(), entry)
        manifest.save()

    return stats


def _walk(
    client: BBClient,
    course_id: str,
    course_name: str,
    items: list[dict],
    dir_path: Path,
    course_dir: Path,
    manifest: Manifest,
    stats: Stats,
    claimed: set[str],
    policy: CourseEntry,
    inside_selected: bool = False,
    inside_assignment: bool = False,
) -> None:
    for item in items:
        handler = (item.get("contentHandler") or {}).get("id", "")
        title = item.get("title") or "Untitled"

        assignment = inside_assignment or "assignment" in handler.lower() or "assessment" in handler.lower()
        selected = inside_selected or not policy.selected_only or item["id"] in policy.content_ids
        category_allowed = policy.include_assignments if assignment else policy.include_content
        if assignment and not policy.include_assignments:
            continue
        if handler in _FILE_TYPES and selected and category_allowed:
            _download_attachments(client, course_id, item, title, dir_path, manifest, stats, claimed)
        elif handler in _LINK_TYPES and selected and category_allowed:
            _record_link(client.base_url, course_id, course_name, item, title,
                         dir_path, course_dir, manifest, stats)

        if item.get("hasChildren"):
            try:
                children = client.children(course_id, item["id"])
            except (RateLimited, AuthExpired):
                raise
            except (Forbidden, BBError) as exc:
                log.warning("cannot open '%s' in %s: %s", title, course_name, exc)
                stats.errors += 1
                enqueue(manifest, Config.load(), course_id, item["id"], title=title,
                        path=dir_path / sanitize(title), reason=str(exc), kind="folder")
                continue
            clear(manifest, course_id, item["id"])
            sub_dir = dir_path if handler in _FILE_TYPES else dir_path / sanitize(title)
            _walk(client, course_id, course_name, children, sub_dir, course_dir,
                  manifest, stats, claimed, policy, selected, assignment)


def _cached_attachment_ok(manifest: Manifest, att_id: str, claimed: set[str], root: Path) -> bool:
    state = manifest.attachment(att_id)
    if state and state.get("skipped"):
        return True
    if not state or not Path(state["path"]).exists() or not Path(state["path"]).is_relative_to(root):
        return False
    claimed.add(state["path"])
    return True


def _download_attachments(
    client: BBClient,
    course_id: str,
    item: dict,
    title: str,
    dir_path: Path,
    manifest: Manifest,
    stats: Stats,
    claimed: set[str],
) -> None:
    item_id = item["id"]
    modified = item.get("modified")
    entry = Config.load().courses[course_id]
    policy_key = [entry.max_file_mb, entry.skip_unknown_size, entry.destination]

    cached = manifest.item(item_id)
    # Recognize a newly placed manual download before considering cached exclusions.
    for aid in (cached or {}).get("attachments", []):
        if adopt_new_arrival(manifest, Config.load(), course_id, item_id, aid, modified):
            log.info("recognized new local download by filename: %s (content not verified)", manifest.attachment(aid)["path"])
    if cached and any(r.get("item_id")==item_id and r.get("kind")=="listing"
                      for r in manifest._data.setdefault("manual",{}).values()):
        cached = None
    if modified is not None and cached and cached.get("modified") == modified and cached.get("policy") == policy_key:
        cached_ids = cached.get("attachments", [])
        if all(manifest.attachment(aid) and manifest.attachment(aid).get("modified") == modified
               and _cached_attachment_ok(manifest, aid, claimed, _course_root(dir_path)) for aid in cached_ids):
            stats.excluded += sum(bool(manifest.attachment(aid).get("skipped")) for aid in cached_ids)
            stats.skipped += sum(not manifest.attachment(aid).get("skipped") for aid in cached_ids)
            return

    try:
        attachments = client.attachments(course_id, item_id)
    except (RateLimited, AuthExpired):
        raise
    except (Forbidden, BBError) as exc:
        log.warning("cannot list attachments for '%s': %s", title, exc)
        stats.errors += 1
        enqueue(manifest, Config.load(), course_id, item_id, title=title,
                path=dir_path, modified=modified, reason=str(exc), kind="listing")
        return
    clear(manifest, course_id, item_id)

    seen_ids = []
    for att in attachments:
        att_id = str(att.get("id"))
        seen_ids.append(att_id)
        fname = sanitize(att.get("fileName") or att_id)
        if adopt_new_arrival(manifest, Config.load(), course_id, item_id, att_id, modified):
            log.info("recognized new local download by filename: %s (content not verified)", manifest.attachment(att_id)["path"])
        state = manifest.attachment(att_id)

        target = dir_path / fname
        if state:
            previous = Path(state["path"])
            root = _course_root(dir_path)
            if previous.exists() and previous.is_relative_to(root):
                target = previous
        if str(target) in claimed and (state is None or state.get("path") != str(target)):
            target = dir_path / f"{sanitize(title)} - {fname}"
            if str(target) in claimed:
                target = dir_path / f"{sanitize(title)} - {sanitize(att_id)} - {fname}"
        claimed.add(str(target))

        if (
            state
            and not state.get("skipped")
            and modified is not None
            and state.get("modified") == modified
            and Path(state["path"]).exists()
            and Path(state["path"]).is_relative_to(_course_root(dir_path))
        ):
            stats.skipped += 1
            continue

        if entry.max_file_mb:
            size = _attachment_size(client, course_id, item_id, att)
            reason = ("Larger than limit" if size is not None and size > entry.max_file_mb * 1024 * 1024
                      else "Size unavailable" if size is None and entry.skip_unknown_size else None)
            if reason:
                stats.excluded += 1
                manifest._data["attachments"][att_id] = {
                    "path": str(target), "modified": modified, "skipped": reason,
                    "size": size, "course_id": course_id, "item_id": item_id,
                    "course_name": Config.load().courses[course_id].name,
                    "title": title, "filename": fname,
                }
                enqueue(manifest, Config.load(), course_id, item_id, att_id, title, fname,
                        target, modified, reason, size=size)
                log.info("manual download available: %s (%s)", fname, reason)
                continue

        try:
            log.info("downloading %s — %s", title, fname)
            from .safe_files import safe_download
            safe_download(client, course_id, item["id"], att_id, target, entry.max_file_mb * 1024 * 1024)
        except (RateLimited, AuthExpired):
            raise
        except FileTooLarge as exc:
            stats.excluded += 1
            manifest._data["attachments"][att_id] = {
                "path": str(target), "modified": modified,
                "skipped": "Actual size exceeds limit", "size": exc.size,
                "course_id": course_id, "item_id": item_id,
                "course_name": entry.name, "title": title, "filename": fname,
            }
            enqueue(manifest, Config.load(), course_id, item_id, att_id, title, fname,
                    target, modified, "Actual size exceeds limit", size=exc.size)
            log.info("downloaded bytes exceeded limit; existing file preserved: %s", fname)
            continue
        except (BBError, OSError, RuntimeError) as exc:
            log.error("download failed for %s: %s", fname, exc)
            stats.errors += 1
            enqueue(manifest, Config.load(), course_id, item_id, att_id, title, fname,
                    target, modified, str(exc))
            continue

        if state:
            stats.updated += 1
            log.info("updated  %s", target)
        else:
            stats.downloaded += 1
            log.info("download %s", target)
        manifest.record_attachment(att_id, str(target), modified)
        manifest.save()

    manifest.record_item(item_id, modified, seen_ids)
    manifest._data["items"][item_id]["policy"] = policy_key


def _record_link(
    base_url: str,
    course_id: str,
    course_name: str,
    item: dict,
    title: str,
    dir_path: Path,
    course_dir: Path,
    manifest: Manifest,
    stats: Stats,
) -> None:
    url = (item.get("contentHandler") or {}).get("url") or (
        f"{base_url}/webapps/blackboard/execute/blti/launchLink"
        f"?course_id={course_id}&content_id={item['id']}"
    )
    if not _VIDEO_PAT.search(f"{title} {url}"):
        return
    key = f"{course_id}:{item['id']}"
    if manifest.has_link(key):
        return

    videos_md = course_dir / "videos.md"
    course_dir.mkdir(parents=True, exist_ok=True)
    if not videos_md.exists():
        videos_md.write_text(f"# Videos & recordings — {course_name}\n\n")
    location = str(dir_path.relative_to(course_dir)) if dir_path != course_dir else ""
    suffix = f" (in {location})" if location and location != "." else ""
    with videos_md.open("a") as f:
        f.write(f"- [{title}]({url}){suffix} — found {date.today().isoformat()}\n")
    manifest.record_link(key, url, title)
    stats.links_added += 1
    log.info("video link: %s (%s)", title, course_name)


def _course_root(path):
    roots = [Path(e.destination) for e in Config.load().courses.values() if e.destination]
    return next((r for r in roots if path.is_relative_to(r)), path)


def _recover_renames(root, manifest):
    """Recover only a unique byte-identical file within the mapped course folder."""
    from .safe_files import digest
    missing = [state for state in manifest._data["attachments"].values()
               if Path(state["path"]).is_relative_to(root)
               and not Path(state["path"]).exists() and state.get("sha256")]
    if not missing:
        return
    candidates = {}
    tracked = {str(Path(s["path"]).resolve()) for s in manifest._data["attachments"].values()
               if Path(s["path"]).exists()}
    for f in root.rglob("*"):
        if (not f.is_file() or "_bbsync_backups" in f.parts
                or f.name.endswith((".pending", ".part")) or str(f.resolve()) in tracked):
            continue
        try:
            candidates.setdefault(digest(f), []).append(f)
        except OSError:
            pass
    for state in missing:
        matches = candidates.get(state["sha256"], [])
        if len(matches) == 1:
            state["path"] = str(matches.pop())
            log.info("recognized renamed file: %s", state["path"])
    manifest.save()


def _attachment_size(client, course_id, item_id, attachment):
    value = attachment.get("size")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return int(value)
    try:
        return client.attachment_size(course_id, item_id, str(attachment["id"]))
    except (RateLimited, AuthExpired):
        raise
    except Exception:
        # Missing/unsupported HEAD or connection failure becomes unknown size.
        # Conservative policy skips it instead of starting a potentially large transfer.
        return None
