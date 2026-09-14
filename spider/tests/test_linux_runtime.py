import os
import signal
import stat
import tempfile
import unittest
from argparse import Namespace
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

import project_config
import run_pipeline
import spider11


class ProjectConfigTests(unittest.TestCase):
    def test_linux_without_display_defaults_to_headless(self) -> None:
        with patch.object(project_config.sys, "platform", "linux"):
            self.assertTrue(project_config.default_headless({}))
            self.assertFalse(project_config.default_headless({"DISPLAY": ":10"}))
            self.assertFalse(project_config.default_headless({"SPIDER_HEADLESS": "0"}))

    def test_invalid_boolean_fails_fast(self) -> None:
        with self.assertRaisesRegex(ValueError, "SPIDER_HEADLESS"):
            project_config.env_bool(
                "SPIDER_HEADLESS", True, {"SPIDER_HEADLESS": "sometimes"}
            )

    def test_runtime_path_is_relative_to_project(self) -> None:
        relative = project_config.runtime_path(
            "SPIDER_TEST_PATH", project_config.PROJECT_DIR / "state", {}
        )
        self.assertEqual(relative, (project_config.PROJECT_DIR / "state").resolve())

        absolute = Path(tempfile.gettempdir()) / "spider-runtime-absolute"
        configured = project_config.runtime_path(
            "SPIDER_TEST_PATH",
            project_config.PROJECT_DIR / "state",
            {"SPIDER_TEST_PATH": str(absolute)},
        )
        self.assertEqual(configured, absolute.resolve())

    def test_browser_channel_is_fixed_to_first_installed_browser(self) -> None:
        found = {
            "microsoft-edge": "/usr/bin/microsoft-edge",
            "google-chrome": "/usr/bin/google-chrome",
        }
        channels = project_config.configured_browser_channels(
            {}, which=lambda command: found.get(command)
        )
        self.assertEqual(channels, ("msedge",))

    def test_explicit_channel_and_empty_channel(self) -> None:
        self.assertEqual(
            project_config.configured_browser_channels(
                {"SPIDER_BROWSER_CHANNEL": "chrome"}, which=lambda _: None
            ),
            ("chrome",),
        )
        self.assertEqual(
            project_config.configured_browser_channels(
                {"SPIDER_BROWSER_CHANNEL": ""}, which=lambda _: "/unused"
            ),
            (),
        )

    def test_linux_default_browser_args_are_server_safe(self) -> None:
        with patch.object(project_config.sys, "platform", "linux"):
            self.assertEqual(project_config.configured_browser_args({}), ())

    def test_runtime_paths_reject_broad_and_overlapping_targets(self) -> None:
        project = project_config.PROJECT_DIR
        broad = project_config.runtime_path_problems(
            project,
            project / ".profile",
        )
        self.assertTrue(any("dedicated child" in item for item in broad))

        overlapping = project_config.runtime_path_problems(
            project / "runtime",
            project / "runtime" / "profile",
        )
        self.assertTrue(any("must not contain" in item for item in overlapping))

    def test_auto_worker_profile_resolves_for_headless_server(self) -> None:
        args = Namespace(
            worker_profile="auto",
            headless=True,
            concurrency=None,
            parse_workers=None,
            render_concurrency=None,
        )
        spider11.resolve_worker_configuration(args)
        self.assertEqual(args.worker_profile, "balanced")


class ProjectCheckTests(unittest.TestCase):
    def test_existing_system_browser_skips_bundled_browser_probe(self) -> None:
        with (
            patch.object(run_pipeline, "find_spec", return_value=object()),
            patch.object(run_pipeline, "DEFAULT_BROWSER_EXECUTABLE_PATH", ""),
            patch.object(run_pipeline, "DEFAULT_BROWSER_CHANNELS", ("msedge",)),
            patch.object(
                run_pipeline,
                "browser_executable_for_channel",
                return_value="/usr/bin/microsoft-edge",
            ),
            patch.object(run_pipeline, "runtime_path_problems", return_value=()),
            patch.object(run_pipeline, "path_is_writable", return_value=True),
            patch.object(run_pipeline, "bundled_chromium_path") as bundled_probe,
        ):
            self.assertEqual(run_pipeline.check_project(), 0)

        bundled_probe.assert_not_called()

    def test_bundled_browser_is_probed_without_a_system_browser(self) -> None:
        bundled_executable = Mock()
        bundled_executable.is_file.return_value = True
        with (
            patch.object(run_pipeline, "find_spec", return_value=object()),
            patch.object(run_pipeline, "DEFAULT_BROWSER_EXECUTABLE_PATH", ""),
            patch.object(run_pipeline, "DEFAULT_BROWSER_CHANNELS", ()),
            patch.object(run_pipeline, "runtime_path_problems", return_value=()),
            patch.object(run_pipeline, "path_is_writable", return_value=True),
            patch.object(
                run_pipeline,
                "bundled_chromium_path",
                return_value=bundled_executable,
            ) as bundled_probe,
        ):
            self.assertEqual(run_pipeline.check_project(), 0)

        bundled_probe.assert_called_once_with()


class BrowserRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_browser_channel_falls_back_from_edge_to_chrome(self) -> None:
        class FakeChromium:
            def __init__(self) -> None:
                self.calls = []

            async def launch_persistent_context(self, **kwargs):
                self.calls.append(kwargs)
                if kwargs.get("channel") == "msedge":
                    raise spider11.PlaywrightError("edge unavailable")
                if kwargs.get("channel") == "chrome":
                    return "chrome-context"
                raise AssertionError(f"unexpected launch: {kwargs}")

        class FakePlaywright:
            def __init__(self) -> None:
                self.chromium = FakeChromium()

        fake = FakePlaywright()
        context = await spider11.launch_context(
            fake,
            Path(tempfile.gettempdir()) / "spider-profile-test",
            True,
            "msedge",
            ("chrome",),
            "",
            ("--disable-dev-shm-usage",),
        )

        self.assertEqual(context, "chrome-context")
        self.assertEqual(
            [call.get("channel") for call in fake.chromium.calls],
            ["msedge", "chrome"],
        )
        self.assertTrue(fake.chromium.calls[0]["headless"])
        self.assertFalse(fake.chromium.calls[0]["ignore_https_errors"])
        self.assertIn("--disable-dev-shm-usage", fake.chromium.calls[0]["args"])

    async def test_headless_auth_failure_has_actionable_message(self) -> None:
        class FakePage:
            def __init__(self) -> None:
                self.closed = False

            async def goto(self, *_args, **_kwargs):
                return None

            async def wait_for_selector(self, *_args, **_kwargs):
                raise spider11.PlaywrightTimeoutError("not ready")

            async def close(self):
                self.closed = True

        class FakeContext:
            def __init__(self) -> None:
                self.page = FakePage()

            async def new_page(self):
                return self.page

        context = FakeContext()
        with self.assertRaisesRegex(RuntimeError, r"run_linux\.sh auth"):
            await spider11.ensure_authenticated(
                context, "https://example.invalid", True, 10, 30
            )
        self.assertTrue(context.page.closed)

    async def test_delayed_authenticated_content_is_accepted(self) -> None:
        class FakePage:
            def __init__(self) -> None:
                self.closed = False

            async def goto(self, *_args, **_kwargs):
                return None

            async def wait_for_selector(self, selector, **_kwargs):
                self.selector = selector
                return object()

            async def close(self):
                self.closed = True

        class FakeContext:
            def __init__(self) -> None:
                self.page = FakePage()

            async def new_page(self):
                return self.page

        context = FakeContext()
        authenticated_now = await spider11.ensure_authenticated(
            context, "https://example.invalid", True, 10, 30
        )
        self.assertFalse(authenticated_now)
        self.assertEqual(context.page.selector, "#pageContent")
        self.assertTrue(context.page.closed)

    async def test_persistent_profile_rejects_different_browser(self) -> None:
        class FakeChromium:
            async def launch_persistent_context(self, **_kwargs):
                raise AssertionError("browser must not be launched")

        class FakePlaywright:
            chromium = FakeChromium()

        with tempfile.TemporaryDirectory() as temporary_directory:
            profile = Path(temporary_directory)
            marker = profile / spider11.BROWSER_IDENTITY_FILENAME
            marker.write_text("channel:msedge\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "different browser"):
                await spider11.launch_context(
                    FakePlaywright(),
                    profile,
                    True,
                    "chrome",
                    allow_fallbacks=False,
                )

    async def test_persistent_launch_records_browser_and_never_falls_back(self) -> None:
        class FakeContext:
            async def close(self):
                return None

        class FakeChromium:
            def __init__(self) -> None:
                self.calls = []

            async def launch_persistent_context(self, **kwargs):
                self.calls.append(kwargs)
                if kwargs.get("channel") == "msedge":
                    return FakeContext()
                raise AssertionError(f"unexpected fallback: {kwargs}")

        class FakePlaywright:
            def __init__(self) -> None:
                self.chromium = FakeChromium()

        with tempfile.TemporaryDirectory() as temporary_directory:
            profile = Path(temporary_directory)
            fake = FakePlaywright()
            await spider11.launch_context(
                fake,
                profile,
                True,
                "msedge",
                ("chrome",),
                allow_fallbacks=False,
            )
            self.assertEqual(len(fake.chromium.calls), 1)
            marker = profile / spider11.BROWSER_IDENTITY_FILENAME
            self.assertEqual(marker.read_text(encoding="utf-8").strip(), "channel:msedge")
            self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o600)

    async def test_strict_browser_launch_does_not_hide_primary_failure(self) -> None:
        class FakeChromium:
            def __init__(self) -> None:
                self.calls = []

            async def launch_persistent_context(self, **kwargs):
                self.calls.append(kwargs)
                if kwargs.get("channel") == "msedge":
                    raise spider11.PlaywrightError("edge failed")
                return object()

        class FakePlaywright:
            def __init__(self) -> None:
                self.chromium = FakeChromium()

        with tempfile.TemporaryDirectory() as temporary_directory:
            fake = FakePlaywright()
            with self.assertRaisesRegex(RuntimeError, "No configured Chromium"):
                await spider11.launch_context(
                    fake,
                    Path(temporary_directory),
                    True,
                    "msedge",
                    ("chrome",),
                    allow_fallbacks=False,
                )
        self.assertEqual(
            [call.get("channel") for call in fake.chromium.calls],
            ["msedge"],
        )


@unittest.skipUnless(os.name == "posix", "profile locking uses POSIX flock")
class ProfileLockTests(unittest.TestCase):
    def test_profile_lock_rejects_second_process_and_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            profile = Path(temporary_directory) / "profile"
            with patch.object(run_pipeline, "BROWSER_PROFILE_DIR", profile):
                with run_pipeline.browser_profile_lock(enabled=True):
                    lock_path = profile / ".crawler.lock"
                    self.assertEqual(stat.S_IMODE(lock_path.stat().st_mode), 0o600)
                    with self.assertRaisesRegex(RuntimeError, "already using"):
                        with run_pipeline.browser_profile_lock(enabled=True):
                            pass

    def test_output_lock_rejects_concurrent_export_or_crawl(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_file = Path(temporary_directory) / "results" / "pages.jsonl"
            with patch.object(run_pipeline, "PAGES_FILE", output_file):
                with run_pipeline.crawler_output_lock(enabled=True):
                    lock_path = output_file.parent / ".crawler-output.lock"
                    self.assertEqual(stat.S_IMODE(lock_path.stat().st_mode), 0o600)
                    with self.assertRaisesRegex(RuntimeError, "crawler output"):
                        with run_pipeline.crawler_output_lock(enabled=True):
                            pass


class PipelineDispatchTests(unittest.TestCase):
    @staticmethod
    def parser_for(command: str):
        return Namespace(parse_args=lambda: Namespace(command=command))

    def test_crawl_holds_profile_and_output_locks(self) -> None:
        with (
            patch.object(run_pipeline, "build_parser", return_value=self.parser_for("crawl")),
            patch.object(run_pipeline, "runtime_path_problems", return_value=()),
            patch.object(run_pipeline, "run_script", return_value=0) as run_script,
        ):
            self.assertEqual(run_pipeline.main(), 0)
        run_script.assert_called_once_with(
            "spider11.py",
            lock_profile=True,
            lock_output=True,
        )

    def test_browser_check_sets_internal_mode_only_for_child(self) -> None:
        with (
            patch.object(
                run_pipeline,
                "build_parser",
                return_value=self.parser_for("browser-check"),
            ),
            patch.object(run_pipeline, "runtime_path_problems", return_value=()),
            patch.object(run_pipeline, "run_script", return_value=0) as run_script,
        ):
            self.assertEqual(run_pipeline.main(), 0)
        run_script.assert_called_once_with(
            "spider11.py",
            environment_overrides={"SPIDER_BROWSER_CHECK_ONLY": "1"},
            timeout_seconds=30,
        )

    def test_auth_without_display_never_launches_browser(self) -> None:
        with (
            patch.object(run_pipeline, "build_parser", return_value=self.parser_for("auth")),
            patch.object(run_pipeline, "runtime_path_problems", return_value=()),
            patch.object(run_pipeline, "graphical_session_available", return_value=False),
            patch.object(run_pipeline, "run_script") as run_script,
        ):
            self.assertEqual(run_pipeline.main(), 1)
        run_script.assert_not_called()

    def test_all_keeps_both_locks_through_export(self) -> None:
        with (
            patch.object(run_pipeline, "build_parser", return_value=self.parser_for("all")),
            patch.object(run_pipeline, "runtime_path_problems", return_value=()),
            patch.object(run_pipeline, "browser_profile_lock", return_value=nullcontext()),
            patch.object(run_pipeline, "crawler_output_lock", return_value=nullcontext()),
            patch.object(run_pipeline, "run_script", return_value=0) as run_script,
            patch.object(run_pipeline, "export_results", return_value=0) as export_results,
        ):
            self.assertEqual(run_pipeline.main(), 0)
        run_script.assert_called_once_with("spider11.py")
        export_results.assert_called_once_with(lock_output=False)

    def test_all_does_not_export_after_crawl_failure(self) -> None:
        with (
            patch.object(run_pipeline, "build_parser", return_value=self.parser_for("all")),
            patch.object(run_pipeline, "runtime_path_problems", return_value=()),
            patch.object(run_pipeline, "browser_profile_lock", return_value=nullcontext()),
            patch.object(run_pipeline, "crawler_output_lock", return_value=nullcontext()),
            patch.object(run_pipeline, "run_script", return_value=7),
            patch.object(run_pipeline, "export_results") as export_results,
        ):
            self.assertEqual(run_pipeline.main(), 7)
        export_results.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "process groups use POSIX signals")
    def test_browser_check_timeout_stops_the_child_process_group(self) -> None:
        process = Mock(pid=43210)
        process.wait.side_effect = [
            run_pipeline.subprocess.TimeoutExpired("browser-check", 1),
            -signal.SIGTERM,
        ]
        with (
            patch.object(run_pipeline.subprocess, "Popen", return_value=process),
            patch.object(run_pipeline.os, "killpg") as killpg,
        ):
            status = run_pipeline.run_script("spider11.py", timeout_seconds=1)
        self.assertEqual(status, 124)
        killpg.assert_called_once_with(process.pid, signal.SIGTERM)


if __name__ == "__main__":
    unittest.main()
