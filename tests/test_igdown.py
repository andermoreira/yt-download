import tempfile
import unittest
import unittest.mock
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
        self.assertFalse(
            ig.is_retryable(ig.InstagramError("instagram_http", "x", http_status=404))
        )
        self.assertFalse(ig.is_retryable(ig.InstagramError("instagram_auth_required", "x")))

    def test_backoff_prefers_retry_after(self) -> None:
        self.assertEqual(ig.backoff_seconds(0, 1.5, 12.0), 12.0)
        self.assertEqual(ig.backoff_seconds(2, 1.5, None), 6.0)

    def test_should_stop_after_existing(self) -> None:
        self.assertTrue(
            ig.should_stop_after_existing(3, pinned=False, full=False, threshold=3)
        )
        self.assertFalse(
            ig.should_stop_after_existing(3, pinned=True, full=False, threshold=3)
        )
        self.assertFalse(
            ig.should_stop_after_existing(3, pinned=False, full=True, threshold=3)
        )

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


if __name__ == "__main__":
    unittest.main()
