#!/usr/bin/env python3
"""protonmgr - a small arrow-key terminal tool for managing Proton builds.

What it does
------------
* Lists the Proton versions installed in Steam's compatibilitytools.d folder,
  with their size on disk.
* Browses the online releases of GE-Proton, Proton-CachyOS and DW Proton in a
  scrolling list (more load as you scroll), marking what is already installed.
* Select several releases with Space; the selection (the "queue") is kept while
  you scroll, filter, or switch branches, then install it all at once. Each
  download is checksum-verified when a checksum is published, then extracted
  into compatibilitytools.d.
* Reads release notes, filters by name, checks for newer releases, and removes
  or bulk-cleans old versions.

Keys (shown at the bottom of every screen)
------------------------------------------
Up/Down (or j/k) move, Space selects, Enter confirms/installs, Esc goes back.

Command line
------------
  protonmgr                  interactive interface
  protonmgr --list           print installed versions
  protonmgr --latest ge      install the newest stable release of a branch
                             (ge, cachyos or dw)
  protonmgr --dir PATH       manage another compatibilitytools.d folder

Only the Python standard library is used (Python 3.8+; 3.12+ recommended).

Where it installs
-----------------
The first existing Steam folder from this list is used:
  ~/.local/share/Steam, ~/.steam/steam, ~/.steam/root, Flatpak Steam.
Override with `--dir PATH` or the PROTONMGR_DIR environment variable
(PATH must be the compatibilitytools.d folder itself).

Adding another Proton branch
----------------------------
Append a `Source(...)` entry to SOURCES below. Any GitHub or Gitea
(Forgejo) releases endpoint that ships .tar.gz/.tar.xz/.tar.zst assets works.
"""

from __future__ import annotations

import argparse
import contextlib
import curses
import hashlib
import http.client
import json
import locale
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

APP_NAME = "protonmgr"
__version__ = "2.0.0"
USER_AGENT = f"{APP_NAME}/1.0 (terminal Proton version manager)"
PAGE_SIZE = 30            # releases fetched per request when browsing online
HTTP_TIMEOUT = 30         # seconds before a network request gives up
ARCHIVE_RE = re.compile(r"\.tar\.(gz|xz|zst)$")          # supported archive types
CHECKSUM_SUFFIXES = (".sha512sum", ".sha256sum")         # sibling checksum assets
SKIP_ASSET_WORDS = ("arm64", "aarch64", "signed")        # builds we never want


# --------------------------------------------------------------------------
# Data model: where releases come from and what a release looks like
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Endpoint:
    """One releases API URL. `kind` is 'github' or 'gitea' (query params differ)."""
    kind: str
    url: str


@dataclass(frozen=True)
class Source:
    """A Proton branch: display name, API endpoints (tried in order), and the
    lowercase text that identifies its installed folders (used for labeling)."""
    key: str
    label: str
    endpoints: tuple
    dir_hint: str


@dataclass
class Asset:
    """A downloadable file attached to a release."""
    name: str
    url: str
    size: int
    digest: str = ""      # GitHub provides e.g. "sha256:<hex>"


@dataclass
class Release:
    """One published version of a Proton branch."""
    tag: str
    published: str        # YYYY-MM-DD
    prerelease: bool
    assets: list
    body: str = ""       # release notes (markdown)


SOURCES = [
    Source(
        key="ge",
        label="GE-Proton (GloriousEggroll)",
        endpoints=(Endpoint("github", "https://api.github.com/repos/GloriousEggroll/proton-ge-custom/releases"),),
        dir_hint="ge-proton",
    ),
    Source(
        key="cachyos",
        label="Proton-CachyOS",
        endpoints=(Endpoint("github", "https://api.github.com/repos/CachyOS/proton-cachyos/releases"),),
        dir_hint="cachyos",
    ),
    Source(
        key="dw",
        label="DW Proton (Dawn Winery)",
        # dawn.wine sits behind a bot-check that can reject scripts, so the
        # official GitHub mirror is used as an automatic fallback.
        endpoints=(
            Endpoint("gitea", "https://dawn.wine/api/v1/repos/dawn-winery/dwproton/releases"),
            Endpoint("github", "https://api.github.com/repos/dawn-winery/dwproton-mirror/releases"),
        ),
        dir_hint="dwproton",
    ),
]


class ProtonMgrError(Exception):
    """A problem worth showing to the user as a plain message (not a traceback)."""


# --------------------------------------------------------------------------
# Paths, manifest and CPU detection
# --------------------------------------------------------------------------

def resolve_compat_dir(override: str | None) -> Path:
    """Find (and create if needed) Steam's compatibilitytools.d folder."""
    override = override or os.environ.get("PROTONMGR_DIR")
    if override:
        path = Path(override).expanduser()
    else:
        home = Path.home()
        steam_roots = [
            home / ".local/share/Steam",
            home / ".steam/steam",
            home / ".steam/root",
            home / ".var/app/com.valvesoftware.Steam/data/Steam",   # Flatpak Steam
        ]
        root = next((r for r in steam_roots if r.is_dir()), steam_roots[0])
        path = root / "compatibilitytools.d"
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_dir() -> Path:
    """Folder holding protonmgr's own bookkeeping file."""
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / APP_NAME


def cache_dir() -> Path:
    """Temporary folder for downloaded archives (deleted after install)."""
    base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / APP_NAME


def load_manifest() -> dict:
    """Read the record of what this tool installed: {folder: {source, tag}}."""
    try:
        data = json.loads((config_dir() / "manifest.json").read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_manifest(manifest: dict) -> None:
    """Write the install record; failures are non-fatal (it is only a hint)."""
    try:
        config_dir().mkdir(parents=True, exist_ok=True)
        (config_dir() / "manifest.json").write_text(json.dumps(manifest, indent=2))
    except OSError:
        pass


def detect_cpu_level() -> int:
    """Return the x86-64 microarchitecture level of this CPU: 2, 3 or 4.

    CachyOS publishes x86_64 (baseline), x86_64_v3 (AVX2) and x86_64_v4
    (AVX-512) builds; this picks the best one the CPU can run.
    Override with PROTONMGR_CPU_LEVEL=2|3|4.
    """
    forced = os.environ.get("PROTONMGR_CPU_LEVEL")
    if forced in ("2", "3", "4"):
        return int(forced)
    try:
        text = Path("/proc/cpuinfo").read_text()
    except OSError:
        return 2
    match = re.search(r"^flags\s*:\s*(.*)$", text, re.M)
    flags = set(match.group(1).split()) if match else set()
    v3 = {"avx", "avx2", "bmi1", "bmi2", "f16c", "fma", "movbe", "abm"}
    v4 = {"avx512f", "avx512bw", "avx512cd", "avx512dq", "avx512vl"}
    if v3 <= flags and v4 <= flags:
        return 4
    if v3 <= flags:
        return 3
    return 2


# --------------------------------------------------------------------------
# Networking: fetching release lists
# --------------------------------------------------------------------------

def http_get(url: str, accept: str = "application/json") -> bytes:
    """GET a URL and return the body. Uses GITHUB_TOKEN for GitHub if set
    (raises the anonymous rate limit of 60 requests/hour)."""
    headers = {"User-Agent": USER_AGENT, "Accept": accept}
    token = os.environ.get("GITHUB_TOKEN")
    if token and "api.github.com" in url:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        return response.read()


def parse_release(raw: dict) -> Release | None:
    """Convert one GitHub/Gitea release JSON object (same field names for the
    parts we use) into a Release. Drafts are ignored."""
    if raw.get("draft") or not raw.get("tag_name"):
        return None
    assets = [
        Asset(
            name=a.get("name", ""),
            url=a.get("browser_download_url", ""),
            size=int(a.get("size") or 0),
            digest=a.get("digest") or "",
        )
        for a in raw.get("assets") or []
        if a.get("name") and a.get("browser_download_url")
    ]
    published = raw.get("published_at") or raw.get("created_at") or ""
    return Release(raw["tag_name"], published[:10], bool(raw.get("prerelease")), assets,
                   raw.get("body") or "")


def fetch_page(source: Source, page: int) -> tuple:
    """Fetch one page of releases. Returns (releases, has_next_page, note).

    Endpoints are tried in order; `note` explains a fallback when one was used.
    Releases without any usable archive are dropped.
    """
    problems = []
    for index, endpoint in enumerate(source.endpoints):
        limit_param = "per_page" if endpoint.kind == "github" else "limit"
        url = f"{endpoint.url}?{limit_param}={PAGE_SIZE}&page={page}"
        try:
            data = json.loads(http_get(url))
            if not isinstance(data, list):
                raise ValueError("unexpected response (not a release list)")
        except urllib.error.HTTPError as exc:
            problems.append(f"{endpoint.url}: HTTP {exc.code}")
            continue
        except (urllib.error.URLError, OSError, ValueError) as exc:
            problems.append(f"{endpoint.url}: {exc}")
            continue
        releases = [r for r in map(parse_release, data) if r and candidate_archives(r)]
        note = ""
        if index > 0:
            note = f"(primary site unavailable, using mirror: {endpoint.url})"
        return releases, len(data) >= PAGE_SIZE, note
    raise ProtonMgrError("Could not fetch releases:\n  " + "\n  ".join(problems))


# --------------------------------------------------------------------------
# Choosing which file of a release to download
# --------------------------------------------------------------------------

def candidate_archives(release: Release) -> list:
    """Archives of a release that are plausible Proton builds for this PC
    (supported archive type, not ARM, not a signed variant)."""
    result = []
    for asset in release.assets:
        name = asset.name.lower()
        if ARCHIVE_RE.search(name) and not any(w in name for w in SKIP_ASSET_WORDS):
            result.append(asset)
    return result


def asset_level(asset: Asset) -> int:
    """x86-64 level an asset targets: v4 -> 4, v3 -> 3, anything else -> 2."""
    name = asset.name.lower()
    if "x86_64_v4" in name:
        return 4
    if "x86_64_v3" in name:
        return 3
    return 2


def pick_asset(release: Release, cpu_level: int) -> Asset | None:
    """Pick the best archive for this CPU: highest level it supports, and
    prefer .gz/.xz over .zst (the .zst needs the external zstd tool)."""
    candidates = candidate_archives(release)
    if not candidates:
        return None
    usable = [a for a in candidates if asset_level(a) <= cpu_level] or candidates
    return max(usable, key=lambda a: (asset_level(a), 0 if a.name.endswith(".zst") else 1))


# --------------------------------------------------------------------------
# Installed versions
# --------------------------------------------------------------------------

def installed_dirs(compat_dir: Path) -> list:
    """Names of installed Proton folders (hidden/staging folders excluded)."""
    try:
        return sorted(
            (p.name for p in compat_dir.iterdir() if p.is_dir() and not p.name.startswith(".")),
            key=str.lower,
        )
    except OSError:
        return []


def tag_in_name(tag: str, name: str) -> bool:
    """True if `tag` appears in `name` as a whole version, so that
    'dwproton-11.0-1' does not match 'dwproton-11.0-15'."""
    pattern = r"(?<![A-Za-z0-9])" + re.escape(tag) + r"(?![0-9])"
    return re.search(pattern, name, re.I) is not None


def is_installed(source: Source, release: Release, dirs: list, manifest: dict) -> bool:
    """Decide whether a release is already installed, using three signals:
    this tool's install record, the release tag inside a folder name, or a
    folder named exactly like one of the release's archives."""
    for name in dirs:
        info = manifest.get(name) or {}
        if info.get("source") == source.key and info.get("tag") == release.tag:
            return True
        if tag_in_name(release.tag, name):
            return True
    stems = {ARCHIVE_RE.sub("", a.name).lower() for a in candidate_archives(release)}
    return any(name.lower() in stems for name in dirs)


def branch_of(name: str) -> str:
    """Human label of the branch an installed folder belongs to."""
    lowered = name.lower()
    for source in SOURCES:
        if source.dir_hint in lowered:
            return source.label.split(" (")[0]
    return "Other / unknown"


def remove_path(path: Path) -> None:
    """Delete a file, symlink or folder tree."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


# --------------------------------------------------------------------------
# Downloading and installing
# --------------------------------------------------------------------------

def human_size(num_bytes: float) -> str:
    """Format a byte count like '537 MiB'."""
    value = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit in ("B", "KiB") else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"


def download(url: str, dest: Path, size_hint: int) -> None:
    """Stream a file to disk while printing a one-line progress indicator."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response, open(dest, "wb") as out:
        total = int(response.headers.get("Content-Length") or 0) or size_hint
        done, started = 0, time.time()
        while True:
            chunk = response.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            speed = done / max(time.time() - started, 0.001)
            percent = f"{done * 100 / total:5.1f}%" if total else "  ?  "
            print(f"\r  {human_size(done)} / {human_size(total) if total else '?'}  "
                  f"{percent}  {human_size(speed)}/s    ", end="", flush=True)
    print()


def expected_checksum(release: Release, asset: Asset) -> tuple | None:
    """Find a published checksum for an asset: (algorithm, hex) or None.

    Looks first for a sibling '<name>.sha512sum' / '.sha256sum' file, then for
    the digest GitHub attaches to every asset.
    """
    stem = ARCHIVE_RE.sub("", asset.name)
    wanted = {stem + s for s in CHECKSUM_SUFFIXES} | {asset.name + s for s in CHECKSUM_SUFFIXES}
    for other in release.assets:
        if other.name in wanted:
            try:
                text = http_get(other.url, accept="*/*").decode("utf-8", "replace")
            except (urllib.error.URLError, OSError):
                continue
            match = re.match(r"\s*([0-9a-fA-F]{128}|[0-9a-fA-F]{64})\b", text)
            if match:
                digest = match.group(1).lower()
                return ("sha512" if len(digest) == 128 else "sha256"), digest
    if asset.digest.startswith("sha256:"):
        return "sha256", asset.digest.split(":", 1)[1].lower()
    return None


def verify_file(path: Path, algorithm: str, expected: str) -> bool:
    """Hash a file in chunks and compare with the expected hex digest."""
    hasher = hashlib.new(algorithm)
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(block)
    return hasher.hexdigest() == expected


def extract_archive(archive: Path, dest: Path) -> None:
    """Unpack a tarball into `dest`. .zst files use the system `tar --zstd`."""
    if archive.name.endswith(".tar.zst"):
        if not (shutil.which("zstd") and shutil.which("tar")):
            raise ProtonMgrError("This release is only available as .tar.zst, which needs the 'zstd' package.")
        subprocess.run(["tar", "--zstd", "-xf", str(archive), "-C", str(dest)], check=True)
        return
    with tarfile.open(archive) as tar:
        try:
            tar.extractall(dest, filter="tar")     # Python 3.12+: blocks path escapes
        except TypeError:
            tar.extractall(dest)                   # older Python without filters


def install_release(source: Source, release: Release, asset: Asset,
                    compat_dir: Path, manifest: dict) -> list:
    """Download, verify and extract one release. Returns the installed folder names.

    Extraction happens in a hidden staging folder inside compatibilitytools.d
    so a failed or interrupted install never leaves a half-unpacked version.
    """
    cache_dir().mkdir(parents=True, exist_ok=True)
    archive = cache_dir() / asset.name
    staging = None
    try:
        print(f"Downloading {asset.name} ...")
        download(asset.url, archive, asset.size)

        checksum = expected_checksum(release, asset)
        if checksum:
            print(f"Verifying {checksum[0]} checksum ...")
            if not verify_file(archive, *checksum):
                raise ProtonMgrError("Checksum mismatch - the download is corrupt. Nothing was installed.")
            print("  checksum OK")
        else:
            print("  (no checksum published for this release - skipping verification)")

        print("Extracting ...")
        staging = Path(tempfile.mkdtemp(prefix=".protonmgr-", dir=compat_dir))
        extract_archive(archive, staging)

        entries = list(staging.iterdir())
        if not entries:
            raise ProtonMgrError("The archive was empty.")
        if len(entries) == 1 and entries[0].is_dir():
            top_level = entries                              # normal Proton layout
        else:
            wrapper = staging / ARCHIVE_RE.sub("", asset.name)   # loose files: give them a folder
            wrapper.mkdir()
            for entry in entries:
                shutil.move(str(entry), str(wrapper / entry.name))
            top_level = [wrapper]

        installed = []
        for entry in top_level:
            target = compat_dir / entry.name
            if target.exists() or target.is_symlink():
                remove_path(target)                          # reinstall over the old copy
            shutil.move(str(entry), str(target))
            manifest[entry.name] = {"source": source.key, "tag": release.tag}
            installed.append(entry.name)
        save_manifest(manifest)
        return installed
    finally:
        if staging:
            shutil.rmtree(staging, ignore_errors=True)
        archive.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# Interface
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Small helpers used by the screens
# --------------------------------------------------------------------------

def version_key(name: str) -> tuple:
    """Sort key that orders folder names by the numbers inside them, so that
    'GE-Proton11-11' sorts after 'GE-Proton11-3' (plain text sorting would not)."""
    return tuple(int(n) for n in re.findall(r"\d+", name))


def old_versions(names: list, keep: int = 2) -> set:
    """Folders that are older than the newest `keep` of their branch.

    Only branches this tool knows (GE, CachyOS, DW) are considered; anything
    else (UMU-Proton, hand-made folders) is never suggested for removal.
    """
    groups = {}
    for name in names:
        branch = branch_of(name)
        if branch != "Other / unknown":
            groups.setdefault(branch, []).append(name)
    old = set()
    for items in groups.values():
        items.sort(key=version_key, reverse=True)
        old.update(items[keep:])
    return old


def dir_size(path: Path) -> int:
    """Approximate size in bytes of everything under `path` (symlinks are not
    followed, unreadable entries are skipped)."""
    total, stack = 0, [str(path)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return total


def plain_markdown(text: str) -> str:
    """Make GitHub release notes readable in a terminal: drop link targets,
    bold markers, heading hashes and Windows line endings."""
    text = text.replace("\r", "")
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = text.replace("**", "").replace("`", "")
    text = re.sub(r"^#+\s*", "", text, flags=re.M)
    return text.strip()


def steam_is_running() -> bool:
    """True if a Steam process is running (it must be restarted to see new tools)."""
    try:
        return subprocess.run(["pgrep", "-x", "steam"], capture_output=True).returncode == 0
    except OSError:
        return False


def pause(message: str) -> None:
    """Wait for Enter (used while the menu is suspended to show install output)."""
    try:
        input(message)
    except (EOFError, KeyboardInterrupt):
        print()


# The download queue is one dict shared by every screen, so it survives
# scrolling, filtering and switching between branches. Key: (source key,
# release tag); value: (Source, Release). Insertion order is install order.

def queue_key(source: Source, release: Release) -> tuple:
    """Identity of a release inside the queue."""
    return (source.key, release.tag)


def toggle_queue(queue: dict, source: Source, release: Release) -> bool:
    """Add the release to the queue, or remove it if already there.
    Returns True when it is queued afterwards."""
    key = queue_key(source, release)
    if key in queue:
        del queue[key]
        return False
    queue[key] = (source, release)
    return True


def queue_total_size(queue: dict, cpu_level: int) -> int:
    """Total download size of everything in the queue, in bytes."""
    total = 0
    for _, release in queue.values():
        asset = pick_asset(release, cpu_level)
        total += asset.size if asset else 0
    return total


# --------------------------------------------------------------------------
# App state: what stays alive while you move between screens
# --------------------------------------------------------------------------

@dataclass
class Feed:
    """The releases of one branch fetched so far (grows as you scroll down)."""
    source: Source
    releases: list = field(default_factory=list)
    next_page: int = 1
    has_more: bool = True
    note: str = ""

    def load_more(self) -> None:
        """Fetch the next page and append it. Raises ProtonMgrError on failure
        and leaves the feed unchanged in that case."""
        releases, has_next, note = fetch_page(self.source, self.next_page)
        known = {r.tag for r in self.releases}
        self.releases += [r for r in releases if r.tag not in known]
        self.next_page += 1
        self.has_more = has_next
        if note:
            self.note = note


@dataclass
class App:
    """Shared state for the whole session."""
    compat_dir: Path
    manifest: dict
    cpu_level: int
    queue: dict = field(default_factory=dict)
    feeds: dict = field(default_factory=dict)

    def feed(self, source: Source) -> Feed:
        """The (cached) release feed of a branch."""
        return self.feeds.setdefault(source.key, Feed(source))


# --------------------------------------------------------------------------
# Terminal layer: reads keys, paints rows. Everything above this layer talks
# to a small interface (size / draw / key / suspended) so it can be tested
# without a real terminal.
# --------------------------------------------------------------------------

KEY_NAMES = {
    curses.KEY_UP: "up", curses.KEY_DOWN: "down",
    curses.KEY_LEFT: "left", curses.KEY_RIGHT: "right",
    curses.KEY_HOME: "home", curses.KEY_END: "end",
    curses.KEY_NPAGE: "pgdn", curses.KEY_PPAGE: "pgup",
    curses.KEY_ENTER: "enter", curses.KEY_BACKSPACE: "backspace",
    curses.KEY_DC: "delete", curses.KEY_RESIZE: "resize",
}
CHAR_NAMES = {
    "\n": "enter", "\r": "enter", " ": "space", "\x1b": "esc",
    "\x7f": "backspace", "\b": "backspace", "\t": "tab",
}


class CursesUi:
    """The real terminal. A row is a list of (text, style) pieces."""

    def __init__(self, screen):
        self.screen = screen
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        screen.keypad(True)
        self.attrs = {"normal": 0, "title": curses.A_BOLD, "dim": curses.A_DIM,
                      "key": curses.A_BOLD, "good": 0, "warn": curses.A_BOLD, "bad": curses.A_BOLD}
        if curses.has_colors():
            curses.start_color()
            try:
                curses.use_default_colors()
                background = -1
            except curses.error:
                background = curses.COLOR_BLACK
            colors = [("title", curses.COLOR_CYAN, curses.A_BOLD),
                      ("key", curses.COLOR_YELLOW, curses.A_BOLD),
                      ("good", curses.COLOR_GREEN, 0),
                      ("warn", curses.COLOR_YELLOW, 0),
                      ("bad", curses.COLOR_RED, curses.A_BOLD)]
            for number, (name, color, extra) in enumerate(colors, 1):
                curses.init_pair(number, color, background)
                self.attrs[name] = curses.color_pair(number) | extra

    def size(self) -> tuple:
        """(height, width) of the terminal."""
        return self.screen.getmaxyx()

    def draw(self, rows: list, cursor: int | None = None) -> None:
        """Paint rows top to bottom; the row at index `cursor` is highlighted."""
        height, width = self.size()
        self.screen.erase()
        for y, row in enumerate(rows[:height]):
            x = 0
            highlight = curses.A_REVERSE if y == cursor else 0
            for text, style in row:
                room = width - x - (1 if y == height - 1 else 0)   # never touch the bottom-right cell
                if room <= 0:
                    break
                try:
                    self.screen.addnstr(y, x, text, room, self.attrs.get(style, 0) | highlight)
                except curses.error:
                    pass
                x += len(text)
            if highlight and x < width - 1:
                try:
                    self.screen.addnstr(y, x, " " * (width - 1 - x), width - 1 - x, highlight)
                except curses.error:
                    pass
        self.screen.refresh()

    def key(self) -> str:
        """Block until a key is pressed; returns 'up', 'enter', 'space', 'a', ..."""
        failures = 0
        while True:
            try:
                ch = self.screen.get_wch()
            except curses.error:
                failures += 1
                if failures > 100:
                    raise EOFError("terminal input closed")
                continue
            failures = 0
            if isinstance(ch, int):
                if ch in KEY_NAMES:
                    return KEY_NAMES[ch]
                continue
            if ch in CHAR_NAMES:
                return CHAR_NAMES[ch]
            if ch.isprintable():
                return ch

    @contextlib.contextmanager
    def suspended(self):
        """Temporarily give the terminal back (for download progress output)."""
        curses.def_prog_mode()
        curses.endwin()
        try:
            yield
        finally:
            curses.reset_prog_mode()
            self.screen.clear()
            self.screen.refresh()


# --------------------------------------------------------------------------
# Layout helpers
# --------------------------------------------------------------------------

class ListState:
    """Cursor and scroll position of a list."""

    def __init__(self):
        self.cursor = 0
        self.top = 0

    def fit(self, count: int, avail: int) -> None:
        """Keep the cursor inside the list and the cursor row on screen."""
        if count <= 0:
            self.cursor = self.top = 0
            return
        self.cursor = min(max(self.cursor, 0), count - 1)
        if self.cursor < self.top:
            self.top = self.cursor
        elif self.cursor >= self.top + avail:
            self.top = self.cursor - avail + 1
        self.top = max(0, min(self.top, max(0, count - avail)))

    def handle(self, key: str, count: int, avail: int) -> str | None:
        """Apply a movement key. Returns None if `key` is not a movement key,
        'past_end' if it tried to go beyond the last item, else 'moved'."""
        if key in ("up", "k"):
            self.cursor = max(0, self.cursor - 1)
        elif key in ("down", "j"):
            if self.cursor >= count - 1:
                return "past_end"
            self.cursor += 1
        elif key == "pgup":
            self.cursor = max(0, self.cursor - avail)
        elif key == "pgdn":
            if self.cursor >= count - 1:
                return "past_end"
            self.cursor = min(count - 1, self.cursor + avail)
        elif key == "home":
            self.cursor = 0
        elif key == "end":
            self.cursor = max(0, count - 1)
        else:
            return None
        return "moved"


def header_rows(title: str, subtitle: str = "") -> list:
    """Title line, grey subtitle line, blank line."""
    return [[(" " + title, "title")], [(" " + subtitle, "dim")], []]


def hint_rows(width: int, hints: list) -> list:
    """Pack (key, description) hints into as many lines as the width needs."""
    rows, current, used = [], [], 1
    for key, description in hints:
        piece = len(key) + len(description) + 4
        if current and used + piece > width - 1:
            rows.append(current)
            current, used = [], 1
        current += [(" " if not current else "", "normal"), (key, "key"), (f" {description}  ", "dim")]
        used += piece
    if current:
        rows.append(current)
    return rows


def paint_list(ui, header: list, body: list, state: ListState, footer: list,
               highlight: bool = True) -> int:
    """Draw header + the visible slice of `body` + footer. Returns how many
    body rows fit on screen."""
    height, width = ui.size()
    if height < 8 or width < 30:
        ui.draw([[("Terminal too small - please enlarge the window.", "bad")]])
        return 1
    avail = max(1, height - len(header) - len(footer))
    state.fit(len(body), avail)
    shown = body[state.top:state.top + avail]
    rows = list(header) + shown + [[] for _ in range(avail - len(shown))] + list(footer)
    cursor = len(header) + state.cursor - state.top if (highlight and body) else None
    ui.draw(rows[:height], cursor)
    return avail


def wrap_text(text: str, width: int) -> list:
    """Word-wrap text to `width` columns, keeping blank lines."""
    lines = []
    for paragraph in text.split("\n"):
        lines += textwrap.wrap(paragraph, width) or [""]
    return lines


# --------------------------------------------------------------------------
# Dialogs: confirm, message, text input, pager
# --------------------------------------------------------------------------

def dialog_lines(ui, lines: list, reserve: int) -> list:
    """Rows for dialog text, cut with '... and N more' so it fits the window."""
    height, _ = ui.size()
    room = max(3, height - reserve)
    lines = list(lines)
    if len(lines) > room:
        extra = len(lines) - (room - 1)
        lines = lines[:room - 1] + [f"... and {extra} more"]
    return [[(" " + line, "normal")] for line in lines]


def confirm_dialog(ui, title: str, lines: list, default: bool = False) -> bool:
    """Yes/no question. y = yes, n or Esc = no, Enter = the default."""
    while True:
        rows = [[(" " + title, "title")], []] + dialog_lines(ui, lines, 7) + [
            [], [(" " + ("[Y/n]" if default else "[y/N]"), "key"),
                 ("  y = yes   n = no   Esc = cancel", "dim")]]
        ui.draw(rows)
        key = ui.key()
        if key in ("y", "Y"):
            return True
        if key in ("n", "N", "esc"):
            return False
        if key == "enter":
            return default


def message_dialog(ui, title: str, lines: list) -> None:
    """Show text until any key is pressed."""
    rows = [[(" " + title, "title")], []] + dialog_lines(ui, lines, 6) + [
        [], [(" Press any key to continue.", "dim")]]
    ui.draw(rows)
    ui.key()


def text_input(ui, title: str, prompt: str, initial: str = "") -> str | None:
    """One-line text box. Enter accepts, Esc cancels (returns None)."""
    buffer = initial
    while True:
        ui.draw([[(" " + title, "title")], [(" " + prompt, "dim")], [],
                 [(" > ", "key"), (buffer + "_", "normal")], [],
                 [(" Enter = apply   Esc = cancel", "dim")]])
        key = ui.key()
        if key == "enter":
            return buffer
        if key == "esc":
            return None
        if key == "backspace":
            buffer = buffer[:-1]
        elif key == "space":
            buffer += " "
        elif len(key) == 1:
            buffer += key


def pager(ui, title: str, text: str) -> None:
    """Scrollable read-only text (release notes). Esc, q or Left closes it."""
    top = 0
    while True:
        height, width = ui.size()
        lines = wrap_text(text, max(20, width - 3))
        header = header_rows(title)[:1] + [[]]
        footer = hint_rows(width, [("Up/Down", "scroll"), ("PgUp/PgDn", "page"), ("Esc", "close")])
        avail = max(1, height - len(header) - len(footer))
        top = max(0, min(top, max(0, len(lines) - avail)))
        body = [[(" " + line, "normal")] for line in lines[top:top + avail]]
        rows = header + body + [[] for _ in range(avail - len(body))] + footer
        ui.draw(rows[:height])
        key = ui.key()
        if key in ("esc", "q", "left", "enter", "backspace"):
            return
        if key in ("up", "k"):
            top -= 1
        elif key in ("down", "j", "space"):
            top += 1
        elif key == "pgup":
            top -= avail
        elif key == "pgdn":
            top += avail
        elif key == "home":
            top = 0
        elif key == "end":
            top = len(lines)


# --------------------------------------------------------------------------
# Installing from the screens
# --------------------------------------------------------------------------

def release_row(app: App, source: Source, release: Release, installed: bool,
                show_branch: bool = False, new_label: str = "") -> list:
    """One line describing a release: checkbox, installed marker, name, date, size."""
    asset = pick_asset(release, app.cpu_level)
    queued = queue_key(source, release) in app.queue
    row = [(" [x] " if queued else " [ ] ", "warn" if queued else "normal")]
    if installed:
        row.append(("[installed] ", "good"))
    elif new_label:
        row.append((f"{new_label:<11} ", "warn"))
    else:
        row.append((" " * 12, "normal"))
    if show_branch:
        row.append((f"{source.label.split(' (')[0]:<15} ", "dim"))
    row.append((f"{release.tag:<36} ", "normal"))
    row.append((f"{release.published}  ", "dim"))
    row.append((f"{human_size(asset.size) if asset else '?':>9}", "dim"))
    if release.prerelease:
        row.append(("  pre-release", "warn"))
    return row


def queue_status_row(app: App) -> list:
    """The 'Queue: ...' line shown under release lists."""
    if not app.queue:
        return [(" Queue is empty - press Space on a version to select it.", "dim")]
    return [(f" Queue: {len(app.queue)} selected, "
             f"{human_size(queue_total_size(app.queue, app.cpu_level))} to download", "warn")]


def plan_install(ui, app: App, items: list) -> list:
    """Ask the questions before installing; returns the final list (maybe empty)."""
    dirs = installed_dirs(app.compat_dir)
    already = [(s, r) for s, r in items if is_installed(s, r, dirs, app.manifest)]
    if already:
        lines = [f"{len(already)} of the selected versions are already installed:"]
        lines += [f"   {r.tag}" for _, r in already] + ["", "Skip them and install only the rest?"]
        if confirm_dialog(ui, "Already installed", lines, default=True):
            skipped = {queue_key(s, r) for s, r in already}
            items = [(s, r) for s, r in items if queue_key(s, r) not in skipped]
            for key in skipped:
                app.queue.pop(key, None)
    if not items:
        message_dialog(ui, "Nothing to install", ["Everything selected is already installed."])
        return []
    total = sum(pick_asset(r, app.cpu_level).size for _, r in items)
    lines = [f"{r.tag:<36} {human_size(pick_asset(r, app.cpu_level).size):>9}" for _, r in items]
    lines += ["", f"Into: {app.compat_dir}", "Builds are chosen automatically for your CPU."]
    if not confirm_dialog(ui, f"Install {len(items)} version(s)?  {human_size(total)} to download",
                          lines, default=True):
        return []
    return items


def run_installs(items: list, app: App) -> tuple:
    """Install items one after another, printing progress. Returns
    (installed [(source, release)], failed [tag]). One failure does not stop
    the rest; Ctrl-C stops after cleaning up the current item."""
    done, failed = [], []
    for position, (source, release) in enumerate(items, 1):
        print(f"\n[{position}/{len(items)}] {source.label}: {release.tag}")
        try:
            install_release(source, release, pick_asset(release, app.cpu_level),
                            app.compat_dir, app.manifest)
        except KeyboardInterrupt:
            print("\nInterrupted - the remaining items were not installed.")
            break
        except (ProtonMgrError, urllib.error.URLError, http.client.HTTPException, OSError,
                tarfile.TarError, subprocess.CalledProcessError) as exc:
            print(f"  FAILED: {exc}")
            failed.append(release.tag)
            continue
        done.append((source, release))
    return done, failed


def install_items(ui, app: App, items: list) -> None:
    """Confirm, then install `items` with normal terminal output."""
    items = plan_install(ui, app, items)
    if not items:
        return
    with ui.suspended():
        done, failed = run_installs(items, app)
        for source, release in done:
            app.queue.pop(queue_key(source, release), None)
        summary = f"\nFinished: {len(done)} installed"
        if failed:
            summary += f", {len(failed)} failed ({', '.join(failed)}) - kept in the queue"
        print(summary + ".")
        if done:
            if steam_is_running():
                print("Steam is running - restart it to see the new version(s).")
            else:
                print("Start Steam, then choose a version under a game's Properties > Compatibility.")
        pause("Press Enter to return to the menu.")


# --------------------------------------------------------------------------
# Screens
# --------------------------------------------------------------------------

def load_feed(ui, app: App, feed: Feed, message: str = "Loading releases ...") -> bool:
    """Fetch the next page of a feed, showing a message. False on failure."""
    ui.draw(header_rows(feed.source.label) + [[(" " + message, "dim")]])
    try:
        feed.load_more()
    except ProtonMgrError as exc:
        message_dialog(ui, "Could not fetch releases",
                       str(exc).splitlines() + ["", "Check your internet connection and try again."])
        return False
    return True


def load_all(ui, app: App, feed: Feed, max_pages: int = 10) -> bool:
    """Load the remaining pages (up to a cap) so a filter can search them all."""
    loaded = 0
    while feed.has_more and loaded < max_pages:
        if not load_feed(ui, app, feed, f"Loading every release so the filter can search them "
                                        f"(page {feed.next_page}) ..."):
            return False
        loaded += 1
    return True


def visible_releases(feed: Feed, text: str, show_pre: bool) -> list:
    """Releases passing the name filter and the pre-release switch."""
    needle = text.lower()
    return [r for r in feed.releases if (show_pre or not r.prerelease) and needle in r.tag.lower()]


def show_notes(ui, app: App, source: Source, release: Release) -> None:
    """Release notes for one version in the pager."""
    asset = pick_asset(release, app.cpu_level)
    head = [source.label, f"{release.tag}  -  published {release.published}"]
    if asset:
        head.append(f"File: {asset.name} ({human_size(asset.size)})")
    notes = plain_markdown(release.body) or "(this release has no notes)"
    pager(ui, release.tag, "\n".join(head) + "\n\n" + notes)


def browse_screen(ui, app: App, source: Source) -> None:
    """Scrolling list of a branch's releases; more load as you reach the end."""
    feed = app.feed(source)
    if not feed.releases and feed.has_more and not load_feed(ui, app, feed):
        return
    state, filter_text, show_pre = ListState(), "", True
    while True:
        visible = visible_releases(feed, filter_text, show_pre)
        dirs = installed_dirs(app.compat_dir)
        flags = [is_installed(source, r, dirs, app.manifest) for r in visible]
        _, width = ui.size()

        bits = [f"{len(visible)} shown"]
        if feed.has_more:
            bits.append("more load as you scroll down")
        if filter_text:
            bits.append(f"filter: '{filter_text}'")
        if not show_pre:
            bits.append("pre-releases hidden")
        if feed.note:
            bits.append(feed.note)
        header = header_rows(source.label, ", ".join(bits))
        body = [release_row(app, source, r, flag) for r, flag in zip(visible, flags)]
        if not body:
            body = [[(" (nothing matches)", "dim")]]
        footer = [queue_status_row(app)] + hint_rows(width, [
            ("Up/Down", "move"), ("Space", "select"), ("Enter", "install"), ("i", "notes"),
            ("/", "filter"), ("a", "select all"), ("p", "pre-releases"), ("r", "refresh"),
            ("v", "queue"), ("Esc", "back")])
        avail = paint_list(ui, header, body, state, footer, highlight=bool(visible))

        key = ui.key()
        moved = state.handle(key, len(visible), avail)
        if moved == "past_end":
            if feed.has_more and load_feed(ui, app, feed):
                visible = visible_releases(feed, filter_text, show_pre)
                state.handle(key, len(visible), avail)
            continue
        if moved:
            continue

        current = visible[state.cursor] if visible else None
        if key == "space" and current:
            toggle_queue(app.queue, source, current)
            state.handle("down", len(visible), avail)
        elif key == "enter":
            if app.queue:
                install_items(ui, app, list(app.queue.values()))
            elif current:
                install_items(ui, app, [(source, current)])
        elif key in ("i", "right") and current:
            show_notes(ui, app, source, current)
        elif key == "a":
            targets = [r for r, flag in zip(visible, flags) if not flag]
            if targets and all(queue_key(source, r) in app.queue for r in targets):
                for release in targets:
                    app.queue.pop(queue_key(source, release), None)
            else:
                for release in targets:
                    app.queue.setdefault(queue_key(source, release), (source, release))
        elif key == "/":
            answer = text_input(ui, f"Filter {source.label}",
                                "Show only versions containing this text (empty clears the filter).",
                                filter_text)
            if answer is not None:
                filter_text = answer.strip()
                state.cursor = state.top = 0
                if filter_text and feed.has_more:
                    load_all(ui, app, feed)
        elif key == "p":
            show_pre = not show_pre
            state.cursor = state.top = 0
        elif key == "r":
            feed = app.feeds[source.key] = Feed(source)
            state = ListState()
            if not load_feed(ui, app, feed):
                return
        elif key == "v":
            queue_screen(ui, app)
        elif key == "esc" and filter_text:
            filter_text = ""
            state.cursor = state.top = 0
        elif key in ("esc", "q", "b", "left", "backspace"):
            return


def updates_screen(ui, app: App) -> None:
    """The newest stable release of every branch, with what is not installed yet."""
    state = ListState()
    results = None
    while True:
        if results is None:
            results = []
            for source in SOURCES:
                feed = app.feed(source)
                ui.draw(header_rows("Check for updates") + [[(f" Checking {source.label} ...", "dim")]])
                error = ""
                if not feed.releases and feed.has_more:
                    try:
                        feed.load_more()
                    except ProtonMgrError as exc:
                        error = str(exc).splitlines()[0]
                latest = next((r for r in feed.releases if not r.prerelease),
                              feed.releases[0] if feed.releases else None)
                results.append((source, latest, error or ("no releases found" if latest is None else "")))
        dirs = installed_dirs(app.compat_dir)
        _, width = ui.size()
        body, selectable = [], []
        for source, latest, error in results:
            if latest is None:
                body.append([(f" {source.label.split(' (')[0]:<15} could not check: {error}", "bad")])
                selectable.append(None)
            else:
                flag = is_installed(source, latest, dirs, app.manifest)
                body.append(release_row(app, source, latest, flag, show_branch=True, new_label="[new]"))
                selectable.append((source, latest))
        missing = sum(1 for item in selectable if item and not is_installed(item[0], item[1], dirs, app.manifest))
        header = header_rows("Check for updates",
                             "newest stable release of each branch - "
                             + (f"{missing} not installed yet" if missing else "you have them all"))
        footer = [queue_status_row(app)] + hint_rows(width, [
            ("Up/Down", "move"), ("Space", "select"), ("a", "select all new"), ("Enter", "install"),
            ("i", "notes"), ("r", "re-check"), ("Esc", "back")])
        avail = paint_list(ui, header, body, state, footer)

        key = ui.key()
        if state.handle(key, len(body), avail):
            continue
        current = selectable[state.cursor] if selectable else None
        if key == "space" and current:
            toggle_queue(app.queue, *current)
            state.handle("down", len(body), avail)
        elif key == "a":
            for item in selectable:
                if item and not is_installed(item[0], item[1], dirs, app.manifest):
                    app.queue.setdefault(queue_key(*item), item)
        elif key == "enter":
            if app.queue:
                install_items(ui, app, list(app.queue.values()))
            elif current:
                install_items(ui, app, [current])
        elif key in ("i", "right") and current:
            show_notes(ui, app, *current)
        elif key == "r":
            app.feeds.clear()
            results = None
        elif key in ("esc", "q", "b", "left", "backspace"):
            return


def queue_screen(ui, app: App) -> None:
    """Everything selected for download, across all branches."""
    state = ListState()
    while True:
        entries = list(app.queue.values())
        dirs = installed_dirs(app.compat_dir)
        _, width = ui.size()
        header = header_rows("Download queue", "versions selected on any branch")
        body = [release_row(app, s, r, is_installed(s, r, dirs, app.manifest), show_branch=True)
                for s, r in entries] or [[(" (the queue is empty)", "dim")]]
        footer = [queue_status_row(app)] + hint_rows(width, [
            ("Up/Down", "move"), ("Space", "remove"), ("c", "clear"), ("Enter", "install all"), ("Esc", "back")])
        avail = paint_list(ui, header, body, state, footer, highlight=bool(entries))

        key = ui.key()
        if state.handle(key, len(entries), avail):
            continue
        if key in ("space", "delete", "x") and entries:
            source, release = entries[state.cursor]
            app.queue.pop(queue_key(source, release), None)
        elif key == "c":
            app.queue.clear()
        elif key == "enter" and entries:
            install_items(ui, app, entries)
        elif key in ("esc", "q", "b", "left", "backspace"):
            return


def installed_screen(ui, app: App) -> None:
    """Installed versions with sizes; select some and delete them."""
    state, selected, sizes = ListState(), set(), {}
    while True:
        names = installed_dirs(app.compat_dir)
        missing = [n for n in names if n not in sizes]
        if missing:
            ui.draw(header_rows("Installed versions") + [[(" Measuring folders ...", "dim")]])
            for name in missing:
                sizes[name] = dir_size(app.compat_dir / name)
        selected &= set(names)
        _, width = ui.size()

        body = []
        for name in names:
            try:
                modified = datetime.fromtimestamp((app.compat_dir / name).stat().st_mtime).strftime("%Y-%m-%d")
            except OSError:
                modified = "?"
            mark = selected and name in selected
            body.append([(" [x] " if mark else " [ ] ", "warn" if mark else "normal"),
                         (f"{name:<46} ", "normal"), (f"{branch_of(name):<15} ", "dim"),
                         (f"{human_size(sizes[name]):>9}  ", "dim"), (modified, "dim")])
        if not body:
            body = [[(" (nothing installed yet)", "dim")]]
        total = sum(sizes.get(n, 0) for n in names)
        picked = sum(sizes.get(n, 0) for n in selected)
        header = header_rows("Installed versions", f"{app.compat_dir}")
        status = f" {len(names)} installed, {human_size(total)} on disk"
        if selected:
            status += f"   |   {len(selected)} selected ({human_size(picked)})"
        footer = [[(status, "warn" if selected else "dim")]] + hint_rows(width, [
            ("Up/Down", "move"), ("Space", "select"), ("a", "select all"),
            ("o", "select old (keep newest 2 per branch)"), ("d", "delete"), ("Esc", "back")])
        avail = paint_list(ui, header, body, state, footer, highlight=bool(names))

        key = ui.key()
        if state.handle(key, len(names), avail):
            continue
        current = names[state.cursor] if names else None
        if key == "space" and current:
            selected ^= {current}
            state.handle("down", len(names), avail)
        elif key == "a":
            selected = set() if selected == set(names) else set(names)
        elif key == "o":
            selected = old_versions(names)
            if not selected:
                message_dialog(ui, "Nothing old to select",
                               ["Every branch has two or fewer versions installed."])
        elif key in ("d", "delete") and (selected or current):
            targets = sorted(selected, key=version_key) if selected else [current]
            lines = [f"{n}   ({human_size(sizes.get(n, 0))})" for n in targets]
            lines += ["", "This permanently deletes the folders.",
                      "Games set to use a removed version will need another one."]
            if confirm_dialog(ui, f"Delete {len(targets)} version(s)?", lines, default=False):
                problems = []
                for name in targets:
                    try:
                        remove_path(app.compat_dir / name)
                    except OSError as exc:
                        problems.append(f"{name}: {exc}")
                        continue
                    app.manifest.pop(name, None)
                    sizes.pop(name, None)
                    selected.discard(name)
                save_manifest(app.manifest)
                if problems:
                    message_dialog(ui, "Some folders could not be deleted", problems)
        elif key in ("esc", "q", "b", "left", "backspace"):
            return


def root_screen(ui, app: App) -> None:
    """Main menu."""
    state = ListState()
    while True:
        count = len(installed_dirs(app.compat_dir))
        entries = [("Installed versions", f"{count} installed", lambda: installed_screen(ui, app))]
        for source in SOURCES:
            entries.append((f"Browse {source.label}", "", lambda s=source: browse_screen(ui, app, s)))
        entries.append(("Check for updates", "newest release of each branch", lambda: updates_screen(ui, app)))
        entries.append(("Download queue", f"{len(app.queue)} selected", lambda: queue_screen(ui, app)))
        entries.append(("Quit", "", None))

        _, width = ui.size()
        body = [[(f" {number}) {label:<44} ", "normal"), (info, "dim")]
                for number, (label, info, _) in enumerate(entries, 1)]
        header = header_rows("Proton Manager", f"Install folder: {app.compat_dir}")
        footer = hint_rows(width, [("Up/Down", "move"), ("Enter", "open"),
                                   ("1-9", "jump"), ("q", "quit")])
        avail = paint_list(ui, header, body, state, footer)

        key = ui.key()
        if state.handle(key, len(entries), avail):
            continue
        chosen = None
        if key == "enter":
            chosen = state.cursor
        elif key.isdigit() and 1 <= int(key) <= len(entries):
            chosen = state.cursor = int(key) - 1
        elif key in ("q", "esc"):
            chosen = len(entries) - 1
        if chosen is None:
            continue
        action = entries[chosen][2]
        if action is None:                       # Quit
            if app.queue and not confirm_dialog(
                    ui, "Quit?", [f"{len(app.queue)} version(s) are still selected for download.",
                                  "Quit and forget the selection?"], default=False):
                continue
            return
        action()


def run_tui(app: App) -> None:
    """Start the arrow-key interface in the current terminal."""
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")          # make the Esc key respond quickly
    curses.wrapper(lambda screen: root_screen(CursesUi(screen), app))


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

def cli_list(app: App) -> int:
    """--list: print installed versions and exit."""
    names = installed_dirs(app.compat_dir)
    print(f"Install folder: {app.compat_dir}")
    if not names:
        print("(nothing installed)")
    for name in names:
        print(f"  {name:<48} {branch_of(name)}")
    return 0


def cli_install_latest(app: App, key: str) -> int:
    """--latest KEY: install the newest stable release of one branch."""
    source = next((s for s in SOURCES if s.key == key), None)
    if source is None:
        print(f"Unknown branch '{key}'. Choose one of: {', '.join(s.key for s in SOURCES)}")
        return 2
    feed = Feed(source)
    try:
        feed.load_more()
    except ProtonMgrError as exc:
        print(exc)
        return 1
    release = next((r for r in feed.releases if not r.prerelease),
                   feed.releases[0] if feed.releases else None)
    if release is None:
        print("No downloadable release found.")
        return 1
    if is_installed(source, release, installed_dirs(app.compat_dir), app.manifest):
        print(f"{release.tag} is already installed.")
        return 0
    done, failed = run_installs([(source, release)], app)
    return 1 if failed or not done else 0


def main() -> int:
    """Entry point: parse options, then run a one-shot command or the interface."""
    parser = argparse.ArgumentParser(description="Browse, install and remove Proton builds.")
    parser.add_argument("--dir", help="compatibilitytools.d folder to manage (default: auto-detect Steam)")
    parser.add_argument("--list", action="store_true", help="print the installed versions and exit")
    parser.add_argument("--latest", metavar="BRANCH",
                        help="install the newest stable release of a branch and exit "
                             f"({', '.join(s.key for s in SOURCES)})")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    args = parser.parse_args()
    try:
        compat_dir = resolve_compat_dir(args.dir)
    except OSError as exc:
        print(f"Cannot use install folder: {exc}")
        return 1
    app = App(compat_dir, load_manifest(), detect_cpu_level())
    try:
        if args.list:
            return cli_list(app)
        if args.latest:
            return cli_install_latest(app, args.latest)
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            print("protonmgr needs an interactive terminal. For scripts use --list or --latest BRANCH.")
            return 2
        run_tui(app)
    except (KeyboardInterrupt, EOFError):
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
