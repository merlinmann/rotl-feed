import unittest
from datetime import datetime, timezone
from email.message import Message
from unittest import mock
from xml.etree import ElementTree as ET

import update
import healthcheck


class FakeResponse:
    def __init__(self, content_type, content_range="bytes 0-0/123", code=206):
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self.headers["Content-Range"] = content_range
        self.code = code

    def getcode(self):
        return self.code

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class FeedToolsTests(unittest.TestCase):
    def test_archive_filename_normalizes_episode_zero(self):
        self.assertEqual(
            update.archive_filename(
                "http://podtrac.test/redirect/www.example.test/rotl_e000.mp3"
            ),
            "rotl_0000.mp3",
        )

    def test_archive_filename_preserves_bonus(self):
        self.assertEqual(
            update.archive_filename("https://example.test/ballew-rotl-song.mp3"),
            "ballew-rotl-song.mp3",
        )

    def test_archive_filename_normalizes_historical_aliases(self):
        cases = {
            "rotl_0580a.mp3": "rotl_0580.mp3",
            "rotl_0047_esquivalience.mp3": "rotl_0047.mp3",
            "b2w-031.mp3": "rotl_0046.mp3",
            "rotl-0018.mp3": "rotl_0018.mp3",
        }
        for old, expected in cases.items():
            with self.subTest(old=old):
                self.assertEqual(
                    update.archive_filename(f"https://example.test/{old}"), expected
                )

    @mock.patch("update.urllib.request.urlopen")
    def test_mp3_length_rejects_html_partial_response(self, urlopen):
        urlopen.return_value = FakeResponse("text/html", "bytes 0-0/7745")
        self.assertIsNone(update.mp3_length("https://example.test/outage.mp3", {}))

    @mock.patch("update.urllib.request.urlopen")
    def test_mp3_length_accepts_audio_partial_response(self, urlopen):
        urlopen.return_value = FakeResponse("audio/mpeg", "bytes 0-0/58814046")
        self.assertEqual(
            update.mp3_length("https://example.test/rotl_0632.mp3", {}),
            58814046,
        )

    @mock.patch("update.urllib.request.urlopen")
    def test_mp3_length_rejects_partial_response_without_total(self, urlopen):
        urlopen.return_value = FakeResponse("audio/mpeg", "", code=206)
        urlopen.return_value.headers["Content-Length"] = "1"
        self.assertIsNone(update.mp3_length("https://example.test/partial.mp3", {}))

    @mock.patch("update.mp3_length", return_value=123)
    def test_migration_requires_matching_declared_size(self, _length):
        channel = ET.fromstring(
            "<channel><item><title>Ep. 1</title>"
            '<enclosure url="https://example.test/rotl_0001.mp3" length="456" />'
            "</item></channel>"
        )
        self.assertEqual(update.migrate_ready_enclosures(channel, {}), [])
        self.assertEqual(
            channel.find("item/enclosure").get("url"),
            "https://example.test/rotl_0001.mp3",
        )

    @mock.patch("update.mp3_length")
    def test_migration_preserves_squarespace_fallback(self, length):
        channel = ET.fromstring(
            "<channel><item><title>Ep. 152</title>"
            '<enclosure url="https://dts.podtrac.com/redirect.mp3/'
            'www.merlinmann.com/storage/rotl/rotl_0152.mp3" length="43778779" />'
            "</item></channel>"
        )
        self.assertEqual(update.migrate_ready_enclosures(channel, {}), [])
        length.assert_not_called()
        self.assertIn(
            "dts.podtrac.com",
            channel.find("item/enclosure").get("url"),
        )

    @mock.patch("healthcheck.urllib.request.urlopen")
    def test_healthcheck_rejects_html_partial_response(self, urlopen):
        urlopen.return_value = FakeResponse("text/html", "bytes 0-0/7745")
        data = (
            "<rss><channel><item><title>Ep. 1</title><guid>one</guid>"
            '<enclosure url="https://radio.contiguous.me/rotl/episodes/rotl_0001.mp3" '
            'length="7745" /></item></channel></rss>'
        ).encode()
        failures = []
        healthcheck.check_media(data, failures)
        self.assertTrue(any("Content-Type text/html" in failure for failure in failures))

    @mock.patch("healthcheck.urllib.request.urlopen")
    def test_healthcheck_allows_playable_enclosure_awaiting_archive(self, urlopen):
        urlopen.return_value = FakeResponse(
            "audio/mpeg", "bytes 0-0/59684053"
        )
        data = (
            "<rss><channel><item><title>Ep. 636</title><guid>new</guid>"
            "<pubDate>Tue, 11 Aug 2026 03:26:55 +0000</pubDate>"
            '<enclosure url="https://www.merlinmann.com/storage/rotl/rotl_0636.mp3" '
            'length="59684053" /></item></channel></rss>'
        ).encode()
        failures = []
        healthcheck.check_media(
            data,
            failures,
            now=datetime(2026, 8, 11, 16, tzinfo=timezone.utc),
        )
        self.assertEqual(failures, [])
        request = urlopen.call_args.args[0]
        self.assertEqual(request.headers["Range"], "bytes=0-0")

    @mock.patch("healthcheck.urllib.request.urlopen")
    def test_healthcheck_rejects_broken_enclosure_during_archive_grace(self, urlopen):
        urlopen.return_value = FakeResponse("text/html", "bytes 0-0/7745")
        data = (
            "<rss><channel><item><title>Ep. 636</title><guid>new</guid>"
            "<pubDate>Tue, 11 Aug 2026 03:26:55 +0000</pubDate>"
            '<enclosure url="https://www.merlinmann.com/storage/rotl/rotl_0636.mp3" '
            'length="7745" /></item></channel></rss>'
        ).encode()
        failures = []
        healthcheck.check_media(
            data,
            failures,
            now=datetime(2026, 8, 11, 16, tzinfo=timezone.utc),
        )
        self.assertTrue(any("Content-Type text/html" in failure for failure in failures))

    @mock.patch("healthcheck.urllib.request.urlopen")
    def test_healthcheck_rejects_old_playable_enclosure_off_archive(self, urlopen):
        urlopen.return_value = FakeResponse("audio/mpeg", "bytes 0-0/123")
        data = (
            "<rss><channel><item><title>Ep. 1</title><guid>old</guid>"
            "<pubDate>Wed, 22 Jun 2011 00:00:00 +0000</pubDate>"
            '<enclosure url="https://www.merlinmann.com/storage/rotl/rotl_0001.mp3" '
            'length="123" /></item></channel></rss>'
        ).encode()
        failures = []
        healthcheck.check_media(
            data,
            failures,
            now=datetime(2026, 8, 11, 16, tzinfo=timezone.utc),
        )
        self.assertTrue(
            any("not on radio.contiguous.me after archive grace" in failure
                for failure in failures)
        )

    def test_feedburner_same_title_with_stale_enclosure_fails_after_grace(self):
        local = (
            "<rss><channel><item><title>Ep. 635</title><guid>same</guid>"
            '<enclosure url="https://radio.example/rotl_0635.mp3" length="100" />'
            "</item></channel></rss>"
        ).encode()
        stale = local.replace(b"radio.example", b"squarespace.example")
        failures = []
        healthcheck.check_feedburner_enclosures(local, stale, failures, age_min=91)
        self.assertEqual(failures, ["media[FeedBurner]: enclosure catalog is stale"])

    def test_feedburner_stale_enclosure_gets_polling_grace(self):
        local = (
            "<rss><channel><item><title>Ep. 635</title><guid>same</guid>"
            '<enclosure url="https://radio.example/rotl_0635.mp3" length="100" />'
            "</item></channel></rss>"
        ).encode()
        stale = local.replace(b"radio.example", b"squarespace.example")
        failures = []
        healthcheck.check_feedburner_enclosures(local, stale, failures, age_min=30)
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
