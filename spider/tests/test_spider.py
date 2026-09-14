from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import spider11


class FakeResponse:
    def __init__(
        self,
        status: int,
        body: str = "",
        url: str = "https://bgn.bosch.com/test.html",
    ) -> None:
        self.status = status
        self.url = url
        self.headers = {"content-type": "text/html; charset=utf-8"}
        self._body = body
        self.disposed = False

    async def text(self) -> str:
        return self._body

    async def dispose(self) -> None:
        self.disposed = True


class FakeRequestContext:
    def __init__(self, response: FakeResponse | None = None) -> None:
        self.response = response
        self.calls = 0

    async def get(self, *_args, **_kwargs) -> FakeResponse:
        self.calls += 1
        if self.response is None:
            raise AssertionError("The API request path should not be used.")
        return self.response


class FakeBrowserContext:
    def __init__(self, request: FakeRequestContext) -> None:
        self.request = request


class FakeNavigationResponse:
    def __init__(self, status: int, url: str, body: str) -> None:
        self.status = status
        self.url = url
        self.body = body

    async def text(self) -> str:
        return self.body


class FakeLocator:
    def __init__(self, page: FakeRenderPage, selector: str) -> None:
        self.page = page
        self.selector = selector

    async def count(self) -> int:
        html_text = self.page.html_text
        if self.selector == spider11.PAGE_CONTENT_SELECTOR:
            lowered = html_text.casefold()
            return int(
                'id="pagecontent"' in lowered
                or "id='pagecontent'" in lowered
                or "<main" in lowered
                or "role=\"main\"" in lowered
                or "role='main'" in lowered
                or 'id="pagecontainer"' in lowered
                or "id='pagecontainer'" in lowered
                or "main-content" in lowered
                or "page-content" in lowered
            )
        if self.selector == "#pageContent":
            return int(spider11.html_has_page_content(html_text))
        if "input[type='password']" in self.selector:
            lowered = html_text.casefold()
            return int(
                "type=\"password\"" in lowered
                or "type='password'" in lowered
                or "action=\"/login" in lowered
                or "action='/login" in lowered
            )
        return 0


class FakeRenderPage:
    def __init__(
        self,
        responses: dict[str, tuple[int, str, str | None]],
        *,
        dom_overrides: dict[str, str] | None = None,
        response_body_overrides: dict[str, str] | None = None,
    ) -> None:
        self.responses = responses
        self.dom_overrides = dom_overrides or {}
        self.response_body_overrides = response_body_overrides or {}
        self.goto_calls: list[str] = []
        self.request_calls: list[str] = []
        self.url = "about:blank"
        self.html_text = ""
        self.closed = False
        self.route_handler = None

    async def route(self, _pattern: str, handler) -> None:
        self.route_handler = handler

    async def goto(self, url: str, **_kwargs) -> FakeNavigationResponse:
        self.goto_calls.append(url)
        if url not in self.responses:
            raise AssertionError(f"Unexpected navigation: {url}")
        status, html_text, final_url = self.responses[url]
        self.url = final_url or url
        self.html_text = self.dom_overrides.get(url, html_text)
        response_body = self.response_body_overrides.get(url, html_text)
        return FakeNavigationResponse(status, self.url, response_body)

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)

    async def content(self) -> str:
        return self.html_text

    async def wait_for_selector(self, selector: str, **_kwargs):
        if await self.locator(selector).count() > 0:
            return object()
        raise spider11.PlaywrightTimeoutError("not ready")

    async def close(self) -> None:
        self.closed = True


class FakeRenderBrowserContext:
    def __init__(self, page: FakeRenderPage) -> None:
        self.page = page
        self.request = FakeRenderRequestContext(page)

    async def new_page(self) -> FakeRenderPage:
        return self.page


class FakeRenderRequestContext:
    def __init__(self, page: FakeRenderPage) -> None:
        self.page = page

    async def get(self, url: str, **_kwargs) -> FakeResponse:
        self.page.request_calls.append(url)
        if url not in self.page.responses:
            raise AssertionError(f"Unexpected request: {url}")
        status, html_text, final_url = self.page.responses[url]
        return FakeResponse(status, html_text, final_url or url)


def fetch_args(*, force_render_sidebar: bool, render_fallback: bool = True):
    return argparse.Namespace(
        force_render_sidebar=force_render_sidebar,
        max_retries=3,
        retry_backoff_seconds=0.0,
        timeout_seconds=5,
        min_sidebar_links=0,
        render_fallback=render_fallback,
    )


def render_args() -> argparse.Namespace:
    return argparse.Namespace(
        block_nonessential_resources=True,
        timeout_seconds=1,
        render_content_wait_seconds=1,
        sidebar_network_idle_seconds=0,
        fail_on_incomplete_sidebar=False,
    )


def task() -> spider11.CrawlTask:
    root = spider11.TARGET_ROOTS[0]
    return spider11.CrawlTask(
        url=root["url"],
        parent_url=None,
        path=(root["label"],),
        depth=0,
        source="test",
        link_text=root["label"],
        root_department=root["label"],
    )


class FetchOptimizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_forced_render_skips_api_get(self) -> None:
        request = FakeRequestContext()
        diagnostics = {"complete": True}

        async def fake_render(*_args, **_kwargs):
            return (
                '<html><div id="pageContent">ok</div></html>',
                task().url,
                200,
                diagnostics,
            )

        counters = spider11.CrawlCounters()
        with patch.object(spider11, "render_page_html", fake_render):
            result = await spider11.fetch_html(
                FakeBrowserContext(request),
                task(),
                fetch_args(force_render_sidebar=True),
                spider11.AsyncRateLimiter(0),
                asyncio.Semaphore(1),
                counters,
            )

        self.assertTrue(result.sidebar_complete)
        self.assertEqual(counters.rendered_fallbacks, 1)
        self.assertEqual(request.calls, 0)

    async def test_static_response_is_disposed(self) -> None:
        response = FakeResponse(
            200,
            '<html><div id="pageContent">ok</div></html>',
        )
        request = FakeRequestContext(response)
        counters = spider11.CrawlCounters()
        with patch.object(
            spider11,
            "count_sidebar_navigation_targets",
        ) as count_sidebar:
            result = await spider11.fetch_html(
                FakeBrowserContext(request),
                task(),
                fetch_args(force_render_sidebar=False, render_fallback=False),
                spider11.AsyncRateLimiter(0),
                asyncio.Semaphore(1),
                counters,
            )

        self.assertTrue(result.sidebar_complete)
        self.assertEqual(counters.rendered_fallbacks, 0)
        self.assertTrue(response.disposed)
        count_sidebar.assert_not_called()

    async def test_rendered_sidebar_completeness_is_preserved(self) -> None:
        async def fake_render(*_args, **_kwargs):
            return (
                '<html><div id="pageContent">ok</div></html>',
                task().url,
                200,
                {"complete": False},
            )

        with patch.object(spider11, "render_page_html", fake_render):
            result = await spider11.fetch_html(
                FakeBrowserContext(FakeRequestContext()),
                task(),
                fetch_args(force_render_sidebar=True),
                spider11.AsyncRateLimiter(0),
                asyncio.Semaphore(1),
                spider11.CrawlCounters(),
            )

        self.assertFalse(result.sidebar_complete)

    async def test_authentication_status_is_recovered_by_caller(self) -> None:
        response = FakeResponse(401)
        request = FakeRequestContext(response)
        with self.assertRaises(spider11.AuthenticationExpiredError):
            await spider11.fetch_html(
                FakeBrowserContext(request),
                task(),
                fetch_args(force_render_sidebar=False),
                spider11.AsyncRateLimiter(0),
                asyncio.Semaphore(1),
                spider11.CrawlCounters(),
            )
        self.assertEqual(request.calls, 1)
        self.assertTrue(response.disposed)

    async def test_transient_missing_content_is_retried(self) -> None:
        request = FakeRequestContext()
        diagnostics = {"complete": True}
        render_calls = 0

        async def fake_render(*_args, **_kwargs):
            nonlocal render_calls
            render_calls += 1
            if render_calls < 3:
                raise spider11.PageContentNotFoundError("content not ready")
            return (
                '<html><div id="pageContent">ok</div></html>',
                task().url,
                200,
                diagnostics,
            )

        counters = spider11.CrawlCounters()
        with (
            patch.object(spider11, "render_page_html", fake_render),
            patch.object(spider11.random, "uniform", return_value=0.0),
        ):
            result = await spider11.fetch_html(
                FakeBrowserContext(request),
                task(),
                fetch_args(force_render_sidebar=True),
                spider11.AsyncRateLimiter(0),
                asyncio.Semaphore(1),
                counters,
            )

        self.assertTrue(result.sidebar_complete)
        self.assertEqual(render_calls, 3)
        self.assertEqual(counters.rendered_fallbacks, 1)
        self.assertEqual(counters.retried_requests, 2)


class ResumeErrorStateTests(unittest.TestCase):
    def test_resume_compacts_only_completed_errors_for_the_same_root(self) -> None:
        url = "https://bgn.bosch.com/old/etasceu1.html"
        completed_root = spider11.TARGET_ROOTS[0]["label"]
        unresolved_root = spider11.TARGET_ROOTS[1]["label"]

        def record(root_department: str, error: str) -> dict[str, object]:
            return {
                "url": url,
                "parent_url": None,
                "depth": 4,
                "root_department": root_department,
                "path": [root_department, "ETAS"],
                "source": "test",
                "error": error,
            }

        with tempfile.TemporaryDirectory() as temporary_dir:
            error_path = Path(temporary_dir) / ".state" / "page_errors.jsonl"
            error_path.parent.mkdir(parents=True)
            records = (
                record(completed_root, "old completed failure"),
                record(unresolved_root, "old unresolved failure"),
                record(unresolved_root, "latest unresolved failure"),
            )
            error_path.write_text(
                "".join(json.dumps(item) + "\n" for item in records),
                encoding="utf-8",
            )
            completed_urls = {
                spider11.selected_root_page_key(url, completed_root)
            }

            latest, stale_count = spider11.load_unresolved_error_records(
                error_path,
                url,
                completed_urls,
            )
            spider11.replace_jsonl_records_atomically(
                error_path,
                latest.values(),
            )

            self.assertEqual(stale_count, 1)
            self.assertEqual(len(latest), 1)
            remaining = list(spider11.load_jsonl(error_path))
            self.assertEqual(len(remaining), 1)
            self.assertEqual(
                remaining[0]["root_department"],
                unresolved_root,
            )
            self.assertEqual(
                remaining[0]["error"],
                "latest unresolved failure",
            )
            self.assertFalse(error_path.with_suffix(".jsonl.tmp").exists())

            completed_urls.add(
                spider11.selected_root_page_key(url, unresolved_root)
            )
            latest, stale_count = spider11.load_unresolved_error_records(
                error_path,
                url,
                completed_urls,
            )
            spider11.replace_jsonl_records_atomically(
                error_path,
                latest.values(),
            )

            self.assertEqual(stale_count, 1)
            self.assertFalse(error_path.exists())

    def test_missing_content_error_does_not_claim_authentication_expired(self) -> None:
        with self.assertRaises(spider11.PageContentNotFoundError) as caught:
            spider11.extract_page(
                "<html><body>content shell</body></html>",
                task(),
                task().url,
                preserve_hidden_content=True,
            )

        message = str(caught.exception).casefold()
        self.assertNotIn("authentication", message)
        self.assertNotIn("expired", message)

    def test_extract_page_accepts_browser_template_attribute_names(self) -> None:
        for attribute_name in ("@click", "(click)", "[value]", "*ngif", "x:y"):
            with self.subTest(attribute_name=attribute_name):
                page_record, _links = spider11.extract_page(
                    "<html><head><title>Template page</title></head>"
                    f'<body><main><button {attribute_name}="run">'
                    "Browser-compatible content"
                    "</button></main></body></html>",
                    task(),
                    task().url,
                    preserve_hidden_content=True,
                )

                self.assertEqual(
                    page_record["content"],
                    "Browser-compatible content",
                )


class InputAndDetectionTests(unittest.TestCase):
    def test_malformed_and_non_https_ports_are_rejected(self) -> None:
        base_url = "https://bgn.bosch.com/root/index.html"

        self.assertIsNone(
            spider11.canonicalize_url(
                base_url,
                "https://bgn.bosch.com:bad/private",
            )
        )
        self.assertIsNone(
            spider11.canonicalize_url(
                base_url,
                "https://bgn.bosch.com:444/private",
            )
        )
        self.assertEqual(
            spider11.canonicalize_url(
                base_url,
                "https://bgn.bosch.com:443/private",
            ),
            "https://bgn.bosch.com/private",
        )
        self.assertFalse(
            spider11.is_internal_url("https://bgn.bosch.com:444/private")
        )
        self.assertFalse(spider11.is_internal_url("ftp://bgn.bosch.com/private"))

    def test_static_content_detection_matches_supported_extractors(self) -> None:
        supported = (
            "<main>content</main>",
            "<section role='main'>content</section>",
            '<div id="pageContainer">content</div>',
            '<div class="layout main-content">content</div>',
            '<div class="page-content">content</div>',
        )
        for html_text in supported:
            with self.subTest(html_text=html_text):
                self.assertTrue(spider11.html_has_page_content(html_text))

        self.assertFalse(
            spider11.html_has_page_content("<mainly>not a main element</mainly>")
        )
        self.assertFalse(
            spider11.html_has_page_content(
                "<div class='not-main-content-placeholder'>shell</div>"
            )
        )
        self.assertFalse(
            spider11.html_has_page_content(
                "<script>const marker = 'main-content';</script>"
            )
        )

    def test_sidebar_diagnostics_are_deduplicated_per_root_and_target(self) -> None:
        first_root, second_root = spider11.TARGET_ROOTS[:2]
        target = "https://bgn.bosch.com/shared/page.html"

        self.assertEqual(
            spider11.sidebar_link_key(target, first_root["label"]),
            spider11.sidebar_link_key(target, first_root["label"]),
        )
        self.assertNotEqual(
            spider11.sidebar_link_key(target, first_root["label"]),
            spider11.sidebar_link_key(target, second_root["label"]),
        )

    def test_sidebar_navigation_path_keeps_root_to_leaf_order(self) -> None:
        page_url = "https://bgn.bosch.com/root/index.html"
        document = spider11.parse_document(
            "<nav><ul><li><a href='/root'>Root</a><ul>"
            "<li><a href='/team'>Team</a><ul>"
            "<li><a id='leaf' href='/leaf'>Leaf</a></li>"
            "</ul></li></ul></li></ul></nav>"
        )
        leaf = document.xpath("//*[@id='leaf']")[0]

        self.assertEqual(
            spider11._sidebar_navigation_path(
                leaf,
                page_url,
                "https://bgn.bosch.com/leaf",
                "Leaf",
            ),
            ("Root", "Team", "Leaf"),
        )


class StreamingWriterTests(unittest.IsolatedAsyncioTestCase):
    async def test_write_failure_unblocks_producers_and_closes_files(self) -> None:
        class FailingHandle:
            def __init__(self) -> None:
                self.closed = False

            def write(self, _value: str) -> int:
                raise OSError("disk full")

            def flush(self) -> None:
                return None

            def close(self) -> None:
                self.closed = True

        with tempfile.TemporaryDirectory() as temporary_dir:
            writer = spider11.StreamingJSONLWriter(
                Path(temporary_dir),
                append=False,
                batch_size=1,
            )
            await writer.start()
            original = writer._files["frontier"]
            original.close()
            failing_handle = FailingHandle()
            writer._files["frontier"] = failing_handle
            opened_handles = list(writer._files.values())

            with self.assertRaisesRegex(OSError, "disk full"):
                await asyncio.wait_for(
                    writer.write("frontier", {"url": "test"}),
                    timeout=1,
                )
            with self.assertRaisesRegex(RuntimeError, "writer failed"):
                await asyncio.wait_for(
                    writer.write("frontier", {"url": "second"}),
                    timeout=1,
                )
            with self.assertRaisesRegex(OSError, "disk full"):
                await asyncio.wait_for(writer.close(), timeout=1)

            self.assertTrue(failing_handle.closed)
            self.assertTrue(all(handle.closed for handle in opened_handles))

    async def test_close_reports_final_filesystem_error(self) -> None:
        class CloseFailingHandle:
            def __init__(self) -> None:
                self.close_attempted = False

            def write(self, value: str) -> int:
                return len(value)

            def flush(self) -> None:
                return None

            def close(self) -> None:
                self.close_attempted = True
                raise OSError("close failed")

        with tempfile.TemporaryDirectory() as temporary_dir:
            writer = spider11.StreamingJSONLWriter(
                Path(temporary_dir),
                append=False,
                batch_size=1,
            )
            await writer.start()
            original = writer._files["page"]
            original.close()
            failing_handle = CloseFailingHandle()
            writer._files["page"] = failing_handle

            with self.assertRaisesRegex(OSError, "close failed"):
                await writer.close()

            self.assertTrue(failing_handle.close_attempted)

    async def test_writer_failure_unblocks_many_backpressured_producers(self) -> None:
        class FailingHandle:
            def write(self, _value: str) -> int:
                raise OSError("disk full")

            def flush(self) -> None:
                return None

            def close(self) -> None:
                return None

        with tempfile.TemporaryDirectory() as temporary_dir:
            writer = spider11.StreamingJSONLWriter(
                Path(temporary_dir),
                append=False,
                batch_size=1,
            )
            await writer.start()
            original = writer._files["frontier"]
            original.close()
            writer._files["frontier"] = FailingHandle()

            producer_count = writer.queue.maxsize * 5
            producers = [
                asyncio.create_task(
                    writer.write("frontier", {"index": index})
                )
                for index in range(producer_count)
            ]
            results = await asyncio.wait_for(
                asyncio.gather(*producers, return_exceptions=True),
                timeout=1,
            )

            self.assertEqual(len(results), producer_count)
            self.assertTrue(
                all(isinstance(result, (OSError, RuntimeError)) for result in results)
            )
            with self.assertRaisesRegex(OSError, "disk full"):
                await writer.close()


class SidebarExpansionTests(unittest.IsolatedAsyncioTestCase):
    async def test_stable_state_is_reused_without_a_redundant_dom_scan(self) -> None:
        class StableSidebarPage:
            def __init__(self) -> None:
                self.runtime_scans = 0

            async def evaluate(self, _script, *arguments):
                if arguments:
                    return {"clicked": 0, "pending": 0}
                self.runtime_scans += 1
                return {"targets": 3, "collapsed": 0}

            async def wait_for_timeout(self, _milliseconds):
                return None

        page = StableSidebarPage()
        args = argparse.Namespace(
            expand_all_sidebar=True,
            sidebar_expand_rounds=4,
            sidebar_expansion_timeout_seconds=1,
            sidebar_max_clicks=10,
            sidebar_click_batch_size=5,
            sidebar_control_max_attempts=1,
            sidebar_settle_ms=0,
            sidebar_stable_passes=2,
            sidebar_stability_interval_ms=0,
        )

        diagnostics = await spider11._expand_all_sidebar_sections(
            page,
            args,
            initial_state={"targets": 3, "collapsed": 0},
        )

        self.assertEqual(page.runtime_scans, 2)
        self.assertEqual(diagnostics["passes"], 2)
        self.assertTrue(diagnostics["complete"])


class WorkerSupervisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_failure_does_not_deadlock_queue_join(self) -> None:
        queue: asyncio.Queue[object | None] = asyncio.Queue()
        await queue.put(object())

        async def broken_worker() -> None:
            await queue.get()
            raise RuntimeError("worker crashed")

        workers = [asyncio.create_task(broken_worker())]
        with self.assertRaisesRegex(RuntimeError, "worker crashed"):
            await asyncio.wait_for(
                spider11.drain_queue_with_workers(queue, workers),
                timeout=1,
            )
        self.assertTrue(workers[0].done())


class ResumeRootSeedTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_frontier_still_reconciles_every_root_seed(self) -> None:
        class FakePlaywrightManager:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, *_args):
                return None

        class FakeContext:
            async def close(self) -> None:
                return None

        with tempfile.TemporaryDirectory() as temporary_dir:
            runtime = Path(temporary_dir)
            output = runtime / "results"
            profile = runtime / "profile"
            frontier = output / spider11.OUTPUT_FILES["frontier"]
            frontier.parent.mkdir(parents=True)
            first_root = spider11.TARGET_ROOTS[0]
            frontier.write_text(
                json.dumps(
                    {
                        "url": first_root["url"],
                        "parent_url": None,
                        "path": [first_root["label"]],
                        "depth": 0,
                        "source": "selected_root_seed",
                        "link_text": first_root["label"],
                        "root_department": first_root["label"],
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            args = spider11.build_args_from_code_config()
            args.output_dir = str(output)
            args.profile_dir = str(profile)
            args.concurrency = 2
            args.parse_workers = 1
            args.render_concurrency = 1
            args.max_pages = 0
            args.verbose = False
            spider11.validate_args(args)

            fetched_urls: list[str] = []

            async def fake_fetch(_context, crawl_task, *_args):
                fetched_urls.append(crawl_task.url)
                return spider11.FetchResult(
                    html_text="<main>content</main>",
                    final_url=crawl_task.url,
                    sidebar_complete=True,
                )

            def fake_extract(_html, crawl_task, _url, _preserve_hidden):
                return (
                    {
                        "title": crawl_task.link_text,
                        "department_path": list(crawl_task.path),
                        "language": "en",
                        "content": "content",
                    },
                    [],
                )

            context = FakeContext()
            with (
                patch.object(
                    spider11,
                    "async_playwright",
                    return_value=FakePlaywrightManager(),
                ),
                patch.object(
                    spider11,
                    "launch_context",
                    AsyncMock(return_value=context),
                ),
                patch.object(
                    spider11,
                    "ensure_authenticated",
                    AsyncMock(return_value=False),
                ),
                patch.object(spider11, "fetch_html", fake_fetch),
                patch.object(spider11, "extract_page", fake_extract),
            ):
                await spider11.crawl(args)

            self.assertCountEqual(
                fetched_urls,
                [root["url"] for root in spider11.TARGET_ROOTS],
            )

    async def test_page_level_access_denial_does_not_abort_other_roots(self) -> None:
        class FakePlaywrightManager:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, *_args):
                return None

        class FakeContext:
            async def close(self) -> None:
                return None

        with tempfile.TemporaryDirectory() as temporary_dir:
            runtime = Path(temporary_dir)
            output = runtime / "results"
            args = spider11.build_args_from_code_config()
            args.output_dir = str(output)
            args.profile_dir = str(runtime / "profile")
            args.resume = False
            args.concurrency = 1
            args.parse_workers = 1
            args.render_concurrency = 1
            args.verbose = False
            spider11.validate_args(args)

            denied_url = spider11.TARGET_ROOTS[0]["url"]
            fetched_urls: list[str] = []

            async def fake_fetch(_context, crawl_task, *_args):
                fetched_urls.append(crawl_task.url)
                if crawl_task.url == denied_url:
                    raise spider11.AuthenticationExpiredError("HTTP 403")
                return spider11.FetchResult(
                    html_text="<main>content</main>",
                    final_url=crawl_task.url,
                    sidebar_complete=True,
                )

            def fake_extract(_html, crawl_task, _url, _preserve_hidden):
                return (
                    {
                        "title": crawl_task.link_text,
                        "department_path": list(crawl_task.path),
                        "language": "en",
                        "content": "content",
                    },
                    [],
                )

            context = FakeContext()
            authenticate = AsyncMock(return_value=False)
            with (
                patch.object(
                    spider11,
                    "async_playwright",
                    return_value=FakePlaywrightManager(),
                ),
                patch.object(
                    spider11,
                    "launch_context",
                    AsyncMock(return_value=context),
                ),
                patch.object(spider11, "ensure_authenticated", authenticate),
                patch.object(spider11, "fetch_html", fake_fetch),
                patch.object(spider11, "extract_page", fake_extract),
            ):
                await spider11.crawl(args)

            self.assertEqual(fetched_urls.count(denied_url), 2)
            self.assertTrue(
                all(
                    root["url"] in fetched_urls
                    for root in spider11.TARGET_ROOTS[1:]
                )
            )
            page_records = list(
                spider11.load_jsonl(output / spider11.OUTPUT_FILES["page"])
            )
            error_records = list(
                spider11.load_jsonl(output / spider11.OUTPUT_FILES["error"])
            )
            self.assertEqual(len(page_records), len(spider11.TARGET_ROOTS) - 1)
            self.assertEqual(len(error_records), 1)
            self.assertEqual(error_records[0]["url"], denied_url)

    async def test_page_limit_cannot_silently_omit_selected_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            runtime = Path(temporary_dir)
            args = spider11.build_args_from_code_config()
            args.output_dir = str(runtime / "results")
            args.profile_dir = str(runtime / "profile")
            args.resume = False
            args.max_pages = 1
            args.verbose = False
            spider11.validate_args(args)

            with self.assertRaisesRegex(ValueError, "all six selected"):
                await spider11.crawl(args)


class MetaRefreshRenderingTests(unittest.IsolatedAsyncioTestCase):
    async def render(self, page: FakeRenderPage):
        with (
            patch.object(
                spider11,
                "_mark_selected_sidebar_scope",
                AsyncMock(return_value={"found": False, "mode": "not_found"}),
            ),
            patch.object(
                spider11,
                "_expand_page_content_sections",
                AsyncMock(return_value={"rounds": 0, "clicks": 0}),
            ),
        ):
            return await spider11.render_page_html(
                FakeRenderBrowserContext(page),
                next(iter(page.responses)),
                root_department=spider11.TARGET_ROOTS[0]["label"],
                args=render_args(),
                render_semaphore=asyncio.Semaphore(1),
            )

    async def test_render_page_html_follows_meta_refresh_to_content(self) -> None:
        old_url = "https://bgn.bosch.com/old/region-africa.html"
        new_url = "https://bgn.bosch.com/new/africa.html"
        final_html = '<html><div id="pageContent">Africa content</div></html>'
        page = FakeRenderPage(
            {
                old_url: (
                    200,
                    '<html><head><meta http-equiv="refresh" '
                    'content="0; URL=/new/africa.html"></head></html>',
                    None,
                ),
                new_url: (200, final_html, None),
            },
            # An immediate refresh may already replace the DOM by the time the
            # crawler inspects it, and Playwright may no longer expose the
            # navigation body. The authenticated request fallback must work.
            dom_overrides={old_url: "<html></html>"},
            response_body_overrides={old_url: ""},
        )

        html_text, final_url, status, diagnostics = await self.render(page)

        self.assertEqual(page.goto_calls, [old_url, new_url])
        self.assertEqual(page.request_calls, [old_url])
        self.assertEqual(html_text, final_html)
        self.assertEqual(final_url, new_url)
        self.assertEqual(status, 200)
        self.assertFalse(diagnostics["selected_root_scope_found"])
        self.assertTrue(page.closed)

    def test_meta_refresh_target_resolves_relative_internal_url(self) -> None:
        result = spider11.meta_refresh_target(
            '<meta HTTP-EQUIV="Refresh" '
            'content="0 ; URL=\'../new/africa.html?utm_source=x#team\'">',
            "https://bgn.bosch.com/old/region-africa.html",
        )

        self.assertEqual(result, "https://bgn.bosch.com/new/africa.html")

    async def test_render_rejects_meta_refresh_loop_and_unsafe_target(self) -> None:
        old_url = "https://bgn.bosch.com/old/region-africa.html"
        targets = (
            old_url,
            "https://example.invalid/outside.html",
            "https://bgn.bosch.com:bad/invalid.html",
        )
        for target in targets:
            with self.subTest(target=target):
                page = FakeRenderPage(
                    {
                        old_url: (
                            200,
                            '<meta http-equiv="refresh" '
                            f'content="0; URL={target}">',
                            None,
                        )
                    }
                )
                with self.assertRaises(spider11.PermanentFetchError):
                    await self.render(page)
                self.assertEqual(page.goto_calls, [old_url])
                self.assertTrue(page.closed)

    async def test_meta_refresh_preserves_authentication_classification(self) -> None:
        old_url = "https://bgn.bosch.com/old/region-africa.html"
        login_url = "https://bgn.bosch.com/login"
        page = FakeRenderPage(
            {
                old_url: (
                    200,
                    '<meta http-equiv="refresh" content="0; URL=/login">',
                    None,
                ),
                login_url: (
                    200,
                    '<html><head><meta http-equiv="refresh" '
                    'content="0; URL=https://login.example.invalid/sso"></head>'
                    '<body><form action="/login"><input type="password"></form></body>'
                    '</html>',
                    None,
                ),
            }
        )

        with self.assertRaises(spider11.AuthenticationExpiredError):
            await self.render(page)

        self.assertEqual(page.goto_calls, [old_url, login_url])
        self.assertTrue(page.closed)

    async def test_render_without_content_is_retryable_not_authentication(self) -> None:
        url = "https://bgn.bosch.com/old/empty-shell.html"
        page = FakeRenderPage({url: (200, "<html><body></body></html>", None)})

        with self.assertRaises(spider11.PageContentNotFoundError) as caught:
            await self.render(page)

        self.assertNotIn("authentication", str(caught.exception).casefold())
        self.assertEqual(page.request_calls, [url])
        self.assertTrue(page.closed)


if __name__ == "__main__":
    unittest.main()
