from __future__ import annotations

import unittest

from jin10_market_report.text_utils import CODEX_MANAGER_LINK_PROBE, strip_html


class TestCodexManagerLink(unittest.TestCase):
    def test_link_probe_constant(self) -> None:
        self.assertEqual(CODEX_MANAGER_LINK_PROBE, "codex-manager-local-link-ok")

    def test_strip_html_basic(self) -> None:
        self.assertEqual(strip_html("<b>hi</b>"), "hi")


if __name__ == "__main__":
    unittest.main()
