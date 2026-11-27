import json
import hashlib
from datetime import date
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET
import publisher


class PublisherTests(unittest.TestCase):
    def test_rerecording_one_language_preserves_verified_other_recording(self):
        with tempfile.TemporaryDirectory() as d:
            site = Path(d); (site / "episodes").mkdir()
            audio = site / "episodes/Homily_2026-11-26_English.mp3"
            audio.write_bytes(b"original English recording")
            info = {"sha256": hashlib.sha256(audio.read_bytes()).hexdigest(), "voice": "Leda", "reader": "English female"}
            old = {"date": "2026-11-26", "audio": {"English": info}}
            self.assertEqual(publisher.preserved_audio(site, old, ["Malayalam"]), {"English": info})
            self.assertEqual(publisher.preserved_audio(site, {}, ["English", "Malayalam"]), {})
            audio.write_bytes(b"unexpected replacement")
            with self.assertRaises(ValueError):
                publisher.preserved_audio(site, old, ["Malayalam"])

    def test_ten_minute_limit_is_measured_from_homily_chapter(self):
        publisher.validate_audio_duration(727.9, 128, 599.8)
        publisher.validate_audio_duration(1119, 600, 519)
        for duration, offset, homily in [(728.01, 128, 599.9), (730, 128, 602), (float('nan'), 128, 500)]:
            with self.assertRaises(ValueError):
                publisher.validate_audio_duration(duration, offset, homily)

    def test_calendar_selects_exact_month_day_and_stops_at_next_day(self):
        text = "October 2026\n26 Mon Wrong day\nNovember 2026\n25 Wed Previous\n26 Thu Fourth Thursday of Dedication of the Church\nHeb 13:9-16 Theme\nLk 10:17-21 Gospel\n27 Fri Next\n"
        entry = publisher.calendar_entry(text, date(2026, 11, 26))
        self.assertEqual(len(entry), 3)
        self.assertEqual(entry[-1], "Lk 10:17-21 Gospel")
        with self.assertRaises(ValueError):
            publisher.calendar_entry(text, date(2026, 12, 26))

    def test_scripture_ranges_do_not_invent_missing_verses(self):
        books = {"EPH": {(5, 21): "one", (5, 22): "two", (6, 1): "three", (6, 2): "four"}}
        self.assertEqual(publisher.passage(books, "Eph 5:21-6:1"), "one\ntwo\nthree")
        self.assertEqual(publisher.passage(books, "Eph 5:21,22;6:2"), "one\ntwo\nfour")
        self.assertEqual(publisher.passage(books, "Eph 5:21 + 6:1-2"), "one\nthree\nfour")
        for invalid in ["Eph 5:20-22", "Eph 5:22-21", "Sir 1:1-3"]:
            with self.assertRaises(ValueError):
                publisher.passage(books, invalid)

    def test_calendar_keeps_complete_partial_and_compound_references(self):
        reading = publisher.reading_from_line("Jn 13:31b-35 Love is the sign of Christ's disciple.")
        self.assertEqual(reading["reference"], "Jn 13:31b-35")
        self.assertEqual(reading["spoken_reference"], "Jn 13:31-35")
        self.assertTrue(reading["whole_verse_expansion"])
        self.assertEqual(publisher.reading_from_line("Heb 1:1-3 + 2:16-18 Son of God")["reference"], "Heb 1:1-3 + 2:16-18")
        self.assertEqual(publisher.reading_from_line("Mat 2:1-12 The visit of Magi")["book"], "MAT")
        self.assertEqual(publisher.reading_from_line("Gen 17:15-22 (17:1-27) Announcement")["reference"], "Gen 17:15-22")

    def test_new_recording_has_versioned_audio_and_matching_skip_point(self):
        with tempfile.TemporaryDirectory() as d:
            site = Path(d); ep = site / "episodes"; ep.mkdir()
            (ep / "Homily_2026-11-26_Malayalam.mp3").write_bytes(b"test-audio")
            metadata = {"date": "2026-11-26", "liturgical_day": "Today", "readings": "Lk 10:17-21", "audio": {"Malayalam": {"sha256": "a"*64, "homily_start_seconds": 123.456, "duration_seconds": 720, "reader": "Google Leda", "scripture_translation": "Licensed Malayalam", "scripture_license": "CC BY-SA 4.0"}}}
            (ep / "2026-11-26.json").write_text(json.dumps(metadata))
            publisher.build_feed(site)
            root = ET.fromstring((site / "feed.xml").read_text())
            item = root.find("channel/item")
            self.assertIn("rev=" + "a"*64, item.find("enclosure").get("url"))
            self.assertEqual(item.find("{https://jdayalu.github.io/homily-podcast/ns/1.0}homilyStart").text, "123.456")
            self.assertIn("Skip to homily", (site / "index.html").read_text())

    def test_audio_chunking_preserves_all_script_words(self):
        text = ("ഈശോമിശിഹായിൽ പ്രിയ സഹോദരീസഹോദരന്മാരേ. " * 70) + "\n\n" + "Amen. "*40
        chunks = publisher.chunks(text)
        self.assertEqual(" ".join(chunks).split(), text.split())
        self.assertTrue(all(len(chunk) <= 1500 for chunk in chunks))


if __name__ == "__main__": unittest.main()
