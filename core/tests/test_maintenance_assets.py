from html.parser import HTMLParser
from pathlib import Path

from django.test import SimpleTestCase


ROOT = Path(__file__).resolve().parents[2]
MIGRATION_ROOT = ROOT / "docker" / "migration"


class _DocumentParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.text = []
        self.script_has_inline_body = False
        self.style_count = 0
        self._inside_script = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        self.tags.append((tag, attributes))
        if tag == "script":
            self._inside_script = True
        if tag == "style":
            self.style_count += 1

    def handle_endtag(self, tag):
        if tag == "script":
            self._inside_script = False

    def handle_data(self, data):
        stripped = data.strip()
        if stripped:
            self.text.append(stripped)
            if self._inside_script:
                self.script_has_inline_body = True


class MaintenanceAssetTests(SimpleTestCase):
    maxDiff = None

    def asset(self, name):
        path = MIGRATION_ROOT / name
        self.assertTrue(path.is_file(), "%s 자산이 필요합니다." % name)
        return path

    def document(self):
        parser = _DocumentParser()
        parser.feed(self.asset("index.html").read_text(encoding="utf-8"))
        return parser

    def test_assets_are_present(self):
        for name in (
            "index.html",
            "migration.css",
            "migration.js",
            "svrx-pinry-dark-ui.png",
            "svrx-pinry-light-ui.png",
        ):
            with self.subTest(name=name):
                self.asset(name)

    def test_page_has_required_korean_fallback_and_accessibility(self):
        html = self.asset("index.html").read_text(encoding="utf-8")
        parser = self.document()
        flattened_text = " ".join(parser.text)

        for text in (
            "기존 Pinry 데이터를 이전하고 있습니다.",
            "브라우저를 닫아도 작업은 계속됩니다.",
            "컨테이너를 중지하지 마세요.",
            "데이터 폴더를 삭제하거나 수정하지 마세요.",
            "Container Manager 로그를 확인하세요.",
        ):
            self.assertIn(text, flattened_text)

        html_tags = [attrs for tag, attrs in parser.tags if tag == "html"]
        self.assertEqual(html_tags[0].get("lang"), "ko")
        self.assertIn('aria-live="polite"', html)

        labels = {
            attrs.get("id"): next(
                (
                    text
                    for text in parser.text
                    if text in ("전체 이전 진행률", "현재 단계 진행률")
                ),
                None,
            )
            for tag, attrs in parser.tags
            if tag == "span"
            and attrs.get("id")
            in ("overall-progress-label", "phase-progress-label")
        }
        progress = [attrs for tag, attrs in parser.tags if tag == "progress"]
        self.assertEqual(len(progress), 2)
        self.assertEqual({item.get("max") for item in progress}, {"100"})
        self.assertEqual(
            {item.get("aria-labelledby") for item in progress},
            {"overall-progress-label", "phase-progress-label"},
        )
        self.assertEqual(
            set(labels), {"overall-progress-label", "phase-progress-label"}
        )

        images = [attrs for tag, attrs in parser.tags if tag == "img"]
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0].get("alt"), "SVRx Pinry")

    def test_page_uses_external_assets_and_color_scheme_logos(self):
        parser = self.document()
        stylesheets = [
            attrs.get("href")
            for tag, attrs in parser.tags
            if tag == "link" and attrs.get("rel") == "stylesheet"
        ]
        scripts = [attrs for tag, attrs in parser.tags if tag == "script"]
        sources = [attrs for tag, attrs in parser.tags if tag == "source"]

        self.assertEqual(stylesheets, ["/migration/migration.css"])
        self.assertEqual(len(scripts), 1)
        self.assertEqual(scripts[0].get("src"), "/migration/migration.js")
        self.assertIn("defer", scripts[0])
        self.assertFalse(parser.script_has_inline_body)
        self.assertEqual(parser.style_count, 0)
        self.assertEqual(
            {(item.get("media"), item.get("srcset")) for item in sources},
            {
                (
                    "(prefers-color-scheme: dark)",
                    "/migration/svrx-pinry-dark-ui.png",
                ),
                (
                    "(prefers-color-scheme: light)",
                    "/migration/svrx-pinry-light-ui.png",
                ),
            },
        )

    def test_action_order_matches_keyboard_flow(self):
        parser = self.document()
        button_text = []
        html = self.asset("index.html").read_text(encoding="utf-8")
        for expected in (
            "상태 다시 확인",
            "오류 코드 복사",
            "진단 정보 복사",
            "SVRx Pinry 열기",
        ):
            button_text.append((html.index(expected), expected))
        self.assertEqual(
            [item[1] for item in sorted(button_text)],
            [
                "상태 다시 확인",
                "오류 코드 복사",
                "진단 정보 복사",
                "SVRx Pinry 열기",
            ],
        )
        self.assertEqual(
            len([attrs for tag, attrs in parser.tags if tag == "button"]), 5
        )

    def test_css_supports_mobile_zoom_focus_and_reduced_motion(self):
        css = self.asset("migration.css").read_text(encoding="utf-8")
        for contract in (
            "max-width: 880px",
            "grid-template-columns: repeat(3, minmax(0, 1fr))",
            "@media (max-width: 640px)",
            "grid-template-columns: 1fr",
            ":focus-visible",
            "outline: 3px solid",
            "overflow-wrap: anywhere",
            "@media (prefers-reduced-motion: reduce)",
            "transition-duration: 0.01ms",
            "animation-duration: 0.01ms",
        ):
            self.assertIn(contract, css)

    def test_migration_logos_are_exact_copies_of_spa_assets(self):
        source_root = ROOT / "pinry-spa" / "src" / "assets"
        for name in ("svrx-pinry-dark-ui.png", "svrx-pinry-light-ui.png"):
            with self.subTest(name=name):
                self.assertEqual(
                    self.asset(name).read_bytes(), (source_root / name).read_bytes()
                )
