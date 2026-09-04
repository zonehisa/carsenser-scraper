import subprocess
import unittest
from unittest.mock import patch

import scraper


class FakeCarLink:
    def __init__(self, href="https://example.test/usedcar/detail/1/"):
        self.href = href

    def get_attribute(self, name):
        return self.href if name == "href" else None


class FakePage:
    def goto(self, *args, **kwargs):
        return None

    def wait_for_selector(self, *args, **kwargs):
        return None

    def query_selector_all(self, *args, **kwargs):
        return [FakeCarLink()]

    def wait_for_timeout(self, *args, **kwargs):
        return None


class FakeContext:
    def new_page(self):
        return FakePage()


class FakeBrowser:
    def new_context(self, *args, **kwargs):
        return FakeContext()

    def close(self):
        return None


class FakeChromium:
    def launch(self, *args, **kwargs):
        return FakeBrowser()


class FakePlaywright:
    def __enter__(self):
        self.chromium = FakeChromium()
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class ScrapeInventoryTest(unittest.TestCase):
    def setUp(self):
        self.stability_wait_patcher = patch.object(scraper, "SHOP_LINK_STABILITY_WAIT", 0)
        self.stability_wait_patcher.start()
        self.addCleanup(self.stability_wait_patcher.stop)

    def test_retries_shop_page_until_success(self):
        class RetryPage:
            def __init__(self):
                self.goto_calls = 0
                self.goto_kwargs = []

            def goto(self, *args, **kwargs):
                self.goto_calls += 1
                self.goto_kwargs.append(kwargs)
                if self.goto_calls == 1:
                    raise TimeoutError("temporary timeout")

            def wait_for_selector(self, *args, **kwargs):
                return None

            def query_selector_all(self, *args, **kwargs):
                return [FakeCarLink()]

            def wait_for_timeout(self, *args, **kwargs):
                return None

        page = RetryPage()

        with (
            patch.object(scraper, "PlaywrightTimeout", TimeoutError),
            patch.object(scraper, "SHOP_MAX_RETRIES", 2),
            patch.object(scraper, "RETRY_DELAY", 0),
            patch.object(scraper, "SHOP_WAIT_UNTIL", "domcontentloaded"),
            patch.object(scraper.time, "sleep"),
        ):
            self.assertTrue(scraper.load_shop_page_with_retries(page))

        self.assertEqual(page.goto_calls, 2)
        self.assertEqual(
            [kwargs["wait_until"] for kwargs in page.goto_kwargs],
            ["domcontentloaded", "domcontentloaded"],
        )

    def test_retries_shop_page_when_car_links_are_not_ready(self):
        class SelectorRetryPage:
            def __init__(self):
                self.goto_calls = 0
                self.selector_calls = 0

            def goto(self, *args, **kwargs):
                self.goto_calls += 1

            def wait_for_selector(self, *args, **kwargs):
                self.selector_calls += 1
                if self.selector_calls == 1:
                    raise TimeoutError("car links are not ready")

            def query_selector_all(self, *args, **kwargs):
                return [FakeCarLink()]

            def wait_for_timeout(self, *args, **kwargs):
                return None

        page = SelectorRetryPage()

        with (
            patch.object(scraper, "PlaywrightTimeout", TimeoutError),
            patch.object(scraper, "SHOP_MAX_RETRIES", 2),
            patch.object(scraper, "RETRY_DELAY", 0),
            patch.object(scraper.time, "sleep"),
        ):
            self.assertTrue(scraper.load_shop_page_with_retries(page))

        self.assertEqual(page.goto_calls, 2)
        self.assertEqual(page.selector_calls, 2)

    def test_waits_until_car_link_count_is_stable(self):
        class DelayedLinksPage:
            def __init__(self):
                self.counts = iter([1, 3, 3])

            def query_selector_all(self, *args, **kwargs):
                count = next(self.counts)
                return [FakeCarLink(f"https://example.test/usedcar/detail/{i}/") for i in range(count)]

            def wait_for_timeout(self, *args, **kwargs):
                return None

        with patch.object(scraper, "SHOP_LINK_STABILITY_WAIT", 0):
            self.assertTrue(scraper.wait_for_stable_car_links(DelayedLinksPage()))

    def test_stops_after_shop_page_retry_limit(self):
        class AlwaysTimeoutPage:
            def __init__(self):
                self.goto_calls = 0

            def goto(self, *args, **kwargs):
                self.goto_calls += 1
                raise TimeoutError("persistent timeout")

            def wait_for_selector(self, *args, **kwargs):
                return None

            def wait_for_timeout(self, *args, **kwargs):
                return None

        page = AlwaysTimeoutPage()

        with (
            patch.object(scraper, "PlaywrightTimeout", TimeoutError),
            patch.object(scraper, "SHOP_MAX_RETRIES", 2),
            patch.object(scraper, "RETRY_DELAY", 0),
            patch.object(scraper.time, "sleep"),
        ):
            self.assertFalse(scraper.load_shop_page_with_retries(page))

        self.assertEqual(page.goto_calls, 2)

    def test_returns_empty_when_shop_page_fails_after_retries(self):
        with (
            patch.object(scraper, "sync_playwright", return_value=FakePlaywright()),
            patch.object(scraper, "load_shop_page_with_retries", return_value=False),
            patch.object(scraper, "extract_car_links") as extract_links,
        ):
            self.assertEqual(scraper.scrape_inventory(), [])

        extract_links.assert_not_called()

    def test_returns_inventory_when_all_detail_pages_succeed(self):
        car_links = [
            "https://example.test/usedcar/detail/1/",
            "https://example.test/usedcar/detail/2/",
        ]
        cars = [
            {"name": "車両1", "detail_link": car_links[0]},
            {"name": "車両2", "detail_link": car_links[1]},
        ]

        with (
            patch.object(scraper, "sync_playwright", return_value=FakePlaywright()),
            patch.object(scraper, "extract_car_links", return_value=car_links),
            patch.object(scraper, "extract_car_details", side_effect=cars),
            patch.object(scraper, "DETAIL_MAX_RETRIES", 1),
            patch.object(scraper.time, "sleep"),
        ):
            self.assertEqual(scraper.scrape_inventory(), cars)

    def test_retries_detail_page_until_success(self):
        car_links = ["https://example.test/usedcar/detail/1/"]
        car_data = {"name": "車両1", "detail_link": car_links[0]}

        with (
            patch.object(scraper, "sync_playwright", return_value=FakePlaywright()),
            patch.object(scraper, "extract_car_links", return_value=car_links),
            patch.object(scraper, "extract_car_details", side_effect=[None, car_data]) as extract_details,
            patch.object(scraper, "DETAIL_MAX_RETRIES", 2),
            patch.object(scraper, "RETRY_DELAY", 0),
            patch.object(scraper.time, "sleep"),
        ):
            self.assertEqual(scraper.scrape_inventory(), [car_data])
            self.assertEqual(extract_details.call_count, 2)

    def test_returns_empty_when_any_detail_page_fails_after_retries(self):
        car_links = [
            "https://example.test/usedcar/detail/1/",
            "https://example.test/usedcar/detail/2/",
        ]
        car_data = {
            "name": "車両1",
            "detail_link": car_links[0],
        }

        with (
            patch.object(scraper, "sync_playwright", return_value=FakePlaywright()),
            patch.object(scraper, "extract_car_links", return_value=car_links),
            patch.object(scraper, "extract_car_details", side_effect=[car_data, None, None]),
            patch.object(scraper, "DETAIL_MAX_RETRIES", 2),
            patch.object(scraper, "RETRY_DELAY", 0),
            patch.object(scraper.time, "sleep"),
        ):
            self.assertEqual(scraper.scrape_inventory(), [])


class GitCommitAndPushTest(unittest.TestCase):
    def test_returns_success_without_commit_when_inventory_file_has_no_staged_diff(self):
        calls = []

        def fake_run(args, **kwargs):
            calls.append(args)
            if args == ['git', 'diff', '--cached', '--quiet', '--', scraper.OUTPUT_FILE]:
                return subprocess.CompletedProcess(args, 0)
            return subprocess.CompletedProcess(args, 0, stdout="")

        with (
            patch.dict(scraper.os.environ, {"GITHUB_TOKEN": "token"}),
            patch.object(scraper.subprocess, "run", side_effect=fake_run),
        ):
            self.assertTrue(scraper.git_commit_and_push())

        self.assertIn(['git', 'add', scraper.OUTPUT_FILE], calls)
        self.assertNotIn(['git', 'commit', '-m'], [call[:3] for call in calls])
        self.assertNotIn(['git', 'push'], calls)

    def test_commits_and_pushes_when_inventory_file_has_staged_diff(self):
        calls = []

        def fake_run(args, **kwargs):
            calls.append(args)
            if args == ['git', 'diff', '--cached', '--quiet', '--', scraper.OUTPUT_FILE]:
                return subprocess.CompletedProcess(args, 1)
            if args == ['git', 'config', '--get', 'remote.origin.url']:
                return subprocess.CompletedProcess(args, 0, stdout="git@github.com:example/repo.git\n")
            return subprocess.CompletedProcess(args, 0, stdout="")

        with (
            patch.dict(scraper.os.environ, {"GITHUB_TOKEN": "token"}),
            patch.object(scraper.subprocess, "run", side_effect=fake_run),
        ):
            self.assertTrue(scraper.git_commit_and_push())

        self.assertIn(['git', 'add', scraper.OUTPUT_FILE], calls)
        self.assertTrue(any(call[:3] == ['git', 'commit', '-m'] for call in calls))
        self.assertIn(['git', 'push'], calls)


class MainTest(unittest.TestCase):
    def test_does_not_save_or_push_when_scrape_returns_empty(self):
        with (
            patch.object(scraper, "scrape_inventory", return_value=[]),
            patch.object(scraper, "save_to_json") as save_to_json,
            patch.object(scraper, "git_commit_and_push") as git_commit_and_push,
        ):
            self.assertEqual(scraper.main(), 1)

        save_to_json.assert_not_called()
        git_commit_and_push.assert_not_called()


if __name__ == "__main__":
    unittest.main()
