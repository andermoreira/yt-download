#!/usr/bin/env python3
"""Download Instagram profile videos, skipping ones already kept."""

from __future__ import annotations

import argparse
import copy
import gzip
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
from http.cookiejar import Cookie, CookieJar, MozillaCookieJar
from pathlib import Path
from typing import BinaryIO, TypeVar, Union

LOG = logging.getLogger("igdown")
T = TypeVar("T")

IG_WEB_APP_ID = "936619743392459"
# yt-dlp InstagramBaseIE._api_headers (2026.08.19); gallery-dl still sends 129477.
IG_ASBD_ID = "359341"
IG_ORIGIN = "https://www.instagram.com"
# Instagram web GraphQL (instaloader 2026). REST clips/user and feed/user 429; these
# still listed. Meta rotates doc_ids — REST remains the fallback.
GQL_CLIPS_DOC_ID = "27234427476213202"
GQL_CLIPS_CONNECTION = "xdt_api__v1__clips__user__connection_v2"
GQL_FEED_DOC_ID = "34579740524958711"
GQL_FEED_CONNECTION = "xdt_api__v1__feed__user_timeline_graphql_connection"
# PolarisPostRootQuery (instaloader 2026) — CDN URLs when REST /media/{pk}/info/ 429s.
GQL_MEDIA_DOC_ID = "27128499623469141"
GQL_MEDIA_CONNECTION = "xdt_api__v1__media__shortcode__web_info"
# Reduced Chrome UA (major must match the browser that created the cookies).
# Override with --user-agent / IG_USER_AGENT when the local Chrome moves on.
CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/152.0.0.0 Safari/537.36"
)
# yt-dlp's Instagram extractor is broken. Hitting IG_ORIGIN during cookie dump
# can challenge the session before we list anything; YouTube is only bait so
# yt-dlp loads the browser jar and writes --cookies.
COOKIE_DUMP_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
CHROME_UA_RE = re.compile(r"Chrome/(\d+)")
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
# 401/403 are often GraphQL WAF or "please wait" throttle, not a dead session.
RETRYABLE_HTTP = frozenset({401, 403, 429, 500, 502, 503, 504})
CDN_HOST_RE = re.compile(r"^(?:[a-z0-9-]+\.)*(?:cdninstagram\.com|fbcdn\.net)$")
RATE_LIMIT_MARKERS = ("please wait a few minutes", "too many requests")
AUTH_FAIL_MESSAGES = frozenset(
    {"login_required", "checkpoint_required", "challenge_required"}
)


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


def is_instagram_home(url: str) -> bool:
    """True for https://www.instagram.com/ with no extra path (gallery-dl 'home')."""
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in {"www.instagram.com", "instagram.com"}:
        return False
    return (parsed.path or "/") in {"", "/"}


def is_logged_in_html(body: str) -> bool:
    """Instagram's rate-limit/WAF HTML still includes class=\"logged-in\" when sessionid is valid."""
    return "logged-in" in body[:4000].lower()


def decode_http_body(raw: bytes, content_encoding: str | None) -> bytes:
    """urllib does not decode Content-Encoding; Instagram often gzips HTML error pages."""
    encoding = (content_encoding or "").lower()
    if "gzip" in encoding or raw[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(raw)
        except OSError:
            return raw
    return raw


def read_decoded_response(resp: object) -> tuple[str, str]:
    """Return (text, final_url) from an urllib response or HTTPError."""
    raw = resp.read()  # type: ignore[attr-defined]
    headers = getattr(resp, "headers", None)
    encoding = headers.get("Content-Encoding") if headers else None
    text = decode_http_body(raw, encoding).decode("utf-8", errors="replace")
    final_url = resp.geturl() if hasattr(resp, "geturl") else ""
    return text, final_url


class InstagramRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Do not follow login, or API bounces to home (urllib would turn POST /clips/user/ into GET /).

    GET / often 302s to /#; that home→home hop must be followed or the session never warms up.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        path = urllib.parse.urlparse(newurl).path.lower()
        if path.startswith("/accounts/login") or path.startswith("/challenge"):
            return None
        if is_instagram_home(newurl) and not is_instagram_home(req.full_url):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def instagram_sessionid_cookies(jar: CookieJar) -> list[Cookie]:
    return [
        cookie
        for cookie in jar
        if cookie.name == "sessionid"
        and "instagram.com" in (cookie.domain or "")
        and cookie.value
    ]


def restore_instagram_sessionid(jar: CookieJar, saved: list[Cookie]) -> None:
    """Instagram 401 throttle responses Set-Cookie-clear sessionid; keep the exported one."""
    if saved and not instagram_sessionid_cookies(jar):
        for cookie in saved:
            jar.set_cookie(cookie)


def normalize_netscape_session_cookies(jar: CookieJar) -> None:
    """Chrome Netscape dumps use expires=0 for session cookies; Python treats 0 as already expired."""
    for cookie in jar:
        if cookie.expires == 0:
            cookie.expires = None
            cookie.discard = True


class PreserveInstagramSessionProcessor(urllib.request.HTTPCookieProcessor):
    def http_response(self, request, response):  # type: ignore[no-untyped-def]
        saved = [copy.copy(cookie) for cookie in instagram_sessionid_cookies(self.cookiejar)]
        code = response.getcode() if hasattr(response, "getcode") else 0
        if code == 200:
            response = super().http_response(request, response)
        restore_instagram_sessionid(self.cookiejar, saved)
        return response

    https_response = http_response


def looks_like_login_page(final_url: str, body: str = "", *, request_url: str = "") -> bool:
    """True only for login/challenge URLs. Home bounces are rate-limit, not a dead session."""
    del body, request_url
    path = urllib.parse.urlparse(final_url).path.lower()
    return "/accounts/login" in path or "/challenge" in path


def read_http_error_body(exc: urllib.error.HTTPError) -> tuple[str, str]:
    try:
        return read_decoded_response(exc)
    except (OSError, AttributeError, ValueError):
        return "", exc.geturl() if hasattr(exc, "geturl") else ""


def error_from_fail_message(message: str, *, http_status: int | None = None) -> InstagramError:
    """Map Instagram JSON `status: fail` to throttle vs real login vs generic API error."""
    text = message.lower()
    if any(marker in text for marker in RATE_LIMIT_MARKERS):
        return InstagramError("instagram_http", message, http_status=429)
    if text in AUTH_FAIL_MESSAGES:
        return InstagramError(
            "instagram_auth_required",
            "Instagram asked for login. Refresh cookies and retry.",
            http_status=http_status,
        )
    return InstagramError("instagram_api", message, http_status=http_status)


def is_rate_limit(exc: InstagramError) -> bool:
    """True for Instagram throttle (please-wait / 429), not a dead session."""
    if exc.http_status == 429:
        return True
    text = str(exc).lower()
    return any(marker in text for marker in RATE_LIMIT_MARKERS)


def raise_for_instagram_http(exc: urllib.error.HTTPError, *, request_url: str) -> None:
    retry_after = parse_retry_after(exc.headers.get("Retry-After") if exc.headers else None)
    location = exc.headers.get("Location") if exc.headers else None
    dest = urllib.parse.urljoin(request_url, location or "")
    body, final_url = read_http_error_body(exc)
    if final_url:
        dest = dest or final_url
    if looks_like_login_page(dest, body, request_url=request_url):
        raise InstagramError(
            "instagram_auth_required",
            f"Instagram redirected to a login/challenge page ({dest}).",
            http_status=exc.code,
        ) from exc
    try:
        payload = json.loads(body) if body else None
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        message = str(payload.get("message") or "")
        if message:
            raise error_from_fail_message(message, http_status=exc.code) from exc
    if is_instagram_home(dest) and not is_instagram_home(request_url):
        raise InstagramError(
            "instagram_http",
            "Instagram redirected to home (rate-limit)",
            http_status=429,
            retry_after=retry_after,
        ) from exc
    raise InstagramError(
        "instagram_http",
        f"Instagram HTTP {exc.code}",
        http_status=exc.code,
        retry_after=retry_after,
    ) from exc


def chrome_client_hint_headers(user_agent: str) -> dict[str, str]:
    match = CHROME_UA_RE.search(user_agent)
    if not match:
        return {}
    major = match.group(1)
    return {
        "Sec-CH-UA": (
            f'"Google Chrome";v="{major}", "Chromium";v="{major}", "Not.A/Brand";v="99"'
        ),
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": '"macOS"',
    }


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
        return limit > 0 and (self.downloaded + self.listed) >= limit

    def log_summary(self) -> None:
        LOG.info(
            "Done downloaded=%s skipped=%s failed=%s",
            self.downloaded,
            self.skipped,
            self.failed,
        )
        if self.listed:
            LOG.info("listed=%s", self.listed)

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
    if exc.code == "instagram_auth_required":
        return False
    if exc.code == "instagram_network":
        return True
    return exc.http_status in RETRYABLE_HTTP


def backoff_seconds(
    attempt: int,
    request_sleep: float,
    retry_after: float | None,
    *,
    rate_limit_sleep: float = 0.0,
    is_throttle: bool = False,
) -> float:
    if retry_after is not None:
        return min(max(120.0, rate_limit_sleep), retry_after)
    if is_throttle and rate_limit_sleep > 0:
        return rate_limit_sleep
    return min(60.0, max(request_sleep, 1.0) * (2 ** attempt))


def call_with_retry(
    operation: Callable[[], T],
    *,
    retries: int,
    request_sleep: float,
    what: str,
    rate_limit_sleep: float = 0.0,
) -> T:
    last_error: InstagramError | None = None
    for attempt in range(retries + 1):
        try:
            return operation()
        except InstagramError as exc:
            last_error = exc
            if not is_retryable(exc) or attempt >= retries:
                raise
            throttle = is_rate_limit(exc)
            delay = backoff_seconds(
                attempt,
                request_sleep,
                exc.retry_after,
                rate_limit_sleep=rate_limit_sleep,
                is_throttle=throttle,
            )
            if throttle and rate_limit_sleep > 0:
                LOG.warning(
                    "Rate limit hit for %s; waiting %.1fs for cooldown before retry %s/%s [%s]",
                    what,
                    delay,
                    attempt + 1,
                    retries,
                    exc.code,
                )
            else:
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


class FailStore:
    """Shortcodes that failed download; skipped on later --from-queue runs until the file is cleared."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._ids: set[str] = set()

    def load(self) -> None:
        if not self.path.is_file():
            return
        for raw in self.path.read_text(encoding="utf-8").splitlines():
            parts = raw.split()
            if len(parts) >= 2 and parts[0].lower() == "instagram":
                self._ids.add(parts[1])
            elif SHORTCODE_RE.fullmatch(raw.strip()):
                self._ids.add(raw.strip())

    def known(self, video_id: str) -> bool:
        return video_id in self._ids

    def remember(self, video_id: str) -> None:
        if not SHORTCODE_RE.fullmatch(video_id) or video_id in self._ids:
            return
        self._ids.add(video_id)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(f"instagram {video_id}\n")


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


class QueueStore:
    """JSONL queue of discovered videos. CDN URLs are not stored; they expire."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._ids: set[str] = set()

    def load(self) -> None:
        if not self.path.is_file():
            return
        for raw in self.path.read_text(encoding="utf-8").splitlines():
            video = parse_queue_line(raw)
            if video is not None:
                self._ids.add(video.video_id)

    def known(self, video_id: str) -> bool:
        return video_id in self._ids

    def append(self, video: ListedVideo) -> bool:
        if not SHORTCODE_RE.fullmatch(video.video_id) or video.video_id in self._ids:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(queue_record(video), sort_keys=True) + "\n")
        self._ids.add(video.video_id)
        return True


def queue_record(video: ListedVideo) -> dict[str, object]:
    return {
        "id": video.video_id,
        "kind": video.kind,
        "pinned": video.pinned,
        "taken_at": video.taken_at,
        "url": video.url,
        "username": video.username,
    }


def parse_queue_line(raw: str) -> ListedVideo | None:
    line = raw.strip()
    if not line or line.startswith("#"):
        return None
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        try:
            entry = parse_line(line)
        except InstagramError:
            return None
        if isinstance(entry, DirectMedia) and entry.video_id:
            return listed_from_media_url(entry)
        return None
    if not isinstance(data, dict):
        return None
    video_id = str(data.get("id") or "")
    if not SHORTCODE_RE.fullmatch(video_id):
        return None
    username = str(data.get("username") or "instagram")
    if not is_safe_username(username):
        return None
    url = str(data.get("url") or f"{IG_ORIGIN}/{username}/reel/{video_id}/")
    kind = str(data.get("kind") or "")
    if kind not in {"reels", "feed"}:
        kind = kind_from_url(url)
    taken_at = data.get("taken_at")
    taken = None
    if taken_at is not None:
        try:
            taken = int(taken_at)
        except (TypeError, ValueError):
            taken = None
    return ListedVideo(
        video_id=video_id,
        url=url.split()[0],
        username=username,
        kind=kind,
        taken_at=taken if taken and taken > 0 else None,
        pinned=bool(data.get("pinned")),
        file_urls=(),
    )


def load_queue_file(path: Path) -> list[ListedVideo]:
    if not path.is_file():
        raise InstagramError("queue_not_found", f"Queue file not found: {path}")
    videos: list[ListedVideo] = []
    seen: set[str] = set()
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        video = parse_queue_line(raw)
        if video is None:
            if raw.strip() and not raw.strip().startswith("#"):
                LOG.warning("Skipping queue line %s", lineno)
            continue
        if video.video_id in seen:
            continue
        seen.add(video.video_id)
        videos.append(video)
    if not videos:
        raise InstagramError("empty_queue", f"No valid videos in {path}")
    return videos


def listed_from_media_url(entry: DirectMedia) -> ListedVideo | None:
    if not entry.video_id:
        return None
    return ListedVideo(
        video_id=entry.video_id,
        url=entry.url.split()[0],
        username=username_from_media_url(entry.url),
        kind=kind_from_url(entry.url),
        taken_at=None,
        pinned=False,
        file_urls=(),
    )


def username_from_media_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 3 and parts[1].lower() in {"p", "tv", "reel", "reels"}:
        name = parts[0]
        if is_safe_username(name) and name.lower() not in RESERVED_PATHS:
            return name
    return "instagram"


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


def cookie_export_cmd(
    yt_dlp: list[str],
    browser: str,
    dest: Path,
    user_agent: str,
) -> list[str]:
    return [
        *yt_dlp,
        "--cookies-from-browser",
        browser,
        "--cookies",
        str(dest),
        "--user-agent",
        user_agent,
        "--skip-download",
        "--no-warnings",
        "-q",
        COOKIE_DUMP_URL,
    ]


def jar_has_instagram_session(path: Path) -> bool:
    jar = MozillaCookieJar(str(path))
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except OSError:
        return False
    return any(
        cookie.name == "sessionid" and "instagram.com" in (cookie.domain or "")
        for cookie in jar
    )


def export_browser_cookies(
    yt_dlp: list[str],
    browser: str,
    dest: Path,
    user_agent: str = CHROME_UA,
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = cookie_export_cmd(yt_dlp, browser, dest, user_agent)
    LOG.info("Exporting cookies from browser %s", browser)
    result = subprocess.run(cmd, check=False)
    if dest.is_file():
        restrict_permissions(dest)
    if result.returncode != 0 and not dest.is_file():
        raise InstagramError(
            "cookie_export_failed",
            f"Could not export cookies from browser {browser}",
        )
    if not dest.is_file() or not jar_has_instagram_session(dest):
        raise InstagramError(
            "cookies_required",
            "Browser dump has no Instagram sessionid. "
            "Log in on that Chrome profile, close the browser, and retry.",
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
        user_agent: str = CHROME_UA,
        cursors: CursorStore | None = None,
        rate_limit_sleep: float = 300.0,
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
        self.user_agent = user_agent
        self.cursors = cursors
        self.rate_limit_sleep = rate_limit_sleep
        self._www_claim = "0"
        self._requests_made = 0
        self._bootstrapped = False
        self._user_ids: dict[str, str] = {}
        self._jar = MozillaCookieJar(str(cookies_path))
        try:
            self._jar.load(ignore_discard=True, ignore_expires=True)
        except OSError as exc:
            raise InstagramError(
                "cookies_invalid",
                f"Could not read cookies file {cookies_path}: {exc}",
            ) from exc
        normalize_netscape_session_cookies(self._jar)
        if not any(cookie.name == "sessionid" for cookie in self._jar):
            raise InstagramError(
                "cookies_required",
                "cookies.txt has no Instagram sessionid. Log in on the browser and export again.",
            )
        # yt-dlp InstagramPlaylistBaseIE sets this before GraphQL pagination.
        self._jar.set_cookie(
            Cookie(
                version=0,
                name="ig_pr",
                value="1",
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
        restrict_permissions(cookies_path)
        self._ensure_csrf()
        self._opener = urllib.request.build_opener(
            InstagramRedirectHandler(),
            PreserveInstagramSessionProcessor(self._jar),
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
        headers = {
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "User-Agent": self.user_agent,
            "X-CSRFToken": self._csrf(),
            "X-IG-App-ID": self.app_id,
            "X-ASBD-ID": IG_ASBD_ID,
            "X-IG-WWW-Claim": self._www_claim,
            "X-Requested-With": "XMLHttpRequest",
            "Origin": IG_ORIGIN,
            "Referer": f"{IG_ORIGIN}/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
        headers.update(chrome_client_hint_headers(self.user_agent))
        return headers

    def _bootstrap_session(self) -> None:
        # GET / Set-Cookie-clears sessionid (same class of bug as hitting IG_ORIGIN
        # during cookie dump). www-claim is taken from later API response headers.
        self._bootstrapped = True

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
            rate_limit_sleep=self.rate_limit_sleep,
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
        self._bootstrap_session()
        if self.request_sleep > 0 and self._requests_made:
            time.sleep(jittered(self.request_sleep))
        self._requests_made += 1
        req = urllib.request.Request(request_url, data=data, headers=self._headers())
        try:
            with self._opener.open(req, timeout=30) as resp:
                claim = resp.headers.get("x-ig-set-www-claim")
                if claim:
                    self._www_claim = claim
                body, final_url = read_decoded_response(resp)
        except urllib.error.HTTPError as exc:
            raise_for_instagram_http(exc, request_url=request_url)
        except urllib.error.URLError as exc:
            raise InstagramError("instagram_network", f"Network error: {exc.reason}") from exc

        if looks_like_login_page(final_url, body, request_url=request_url):
            raise InstagramError(
                "instagram_auth_required",
                "Instagram redirected to a login/challenge page "
                f"({final_url}) from {request_url}. Refresh cookies and retry "
                "with a User-Agent that matches the browser that exported them.",
            )
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            if is_logged_in_html(body) or (
                is_instagram_home(final_url) and not is_instagram_home(request_url)
            ):
                raise InstagramError(
                    "instagram_http",
                    "Instagram returned HTML instead of JSON (rate-limit)",
                    http_status=429,
                ) from exc
            raise InstagramError("instagram_api", "Instagram returned a non-JSON response") from exc
        if isinstance(payload, dict) and payload.get("status") == "fail":
            raise error_from_fail_message(
                str(payload.get("message") or "request failed"),
            )
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
        user_id = self.user_id(username)
        yield from self._iter_graphql_then_rest(
            what=f"@{username} reels",
            graphql=lambda: self._iter_clips_graphql(user_id, username),
            rest=lambda: self._iter_clips_rest(user_id, username),
        )

    def iter_feed_videos(self, username: str) -> Iterator[ListedVideo]:
        user_id = self.user_id(username)
        yield from self._iter_graphql_then_rest(
            what=f"@{username} feed",
            graphql=lambda: self._iter_feed_graphql(username),
            rest=lambda: self._iter_feed_rest(user_id, username),
        )

    def _iter_graphql_then_rest(
        self,
        *,
        what: str,
        graphql: Callable[[], Iterator[ListedVideo]],
        rest: Callable[[], Iterator[ListedVideo]],
    ) -> Iterator[ListedVideo]:
        yielded = False
        try:
            for video in graphql():
                yielded = True
                yield video
        except InstagramError as exc:
            if yielded:
                raise
            LOG.warning("%s GraphQL listing failed [%s]; trying REST", what, exc.code)
            yield from rest()

    def _graphql_query(self, doc_id: str, variables: dict) -> dict:
        return self._request(
            f"{IG_ORIGIN}/graphql/query",
            form={
                "doc_id": doc_id,
                "variables": json.dumps(variables, separators=(",", ":")),
                "server_timestamps": "true",
            },
        )

    def _paginate_graphql(
        self,
        *,
        username: str,
        cursor_key: str,
        kind: str,
        doc_id: str,
        connection_key: str,
        variables: dict,
    ) -> Iterator[ListedVideo]:
        data_obj = variables.setdefault("data", {})
        if not isinstance(data_obj, dict):
            raise InstagramError("instagram_api", "GraphQL variables.data must be an object")
        self._apply_cursor(cursor_key, data_obj)
        while True:
            payload = self._graphql_query(doc_id, variables)
            root = payload.get("data")
            if not isinstance(root, dict) or connection_key not in root:
                raise InstagramError(
                    "instagram_api",
                    f"GraphQL listing missing {connection_key}",
                )
            items, page_info = graphql_connection_media(payload, connection_key)
            for media in items:
                video = listed_video_from_media(media, username, kind=kind)
                if video:
                    yield video
            more = page_info.get("has_next_page")
            cursor = page_info.get("end_cursor")
            self._save_cursor(cursor_key, more, cursor)
            if not more or not cursor:
                return
            data_obj["max_id"] = str(cursor)

    def _apply_cursor(self, key: str, target: dict[str, object]) -> None:
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

    def _iter_clips_graphql(self, user_id: str, username: str) -> Iterator[ListedVideo]:
        yield from self._paginate_graphql(
            username=username,
            cursor_key=f"{username.lower()}:reels",
            kind="reels",
            doc_id=GQL_CLIPS_DOC_ID,
            connection_key=GQL_CLIPS_CONNECTION,
            variables={
                "data": {
                    "include_feed_video": True,
                    "page_size": 12,
                    "target_user_id": str(user_id),
                }
            },
        )

    def _iter_clips_rest(self, user_id: str, username: str) -> Iterator[ListedVideo]:
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

    def _iter_feed_graphql(self, username: str) -> Iterator[ListedVideo]:
        yield from self._paginate_graphql(
            username=username,
            cursor_key=f"{username.lower()}:feed",
            kind="feed",
            doc_id=GQL_FEED_DOC_ID,
            connection_key=GQL_FEED_CONNECTION,
            variables={
                "data": {"count": 12, "include_relationship_info": True},
                "username": username,
            },
        )

    def _iter_feed_rest(self, user_id: str, username: str) -> Iterator[ListedVideo]:
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
        # Prefer GraphQL: REST /media/{pk}/info/ is frequently 429 after bulk downloads.
        try:
            return self._media_by_shortcode_graphql(shortcode)
        except InstagramError as exc:
            if (
                exc.code in {"instagram_auth_required", "invalid_entry", "media_not_found"}
                or is_rate_limit(exc)
            ):
                raise
            LOG.warning(
                "GraphQL media failed for %s [%s]; trying REST",
                shortcode,
                exc.code,
            )
            try:
                return self._media_by_shortcode_rest(shortcode)
            except InstagramError as rest_exc:
                if rest_exc.code == "instagram_auth_required" and (
                    exc.code == "instagram_http" or is_rate_limit(exc)
                ):
                    raise exc from rest_exc
                if rest_exc.code == "instagram_auth_required":
                    raise InstagramError(
                        "media_not_found",
                        f"No media for {shortcode}",
                    ) from rest_exc
                raise

    def _media_by_shortcode_rest(self, shortcode: str) -> dict:
        pk = shortcode_to_pk(shortcode)
        data = self._request(f"{IG_ORIGIN}/api/v1/media/{pk}/info/")
        items = data.get("items") or []
        if not items:
            raise InstagramError("media_not_found", f"No media for {shortcode}")
        return items[0]

    def _media_by_shortcode_graphql(self, shortcode: str) -> dict:
        data = self._graphql_query(
            GQL_MEDIA_DOC_ID,
            {
                "shortcode": shortcode,
                "__relay_internal__pv__PolarisAIGMMediaWebLabelEnabledrelayprovider": False,
            },
        )
        connection = (data.get("data") or {}).get(GQL_MEDIA_CONNECTION)
        if data.get("errors") and not connection:
            # status=ok + execution error + null data: this shortcode is gone or
            # blocked. REST /media/{pk}/info/ often 302s to login, which would
            # abort the whole --from-queue run as instagram_auth_required.
            raise InstagramError("media_not_found", f"No media for {shortcode}")
        info = connection or {}
        items = info.get("items") or []
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
            rate_limit_sleep=self.rate_limit_sleep,
        )
        return True

    def _download_url_once(self, url: str, dest: Path) -> None:
        tmp = dest.with_name(dest.name + ".part")
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.user_agent,
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


def graphql_connection_media(payload: dict, connection_key: str) -> tuple[list[dict], dict]:
    """Pull media dicts from a GraphQL connection (clips wrap node.media; feed is node)."""
    root = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(root, dict):
        return [], {}
    conn = root.get(connection_key)
    if not isinstance(conn, dict):
        return [], {}
    items: list[dict] = []
    for edge in conn.get("edges") or []:
        if not isinstance(edge, dict):
            continue
        node = edge.get("node")
        if not isinstance(node, dict):
            continue
        media = node.get("media")
        items.append(media if isinstance(media, dict) else node)
    page_info = conn.get("page_info")
    return items, page_info if isinstance(page_info, dict) else {}


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
    dests = native_destinations(download_dir, video)
    if len(dests) != len(video.file_urls):
        raise ValueError("CDN URL count does not match destination count")
    try:
        for url, dest in zip(video.file_urls, dests):
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
        if is_rate_limit(exc) or exc.code == "instagram_auth_required":
            raise
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


def discover_profile(
    entry: Profile,
    skip: SkipStore,
    queue: QueueStore,
    client: InstagramClient,
    args: argparse.Namespace,
    stats: RunStats,
) -> None:
    seen: set[str] = set()
    LOG.info("Discovering reels for @%s", entry.username)
    try:
        enqueue_video_stream(
            client.iter_reels(entry.username),
            label="reels",
            username=entry.username,
            seen=seen,
            skip=skip,
            queue=queue,
            args=args,
            stats=stats,
        )
    except InstagramError as exc:
        LOG.error("Discover @%s reels failed: %s [%s]", entry.username, exc, exc.code)
        stats.record("failed")
        return
    if args.reels_only or stats.max_reached(args.max):
        return
    LOG.info("Discovering feed videos for @%s", entry.username)
    try:
        enqueue_video_stream(
            client.iter_feed_videos(entry.username),
            label="feed",
            username=entry.username,
            seen=seen,
            skip=skip,
            queue=queue,
            args=args,
            stats=stats,
        )
    except InstagramError as exc:
        LOG.error("Discover @%s feed failed: %s [%s]", entry.username, exc, exc.code)
        stats.record("failed")


def enqueue_video_stream(
    videos: Iterator[ListedVideo],
    *,
    label: str,
    username: str,
    seen: set[str],
    skip: SkipStore,
    queue: QueueStore,
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
        if skip.known(video.video_id) or queue.known(video.video_id):
            LOG.info("Already have %s", video.video_id)
            stats.record("skipped")
            if skip.known(video.video_id) and not video.pinned:
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
        if queue.append(video):
            LOG.info("Queued %s", video.url)
            stats.record("listed")


def process_queued(
    queued: ListedVideo,
    skip: SkipStore,
    failed: FailStore,
    client: InstagramClient | None,
    yt_dlp: list[str] | None,
    args: argparse.Namespace,
    stats: RunStats,
) -> None:
    if stats.max_reached(args.max):
        return
    if skip.known(queued.video_id) or failed.known(queued.video_id):
        LOG.debug("Already have %s", queued.video_id)
        stats.record("skipped")
        return
    try:
        video = refresh_queued_video(queued, client)
    except InstagramError as exc:
        if exc.code == "instagram_auth_required" or is_rate_limit(exc):
            raise
        LOG.error("%s [%s]", exc, exc.code)
        if exc.code == "media_not_found":
            failed.remember(queued.video_id)
        stats.record("failed")
        return
    if args.dry_run:
        LOG.info("Would download %s", video.url)
        stats.record("listed")
        return
    if download_listed(video, video.url, skip, client, yt_dlp, args):
        stats.record("downloaded")
    else:
        failed.remember(queued.video_id)
        stats.record("failed")


def refresh_queued_video(queued: ListedVideo, client: InstagramClient | None) -> ListedVideo:
    if client is None:
        return queued
    fresh = listed_from_direct(
        DirectMedia(url=queued.url, video_id=queued.video_id),
        client,
    )
    if fresh is None:
        raise InstagramError("media_not_found", f"No video found for {queued.url}")
    username = queued.username if queued.username != "instagram" else fresh.username
    kind = queued.kind if queued.kind in {"reels", "feed"} else fresh.kind
    return ListedVideo(
        video_id=fresh.video_id,
        url=fresh.url,
        username=username,
        kind=kind,
        taken_at=fresh.taken_at or queued.taken_at,
        pinned=queued.pinned or fresh.pinned,
        file_urls=fresh.file_urls,
    )


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
            "Download Instagram videos. Profile listing is --discover-only "
            "(writes --queue); downloads are --from-queue or direct reel/post URLs. "
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
        help="Stop after N new queue rows (--discover-only) or N successful downloads (0 = unlimited; failures do not count)",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        metavar="N",
        help="Retries for 429, 401/403 throttle, 5xx, and network errors (default: 3)",
    )
    parser.add_argument(
        "--request-sleep",
        type=float,
        default=6.0,
        help="Seconds to wait between Instagram listing requests (±25%% jitter; gallery-dl uses 6-12)",
    )
    parser.add_argument(
        "--rate-limit-sleep",
        type=float,
        default=300.0,
        metavar="SECONDS",
        help=(
            "Seconds to wait for cooldown when Instagram returns 'Please wait a few minutes' "
            "or 429 throttle (default: 300.0 / 5 min; set 0 to disable)"
        ),
    )
    parser.add_argument(
        "--ig-app-id",
        default=os.environ.get("IG_APP_ID", IG_WEB_APP_ID),
        help="Instagram web app id sent as X-IG-App-ID (env: IG_APP_ID)",
    )
    parser.add_argument(
        "--user-agent",
        default=os.environ.get("IG_USER_AGENT", CHROME_UA),
        help=(
            "HTTP User-Agent; must match the browser that created the cookies "
            "(env: IG_USER_AGENT)"
        ),
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
    parser.add_argument(
        "--discover-only",
        action="store_true",
        help="List profiles into --queue (JSONL); do not download MP4s",
    )
    parser.add_argument(
        "--from-queue",
        action="store_true",
        help="Download videos from --queue instead of listing --profiles",
    )
    parser.add_argument(
        "--queue",
        type=Path,
        default=Path("data/queue.jsonl"),
        help="JSONL queue written by --discover-only and read by --from-queue",
    )
    parser.add_argument(
        "--failed",
        type=Path,
        default=Path("data/failed.txt"),
        help="IDs that failed CDN/download; skipped on later --from-queue runs",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    try:
        if args.max < 0 or args.retries < 0:
            raise InstagramError("invalid_args", "--max and --retries must be >= 0")
        if args.discover_only and args.from_queue:
            raise InstagramError(
                "invalid_args",
                "Use either --discover-only or --from-queue, not both",
            )
        yt_dlp = maybe_yt_dlp(args)
        if cookies_export_only(args):
            LOG.info("Wrote cookies to %s", args.cookies)
            return 0
        skip = SkipStore(args.archive, args.out)
        skip.load()
        stats = RunStats()
        if args.from_queue:
            queued = load_queue_file(args.queue)
            failed = FailStore(args.failed)
            failed.load()
            pending = [
                video
                for video in queued
                if not skip.known(video.video_id) and not failed.known(video.video_id)
            ]
            already = len(queued) - len(pending)
            if already:
                LOG.info("Skipping %s already-done or previously-failed queue rows", already)
                stats.skipped += already
            if not pending:
                LOG.info("Queue has nothing left to download")
                stats.log_summary()
                return stats.exit_code()
            client = maybe_client(args, pending)
            for video in pending:
                if stats.max_reached(args.max):
                    LOG.info("Reached --max %s", args.max)
                    break
                process_queued(video, skip, failed, client, yt_dlp, args, stats)
            stats.log_summary()
            return stats.exit_code()
        entries = parse_profile_file(args.profiles)
        cursors = CursorStore(args.cursors) if args.full else None
        if cursors is not None:
            cursors.load()
        client = maybe_client(args, entries, cursors)
        if args.discover_only:
            queue = QueueStore(args.queue)
            queue.load()
            for entry in entries:
                if stats.max_reached(args.max):
                    LOG.info("Reached --max %s", args.max)
                    break
                if isinstance(entry, DirectMedia):
                    video = listed_from_media_url(entry)
                    if video is None:
                        continue
                    if queue.known(video.video_id):
                        stats.record("skipped")
                        continue
                    if queue.append(video):
                        LOG.info("Queued %s", video.url)
                        stats.record("listed")
                    continue
                if client is None:
                    raise InstagramError("cookies_required", "Profile listing needs cookies")
                discover_profile(entry, skip, queue, client, args, stats)
            if stats.listed == 0 and not args.queue.is_file():
                LOG.warning("Queue file was not created because nothing was enqueued")
            stats.log_summary()
            return stats.exit_code()
        for entry in entries:
            if stats.max_reached(args.max):
                LOG.info("Reached --max %s", args.max)
                break
            if isinstance(entry, DirectMedia):
                process_direct(entry, skip, client, yt_dlp, args, stats)
                continue
            LOG.warning(
                "Skipping @%s — profile listing is --discover-only; download with --from-queue",
                entry.username,
            )
        stats.log_summary()
        return stats.exit_code()
    except InstagramError as exc:
        LOG.error("%s [%s]", exc, exc.code)
        return 1
    except KeyboardInterrupt:
        LOG.warning("Interrupted — progress so far is kept in archive, queue, and cursors")
        return 130


def cookies_export_only(args: argparse.Namespace) -> bool:
    """True when --cookies-from-browser was the job (no queue/discover, no reel URLs)."""
    if not args.cookies_from_browser or args.from_queue or args.discover_only:
        return False
    if not args.profiles.is_file():
        return True
    try:
        entries = parse_profile_file(args.profiles)
    except InstagramError:
        return True
    return not any(isinstance(entry, DirectMedia) for entry in entries)


def maybe_yt_dlp(args: argparse.Namespace) -> list[str] | None:
    if args.cookies_from_browser:
        try:
            yt_dlp = find_yt_dlp()
        except InstagramError as exc:
            raise InstagramError(
                "cookie_export_failed",
                "Install yt-dlp to use --cookies-from-browser, or pass a Netscape --cookies file",
            ) from exc
        export_browser_cookies(
            yt_dlp,
            args.cookies_from_browser,
            args.cookies,
            user_agent=args.user_agent,
        )
        if args.downloader == "yt-dlp":
            return yt_dlp
        return None
    if args.downloader == "yt-dlp":
        return find_yt_dlp()
    return None


def maybe_client(
    args: argparse.Namespace,
    entries: list[Entry] | list[ListedVideo],
    cursors: CursorStore | None = None,
) -> InstagramClient | None:
    if args.from_queue:
        needs_client = args.downloader == "native" and bool(entries)
    elif args.discover_only:
        needs_client = any(isinstance(entry, Profile) for entry in entries)
    else:
        needs_client = args.downloader == "native" and any(
            isinstance(entry, DirectMedia) for entry in entries
        )
    if not needs_client:
        return None
    return InstagramClient(
        args.cookies,
        args.request_sleep,
        retries=args.retries,
        app_id=args.ig_app_id,
        user_agent=args.user_agent,
        cursors=cursors,
        rate_limit_sleep=args.rate_limit_sleep,
    )


if __name__ == "__main__":
    sys.exit(main())
