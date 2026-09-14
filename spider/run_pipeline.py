from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from contextlib import contextmanager
from importlib.util import find_spec
from pathlib import Path
from typing import Iterator

from project_config import (
    BROWSER_CHANNEL_COMMANDS,
    BROWSER_PROFILE_DIR,
    DEFAULT_BROWSER_CHANNELS,
    DEFAULT_BROWSER_EXECUTABLE_PATH,
    DEFAULT_HEADLESS,
    EXCEL_OUTPUT_FILE,
    PAGES_FILE,
    PROJECT_DIR,
    browser_executable_for_channel,
    configure_console_utf8,
    graphical_session_available,
    runtime_path_problems,
)


REQUIRED_MODULES = {
    "lxml": "lxml",
    "openpyxl": "openpyxl",
    "playwright": "playwright",
}


def nearest_existing_parent(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def path_is_writable(path: Path) -> bool:
    candidate = path if path.is_dir() else nearest_existing_parent(path)
    return candidate.is_dir() and os.access(candidate, os.W_OK | os.X_OK)


def bundled_chromium_path(timeout_seconds: float = 5.0) -> Path | None:
    if find_spec("playwright") is None:
        return None

    command = [
        sys.executable,
        "-c",
        (
            "from playwright.sync_api import sync_playwright\n"
            "with sync_playwright() as playwright:\n"
            "    print(playwright.chromium.executable_path, flush=True)\n"
        ),
    ]
    try:
        process = subprocess.Popen(
            command,
            cwd=PROJECT_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=os.name == "posix",
        )
        try:
            stdout, _stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass
            process.communicate()
            return None
    except (OSError, subprocess.SubprocessError):
        return None

    if process.returncode != 0:
        return None
    candidates = [line.strip() for line in stdout.splitlines() if line.strip()]
    return Path(candidates[-1]) if candidates else None


def ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


@contextmanager
def directory_lock(
    directory: Path,
    filename: str,
    resource_description: str,
    enabled: bool,
) -> Iterator[None]:
    if not enabled:
        yield
        return

    lock_path = directory / filename
    try:
        ensure_private_directory(directory)
        handle = lock_path.open("a+", encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(
            f"Unable to create the {resource_description} lock: {lock_path}: {exc}"
        ) from exc
    try:
        try:
            os.chmod(lock_path, 0o600)
        except OSError:
            pass

        if os.name == "posix":
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError(
                    f"Another process is already using the {resource_description}: "
                    f"{directory}"
                ) from exc
        yield
    finally:
        if os.name == "posix":
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


@contextmanager
def browser_profile_lock(enabled: bool) -> Iterator[None]:
    with directory_lock(
        BROWSER_PROFILE_DIR,
        ".crawler.lock",
        "browser profile",
        enabled,
    ):
        yield


@contextmanager
def crawler_output_lock(enabled: bool) -> Iterator[None]:
    with directory_lock(
        PAGES_FILE.parent,
        ".crawler-output.lock",
        "crawler output",
        enabled,
    ):
        yield


def check_project() -> int:
    missing = [
        package
        for module, package in REQUIRED_MODULES.items()
        if find_spec(module) is None
    ]

    print(f"Python: {sys.executable}")
    print(f"Project: {PROJECT_DIR}")
    print(f"Crawler output: {PAGES_FILE}")
    print(f"Excel output: {EXCEL_OUTPUT_FILE}")
    print(f"Browser profile: {BROWSER_PROFILE_DIR}")
    print(f"Browser mode: {'headless' if DEFAULT_HEADLESS else 'headed'}")

    problems: list[str] = []
    problems.extend(runtime_path_problems(PAGES_FILE.parent, BROWSER_PROFILE_DIR))

    if missing:
        problems.append("Missing packages: " + ", ".join(missing))
        problems.append(
            f'Install with: "{sys.executable}" -m pip install -r '
            f'"{PROJECT_DIR / "requirements.txt"}"'
        )

    configured_browsers: list[str] = []
    if DEFAULT_BROWSER_EXECUTABLE_PATH:
        executable = Path(DEFAULT_BROWSER_EXECUTABLE_PATH).expanduser()
        if executable.is_file() and os.access(executable, os.X_OK):
            configured_browsers.append(str(executable))
        else:
            problems.append(
                "SPIDER_BROWSER_EXECUTABLE is not an executable file: "
                f"{executable}"
            )

    for channel in DEFAULT_BROWSER_CHANNELS:
        if channel not in BROWSER_CHANNEL_COMMANDS:
            problems.append(
                f"Unsupported browser channel {channel!r}; use 'msedge', "
                "'chrome', or SPIDER_BROWSER_EXECUTABLE."
            )
            continue
        executable = browser_executable_for_channel(channel)
        if executable:
            configured_browsers.append(f"{channel} ({executable})")
        elif channel in {"msedge", "chrome"}:
            problems.append(
                f"Configured browser channel {channel!r} is not installed."
            )
        else:
            configured_browsers.append(f"{channel} (validated by browser-check)")

    # Starting Playwright just to resolve its bundled executable can be slow or
    # leave background tasks behind on locked-down servers. Only pay that cost
    # when no configured system browser has already been found.
    bundled_browser = (
        bundled_chromium_path()
        if not missing and not configured_browsers
        else None
    )
    if bundled_browser is not None and bundled_browser.is_file():
        configured_browsers.append(f"playwright-chromium ({bundled_browser})")

    if not configured_browsers:
        problems.append(
            "No runnable Edge/Chrome/Playwright Chromium was found. Install a "
            "system browser or run: python -m playwright install chromium"
        )
    else:
        print("Browsers: " + ", ".join(configured_browsers))

    if not DEFAULT_HEADLESS and not graphical_session_available():
        problems.append(
            "Headed mode requires DISPLAY or WAYLAND_DISPLAY. Set "
            "SPIDER_HEADLESS=1 for server crawling."
        )

    for label, path in (
        ("output", PAGES_FILE.parent),
        ("profile", BROWSER_PROFILE_DIR),
    ):
        if not path_is_writable(path):
            problems.append(f"The {label} path is not writable: {path}")

    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1

    print("Project check passed.")
    print("Run 'run_linux.sh browser-check' to verify a real browser launch.")
    return 0


def run_script(
    script_name: str,
    *,
    environment_overrides: dict[str, str] | None = None,
    lock_profile: bool = False,
    lock_output: bool = False,
    timeout_seconds: int | None = None,
) -> int:
    script = PROJECT_DIR / script_name
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONUNBUFFERED"] = "1"
    if environment_overrides:
        environment.update(environment_overrides)
    try:
        with browser_profile_lock(lock_profile), crawler_output_lock(lock_output):
            command = [sys.executable, str(script)]
            if timeout_seconds is not None:
                process = subprocess.Popen(
                    command,
                    cwd=PROJECT_DIR,
                    env=environment,
                    start_new_session=os.name == "posix",
                )
                try:
                    return process.wait(timeout=timeout_seconds)
                except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
                    try:
                        if os.name == "posix":
                            os.killpg(process.pid, signal.SIGTERM)
                        else:
                            process.terminate()
                    except ProcessLookupError:
                        pass
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        try:
                            if os.name == "posix":
                                os.killpg(process.pid, signal.SIGKILL)
                            else:
                                process.kill()
                        except ProcessLookupError:
                            pass
                        process.wait()
                    if isinstance(exc, KeyboardInterrupt):
                        raise
                    print(
                        f"{script.name} timed out after {timeout_seconds} seconds; "
                        "its temporary browser process was stopped.",
                        file=sys.stderr,
                    )
                    return 124
            completed = subprocess.run(
                command,
                cwd=PROJECT_DIR,
                env=environment,
                check=False,
            )
    except (OSError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return completed.returncode


def export_results(*, lock_output: bool = True) -> int:
    try:
        with crawler_output_lock(lock_output):
            if not PAGES_FILE.is_file():
                print(
                    f"Crawler output was not found: {PAGES_FILE}",
                    file=sys.stderr,
                )
                return 1
            return run_script("extract.py")
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run or validate the Bosch organization crawler pipeline."
    )
    parser.add_argument(
        "command",
        choices=("check", "browser-check", "auth", "crawl", "export", "all"),
        nargs="?",
        default="check",
    )
    return parser


def main() -> int:
    configure_console_utf8()
    command = build_parser().parse_args().command
    if command == "check":
        return check_project()
    path_problems = runtime_path_problems(PAGES_FILE.parent, BROWSER_PROFILE_DIR)
    if path_problems:
        for problem in path_problems:
            print(problem, file=sys.stderr)
        return 1
    if command == "browser-check":
        return run_script(
            "spider11.py",
            environment_overrides={"SPIDER_BROWSER_CHECK_ONLY": "1"},
            timeout_seconds=30,
        )
    if command == "auth":
        if not graphical_session_available():
            print(
                "Interactive SSO login requires DISPLAY or WAYLAND_DISPLAY. "
                "Connect with SSH X11 forwarding or a remote desktop, then rerun "
                "'run_linux.sh auth'.",
                file=sys.stderr,
            )
            return 1
        return run_script(
            "spider11.py",
            environment_overrides={
                "SPIDER_AUTH_ONLY": "1",
                "SPIDER_HEADLESS": "0",
            },
            lock_profile=True,
        )
    if command == "crawl":
        return run_script(
            "spider11.py",
            lock_profile=True,
            lock_output=True,
        )
    if command == "export":
        return export_results()

    try:
        with browser_profile_lock(True), crawler_output_lock(True):
            crawl_status = run_script("spider11.py")
            if crawl_status != 0:
                return crawl_status
            return export_results(lock_output=False)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
