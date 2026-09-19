from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from voice_terminal.kws.keywords import (
    MAX_WAKE_KEYWORDS,
    canonical_labels,
    keyword_label,
    normalize_keyword,
    normalize_keyword_list,
    raw_keyword_line,
    strip_wake_prefix,
    validate_keywords_file,
    write_raw_keywords,
    write_raw_keywords_multi,
)


class KeywordTests(unittest.TestCase):
    def test_keyword_is_canonicalized_to_hei_claw(self) -> None:
        self.assertEqual(normalize_keyword("  HEI   CLAW "), "hei claw")
        self.assertEqual(keyword_label("hei claw"), "HEI_CLAW")
        self.assertEqual(raw_keyword_line("hei claw"), "HEI CLAW @HEI_CLAW")

    def test_chinese_keyword_keeps_text_and_label(self) -> None:
        self.assertEqual(normalize_keyword(" 嘿，小爪 "), "嘿 小爪")
        self.assertEqual(keyword_label("嘿，小爪"), "嘿_小爪")
        self.assertEqual(raw_keyword_line("嘿，小爪"), "嘿 小爪 @嘿_小爪")

    def test_wake_prefix_only_at_start_is_removed(self) -> None:
        self.assertEqual(strip_wake_prefix("hei claw 打开浏览器"), "打开浏览器")
        self.assertEqual(strip_wake_prefix("  HEI_CLAW: 打开浏览器"), "打开浏览器")
        self.assertEqual(strip_wake_prefix("嘿小爪打开浏览器", "嘿，小爪"), "打开浏览器")
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

    def test_keyword_list_dedupes_bounds_and_normalizes(self) -> None:
        self.assertEqual(MAX_WAKE_KEYWORDS, 5)
        self.assertEqual(
            normalize_keyword_list([" 七三 ", "你好七三", "七三", "嘿，小爪"]),
            ("七三", "你好七三", "嘿 小爪"),
        )
        # Case and separator variants of one phrase collapse to a single entry.
        self.assertEqual(normalize_keyword_list(["hei claw", "HEI_CLAW", "hei-claw"]), ("hei claw",))
        with self.assertRaisesRegex(ValueError, "至少填写 1 个"):
            normalize_keyword_list(["", "   "])
        with self.assertRaisesRegex(ValueError, "最多支持 5 个"):
            normalize_keyword_list(["一", "二", "三", "四", "五", "六"])
        with self.assertRaisesRegex(ValueError, "过长"):
            normalize_keyword_list(["x" * 121])
        with self.assertRaisesRegex(ValueError, "字符串"):
            normalize_keyword_list(["七三", 7])

    def test_canonical_labels_follow_the_phrase_order(self) -> None:
        self.assertEqual(canonical_labels(["七三", "你好七三", "嘿，小爪"]), ("七三", "你好七三", "嘿_小爪"))
        # Two spellings that share a label are reported once.
        self.assertEqual(canonical_labels(["hei claw", "HEI_CLAW"]), ("HEI_CLAW",))

    def test_multi_line_raw_and_compiled_files_validate_every_label(self) -> None:
        with TemporaryDirectory() as directory:
            raw = Path(directory) / "keywords_raw.txt"
            write_raw_keywords_multi(raw, (("七三", "七三"), ("你好七三", "你好七三")))
            self.assertEqual(
                raw.read_text(encoding="utf-8").strip().splitlines(),
                ["七三 @七三", "你好七三 @你好七三"],
            )

            compiled = Path(directory) / "keywords.txt"
            compiled.write_text("q ī s ān @七三\nn ǐ h ǎo q ī s ān @你好七三\n", encoding="utf-8")
            validate_keywords_file(compiled, ["七三", "你好七三"])
            validate_keywords_file(compiled, ("七三",))

            # One missing spelling names itself in the error, because that is
            # the 现场 failure mode after editing YAML without re-saving.
            with self.assertRaisesRegex(ValueError, "@七三同学"):
                validate_keywords_file(compiled, ["七三", "七三同学"])

            # A Notepad-saved BOM must not turn into a phantom token.
            compiled.write_text("\ufeffq ī s ān @七三\n", encoding="utf-8")
            validate_keywords_file(compiled, ["七三"])

    def test_wake_prefix_strips_the_longest_configured_phrase(self) -> None:
        phrases = ["七三", "你好七三", "七三同学"]
        self.assertEqual(strip_wake_prefix("七三同学，帮我查下天气", phrases), "帮我查下天气")
        self.assertEqual(strip_wake_prefix("你好七三打开浏览器", phrases), "打开浏览器")
        self.assertEqual(strip_wake_prefix("七三 打开浏览器", phrases), "打开浏览器")
        # Nothing matches: the text is returned untouched.
        self.assertEqual(strip_wake_prefix("今天天气怎么样", phrases), "今天天气怎么样")
        # A malformed entry must not break stripping for the valid ones.
        self.assertEqual(strip_wake_prefix("七三你好", ["七三", " "]), "你好")


if __name__ == "__main__":
    unittest.main()
