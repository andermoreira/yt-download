import gzip
import json
import os
import tempfile
import unittest
import unittest.mock
import urllib.error
import urllib.request
from copy import copy
from email.message import Message
from io import BytesIO
from pathlib import Path

import download_instagram as ig


class ParseTests(unittest.TestCase):
    def test_parse_username_and_urls(self) -> None:
        self.assertEqual(ig.parse_line("andres.ague"), ig.Profile(username="andres.ague"))
        self.assertEqual(ig.parse_line("@andres.ague"), ig.Profile(username="andres.ague"))
        self.assertEqual(
            ig.parse_line("https://www.instagram.com/andres.ague/"),
            ig.Profile(username="andres.ague"),
        )
        self.assertEqual(
            ig.parse_line("https://www.instagram.com/andres.ague/reel/CSIeW8lg-Pd/"),
            ig.DirectMedia(
                url="https://www.instagram.com/andres.ague/reel/CSIeW8lg-Pd/",
                video_id="CSIeW8lg-Pd",
            ),
        )
        self.assertIsNone(ig.parse_line("# comment"))
        self.assertIsNone(ig.parse_line("  "))

    def test_looks_like_login_page(self) -> None:
        html = "<!doctype html><html>"
        api = "https://www.instagram.com/api/v1/users/web_profile_info/"
        home = "https://www.instagram.com/"
        self.assertTrue(
            ig.looks_like_login_page(
                "https://www.instagram.com/accounts/login/",
                html,
                request_url=api,
            )
        )
        self.assertTrue(
            ig.looks_like_login_page(
                "https://www.instagram.com/challenge/",
                html,
                request_url=api,
            )
        )
        self.assertFalse(ig.looks_like_login_page(home, html, request_url=api))
        self.assertFalse(
            ig.looks_like_login_page(
                home,
                '<html lang="en" class="no-js logged-in ">',
                request_url=api,
            )
        )
        self.assertFalse(ig.looks_like_login_page(home, html, request_url=home))
        self.assertFalse(ig.looks_like_login_page(api, html, request_url=api))
        self.assertFalse(ig.looks_like_login_page(api, '{"ok":true}', request_url=api))

    def test_decode_gzip_body(self) -> None:
        payload = b'{"status":"ok"}'
        compressed = gzip.compress(payload)
        self.assertEqual(ig.decode_http_body(compressed, "gzip"), payload)
        self.assertEqual(ig.decode_http_body(compressed, None), payload)
        self.assertEqual(ig.decode_http_body(payload, None), payload)

    def test_redirect_handler_blocks_home_bounce(self) -> None:
        handler = ig.InstagramRedirectHandler()
        req = urllib.request.Request("https://www.instagram.com/api/v1/clips/user/")
        self.assertIsNone(
            handler.redirect_request(
                req, None, 302, "Found", {}, "https://www.instagram.com/"
            )
        )
        self.assertIsNone(
            handler.redirect_request(
                req, None, 302, "Found", {}, "https://www.instagram.com/accounts/login/"
            )
        )
        home = urllib.request.Request("https://www.instagram.com/")
        self.assertIsNotNone(
            handler.redirect_request(
                home, None, 302, "Found", {}, "https://www.instagram.com/#"
            )
        )

    def test_chrome_client_hint_headers(self) -> None:
        hints = ig.chrome_client_hint_headers(ig.CHROME_UA)
        self.assertIn("152", hints["Sec-CH-UA"])
        self.assertEqual(ig.chrome_client_hint_headers("TestUA/1.0"), {})

    def test_parse_profile_file_skips_comments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "profiles.txt"
            path.write_text("# ignore\nandres.ague\n\n", encoding="utf-8")
            self.assertEqual(
                ig.parse_profile_file(path),
                [ig.Profile(username="andres.ague")],
            )


class SkipTests(unittest.TestCase):
    def test_skip_store_uses_archive_and_existing_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "archive.txt"
            downloads = root / "downloads"
            profile_dir = downloads / "andres.ague" / "reels"
            profile_dir.mkdir(parents=True)
            (profile_dir / "2021-07-15_CSIeW8lg-Pd.mp4").write_bytes(b"fake")
            archive.write_text("instagram alreadyOnDisk\n", encoding="utf-8")

            skip = ig.SkipStore(archive, downloads)
            skip.load()

            self.assertTrue(skip.known("alreadyOnDisk"))
            self.assertTrue(skip.known("CSIeW8lg-Pd"))
            self.assertIn("instagram CSIeW8lg-Pd", archive.read_text(encoding="utf-8"))
            self.assertFalse(skip.known("brandNewReel"))

    def test_video_id_from_filename(self) -> None:
        self.assertEqual(ig.video_id_from_filename(Path("CSIeW8lg-Pd.mp4")), "CSIeW8lg-Pd")
        self.assertEqual(
            ig.video_id_from_filename(Path("Title [CSIeW8lg-Pd].mp4")),
            "CSIeW8lg-Pd",
        )
        self.assertIsNone(ig.video_id_from_filename(Path("CSIeW8lg-Pd.mp4.part")))
        self.assertIsNone(ig.video_id_from_filename(Path("notes.txt")))
        self.assertEqual(
            ig.video_id_from_filename(Path("2021-07-15_CSIeW8lg-Pd.mp4")),
            "CSIeW8lg-Pd",
        )


class MediaTests(unittest.TestCase):
    def test_listed_video_skips_photos(self) -> None:
        self.assertIsNone(
            ig.listed_video_from_media(
                {"code": "photoCode1", "media_type": 1},
                "andres.ague",
            )
        )
        video = ig.listed_video_from_media(
            {
                "code": "CSIeW8lg-Pd",
                "media_type": 2,
                "product_type": "clips",
                "taken_at": 1626307200,
                "user": {"username": "andres.ague"},
            },
            "andres.ague",
            kind="reels",
        )
        self.assertIsNotNone(video)
        assert video is not None
        self.assertEqual(video.video_id, "CSIeW8lg-Pd")
        self.assertEqual(video.username, "andres.ague")
        self.assertEqual(video.kind, "reels")
        self.assertEqual(video.taken_at, 1626307200)
        self.assertTrue(video.url.endswith("/andres.ague/reel/CSIeW8lg-Pd/"))
        self.assertEqual(video.file_urls, ())

    def test_picks_highest_video_version(self) -> None:
        urls = ig.video_urls_from_media(
            {
                "media_type": 2,
                "video_versions": [
                    {"url": "https://cdn.example/low.mp4", "width": 480, "height": 854, "type": 101},
                    {"url": "https://cdn.example/high.mp4", "width": 1080, "height": 1920, "type": 102},
                ],
            }
        )
        self.assertEqual(urls, ("https://cdn.example/high.mp4",))

    def test_carousel_collects_only_videos(self) -> None:
        urls = ig.video_urls_from_media(
            {
                "media_type": 8,
                "carousel_media": [
                    {"media_type": 1, "video_versions": []},
                    {
                        "media_type": 2,
                        "video_versions": [
                            {"url": "https://cdn.example/a.mp4", "width": 720, "height": 1280, "type": 101},
                        ],
                    },
                ],
            }
        )
        self.assertEqual(urls, ("https://cdn.example/a.mp4",))

    def test_graphql_connection_media_clips_and_feed(self) -> None:
        clips = {
            "data": {
                ig.GQL_CLIPS_CONNECTION: {
                    "edges": [
                        {
                            "node": {
                                "media": {
                                    "code": "Dc1JqMHgkm1",
                                    "media_type": 2,
                                    "product_type": "clips",
                                }
                            }
                        }
                    ],
                    "page_info": {"has_next_page": True, "end_cursor": "abc"},
                }
            }
        }
        items, info = ig.graphql_connection_media(clips, ig.GQL_CLIPS_CONNECTION)
        self.assertEqual(items[0]["code"], "Dc1JqMHgkm1")
        self.assertEqual(info["end_cursor"], "abc")
        video = ig.listed_video_from_media(items[0], "andres.ague", kind="reels")
        self.assertIsNotNone(video)
        assert video is not None
        self.assertEqual(video.video_id, "Dc1JqMHgkm1")
        self.assertEqual(video.file_urls, ())
        feed = {
            "data": {
                ig.GQL_FEED_CONNECTION: {
                    "edges": [
                        {
                            "node": {
                                "code": "CSIeW8lg-Pd",
                                "media_type": 2,
                                "product_type": "clips",
                                "taken_at": 1628092800,
                            }
                        }
                    ],
                    "page_info": {"has_next_page": False, "end_cursor": None},
                }
            }
        }
        feed_items, feed_info = ig.graphql_connection_media(feed, ig.GQL_FEED_CONNECTION)
        self.assertEqual(feed_items[0]["taken_at"], 1628092800)
        self.assertFalse(feed_info.get("has_next_page"))

    def test_shortcode_to_pk(self) -> None:
        self.assertEqual(ig.shortcode_to_pk("A"), "0")
        self.assertEqual(ig.shortcode_to_pk("B"), "1")

    def test_native_destinations(self) -> None:
        video = ig.ListedVideo(
            video_id="CSIeW8lg-Pd",
            url="https://www.instagram.com/andres.ague/reel/CSIeW8lg-Pd/",
            username="andres.ague",
            kind="reels",
            taken_at=1626307200,
            pinned=False,
            file_urls=("https://cdn.example/a.mp4", "https://cdn.example/b.mp4"),
        )
        dests = ig.native_destinations(Path("downloads"), video)
        self.assertEqual(
            dests,
            [
                Path("downloads/andres.ague/reels/2021-07-15_CSIeW8lg-Pd~1.mp4"),
                Path("downloads/andres.ague/reels/2021-07-15_CSIeW8lg-Pd~2.mp4"),
            ],
        )

    def test_carousel_filename_seeds_archive_id(self) -> None:
        self.assertEqual(
            ig.video_id_from_filename(Path("2021-07-15_CSIeW8lg-Pd~1.mp4")),
            "CSIeW8lg-Pd",
        )
        self.assertEqual(
            ig.video_id_from_filename(Path("undated_CSIeW8lg-Pd.mp4")),
            "CSIeW8lg-Pd",
        )


class RetryTests(unittest.TestCase):
    def test_parse_retry_after(self) -> None:
        self.assertEqual(ig.parse_retry_after("7"), 7.0)
        self.assertIsNone(ig.parse_retry_after("Fri, 01 Jan 2030 00:00:00 GMT"))
        self.assertIsNone(ig.parse_retry_after(None))

    def test_is_retryable(self) -> None:
        self.assertTrue(ig.is_retryable(ig.InstagramError("instagram_network", "x")))
        self.assertTrue(
            ig.is_retryable(ig.InstagramError("instagram_http", "x", http_status=429))
        )
        self.assertTrue(
            ig.is_retryable(ig.InstagramError("instagram_http", "x", http_status=403))
        )
        self.assertTrue(
            ig.is_retryable(ig.InstagramError("instagram_http", "x", http_status=401))
        )
        self.assertFalse(
            ig.is_retryable(ig.InstagramError("instagram_http", "x", http_status=404))
        )
        self.assertFalse(ig.is_retryable(ig.InstagramError("instagram_auth_required", "x")))

    def test_backoff_prefers_retry_after(self) -> None:
        self.assertEqual(ig.backoff_seconds(0, 1.5, 12.0), 12.0)
        self.assertEqual(ig.backoff_seconds(2, 1.5, None), 6.0)

    def test_please_wait_401_is_retryable_not_login(self) -> None:
        err = ig.error_from_fail_message(
            "Please wait a few minutes before you try again.",
            http_status=401,
        )
        self.assertEqual(err.code, "instagram_http")
        self.assertEqual(err.http_status, 429)
        self.assertTrue(ig.is_retryable(err))
        self.assertTrue(ig.is_rate_limit(err))
        login = ig.error_from_fail_message("login_required", http_status=401)
        self.assertEqual(login.code, "instagram_auth_required")
        self.assertFalse(ig.is_retryable(login))
        self.assertFalse(ig.is_rate_limit(login))

    def test_raise_for_instagram_http_reads_401_json(self) -> None:
        payload = {
            "message": "Please wait a few minutes before you try again.",
            "require_login": True,
            "status": "fail",
        }
        hdrs = Message()
        hdrs["Content-Type"] = "application/json; charset=utf-8"
        exc = urllib.error.HTTPError(
            "https://www.instagram.com/graphql/query",
            401,
            "Unauthorized",
            hdrs,
            BytesIO(json.dumps(payload).encode()),
        )
        with self.assertRaises(ig.InstagramError) as raised:
            ig.raise_for_instagram_http(
                exc, request_url="https://www.instagram.com/graphql/query"
            )
        self.assertEqual(raised.exception.code, "instagram_http")
        self.assertEqual(raised.exception.http_status, 429)

    def test_raise_for_instagram_http_login_redirect(self) -> None:
        hdrs = Message()
        hdrs["Location"] = "https://www.instagram.com/accounts/login/?next=/api/v1/media/1/info/"
        exc = urllib.error.HTTPError(
            "https://www.instagram.com/api/v1/media/1/info/",
            302,
            "Found",
            hdrs,
            BytesIO(b""),
        )
        with self.assertRaises(ig.InstagramError) as raised:
            ig.raise_for_instagram_http(
                exc, request_url="https://www.instagram.com/api/v1/media/1/info/"
            )
        self.assertEqual(raised.exception.code, "instagram_auth_required")

    def test_should_stop_after_existing(self) -> None:
        self.assertTrue(ig.should_stop_after_existing(3, full=False, threshold=3))
        self.assertFalse(ig.should_stop_after_existing(2, full=False, threshold=3))
        self.assertFalse(ig.should_stop_after_existing(3, full=True, threshold=3))
        self.assertFalse(ig.should_stop_after_existing(3, full=False, threshold=0))

    def test_call_with_retry_then_succeeds(self) -> None:
        calls = {"n": 0}

        def operation() -> str:
            calls["n"] += 1
            if calls["n"] < 3:
                raise ig.InstagramError("instagram_http", "rate", http_status=429)
            return "ok"

        with unittest.mock.patch("download_instagram.time.sleep"):
            self.assertEqual(
                ig.call_with_retry(operation, retries=3, request_sleep=0.1, what="t"),
                "ok",
            )
        self.assertEqual(calls["n"], 3)


class StatsTests(unittest.TestCase):
    def test_exit_code_and_max(self) -> None:
        stats = ig.RunStats()
        self.assertEqual(stats.exit_code(), 0)
        self.assertFalse(stats.max_reached(0))
        stats.record("downloaded")
        stats.record("downloaded")
        self.assertTrue(stats.max_reached(2))
        stats.record("failed")
        self.assertEqual(stats.exit_code(), 1)
        listed = ig.RunStats()
        listed.record("listed")
        self.assertTrue(listed.max_reached(1))


class SinkSafetyTests(unittest.TestCase):
    def test_listed_video_rejects_unsafe_ids(self) -> None:
        self.assertIsNone(
            ig.listed_video_from_media({"code": "../../etc", "media_type": 2}, "user")
        )
        self.assertIsNone(
            ig.listed_video_from_media({"code": "a b c", "media_type": 2}, "user")
        )

    def test_listed_video_rejects_unsafe_owner(self) -> None:
        base = {"code": "CSIeW8lg-Pd", "media_type": 2}
        self.assertIsNone(
            ig.listed_video_from_media({**base, "user": {"username": "../../evil"}}, "user")
        )
        self.assertIsNone(
            ig.listed_video_from_media({**base, "user": {"username": "..."}}, "user")
        )
        video = ig.listed_video_from_media(
            {**base, "user": {"username": "andres.ague"}}, "user"
        )
        self.assertIsNotNone(video)

    def test_parse_line_rejects_unsafe_usernames(self) -> None:
        for line in ("...", "@..", "https://www.instagram.com/../"):
            with self.assertRaises(ig.InstagramError):
                ig.parse_line(line)

    def test_remember_refuses_malformed_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "archive.txt"
            skip = ig.SkipStore(archive, root / "downloads")
            skip.load()
            skip.remember("bad id")
            skip.remember("OKid1234")
            self.assertEqual(archive.read_text(encoding="utf-8"), "instagram OKid1234\n")
            self.assertFalse(skip.known("bad id"))
            self.assertTrue(skip.known("OKid1234"))

    def test_is_safe_media_url(self) -> None:
        self.assertTrue(ig.is_safe_media_url("https://scontent.cdninstagram.com/v/t50/x.mp4"))
        self.assertTrue(ig.is_safe_media_url("https://d1.fbcdn.net/v/x.mp4"))
        self.assertFalse(ig.is_safe_media_url("http://scontent.cdninstagram.com/x.mp4"))
        self.assertFalse(ig.is_safe_media_url("https://evil.com/x.mp4"))
        self.assertFalse(ig.is_safe_media_url("https://cdninstagram.com.evil.com/x.mp4"))
        self.assertFalse(ig.is_safe_media_url("file:///etc/passwd"))

    def test_open_part_file_refuses_symlink(self) -> None:
        if not hasattr(os, "O_NOFOLLOW"):
            self.skipTest("O_NOFOLLOW unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "victim.txt"
            target.write_text("secret", encoding="utf-8")
            part = root / "video.mp4.part"
            part.symlink_to(target)
            with self.assertRaises(OSError):
                ig.open_part_file(part)
            self.assertEqual(target.read_text(encoding="utf-8"), "secret")

    def test_open_part_file_creates_regular_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            part = Path(tmp) / "video.mp4.part"
            with ig.open_part_file(part) as handle:
                handle.write(b"data")
            self.assertEqual(part.read_bytes(), b"data")
            self.assertEqual(part.stat().st_mode & 0o777, 0o600)

    def test_open_part_file_truncates_leftover(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            part = Path(tmp) / "video.mp4.part"
            part.write_bytes(b"stale bytes from an interrupted run")
            with ig.open_part_file(part) as handle:
                handle.write(b"new")
            self.assertEqual(part.read_bytes(), b"new")


class CursorTests(unittest.TestCase):
    def test_cursor_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cursors.json"
            store = ig.CursorStore(path)
            store.load()
            self.assertIsNone(store.get("user:reels"))
            store.set("user:reels", "abc123")
            store.set("user:feed", "xyz")
            again = ig.CursorStore(path)
            again.load()
            self.assertEqual(again.get("user:reels"), "abc123")
            again.set("user:reels", None)
            self.assertIsNone(again.get("user:reels"))
            self.assertIn("user:feed", path.read_text(encoding="utf-8"))

    def test_cursor_load_ignores_corrupt_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cursors.json"
            path.write_text("{not json", encoding="utf-8")
            store = ig.CursorStore(path)
            store.load()
            self.assertIsNone(store.get("user:reels"))

    def test_cursor_load_drops_non_string_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cursors.json"
            path.write_text(
                json.dumps({"user:reels": None, "user:feed": "", "user:tv": "abc"}),
                encoding="utf-8",
            )
            store = ig.CursorStore(path)
            store.load()
            self.assertIsNone(store.get("user:reels"))
            self.assertIsNone(store.get("user:feed"))
            self.assertEqual(store.get("user:tv"), "abc")


class MetadataTests(unittest.TestCase):
    def test_write_metadata_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "2021-07-15_CSIeW8lg-Pd.mp4"
            video = ig.ListedVideo(
                video_id="CSIeW8lg-Pd",
                url="https://www.instagram.com/andres.ague/reel/CSIeW8lg-Pd/",
                username="andres.ague",
                kind="reels",
                taken_at=1626307200,
                pinned=False,
                file_urls=("https://cdn.example/a.mp4",),
            )
            ig.write_metadata_file(dest, video, "https://cdn.example/a.mp4")
            sidecar = dest.with_name(dest.name + ".json")
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
            self.assertEqual(meta["id"], "CSIeW8lg-Pd")
            self.assertEqual(meta["username"], "andres.ague")
            self.assertEqual(meta["taken_at"], "2021-07-15")
            self.assertEqual(meta["media_url"], "https://cdn.example/a.mp4")


class ClientTests(unittest.TestCase):
    def make_cookies(self, path: Path) -> None:
        path.write_text(
            "# Netscape HTTP Cookie File\n"
            ".instagram.com\tTRUE\t/\tTRUE\t9999999999\tsessionid\tabc\n",
            encoding="utf-8",
        )

    def test_headers_use_configured_app_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cookies = Path(tmp) / "cookies.txt"
            self.make_cookies(cookies)
            client = ig.InstagramClient(cookies, 0.0, app_id="123456789")
            self.assertEqual(client._headers()["X-IG-App-ID"], "123456789")

    def test_headers_use_configured_user_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cookies = Path(tmp) / "cookies.txt"
            self.make_cookies(cookies)
            client = ig.InstagramClient(cookies, 0.0, user_agent="TestUA/1.0")
            self.assertEqual(client._headers()["User-Agent"], "TestUA/1.0")
            self.assertIn("Chrome/152", ig.CHROME_UA)
            self.assertNotIn("Sec-CH-UA", client._headers())

    def test_headers_include_chrome_client_hints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cookies = Path(tmp) / "cookies.txt"
            self.make_cookies(cookies)
            client = ig.InstagramClient(cookies, 0.0)
            headers = client._headers()
            self.assertIn("152", headers["Sec-CH-UA"])
            self.assertEqual(headers["X-ASBD-ID"], "359341")
            self.assertEqual(headers["Sec-CH-UA-Mobile"], "?0")
            self.assertEqual(headers["Sec-CH-UA-Platform"], '"macOS"')

    def test_restore_instagram_sessionid_after_clear(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cookies = Path(tmp) / "cookies.txt"
            self.make_cookies(cookies)
            client = ig.InstagramClient(cookies, 0.0)
            saved = [copy(c) for c in ig.instagram_sessionid_cookies(client._jar)]
            self.assertTrue(saved)
            for cookie in list(client._jar):
                if cookie.name == "sessionid":
                    client._jar.clear(cookie.domain, cookie.path, cookie.name)
            self.assertFalse(ig.instagram_sessionid_cookies(client._jar))
            ig.restore_instagram_sessionid(client._jar, saved)
            self.assertTrue(ig.instagram_sessionid_cookies(client._jar))

    def test_normalize_zero_expiry_session_cookies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cookies.txt"
            path.write_text(
                "# Netscape HTTP Cookie File\n"
                ".instagram.com\tTRUE\t/\tTRUE\t0\trur\tPRN\n"
                ".instagram.com\tTRUE\t/\tTRUE\t9999999999\tsessionid\tabc\n",
                encoding="utf-8",
            )
            client = ig.InstagramClient(path, 0.0)
            rur = next(cookie for cookie in client._jar if cookie.name == "rur")
            self.assertIsNone(rur.expires)
            self.assertTrue(rur.discard)

    def test_init_rejects_missing_sessionid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cookies = Path(tmp) / "cookies.txt"
            cookies.write_text("# empty\n", encoding="utf-8")
            with self.assertRaises(ig.InstagramError):
                ig.InstagramClient(cookies, 0.0)

    def test_download_url_skips_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cookies = root / "cookies.txt"
            self.make_cookies(cookies)
            client = ig.InstagramClient(cookies, 0.0)
            dest = root / "andres.ague" / "reels" / "2021-07-15_CSIeW8lg-Pd.mp4"
            dest.parent.mkdir(parents=True)
            dest.write_bytes(b"already here")
            self.assertFalse(
                client.download_url("https://scontent.cdninstagram.com/v/x.mp4", dest)
            )


class CookieExportTests(unittest.TestCase):
    def test_cookie_export_cmd_does_not_hit_instagram(self) -> None:
        cmd = ig.cookie_export_cmd(
            ["yt-dlp"],
            "chrome",
            Path("cookies.txt"),
            "TestUA/1.0",
        )
        joined = " ".join(cmd)
        self.assertNotIn("instagram.com", joined.lower())
        self.assertIn(ig.COOKIE_DUMP_URL, cmd)
        self.assertIn("--user-agent", cmd)
        self.assertIn("TestUA/1.0", cmd)
        self.assertIn("--cookies-from-browser", cmd)

    def test_jar_has_instagram_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cookies.txt"
            self.assertFalse(ig.jar_has_instagram_session(path))
            path.write_text(
                "# Netscape HTTP Cookie File\n"
                ".youtube.com\tTRUE\t/\tTRUE\t9999999999\tsessionid\tnope\n",
                encoding="utf-8",
            )
            self.assertFalse(ig.jar_has_instagram_session(path))
            path.write_text(
                "# Netscape HTTP Cookie File\n"
                ".instagram.com\tTRUE\t/\tTRUE\t9999999999\tsessionid\tabc\n",
                encoding="utf-8",
            )
            self.assertTrue(ig.jar_has_instagram_session(path))

    def test_export_rejects_dump_without_instagram_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "cookies.txt"

            def fake_run(_cmd: list[str], check: bool) -> unittest.mock.Mock:
                dest.write_text(
                    "# Netscape HTTP Cookie File\n"
                    ".youtube.com\tTRUE\t/\tTRUE\t9999999999\tSID\tabc\n",
                    encoding="utf-8",
                )
                result = unittest.mock.Mock()
                result.returncode = 0
                return result

            with unittest.mock.patch("download_instagram.subprocess.run", fake_run):
                with self.assertRaises(ig.InstagramError) as raised:
                    ig.export_browser_cookies(["yt-dlp"], "chrome", dest)
            self.assertEqual(raised.exception.code, "cookies_required")


class JitterTests(unittest.TestCase):
    def test_jittered_bounds(self) -> None:
        for _ in range(50):
            value = ig.jittered(2.0)
            self.assertGreaterEqual(value, 1.5)
            self.assertLessEqual(value, 2.5)


class QueueTests(unittest.TestCase):
    SAMPLE_URL = "https://www.instagram.com/andres.ague/reel/CSIeW8lg-Pd/"

    def sample_video(self, video_id: str = "CSIeW8lg-Pd") -> ig.ListedVideo:
        return ig.ListedVideo(
            video_id=video_id,
            url=f"https://www.instagram.com/andres.ague/reel/{video_id}/",
            username="andres.ague",
            kind="reels",
            taken_at=1628092800,
            pinned=False,
            file_urls=("https://scontent.cdninstagram.com/v/expire.mp4",),
        )

    def test_queue_roundtrip_skips_cdn_urls_and_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "queue.jsonl"
            store = ig.QueueStore(path)
            store.load()
            video = self.sample_video()
            self.assertTrue(store.append(video))
            self.assertFalse(store.append(video))
            record = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(record["id"], "CSIeW8lg-Pd")
            self.assertNotIn("file_urls", record)
            again = ig.QueueStore(path)
            again.load()
            self.assertTrue(again.known("CSIeW8lg-Pd"))
            loaded = ig.load_queue_file(path)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].file_urls, ())
            self.assertEqual(loaded[0].username, "andres.ague")

    def test_parse_queue_bare_url_and_json(self) -> None:
        from_url = ig.parse_queue_line(self.SAMPLE_URL)
        self.assertIsNotNone(from_url)
        assert from_url is not None
        self.assertEqual(from_url.video_id, "CSIeW8lg-Pd")
        self.assertEqual(from_url.username, "andres.ague")
        self.assertEqual(from_url.kind, "reels")
        line = json.dumps(
            {
                "id": "CSIeW8lg-Pd",
                "url": self.SAMPLE_URL,
                "username": "andres.ague",
                "kind": "reels",
                "taken_at": 1628092800,
                "pinned": False,
            }
        )
        parsed = ig.parse_queue_line(line)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.taken_at, 1628092800)
        self.assertIsNone(ig.parse_queue_line("# comment"))
        self.assertIsNone(ig.parse_queue_line("not-a-media-url"))

    def test_username_from_media_url(self) -> None:
        self.assertEqual(ig.username_from_media_url(self.SAMPLE_URL), "andres.ague")
        self.assertEqual(
            ig.username_from_media_url("https://www.instagram.com/reel/CSIeW8lg-Pd/"),
            "instagram",
        )

    def test_refresh_queued_without_client_keeps_record(self) -> None:
        queued = self.sample_video()
        self.assertEqual(ig.refresh_queued_video(queued, None), queued)

    def test_mutually_exclusive_discover_and_from_queue(self) -> None:
        self.assertEqual(ig.main(["--discover-only", "--from-queue"]), 1)

    def test_main_skips_profile_without_listing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profiles = root / "profiles.txt"
            profiles.write_text("andres.ague\n", encoding="utf-8")
            with unittest.mock.patch.object(
                ig, "InstagramClient", side_effect=AssertionError("must not list profiles")
            ):
                code = ig.main(
                    [
                        "--profiles",
                        str(profiles),
                        "--out",
                        str(root / "downloads"),
                        "--archive",
                        str(root / "archive.txt"),
                        "--cookies",
                        str(root / "missing-cookies.txt"),
                    ]
                )
            self.assertEqual(code, 0)

    def test_discover_only_enqueues_direct_media_up_to_max(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profiles = root / "profiles.txt"
            profiles.write_text(
                "https://www.instagram.com/andres.ague/reel/CSIeW8lg-Pd/\n"
                "https://www.instagram.com/andres.ague/reel/CSIeW8lgXXX/\n",
                encoding="utf-8",
            )
            queue = root / "queue.jsonl"
            with unittest.mock.patch.object(
                ig, "InstagramClient", side_effect=AssertionError("must not list profiles")
            ):
                code = ig.main(
                    [
                        "--discover-only",
                        "--max",
                        "1",
                        "--profiles",
                        str(profiles),
                        "--queue",
                        str(queue),
                        "--out",
                        str(root / "downloads"),
                        "--archive",
                        str(root / "archive.txt"),
                        "--cookies",
                        str(root / "missing-cookies.txt"),
                    ]
                )
            self.assertEqual(code, 0)
            rows = [json.loads(line) for line in queue.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["id"], "CSIeW8lg-Pd")

    def test_discover_only_enqueues_direct_media_already_in_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profiles = root / "profiles.txt"
            profiles.write_text(self.SAMPLE_URL + "\n", encoding="utf-8")
            queue = root / "queue.jsonl"
            archive = root / "archive.txt"
            archive.write_text("instagram CSIeW8lg-Pd\n", encoding="utf-8")
            with unittest.mock.patch.object(
                ig, "InstagramClient", side_effect=AssertionError("must not list profiles")
            ):
                code = ig.main(
                    [
                        "--discover-only",
                        "--profiles",
                        str(profiles),
                        "--queue",
                        str(queue),
                        "--out",
                        str(root / "downloads"),
                        "--archive",
                        str(archive),
                        "--cookies",
                        str(root / "missing-cookies.txt"),
                    ]
                )
            self.assertEqual(code, 0)
            self.assertTrue(queue.is_file())
            rows = [json.loads(line) for line in queue.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(rows[0]["id"], "CSIeW8lg-Pd")

    def test_cookies_from_browser_without_queue_only_exports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profiles = root / "profiles.txt"
            profiles.write_text("andres.ague\njohnny.guitars\n", encoding="utf-8")
            cookies = root / "cookies.txt"
            with unittest.mock.patch.object(ig, "find_yt_dlp", return_value=["yt-dlp"]):
                with unittest.mock.patch.object(ig, "export_browser_cookies") as export:
                    with unittest.mock.patch.object(
                        ig, "InstagramClient", side_effect=AssertionError("must not start a run")
                    ):
                        code = ig.main(
                            [
                                "--cookies-from-browser",
                                "chrome",
                                "--profiles",
                                str(profiles),
                                "--cookies",
                                str(cookies),
                                "--out",
                                str(root / "downloads"),
                                "--archive",
                                str(root / "archive.txt"),
                            ]
                        )
            self.assertEqual(code, 0)
            export.assert_called_once()

    def test_cookies_export_only_false_when_from_queue(self) -> None:
        args = unittest.mock.Mock(
            cookies_from_browser="chrome",
            from_queue=True,
            discover_only=False,
            profiles=Path("profiles.txt"),
        )
        self.assertFalse(ig.cookies_export_only(args))

    def test_from_queue_dry_run_skips_archived(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = root / "queue.jsonl"
            queue.write_text(self.SAMPLE_URL + "\n", encoding="utf-8")
            archive = root / "archive.txt"
            archive.write_text("instagram CSIeW8lg-Pd\n", encoding="utf-8")
            with unittest.mock.patch.object(
                ig, "InstagramClient", side_effect=AssertionError("must not refresh skipped")
            ):
                code = ig.main(
                    [
                        "--from-queue",
                        "--dry-run",
                        "--queue",
                        str(queue),
                        "--out",
                        str(root / "downloads"),
                        "--archive",
                        str(archive),
                        "--failed",
                        str(root / "failed.txt"),
                        "--cookies",
                        str(root / "missing-cookies.txt"),
                    ]
                )
            self.assertEqual(code, 0)

    def test_media_by_shortcode_falls_back_to_rest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cookies = Path(tmp) / "cookies.txt"
            cookies.write_text(
                "# Netscape HTTP Cookie File\n"
                ".instagram.com\tTRUE\t/\tTRUE\t9999999999\tsessionid\tabc\n",
                encoding="utf-8",
            )
            client = ig.InstagramClient(cookies, 0.0, retries=0)

            def gql_fail(_shortcode: str) -> dict:
                raise ig.InstagramError("instagram_api", "GraphQL media errors")

            rest_item = {
                "code": "Czdv17nrnpR",
                "media_type": 2,
                "video_versions": [
                    {
                        "url": "https://scontent.cdninstagram.com/v/t50/x.mp4",
                        "width": 720,
                        "height": 1280,
                        "type": 101,
                    }
                ],
            }

            with unittest.mock.patch.object(client, "_media_by_shortcode_graphql", gql_fail):
                with unittest.mock.patch.object(
                    client, "_media_by_shortcode_rest", return_value=rest_item
                ):
                    media = client.media_by_shortcode("Czdv17nrnpR")
            self.assertEqual(media["code"], "Czdv17nrnpR")
            self.assertTrue(ig.video_urls_from_media(media))

    def test_media_by_shortcode_does_not_hit_rest_on_throttle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cookies = Path(tmp) / "cookies.txt"
            cookies.write_text(
                "# Netscape HTTP Cookie File\n"
                ".instagram.com\tTRUE\t/\tTRUE\t9999999999\tsessionid\tabc\n",
                encoding="utf-8",
            )
            client = ig.InstagramClient(cookies, 0.0, retries=0)

            def gql_fail(_shortcode: str) -> dict:
                raise ig.InstagramError(
                    "instagram_http",
                    "Please wait a few minutes before you try again.",
                    http_status=429,
                )

            with unittest.mock.patch.object(client, "_media_by_shortcode_graphql", gql_fail):
                with unittest.mock.patch.object(
                    client,
                    "_media_by_shortcode_rest",
                    side_effect=AssertionError("REST skipped on throttle"),
                ):
                    with self.assertRaises(ig.InstagramError) as raised:
                        client.media_by_shortcode("Czdv17nrnpR")
            self.assertEqual(raised.exception.code, "instagram_http")
            self.assertTrue(ig.is_rate_limit(raised.exception))


if __name__ == "__main__":
    unittest.main()
