"""Record of everything already downloaded, so syncs are idempotent."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .config import BBSYNC_DIR, MANIFEST_PATH


class Manifest:
    def __init__(self, data: dict):
        self._data = data

    @classmethod
    def load(cls) -> "Manifest":
        if MANIFEST_PATH.exists():
            data = json.loads(MANIFEST_PATH.read_text())
        else:
            data = {}
        data.setdefault("attachments", {})
        data.setdefault("items", {})
        data.setdefault("links", {})
        data.setdefault("manual", {})
        return cls(data)

    def save(self) -> None:
        BBSYNC_DIR.mkdir(parents=True, exist_ok=True)
        temp = MANIFEST_PATH.with_suffix(".tmp")
        temp.write_text(json.dumps(self._data, indent=2))
        temp.replace(MANIFEST_PATH)

    # -- attachments ----------------------------------------------------

    def attachment(self, att_id: str) -> dict | None:
        return self._data["attachments"].get(att_id)

    def record_attachment(self, att_id: str, path: str, content_modified: str | None) -> None:
        self._data["attachments"][att_id] = {
            "path": path,
            "sha256": __import__("bbsync.safe_files", fromlist=["digest"]).digest(path),
            "modified": content_modified,
            "downloaded": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

        # Successfully downloaded/adopted attachments clear their file failures.
        for k, record in list(self._data.setdefault("manual", {}).items()):
            if str(record.get("attachment_id")) == str(att_id):
                self._data["manual"].pop(k, None)

    # -- content items ----------------------------------------------------
    # Lets a sync skip the attachments() API call entirely for items whose
    # `modified` timestamp hasn't changed since we last listed them.

    def item(self, item_id: str) -> dict | None:
        return self._data["items"].get(item_id)

    def record_item(self, item_id: str, modified: str | None, attachment_ids: list[str]) -> None:
        self._data["items"][item_id] = {"modified": modified, "attachments": attachment_ids}

    # -- video/external links -------------------------------------------

    def has_link(self, key: str) -> bool:
        return key in self._data["links"]

    def record_link(self, key: str, url: str, title: str) -> None:
        self._data["links"][key] = {"url": url, "title": title}

    # -- misc -------------------------------------------------------------

    @property
    def last_sync(self) -> str | None:
        return self._data.get("last_sync")

    def mark_synced(self) -> None:
        self._data["last_sync"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
