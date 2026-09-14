from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from voice_terminal.kws.keywords import (
    keyword_label,
    normalize_keyword,
    raw_keyword_line,
    strip_wake_prefix,
    validate_keywords_file,
    write_raw_keywords,
)


class KeywordTests(unittest.TestCase):
    def test_keyword_is_canonicalized_to_hei_claw(self) -> None:
        self.assertEqual(normalize_keyword("  HEI   CLAW "), "hei claw")
        self.assertEqual(keyword_label("hei claw"), "HEI_CLAW")
        self.assertEqual(raw_keyword_line("hei claw"), "HEI CLAW @HEI_CLAW")

    def test_wake_prefix_only_at_start_is_removed(self) -> None:
        self.assertEqual(strip_wake_prefix("hei claw 打开浏览器"), "打开浏览器")
        self.assertEqual(strip_wake_prefix("  HEI_CLAW: 打开浏览器"), "打开浏览器")
        self.assertEqual(strip_wake_prefix("请不要删除 hei claw"), "请不要删除 hei claw")

    def test_raw_file_can_be_checked(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "keywords_raw.txt"
            write_raw_keywords(path, "hei claw")
            self.assertEqual(path.read_text(encoding="utf-8").strip(), "HEI CLAW @HEI_CLAW")

            generated = Path(directory) / "keywords.txt"
            generated.write_text("h e i c l a w @HEI_CLAW\n", encoding="utf-8")
            validate_keywords_file(generated)

    def test_keyword_label_must_match_exactly(self) -> None:
        with TemporaryDirectory() as directory:
            generated = Path(directory) / "keywords.txt"
            generated.write_text("h e i c l a w @HEI_CLAW_EXTRA\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "HEI_CLAW"):
                validate_keywords_file(generated)


if __name__ == "__main__":
    unittest.main()
