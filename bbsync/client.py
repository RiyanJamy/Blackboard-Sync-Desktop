"""Thin wrapper over Blackboard's REST API, authenticated by browser cookies."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from playwright.sync_api import BrowserContext, TimeoutError as PlaywrightTimeout, Error as PlaywrightError

log = logging.getLogger("bbsync")

# polite delay between requests, seconds
_REQUEST_DELAY = 1.0


class BBError(Exception):
    pass


class RateLimited(BBError):
    pass


class AuthExpired(BBError):
    pass


class Forbidden(BBError):
    pass


class BBClient:
    def __init__(self, ctx: BrowserContext, base_url: str):
        self._req = ctx.request
        self.base_url = base_url
        # Flipped to the private SPA API if this deployment rejects cookie
        # auth on the public endpoints.
        self._public = True

    def _path(self, path: str) -> str:
        if not self._public:
            return path.replace("/learn/api/public/v1/", "/learn/api/v1/")
        return path

    def _get_with_retry(self, url):
        for attempt in range(2):
            time.sleep(_REQUEST_DELAY)
            try:
                return self._req.get(url, timeout=120000)
            except PlaywrightTimeout:
                if attempt:
                    raise BBError("Request timed out twice after 120 seconds each. Retry Sync Now later.") from None
                log.warning("request timed out; retrying once")
                time.sleep(2)
            except PlaywrightError as exc:
                if "EACCES" in str(exc):
                    raise BBError("Connection denied (EACCES). Check network/security settings.") from None
                raise BBError("Network request failed. Try again or use the direct download link.") from None

    def _get(self, path: str):
        resp = self._get_with_retry(self.base_url + self._path(path))
        if resp.status == 401 and self._public and "/learn/api/public/v1/" in path:
            log.info("public API rejected session cookies; falling back to private API")
            self._public = False
            resp = self._get_with_retry(self.base_url + self._path(path))
        if resp.status == 429:
            raise RateLimited("Blackboard rate limit (429): stopping; do not retry repeatedly.")
        if resp.status == 401:
            raise AuthExpired("Blackboard session rejected (401)")
        if resp.status == 403:
            raise Forbidden(f"access denied: {path}")
        if not resp.ok:
            raise BBError(f"GET {path} -> {resp.status}")
        return resp

    def _paged(self, path: str) -> list[dict]:
        results: list[dict] = []
        while path:
            data = self._get(path).json()
            results.extend(data.get("results", []))
            path = (data.get("paging") or {}).get("nextPage")
        return results

    def memberships(self, user_id: str) -> list[dict]:
        return self._paged(f"/learn/api/public/v1/users/{user_id}/courses?expand=course")

    def contents(self, course_id: str) -> list[dict]:
        return self._paged(f"/learn/api/public/v1/courses/{course_id}/contents")

    def children(self, course_id: str, content_id: str) -> list[dict]:
        return self._paged(
            f"/learn/api/public/v1/courses/{course_id}/contents/{content_id}/children"
        )

    def attachments(self, course_id: str, content_id: str) -> list[dict]:
        return self._paged(
            f"/learn/api/public/v1/courses/{course_id}/contents/{content_id}/attachments"
        )

    def download(self, course_id: str, content_id: str, attachment_id: str, dest: Path) -> None:
        log.info("requesting attachment %s in item %s", attachment_id, content_id)
        resp = self._get(
            f"/learn/api/public/v1/courses/{course_id}/contents/{content_id}"
            f"/attachments/{attachment_id}/download"
        )
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        part.write_bytes(resp.body())
        part.replace(dest)


    def attachment_size(self, course_id, item_id, att_id):
        """HEAD follows signed redirects but does not fetch the file body."""
        path = (f"/learn/api/public/v1/courses/{course_id}/contents/{item_id}"
                f"/attachments/{att_id}/download")
        time.sleep(_REQUEST_DELAY)
        resp = self._req.head(self.base_url + self._path(path), timeout=30000)
        if resp.status == 429:
            raise RateLimited("Blackboard rate limit (429): stopping.")
        if not resp.ok:
            return None
        value = resp.headers.get("content-length")
        if resp.headers.get("content-encoding", "identity") != "identity":
            return None
        return int(value) if value and value.isdigit() else None
