from __future__ import annotations

import os
import shlex
import shutil
import sys
from collections.abc import Callable, Mapping
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent

TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_VALUES = frozenset({"0", "false", "no", "off"})
BROWSER_CHANNEL_COMMANDS: dict[str, tuple[str, ...]] = {
    "msedge": ("microsoft-edge", "microsoft-edge-stable"),
    "chrome": ("google-chrome", "google-chrome-stable"),
}


def env_bool(
    name: str,
    default: bool,
    environ: Mapping[str, str] | None = None,
) -> bool:
    values = os.environ if environ is None else environ
    raw = values.get(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().casefold()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    raise ValueError(
        f"{name} must be one of: 1/0, true/false, yes/no, on/off."
    )


def env_optional_int(
    name: str,
    default: int | None,
    environ: Mapping[str, str] | None = None,
) -> int | None:
    values = os.environ if environ is None else environ
    raw = values.get(name)
    if raw is None:
        return default
    normalized = raw.strip().casefold()
    if normalized in {"", "auto", "none"}:
        return None
    try:
        return int(normalized)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer or 'auto'.") from exc


def env_float(
    name: str,
    default: float,
    environ: Mapping[str, str] | None = None,
) -> float:
    values = os.environ if environ is None else environ
    raw = values.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number.") from exc


def runtime_path(
    name: str,
    default: Path,
    environ: Mapping[str, str] | None = None,
) -> Path:
    values = os.environ if environ is None else environ
    raw = values.get(name, "").strip()
    if not raw:
        return default.resolve()
    candidate = Path(os.path.expandvars(raw)).expanduser()
    if not candidate.is_absolute():
        candidate = PROJECT_DIR / candidate
    return candidate.resolve()


def graphical_session_available(
    environ: Mapping[str, str] | None = None,
) -> bool:
    values = os.environ if environ is None else environ
    return bool(values.get("DISPLAY") or values.get("WAYLAND_DISPLAY"))


def default_headless(
    environ: Mapping[str, str] | None = None,
    platform_name: str | None = None,
) -> bool:
    platform_value = sys.platform if platform_name is None else platform_name
    server_default = (
        platform_value.startswith("linux")
        and not graphical_session_available(environ)
    )
    return env_bool("SPIDER_HEADLESS", server_default, environ)


def browser_executable_for_channel(
    channel: str,
    which: Callable[[str], str | None] = shutil.which,
) -> str | None:
    for command in BROWSER_CHANNEL_COMMANDS.get(channel, ()):
        executable = which(command)
        if executable:
            return executable
    return None


def installed_browser_channels(
    which: Callable[[str], str | None] = shutil.which,
) -> tuple[str, ...]:
    return tuple(
        channel
        for channel in ("msedge", "chrome")
        if browser_executable_for_channel(channel, which) is not None
    )


def configured_browser_channels(
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> tuple[str, ...]:
    values = os.environ if environ is None else environ
    detected = installed_browser_channels(which)
    if "SPIDER_BROWSER_CHANNEL" not in values:
        # A persistent Chromium profile must stay bound to one browser. Pick
        # the first installed channel instead of crossing browser brands.
        return detected[:1]
    explicit = values.get("SPIDER_BROWSER_CHANNEL", "").strip()
    if not explicit:
        return ()
    return (explicit,)


def configured_browser_args(
    environ: Mapping[str, str] | None = None,
    platform_name: str | None = None,
) -> tuple[str, ...]:
    values = os.environ if environ is None else environ
    raw = values.get("SPIDER_BROWSER_ARGS")
    if raw is not None:
        return tuple(shlex.split(raw))
    return ()


def runtime_path_problems(
    output_dir: Path,
    profile_dir: Path,
    *,
    project_dir: Path = PROJECT_DIR,
    home_dir: Path | None = None,
) -> tuple[str, ...]:
    """Reject broad or overlapping directories before cleanup can run."""
    output = output_dir.expanduser().resolve()
    profile = profile_dir.expanduser().resolve()
    project = project_dir.resolve()
    home = (Path.home() if home_dir is None else home_dir).resolve()

    protected = {Path("/").resolve(), home, project, *project.parents}
    problems: list[str] = []
    for label, path in (("output", output), ("profile", profile)):
        if path in protected or len(path.parts) < 3:
            problems.append(
                f"The {label} directory must be a dedicated child directory, "
                f"not a filesystem, home, repository, or project root: {path}"
            )

    if output == profile or output in profile.parents or profile in output.parents:
        problems.append(
            "The output and browser profile directories must be separate and "
            "must not contain one another."
        )
    return tuple(problems)


RUNTIME_STATE_DIR = runtime_path("SPIDER_STATE_DIR", PROJECT_DIR)
CRAWL_OUTPUT_DIR = runtime_path(
    "SPIDER_OUTPUT_DIR",
    RUNTIME_STATE_DIR / "results",
)
BROWSER_PROFILE_DIR = runtime_path(
    "SPIDER_PROFILE_DIR",
    RUNTIME_STATE_DIR / ".playwright-profile",
)
PAGES_FILE = CRAWL_OUTPUT_DIR / "pages.jsonl"
SUMMARY_FILE = CRAWL_OUTPUT_DIR / "summary.json"
EXCEL_OUTPUT_FILE = CRAWL_OUTPUT_DIR / "organization_unit.xlsx"

DEFAULT_HEADLESS = default_headless()
DEFAULT_BROWSER_CHANNELS = configured_browser_channels()
DEFAULT_BROWSER_CHANNEL = (
    DEFAULT_BROWSER_CHANNELS[0] if DEFAULT_BROWSER_CHANNELS else ""
)
DEFAULT_BROWSER_FALLBACK_CHANNELS = DEFAULT_BROWSER_CHANNELS[1:]
DEFAULT_BROWSER_EXECUTABLE_PATH = os.environ.get(
    "SPIDER_BROWSER_EXECUTABLE",
    "",
).strip()
DEFAULT_BROWSER_ARGS = configured_browser_args()
DEFAULT_IGNORE_HTTPS_ERRORS = env_bool("SPIDER_IGNORE_HTTPS_ERRORS", False)

CRAWLER_OUTPUT_SCHEMA_VERSION = 5

DEFAULT_START_URL = (
    "https://bgn.bosch.com/FIRSTspiritWeb/wcms/wcms_c/en/"
    "bosch-globalnet/organization/organization-startpage.html"
)

TARGET_ROOTS: tuple[dict[str, str], ...] = (
    {
        "label": "Corporate Headquarters and Service Areas",
        "url": (
            "https://bgn.bosch.com/FIRSTspiritWeb/wcms/wcms_corpfunc/en/"
            "bosch-globalnet/02-organization/corporate-functions/"
            "corporate-functions.html"
        ),
        "fsid": "22775415",
    },
    {
        "label": "Mobility (BBM)",
        "url": (
            "https://bgn.bosch.com/FIRSTspiritWeb/wcms/wcms_c/en/"
            "bosch-globalnet/organization/mobility-bbm/about-bbm.html"
        ),
        "fsid": "7023565",
    },
    {
        "label": "Industrial Technology (BBI)",
        "url": (
            "https://bgn.bosch.com/FIRSTspiritWeb/wcms/wcms_c/en/"
            "bosch-globalnet/organization/industrial-technology-bbi/"
            "industrial-technology-bbi-startpage.html"
        ),
        "fsid": "7023569",
    },
    {
        "label": "Consumer Goods (BBG)",
        "url": (
            "https://bgn.bosch.com/FIRSTspiritWeb/wcms/wcms_c/en/"
            "bosch-globalnet/organization/consumer-goods-bbg/"
            "consumer-goods-bbg.html"
        ),
        "fsid": "7023989",
    },
    {
        "label": "Energy and Building Technology (BBE)",
        "url": (
            "https://bgn.bosch.com/FIRSTspiritWeb/wcms/wcms_c/en/"
            "bosch-globalnet/organization/energy-and-building-technology-bbe/"
            "08-energy-and-building-technology-bbe.html"
        ),
        "fsid": "14028397",
    },
    {
        "label": "Value Accelerator & Portfolio Companies (VP)",
        "url": (
            "https://bgn.bosch.com/FIRSTspiritWeb/wcms/wcms_c/en/"
            "bosch-globalnet/organization/vp/vp-start-page.html"
        ),
        "fsid": "30181433",
    },
)

TARGET_ORGANIZATIONS = tuple(item["label"] for item in TARGET_ROOTS)


def configure_console_utf8() -> None:
    """Prevent Windows legacy console encodings from breaking Unicode logs."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")
