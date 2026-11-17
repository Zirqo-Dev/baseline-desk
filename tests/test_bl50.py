"""Offline tests for BL-50 (display and docs only): the GeckoTerminal attribution (CoinGecko API Terms 4.3) in the
site and the README. No network."""
import re
import unittest
from pathlib import Path

from tests.test_site import PAGE

ROOT = Path(__file__).resolve().parents[1]
TEXT = "On-chain data provided by GeckoTerminal"
LINK = "https://www.geckoterminal.com"


class AttributionTest(unittest.TestCase):
    def test_site_line_after_foot(self):
        page = PAGE.read_text(encoding="utf-8")
        m = re.search(r'<p class="foot" id="attrib"><a href="([^"]+)"[^>]*>([^<]+)</a></p>', page)
        self.assertIsNotNone(m)
        self.assertEqual((m.group(1), m.group(2)), (LINK, TEXT))
        self.assertEqual(page.count(TEXT), 1)
        self.assertLess(page.index('id="foot"'), page.index('id="attrib"'))
        self.assertLess(page.index('id="attrib"'), page.index("</div>", page.index('id="attrib"')))
        self.assertNotIn("attrib", re.search(r"<script.*?</script>", page, re.S).group(0))  # not touched by the script

    def test_readme_line(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(f"Data sources: [{TEXT}]({LINK}).", readme)
        self.assertEqual(readme.count(TEXT), 1)


if __name__ == "__main__":
    unittest.main()
