#!/usr/bin/env python3
"""Download Instagram profile videos, skipping ones already kept."""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from http.cookiejar import Cookie, MozillaCookieJar
from pathlib import Path
from typing import BinaryIO, TypeVar, Union

LOG = logging.getLogger("igdown")
T = TypeVar("T")

IG_WEB_APP_ID = "936619743392459"
IG_ORIGIN = "https://www.instagram.com"
CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
SHORTCODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
SHORTCODE_RE = re.compile(r"^[A-Za-z0-9_-]{8,15}$")
STEM_PREFIX_RE = re.compile(r"^(?:\d{4}-\d{2}-\d{2}|undated)_(.+)$")
MEDIA_URL_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/(?:[^/?#]+/)?(?:p|tv|reels?)/([^/?#&]+)/?",
    re.IGNORECASE,
)
PROFILE_URL_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/([^/?#]+)/?",
    re.IGNORECASE,
)
RESERVED_PATHS = {
    "p",
    "tv",
    "reel",
    "reels",
    "stories",
    "explore",
    "accounts",
    "share",
    "legal",
    "about",
    "developer",
    "directory",
    "emails",
    "web",
    "api",
}
USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
VIDEO_EXTS = {".mp4", ".m4v", ".webm", ".mkv", ".mov"}
RETRYABLE_HTTP = frozenset({429, 500, 502, 503, 504})
CDN_HOST_RE = re.compile(r"^(?:[a-z0-9-]+\.)*(?:cdninstagram\.com|fbcdn\.net)$")


class InstagramError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.retry_after = retry_after


@dataclass(frozen=True)
class DirectMedia:
    url: str
    video_id: str | None


@dataclass(frozen=True)
class Profile:
    username: str


Entry = Union[DirectMedia, Profile]  # noqa: UP007 (runtime alias; X | Y needs py3.10 at runtime)


def is_safe_username(name: str) -> bool:
    """Usernames become folder names; reject path-ish or all-dot values."""
    return bool(USERNAME_RE.fullmatch(name)) and bool(re.search(r"[A-Za-z0-9]", name))


def is_safe_media_url(url: str) -> bool:
    """Media URLs come from API JSON; only Instagram CDN HTTPS is allowed."""
    parsed = urllib.parse.urlparse(url)
    return parsed.scheme == "https" and bool(CDN_HOST_RE.fullmatch(parsed.hostname or ""))


def jittered(seconds: float) -> float:
    """±25% jitter so a fixed request cadence looks less bot-like."""
    return seconds * random.uniform(0.75, 1.25)


def looks_like_login_page(final_url: str, body: str) -> bool:
    """gallery-dl aborts on /accounts/login and /challenge redirects; so do we."""
    head = body[:200].lstrip().lower()
    return (
        "/accounts/login" in final_url
        or "/challenge" in final_url
        or head.startswith(("<!doctype", "<html"))
    )


@dataclass(frozen=True)
class ListedVideo:
    video_id: str
    url: str
    username: str
    kind: str
    taken_at: int | None
    pinned: bool
    file_urls: tuple[str, ...]


@dataclass
class RunStats:
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    listed: int = 0

    def record(self, result: str) -> None:
        if result == "downloaded":
            self.downloaded += 1
        elif result == "skipped":
            self.skipped += 1
        elif result == "failed":
            self.failed += 1
        elif result == "listed":
            self.listed += 1

    def max_reached(self, limit: int) -> bool:
        return limit > 0 and self.downloaded >= limit

    def log_summary(self) -> None:
        LOG.info(
            "Done downloaded=%s skipped=%s failed=%s",
            self.downloaded,
            self.skipped,
            self.failed,
        )
        if self.listed:
            LOG.info("Dry-run listed=%s", self.listed)

    def exit_code(self) -> int:
        return 1 if self.failed else 0


def parse_line(raw: str) -> Entry | None:
    line = raw.strip()
    if not line or line.startswith("#"):
        return None

    media = MEDIA_URL_RE.search(line)
    if media:
        return DirectMedia(url=line.split()[0], video_id=media.group(1))

    profile = PROFILE_URL_RE.match(line)
    if profile:
        username = profile.group(1).rstrip("/")
        if username.lower() in RESERVED_PATHS:
            raise InstagramError("invalid_entry", f"Not a profile or media URL: {line}")
        if not is_safe_username(username):
            raise InstagramError("invalid_entry", f"Unsafe username in URL: {line}")
        return Profile(username=username)

    name = line.lstrip("@")
    if is_safe_username(name):
        return Profile(username=name)
    raise InstagramError("invalid_entry", f"Not a profile or media URL: {line}")


def parse_profile_file(path: Path) -> list[Entry]:
    if not path.is_file():
        raise InstagramError("profiles_not_found", f"Profiles file not found: {path}")

    entries: list[Entry] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            entry = parse_line(raw)
        except InstagramError as exc:
            LOG.warning("Skipping line %s: %s", lineno, exc)
            continue
        if entry is not None:
            entries.append(entry)
    if not entries:
        raise InstagramError("empty_profiles", f"No valid profiles or URLs in {path}")
    return entries


def video_id_from_filename(path: Path) -> str | None:
    if path.suffix.lower() not in VIDEO_EXTS:
        return None
    if path.name.endswith(".part"):
        return None
    bracket = re.search(r"\[([A-Za-z0-9_-]{8,15})\]", path.stem)
    if bracket:
        return bracket.group(1)
    stem = path.stem.split("~", 1)[0]
    prefixed = STEM_PREFIX_RE.match(stem)
    if prefixed:
        stem = prefixed.group(1)
    if SHORTCODE_RE.fullmatch(stem):
        return stem
    return None


def parse_retry_after(raw: str | None) -> float | None:
    # Ceiling: only the integer-seconds form is honored; HTTP-date responses are
    # treated as absent and fall back to exponential backoff. Upgrade path:
    # email.utils.parsedate_to_datetime, if Instagram ever sends dates here.
    if not raw:
        return None
    try:
        return max(0.0, float(int(raw.strip())))
    except ValueError:
        return None


def is_retryable(exc: InstagramError) -> bool:
    if exc.code == "instagram_network":
        return True
    return exc.http_status in RETRYABLE_HTTP


def backoff_seconds(
    attempt: int,
    request_sleep: float,
    retry_after: float | None,
) -> float:
    if retry_after is not None:
        return min(120.0, retry_after)
    return min(60.0, max(request_sleep, 1.0) * (2 ** attempt))


def call_with_retry(
    operation: Callable[[], T],
    *,
    retries: int,
    request_sleep: float,
    what: str,
) -> T:
    last_error: InstagramError | None = None
    for attempt in range(retries + 1):
        try:
            return operation()
        except InstagramError as exc:
            last_error = exc
            if not is_retryable(exc) or attempt >= retries:
                raise
            delay = backoff_seconds(attempt, request_sleep, exc.retry_after)
            LOG.warning(
                "Retry %s/%s for %s in %.1fs [%s]",
                attempt + 1,
                retries,
                what,
                delay,
                exc.code,
            )
            time.sleep(delay)
    assert last_error is not None
    raise last_error


def should_stop_after_existing(consecutive_known: int, *, full: bool, threshold: int) -> bool:
    """True when a tab scan should stop (pinned videos never increment the counter)."""
    if full or threshold <= 0:
        return False
    return consecutive_known >= threshold


class SkipStore:
    """Tracks videos already on disk or recorded in the download archive."""

    def __init__(self, archive_path: Path, download_dir: Path) -> None:
        self.archive_path = archive_path
        self.download_dir = download_dir
        self._ids: set[str] = set()

    def load(self) -> None:
        self.archive_path.parent.mkdir(parents=True, exist_ok=True)
        if self.archive_path.is_file():
            for raw in self.archive_path.read_text(encoding="utf-8").splitlines():
                parts = raw.split()
                if len(parts) >= 2 and parts[0].lower() == "instagram":
                    self._ids.add(parts[1])
        self._seed_from_disk()

    def known(self, video_id: str) -> bool:
        return video_id in self._ids

    def mark(self, video_id: str) -> None:
        self._ids.add(video_id)

    def remember(self, video_id: str) -> None:
        if not SHORTCODE_RE.fullmatch(video_id):
            LOG.warning("Refusing to archive malformed id %r", video_id[:60])
            return
        if video_id in self._ids:
            return
        self.mark(video_id)
        with self.archive_path.open("a", encoding="utf-8") as handle:
            handle.write(f"instagram {video_id}\n")

    def _seed_from_disk(self) -> None:
        if not self.download_dir.is_dir():
            return
        for file_path in self.download_dir.rglob("*"):
            if not file_path.is_file():
                continue
            video_id = video_id_from_filename(file_path)
            if video_id:
                self.remember(video_id)


class CursorStore:
    """Persists pagination cursors so interrupted --full scans resume where they stopped."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, str] = {}

    def load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            LOG.warning("Ignoring corrupt cursor file %s", self.path)
            return
        if isinstance(data, dict):
            self._data = {
                str(key): value
                for key, value in data.items()
                if isinstance(value, str) and value
            }

    def get(self, key: str) -> str | None:
        return self._data.get(key)

    def set(self, key: str, value: str | None) -> None:
        if value is None:
            self._data.pop(key, None)
        else:
            self._data[key] = value
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self._data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(self.path)


def restrict_permissions(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        LOG.warning("Could not restrict permissions on %s", path)


def open_part_file(path: Path) -> BinaryIO:
    """Create/truncate a .part file, refusing a symlink at the final component.

    O_TRUNC keeps a leftover .part from an interrupted run from bleeding its tail
    into a shorter new download. O_NOFOLLOW only guards the last path component;
    the parent dirs are program-controlled (validated username / fixed kind).
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    return os.fdopen(os.open(path, flags, 0o600), "wb")


def find_yt_dlp() -> list[str]:
    binary = shutil.which("yt-dlp")
    if binary:
        return [binary]
    module_cmd = [sys.executable, "-m", "yt_dlp"]
    probe = subprocess.run(
        [*module_cmd, "--version"],
        check=False,
        capture_output=True,
        text=True,
    )
    if probe.returncode == 0:
        return module_cmd
    raise InstagramError(
        "yt_dlp_missing",
        "yt-dlp was not found in PATH. Install with: pip install -U yt-dlp",
    )


def export_browser_cookies(yt_dlp: list[str], browser: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        *yt_dlp,
        "--cookies-from-browser",
        browser,
        "--cookies",
        str(dest),
        "--skip-download",
        "--no-warnings",
        "-q",
        f"{IG_ORIGIN}/",
    ]
    LOG.info("Exporting cookies from browser %s", browser)
    result = subprocess.run(cmd, check=False)
    if dest.is_file():
        restrict_permissions(dest)
    if result.returncode != 0 and not dest.is_file():
        raise InstagramError(
            "cookie_export_failed",
            f"Could not export cookies from browser {browser}",
        )


class InstagramClient:
    """Lists profile videos through Instagram web API endpoints used by gallery-dl."""

    def __init__(
        self,
        cookies_path: Path,
        request_sleep: float,
        retries: int = 3,
        *,
        app_id: str = IG_WEB_APP_ID,
        cursors: CursorStore | None = None,
    ) -> None:
        if not cookies_path.is_file():
            raise InstagramError(
                "cookies_required",
                "Instagram profile listing needs a Netscape cookies file "
                "(export with --cookies-from-browser or pass --cookies)",
            )
        self.request_sleep = request_sleep
        self.retries = retries
        self.app_id = app_id
        self.cursors = cursors
        self._www_claim = "0"
        self._requests_made = 0
        self._user_ids: dict[str, str] = {}
        self._jar = MozillaCookieJar(str(cookies_path))
        try:
            self._jar.load(ignore_discard=True, ignore_expires=True)
        except OSError as exc:
            raise InstagramError(
                "cookies_invalid",
                f"Could not read cookies file {cookies_path}: {exc}",
            ) from exc
        if not any(cookie.name == "sessionid" for cookie in self._jar):
            raise InstagramError(
                "cookies_required",
                "cookies.txt has no Instagram sessionid. Log in on the browser and export again.",
            )
        restrict_permissions(cookies_path)
        self._ensure_csrf()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar),
        )

    def _ensure_csrf(self) -> None:
        for cookie in self._jar:
            if cookie.name == "csrftoken":
                return
        self._jar.set_cookie(
            Cookie(
                version=0,
                name="csrftoken",
                value=secrets.token_hex(16),
                port=None,
                port_specified=False,
                domain=".instagram.com",
                domain_specified=True,
                domain_initial_dot=True,
                path="/",
                path_specified=True,
                secure=True,
                expires=None,
                discard=True,
                comment=None,
                comment_url=None,
                rest={},
                rfc2109=False,
            )
        )

    def _csrf(self) -> str:
        for cookie in self._jar:
            if cookie.name == "csrftoken":
                return cookie.value or ""
        return ""

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "*/*",
            "User-Agent": CHROME_UA,
            "X-CSRFToken": self._csrf(),
            "X-IG-App-ID": self.app_id,
            "X-ASBD-ID": "129477",
            "X-IG-WWW-Claim": self._www_claim,
            "X-Requested-With": "XMLHttpRequest",
            "Origin": IG_ORIGIN,
            "Referer": f"{IG_ORIGIN}/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }

    def _request(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        form: dict[str, str] | None = None,
    ) -> dict:
        target = url.split("?", 1)[0]
        return call_with_retry(
            lambda: self._request_once(url, params=params, form=form),
            retries=self.retries,
            request_sleep=self.request_sleep,
            what=target,
        )

    def _request_once(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        form: dict[str, str] | None = None,
    ) -> dict:
        request_url = url
        if params:
            request_url = f"{url}?{urllib.parse.urlencode(params)}"
        data = urllib.parse.urlencode(form).encode() if form is not None else None
        if self.request_sleep > 0 and self._requests_made:
            time.sleep(jittered(self.request_sleep))
        self._requests_made += 1
        req = urllib.request.Request(request_url, data=data, headers=self._headers())
        try:
            with self._opener.open(req, timeout=30) as resp:
                claim = resp.headers.get("x-ig-set-www-claim")
                if claim:
                    self._www_claim = claim
                body = resp.read().decode("utf-8", errors="replace")
                final_url = resp.geturl()
        except urllib.error.HTTPError as exc:
            retry_after = parse_retry_after(exc.headers.get("Retry-After") if exc.headers else None)
            if exc.code in {401, 403}:
                raise InstagramError(
                    "instagram_auth_required",
                    "Instagram asked for login. Refresh cookies and retry.",
                    http_status=exc.code,
                ) from exc
            raise InstagramError(
                "instagram_http",
                f"Instagram HTTP {exc.code}",
                http_status=exc.code,
                retry_after=retry_after,
            ) from exc
        except urllib.error.URLError as exc:
            raise InstagramError("instagram_network", f"Network error: {exc.reason}") from exc

        if looks_like_login_page(final_url, body):
            raise InstagramError(
                "instagram_auth_required",
                "Instagram redirected to a login/challenge page. Refresh cookies and retry.",
            )
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise InstagramError("instagram_api", "Instagram returned a non-JSON response") from exc
        if isinstance(payload, dict) and payload.get("status") == "fail":
            message = str(payload.get("message") or "request failed")
            code = "instagram_auth_required" if "login" in message.lower() else "instagram_api"
            raise InstagramError(code, message)
        return payload

    def user_id(self, username: str) -> str:
        key = username.lower()
        cached = self._user_ids.get(key)
        if cached is not None:
            return cached
        user_id = self._resolve_user_id(username)
        self._user_ids[key] = user_id
        return user_id

    def _resolve_user_id(self, username: str) -> str:
        name = username.lower()
        try:
            data = self._request(
                f"{IG_ORIGIN}/web/search/topsearch/",
                params={"query": username},
            )
            for result in data.get("users") or []:
                user = result.get("user") or {}
                if str(user.get("username", "")).lower() == name:
                    user_id = str(user.get("pk") or user.get("id") or "")
                    if user_id:
                        return user_id
        except InstagramError:
            LOG.debug("topsearch failed for %s", username)

        try:
            data = self._request(
                f"{IG_ORIGIN}/api/v1/users/web_profile_info/",
                params={"username": username},
            )
            user = (data.get("data") or {}).get("user") or {}
            user_id = str(user.get("id") or "")
            if user_id:
                return user_id
        except InstagramError:
            LOG.debug("web_profile_info failed for %s", username)

        try:
            data = self._request(
                f"{IG_ORIGIN}/api/v1/feed/user/{username}/username/",
                params={"count": "1"},
            )
            owner = data.get("user") or {}
            user_id = str(owner.get("pk") or owner.get("id") or "")
            if user_id:
                return user_id
            items = data.get("items") or []
            if items:
                nested = (items[0].get("user") or {})
                user_id = str(nested.get("pk") or nested.get("id") or "")
                if user_id:
                    return user_id
        except InstagramError:
            LOG.debug("feed-by-username failed for %s", username)

        raise InstagramError("user_not_found", f"Could not resolve user id for {username}")

    def iter_reels(self, username: str) -> Iterator[ListedVideo]:
        yield from self._iter_clips(self.user_id(username), username)

    def iter_feed_videos(self, username: str) -> Iterator[ListedVideo]:
        yield from self._iter_feed_videos(self.user_id(username), username)

    def _apply_cursor(self, key: str, target: dict[str, str]) -> None:
        if self.cursors is None:
            return
        saved = self.cursors.get(key)
        if saved:
            target["max_id"] = saved
            LOG.info("Resuming %s from saved cursor", key)

    def _save_cursor(self, key: str, more_available: object, max_id: object) -> None:
        if self.cursors is None:
            return
        if more_available and max_id:
            self.cursors.set(key, str(max_id))
        else:
            self.cursors.set(key, None)

    def _iter_clips(self, user_id: str, username: str) -> Iterator[ListedVideo]:
        key = f"{username.lower()}:reels"
        form = {
            "target_user_id": user_id,
            "page_size": "50",
            "include_feed_video": "true",
        }
        self._apply_cursor(key, form)
        while True:
            data = self._request(f"{IG_ORIGIN}/api/v1/clips/user/", form=form)
            for item in data.get("items") or []:
                video = listed_video_from_media(item.get("media") or {}, username, kind="reels")
                if video:
                    yield video
            info = data.get("paging_info") or {}
            self._save_cursor(key, info.get("more_available"), info.get("max_id"))
            if not info.get("more_available"):
                return
            max_id = info.get("max_id")
            if not max_id:
                return
            form["max_id"] = str(max_id)

    def _iter_feed_videos(self, user_id: str, username: str) -> Iterator[ListedVideo]:
        key = f"{username.lower()}:feed"
        params = {"count": "30"}
        self._apply_cursor(key, params)
        while True:
            data = self._request(
                f"{IG_ORIGIN}/api/v1/feed/user/{user_id}/",
                params=params,
            )
            for item in data.get("items") or []:
                video = listed_video_from_media(item, username, kind="feed")
                if video:
                    yield video
            self._save_cursor(key, data.get("more_available"), data.get("next_max_id"))
            if not data.get("more_available"):
                return
            max_id = data.get("next_max_id")
            if not max_id:
                return
            params["max_id"] = str(max_id)

    def media_by_shortcode(self, shortcode: str) -> dict:
        pk = shortcode_to_pk(shortcode)
        data = self._request(f"{IG_ORIGIN}/api/v1/media/{pk}/info/")
        items = data.get("items") or []
        if not items:
            raise InstagramError("media_not_found", f"No media for {shortcode}")
        return items[0]

    def download_url(self, url: str, dest: Path) -> bool:
        """Return True when the file was fetched now, False when it was already on disk."""
        if not is_safe_media_url(url):
            raise InstagramError("unsafe_media_url", f"Refusing non-CDN URL for {dest.name}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            return False
        call_with_retry(
            lambda: self._download_url_once(url, dest),
            retries=self.retries,
            request_sleep=self.request_sleep,
            what=dest.name,
        )
        return True

    def _download_url_once(self, url: str, dest: Path) -> None:
        tmp = dest.with_name(dest.name + ".part")
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": CHROME_UA,
                "Referer": f"{IG_ORIGIN}/",
                "Accept": "*/*",
            },
        )
        try:
            with self._opener.open(req, timeout=120) as resp, open_part_file(tmp) as handle:
                shutil.copyfileobj(resp, handle)
        except urllib.error.HTTPError as exc:
            if tmp.exists():
                tmp.unlink()
            raise InstagramError(
                "download_failed",
                f"Could not save {dest.name}",
                http_status=exc.code,
                retry_after=parse_retry_after(exc.headers.get("Retry-After") if exc.headers else None),
            ) from exc
        except urllib.error.URLError as exc:
            if tmp.exists():
                tmp.unlink()
            raise InstagramError("instagram_network", f"Network error: {exc.reason}") from exc
        except OSError as exc:
            if tmp.exists():
                tmp.unlink()
            raise InstagramError("download_failed", f"Could not save {dest.name}") from exc
        tmp.replace(dest)


def shortcode_to_pk(shortcode: str) -> str:
    """Convert an Instagram shortcode to the numeric media pk used by /media/ID/info/."""
    if len(shortcode) > 28:
        shortcode = shortcode[:-28]
    value = 0
    try:
        for char in shortcode:
            value = value * 64 + SHORTCODE_ALPHABET.index(char)
    except ValueError as exc:
        raise InstagramError("invalid_entry", f"Invalid shortcode: {shortcode}") from exc
    return str(value)


def video_urls_from_media(media: dict) -> tuple[str, ...]:
    """Pick the highest-resolution MP4 from Instagram video_versions (gallery-dl)."""
    items = media.get("carousel_media") or [media]
    urls: list[str] = []
    for item in items:
        versions = item.get("video_versions") or []
        if not versions:
            continue
        best = max(
            versions,
            key=lambda version: (
                version.get("width") or 0,
                version.get("height") or 0,
                version.get("type") or 0,
            ),
        )
        url = best.get("url")
        if url:
            urls.append(url)
    return tuple(urls)


def media_taken_at(media: dict) -> int | None:
    raw = media.get("taken_at") or media.get("device_timestamp")
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def format_taken_at(taken_at: int | None) -> str:
    if not taken_at:
        return "undated"
    try:
        return datetime.fromtimestamp(taken_at, tz=timezone.utc).strftime("%Y-%m-%d")
    except (OSError, OverflowError, ValueError):
        return "undated"


def kind_from_url(url: str) -> str:
    path = urllib.parse.urlparse(url).path.lower()
    if "/reel" in path:
        return "reels"
    return "feed"


def kind_from_media(media: dict, fallback: str) -> str:
    product = str(media.get("product_type") or "")
    if product == "clips":
        return "reels"
    if product in {"feed", "carousel_container", "igtv"}:
        return "feed"
    return fallback


def listed_video_from_media(
    media: dict,
    username: str,
    *,
    kind: str = "feed",
) -> ListedVideo | None:
    if not media:
        return None
    media_type = media.get("media_type")
    carousel = media.get("carousel_media") or []
    is_video = media_type == 2 or (
        media_type == 8 and any(child.get("media_type") == 2 for child in carousel)
    )
    if not is_video:
        return None
    video_id = str(media.get("code") or media.get("shortcode") or "")
    if not SHORTCODE_RE.fullmatch(video_id):
        LOG.warning("Skipping media with unsafe id %r", video_id[:60])
        return None
    owner = (media.get("user") or {}).get("username") or username
    if not is_safe_username(owner):
        LOG.warning("Skipping %s with unsafe owner %r", video_id, owner[:60])
        return None
    pinned = bool(
        media.get("timeline_pinned_user_ids") or media.get("clips_tab_pinned_user_ids")
    )
    return ListedVideo(
        video_id=video_id,
        url=f"{IG_ORIGIN}/{owner}/reel/{video_id}/",
        username=owner,
        kind=kind_from_media(media, kind),
        taken_at=media_taken_at(media),
        pinned=pinned,
        file_urls=video_urls_from_media(media),
    )


def video_stem(video: ListedVideo, index: int | None = None) -> str:
    suffix = f"~{index}" if index is not None else ""
    return f"{format_taken_at(video.taken_at)}_{video.video_id}{suffix}"


def native_destinations(download_dir: Path, video: ListedVideo) -> list[Path]:
    folder = download_dir / video.username / video.kind
    if len(video.file_urls) <= 1:
        return [folder / f"{video_stem(video)}.mp4"]
    return [
        folder / f"{video_stem(video, index)}.mp4"
        for index in range(1, len(video.file_urls) + 1)
    ]


def download_native(
    client: InstagramClient,
    video: ListedVideo,
    download_dir: Path,
    *,
    write_metadata: bool = False,
) -> bool:
    if not video.file_urls:
        LOG.warning("No CDN URL for %s", video.video_id)
        return False
    LOG.info("Downloading %s", video.url)
    try:
        for url, dest in zip(video.file_urls, native_destinations(download_dir, video), strict=True):
            fetched = client.download_url(url, dest)
            sidecar = dest.parent / (dest.name + ".json")
            if write_metadata and (fetched or not sidecar.exists()):
                write_metadata_file(dest, video, url)
    except InstagramError as exc:
        if exc.code not in {"download_failed", "instagram_network", "unsafe_media_url"}:
            raise
        LOG.error("%s [%s]", exc, exc.code)
        return False
    return True


def write_metadata_file(dest: Path, video: ListedVideo, media_url: str) -> None:
    """Write a .json sidecar next to each downloaded file (instaloader-style)."""
    meta = {
        "id": video.video_id,
        "page_url": video.url,
        "media_url": media_url,
        "username": video.username,
        "kind": video.kind,
        "taken_at": format_taken_at(video.taken_at),
        "taken_at_ts": video.taken_at,
        "pinned": video.pinned,
        "downloaded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    sidecar = dest.parent / (dest.name + ".json")
    sidecar.write_text(json.dumps(meta, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def download_with_yt_dlp(
    yt_dlp: list[str],
    url: str,
    *,
    download_dir: Path,
    archive_path: Path,
    cookies_path: Path | None,
    cookies_from_browser: str | None,
    video: ListedVideo | None = None,
) -> int:
    download_dir.mkdir(parents=True, exist_ok=True)
    cmd = [*yt_dlp]
    if cookies_path is not None and cookies_path.is_file():
        cmd += ["--cookies", str(cookies_path)]
    elif cookies_from_browser:
        cmd += ["--cookies-from-browser", cookies_from_browser]
    if video is not None:
        output = str(
            download_dir / video.username / video.kind / f"{video_stem(video)}.%(ext)s"
        )
    else:
        output = str(download_dir / "%(uploader,channel)s/%(id)s.%(ext)s")
    cmd += [
        "--download-archive",
        str(archive_path),
        "--no-overwrites",
        "--ignore-errors",
        "--no-abort-on-error",
        "--restrict-filenames",
        "--sleep-requests",
        "1",
        "-o",
        output,
        url,
    ]
    LOG.info("Downloading %s", url)
    return subprocess.run(cmd, check=False).returncode


def process_direct(
    entry: DirectMedia,
    skip: SkipStore,
    client: InstagramClient | None,
    yt_dlp: list[str] | None,
    args: argparse.Namespace,
    stats: RunStats,
) -> str:
    if args.max > 0 and stats.max_reached(args.max):
        return "skipped"
    if entry.video_id and skip.known(entry.video_id):
        LOG.info("Skipping existing %s", entry.video_id)
        stats.record("skipped")
        return "skipped"
    try:
        video = listed_from_direct(entry, client)
    except InstagramError as exc:
        LOG.error("%s [%s]", exc, exc.code)
        stats.record("failed")
        return "failed"
    if args.dry_run:
        LOG.info("Would download %s", video.url if video else entry.url)
        stats.record("listed")
        return "listed"
    if download_listed(video, entry.url, skip, client, yt_dlp, args):
        stats.record("downloaded")
        return "downloaded"
    stats.record("failed")
    return "failed"


def listed_from_direct(entry: DirectMedia, client: InstagramClient | None) -> ListedVideo | None:
    if client is None or not entry.video_id:
        return None
    media = client.media_by_shortcode(entry.video_id)
    owner = (media.get("user") or {}).get("username") or "instagram"
    return listed_video_from_media(media, owner, kind=kind_from_url(entry.url))


def process_profile(
    entry: Profile,
    skip: SkipStore,
    client: InstagramClient,
    yt_dlp: list[str] | None,
    args: argparse.Namespace,
    stats: RunStats,
) -> None:
    seen: set[str] = set()
    LOG.info("Listing reels for @%s", entry.username)
    consume_video_stream(
        client.iter_reels(entry.username),
        label="reels",
        username=entry.username,
        seen=seen,
        skip=skip,
        client=client,
        yt_dlp=yt_dlp,
        args=args,
        stats=stats,
    )
    if args.reels_only or stats.max_reached(args.max):
        return
    LOG.info("Listing feed videos for @%s", entry.username)
    consume_video_stream(
        client.iter_feed_videos(entry.username),
        label="feed",
        username=entry.username,
        seen=seen,
        skip=skip,
        client=client,
        yt_dlp=yt_dlp,
        args=args,
        stats=stats,
    )


def consume_video_stream(
    videos: Iterator[ListedVideo],
    *,
    label: str,
    username: str,
    seen: set[str],
    skip: SkipStore,
    client: InstagramClient,
    yt_dlp: list[str] | None,
    args: argparse.Namespace,
    stats: RunStats,
) -> None:
    consecutive_known = 0
    for video in videos:
        if stats.max_reached(args.max):
            LOG.info("Reached --max %s", args.max)
            return
        if video.video_id in seen:
            continue
        seen.add(video.video_id)
        if skip.known(video.video_id):
            LOG.info("Already have %s", video.video_id)
            stats.record("skipped")
            if not video.pinned:
                consecutive_known += 1
            if should_stop_after_existing(
                consecutive_known,
                full=args.full,
                threshold=args.stop_after_existing,
            ):
                LOG.info(
                    "Stopping @%s %s after %s known videos in a row",
                    username,
                    label,
                    args.stop_after_existing,
                )
                return
            continue
        consecutive_known = 0
        if args.dry_run:
            LOG.info("Would download %s", video.url)
            stats.record("listed")
            continue
        if download_listed(video, video.url, skip, client, yt_dlp, args):
            stats.record("downloaded")
        else:
            stats.record("failed")


def download_listed(
    video: ListedVideo | None,
    page_url: str,
    skip: SkipStore,
    client: InstagramClient | None,
    yt_dlp: list[str] | None,
    args: argparse.Namespace,
) -> bool:
    if args.downloader == "native":
        if video is None:
            LOG.warning("No video found for %s", page_url)
            return False
        if client is None:
            raise InstagramError("cookies_required", "Native download needs cookies")
        ok = download_native(client, video, args.out, write_metadata=args.write_metadata)
        if ok:
            skip.remember(video.video_id)
        return ok
    if yt_dlp is None:
        raise InstagramError("yt_dlp_missing", "yt-dlp is required for --downloader yt-dlp")
    code = download_with_yt_dlp(
        yt_dlp,
        page_url,
        download_dir=args.out,
        archive_path=args.archive,
        cookies_path=args.cookies,
        cookies_from_browser=args.cookies_from_browser,
        video=video,
    )
    if code == 0 and video is not None:
        skip.mark(video.video_id)
        return True
    return code == 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Download Instagram videos from profiles listed in a text file. "
            "Skips videos already in the download archive or output folder."
        )
    )
    parser.add_argument(
        "--profiles",
        type=Path,
        default=Path("profiles.txt"),
        help="Text file with usernames, profile URLs, or reel/post URLs",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("downloads"),
        help="Download directory",
    )
    parser.add_argument(
        "--archive",
        type=Path,
        default=Path("data/archive.txt"),
        help="Archive file used to skip known video IDs",
    )
    parser.add_argument(
        "--cookies",
        type=Path,
        default=Path("cookies.txt"),
        help="Netscape cookies.txt (required to list a profile)",
    )
    parser.add_argument(
        "--cookies-from-browser",
        metavar="BROWSER",
        help="Export cookies from a browser (chrome, safari, firefox, brave, edge)",
    )
    parser.add_argument(
        "--reels-only",
        action="store_true",
        help="List only the Reels tab, not feed videos",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Scan the whole profile instead of stopping at already-kept videos",
    )
    parser.add_argument(
        "--stop-after-existing",
        type=int,
        default=3,
        metavar="N",
        help="Stop a profile tab after N consecutive already-kept videos (default: 3)",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=0,
        metavar="N",
        help="Stop after N new downloads this run (0 = unlimited)",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        metavar="N",
        help="Retries for 429/5xx/network errors (default: 3)",
    )
    parser.add_argument(
        "--request-sleep",
        type=float,
        default=1.5,
        help="Seconds to wait between Instagram listing requests (±25%% jitter)",
    )
    parser.add_argument(
        "--ig-app-id",
        default=os.environ.get("IG_APP_ID", IG_WEB_APP_ID),
        help="Instagram web app id sent as X-IG-App-ID (env: IG_APP_ID)",
    )
    parser.add_argument(
        "--cursors",
        type=Path,
        default=Path("data/cursors.json"),
        help="JSON file where --full scans save their pagination position",
    )
    parser.add_argument(
        "--write-metadata",
        action="store_true",
        help="Write a .json sidecar with metadata next to each downloaded file",
    )
    parser.add_argument(
        "--downloader",
        choices=("native", "yt-dlp"),
        default="native",
        help="native downloads Instagram CDN MP4s (default, no yt-dlp). yt-dlp is optional",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List URLs that would be downloaded without writing files",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    try:
        if args.max < 0 or args.retries < 0:
            raise InstagramError("invalid_args", "--max and --retries must be >= 0")
        entries = parse_profile_file(args.profiles)
        yt_dlp = maybe_yt_dlp(args)
        skip = SkipStore(args.archive, args.out)
        skip.load()
        cursors = CursorStore(args.cursors) if args.full else None
        if cursors is not None:
            cursors.load()
        client = maybe_client(args, entries, cursors)
        stats = RunStats()
        for entry in entries:
            if stats.max_reached(args.max):
                LOG.info("Reached --max %s", args.max)
                break
            if isinstance(entry, DirectMedia):
                process_direct(entry, skip, client, yt_dlp, args, stats)
                continue
            if client is None:
                raise InstagramError("cookies_required", "Profile listing needs cookies")
            process_profile(entry, skip, client, yt_dlp, args, stats)
        stats.log_summary()
        return stats.exit_code()
    except InstagramError as exc:
        LOG.error("%s [%s]", exc, exc.code)
        return 1
    except KeyboardInterrupt:
        LOG.warning("Interrupted — progress so far is kept in archive and cursors")
        return 130


def maybe_yt_dlp(args: argparse.Namespace) -> list[str] | None:
    if args.cookies_from_browser:
        try:
            yt_dlp = find_yt_dlp()
        except InstagramError as exc:
            raise InstagramError(
                "cookie_export_failed",
                "Install yt-dlp to use --cookies-from-browser, or pass a Netscape --cookies file",
            ) from exc
        export_browser_cookies(yt_dlp, args.cookies_from_browser, args.cookies)
        if args.downloader == "yt-dlp":
            return yt_dlp
        return None
    if args.downloader == "yt-dlp":
        return find_yt_dlp()
    return None


def maybe_client(
    args: argparse.Namespace,
    entries: list[Entry],
    cursors: CursorStore | None,
) -> InstagramClient | None:
    needs_client = args.downloader == "native" or any(
        isinstance(entry, Profile) for entry in entries
    )
    if not needs_client:
        return None
    return InstagramClient(
        args.cookies,
        args.request_sleep,
        retries=args.retries,
        app_id=args.ig_app_id,
        cursors=cursors,
    )


if __name__ == "__main__":
    sys.exit(main())
