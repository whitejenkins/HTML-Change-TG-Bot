import importlib
import os
import sys
import tempfile
import types
import unittest


def install_dependency_stubs():
    requests = types.ModuleType("requests")
    sys.modules.setdefault("requests", requests)

    bs4 = types.ModuleType("bs4")
    bs4.BeautifulSoup = lambda *args, **kwargs: None
    sys.modules.setdefault("bs4", bs4)

    playwright = types.ModuleType("playwright")
    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.sync_playwright = lambda: None
    sys.modules.setdefault("playwright", playwright)
    sys.modules.setdefault("playwright.sync_api", sync_api)


class MonitorChangeDetectionTest(unittest.TestCase):
    def setUp(self):
        install_dependency_stubs()
        os.environ["DATA_DIR"] = tempfile.mkdtemp()
        os.environ["TELEGRAM_BOT_TOKEN"] = "token"
        os.environ["TELEGRAM_CHAT_ID"] = "123"
        os.environ["ALLOW_NON_SETAM_URLS"] = "true"
        sys.modules.pop("monitor", None)
        self.monitor = importlib.import_module("monitor")
        self.sent = []
        self.monitor.tg_send = lambda chat_id, text: self.sent.append((chat_id, text))
        self.monitor.send_page_screenshot = lambda *args, **kwargs: None

    def test_page_change_notifies_even_when_lot_numbers_do_not_change(self):
        self.monitor.update_config(notify_chat_id="123", send_screenshot_on_change=False)
        state = self.monitor.get_state()
        state["pages"] = {"1": {"url": "https://example.com"}}
        self.monitor.save_state_unlocked(state)

        html_versions = iter(["first", "second"])
        self.monitor.fetch_html = lambda url, timeout: next(html_versions)
        self.monitor.normalize_html = lambda html, ignore_patterns: (f"same visible lot count, offer={html}", ["42"])

        first = self.monitor.check_page("1")
        second = self.monitor.check_page("1")

        self.assertIn("initialized", first)
        self.assertIn("changed", second)
        self.assertEqual(len(self.monitor.get_state()["pages"]["1"]["lots"]), 1)
        self.assertTrue(any("page #1 changed" in text for _, text in self.sent))


if __name__ == "__main__":
    unittest.main()
