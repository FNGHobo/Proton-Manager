#!/usr/bin/env python3
"""protonmgr - a small menu-driven terminal tool for managing Proton builds.

What it does
------------
* Lists the Proton versions installed in Steam's compatibilitytools.d folder.
* Browses the online releases of GE-Proton, Proton-CachyOS and DW Proton and
  marks every release that is already installed.
* Lets you select several releases at once (numbers, ranges like 1-3) into a
  download queue that is kept while you change pages or switch branches, then
  installs the whole queue: each download is checksum-verified when a checksum
  is published, then extracted into compatibilitytools.d.
* Removes installed versions (with a typed confirmation).

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
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

APP_NAME = "protonmgr"
USER_AGENT = f"{APP_NAME}/1.0 (terminal Proton version manager)"
PAGE_SIZE = 15            # releases shown per page when browsing online
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
    return Release(raw["tag_name"], published[:10], bool(raw.get("prerelease")), assets)


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
# Menus
# --------------------------------------------------------------------------

def ask(message: str) -> str:
    """Prompt for one line of input (stripped)."""
    return input(message).strip()


def confirm(message: str, default_yes: bool) -> bool:
    """Yes/no question; Enter picks the default."""
    hint = "[Y/n]" if default_yes else "[y/N]"
    answer = ask(f"{message} {hint} ").lower()
    return default_yes if not answer else answer.startswith("y")


def parse_selection(text: str, count: int) -> list | None:
    """Turn input like '1 2 4' or '1,3-5' into sorted unique 1-based numbers.

    Returns None when any token is not a number or range inside 1..count, so a
    typo never half-applies.
    """
    picked = set()
    for token in re.split(r"[,\s]+", text.strip()):
        if not token:
            continue
        match = re.fullmatch(r"(\d+)(?:-(\d+))?", token)
        if not match:
            return None
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if start < 1 or start > end or end > count:
            return None
        picked.update(range(start, end + 1))
    return sorted(picked) or None


# The download queue is one dict shared by every menu, so it survives page
# changes and switching between branches. Key: (source key, release tag);
# value: (Source, Release). Insertion order is the install order.

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
    return sum(pick_asset(r, cpu_level).size for _, r in queue.values())


def installed_menu(compat_dir: Path, manifest: dict) -> None:
    """Show installed versions; numbers (e.g. '1 3' or '2-4') remove them."""
    while True:
        names = installed_dirs(compat_dir)
        print("\n=== Installed versions ===")
        print(f"Folder: {compat_dir}\n")
        if not names:
            print("  (nothing installed yet)")
        for number, name in enumerate(names, 1):
            try:
                modified = datetime.fromtimestamp((compat_dir / name).stat().st_mtime).strftime("%Y-%m-%d")
            except OSError:
                modified = "?"
            print(f"{number:>3}) {name:<48} {branch_of(name):<14} {modified}")
        print("\nEnter numbers to REMOVE those versions (e.g. 1 3 or 2-4), or b to go back.")
        choice = ask("> ").lower()
        if choice in ("b", "back", ""):
            return
        picked = parse_selection(choice, len(names))
        if not picked:
            print("Not a valid choice.")
            continue
        doomed = [names[n - 1] for n in picked]
        print("About to permanently delete:")
        for name in doomed:
            print(f"   {name}")
        if ask("Type 'yes' to confirm: ").lower() == "yes":
            for name in doomed:
                remove_path(compat_dir / name)
                manifest.pop(name, None)
            save_manifest(manifest)
            print(f"Removed {len(doomed)} version(s).")


def install_queue(queue: dict, compat_dir: Path, manifest: dict, cpu_level: int) -> None:
    """Install everything in the queue, one after another.

    Already-installed items can be skipped. One failure does not stop the rest;
    failed items stay queued so they can be retried. Ctrl-C stops after the
    current item is cleaned up and leaves the remainder queued.
    """
    if not queue:
        print("\nThe queue is empty.")
        return

    items = list(queue.values())
    dirs = installed_dirs(compat_dir)
    already = [(s, r) for s, r in items if is_installed(s, r, dirs, manifest)]
    if already:
        print(f"\n{len(already)} queued version(s) are already installed:")
        for _, release in already:
            print(f"   {release.tag}")
        if confirm("Skip those and install only the rest?", default_yes=True):
            skipped = {queue_key(s, r) for s, r in already}
            for key in skipped:
                queue.pop(key, None)
            items = [(s, r) for s, r in items if queue_key(s, r) not in skipped]
    if not items:
        print("Nothing left to install.")
        return

    total = sum(pick_asset(r, cpu_level).size for _, r in items)
    print(f"\nWill install {len(items)} version(s) - {human_size(total)} to download:")
    for source, release in items:
        asset = pick_asset(release, cpu_level)
        print(f"   {release.tag:<36} {asset.name}  ({human_size(asset.size)})")
    print("(Builds are chosen automatically for your CPU.)")
    if not confirm("Start?", default_yes=True):
        return

    done, failed = [], []
    for position, (source, release) in enumerate(items, 1):
        print(f"\n[{position}/{len(items)}] {source.label}: {release.tag}")
        try:
            install_release(source, release, pick_asset(release, cpu_level), compat_dir, manifest)
        except KeyboardInterrupt:
            print("\nInterrupted - the remaining items stay in the queue.")
            break
        except (ProtonMgrError, urllib.error.URLError, OSError,
                tarfile.TarError, subprocess.CalledProcessError) as exc:
            print(f"  FAILED: {exc}")
            failed.append(release.tag)
            continue
        queue.pop(queue_key(source, release), None)
        done.append(release.tag)

    summary = f"\nFinished: {len(done)} installed"
    if failed:
        summary += f", {len(failed)} failed ({', '.join(failed)}) - kept in the queue"
    print(summary + ".")
    if done:
        print("Restart Steam, then choose a version under a game's Properties > Compatibility.")
    ask("Press Enter to continue.")


def queue_menu(queue: dict, compat_dir: Path, manifest: dict, cpu_level: int) -> None:
    """View the queue: remove entries, clear it, or install it."""
    while True:
        print("\n=== Download queue ===")
        entries = list(queue.values())
        if not entries:
            print("  (empty)")
        for number, (source, release) in enumerate(entries, 1):
            asset = pick_asset(release, cpu_level)
            dirs = installed_dirs(compat_dir)
            mark = "[installed]" if is_installed(source, release, dirs, manifest) else "           "
            print(f"{number:>3}) {mark} {release.tag:<36} {source.label.split(' (')[0]:<15} {human_size(asset.size):>9}")
        if entries:
            print(f"\nTotal: {len(entries)} version(s), {human_size(queue_total_size(queue, cpu_level))}")
        print("\nnumbers = remove from queue   i = install all   c = clear   b = back")
        choice = ask("> ").lower()
        if choice in ("b", "back", ""):
            return
        if choice == "c":
            queue.clear()
        elif choice == "i":
            install_queue(queue, compat_dir, manifest, cpu_level)
        else:
            picked = parse_selection(choice, len(entries))
            if not picked:
                print("Not a valid choice.")
                continue
            for number in picked:
                source, release = entries[number - 1]
                queue.pop(queue_key(source, release), None)


def browse_menu(source: Source, compat_dir: Path, manifest: dict,
                cpu_level: int, queue: dict) -> None:
    """Page through a branch's online releases and queue versions for install.

    Fetched pages are cached while this menu is open, so changing pages back
    and forth (or mistyping) does not hit the network again; `r` refreshes.
    """
    page = last_good_page = 1
    cache = {}
    while True:
        if page not in cache:
            print(f"\nFetching {source.label} releases (page {page}) ...")
            try:
                cache[page] = fetch_page(source, page)
            except ProtonMgrError as exc:
                print(exc)
                if not cache:
                    ask("Press Enter to go back.")
                    return
                page = last_good_page
                continue
        last_good_page = page
        releases, has_next, note = cache[page]
        dirs = installed_dirs(compat_dir)
        installed = [is_installed(source, r, dirs, manifest) for r in releases]

        print(f"\n=== {source.label} - page {page} ===")
        if note:
            print(note)
        if not releases:
            print("  (no downloadable releases on this page)")
        for number, release in enumerate(releases, 1):
            asset = pick_asset(release, cpu_level)
            queued = "[x]" if queue_key(source, release) in queue else "[ ]"
            mark = "[installed]" if installed[number - 1] else "           "
            pre = " (pre-release)" if release.prerelease else ""
            print(f"{number:>3}) {queued} {mark} {release.tag:<36} {release.published}  "
                  f"{human_size(asset.size):>9}{pre}")

        if queue:
            print(f"\nQueue: {len(queue)} selected, {human_size(queue_total_size(queue, cpu_level))}"
                  "  (stays when you change pages or branches)")
        print("\nSelect: numbers/ranges to toggle (e.g. 1 3 5-7)   a = toggle all not-installed on this page")
        nav = []
        if has_next:
            nav.append("n = next page")
        if page > 1:
            nav.append("p = previous page")
        nav += ["r = refresh", "v = view queue", "i = install queue", "b = back"]
        print("Go:     " + "   ".join(nav))

        choice = ask("> ").lower()
        if choice in ("b", "back", ""):
            return
        if choice == "n" and has_next:
            page += 1
        elif choice == "p" and page > 1:
            page -= 1
        elif choice == "r":
            cache.pop(page, None)
        elif choice == "v":
            queue_menu(queue, compat_dir, manifest, cpu_level)
        elif choice == "i":
            install_queue(queue, compat_dir, manifest, cpu_level)
        elif choice == "a":
            targets = [r for r, have in zip(releases, installed) if not have]
            if targets and all(queue_key(source, r) in queue for r in targets):
                for release in targets:
                    queue.pop(queue_key(source, release), None)
            else:
                for release in targets:
                    queue.setdefault(queue_key(source, release), (source, release))
        else:
            picked = parse_selection(choice, len(releases))
            if not picked:
                print("Not a valid choice.")
                continue
            for number in picked:
                toggle_queue(queue, source, releases[number - 1])


def root_menu(compat_dir: Path, cpu_level: int) -> None:
    """Top-level menu: installed versions, the download queue, and one entry
    per Proton branch. The queue lives here so it outlasts every submenu."""
    manifest = load_manifest()
    queue = {}
    while True:
        count = len(installed_dirs(compat_dir))
        print("\n" + "=" * 60)
        print(" Proton Manager")
        print(f" Install folder: {compat_dir}")
        print("=" * 60)
        print(f"  1) Installed versions ({count})")
        for number, source in enumerate(SOURCES, 2):
            print(f"  {number}) Browse online: {source.label}")
        print(f"  d) Download queue ({len(queue)})")
        print("  q) Quit")
        choice = ask("> ").lower()
        if choice in ("q", "quit", "exit"):
            return
        if choice == "1":
            installed_menu(compat_dir, manifest)
        elif choice == "d":
            queue_menu(queue, compat_dir, manifest, cpu_level)
        elif choice.isdigit() and 2 <= int(choice) < 2 + len(SOURCES):
            browse_menu(SOURCES[int(choice) - 2], compat_dir, manifest, cpu_level, queue)
        else:
            print("Not a valid choice.")


def main() -> int:
    """Entry point: parse options and run the menu until the user quits."""
    parser = argparse.ArgumentParser(description="Browse, install and remove Proton builds.")
    parser.add_argument("--dir", help="compatibilitytools.d folder to manage (default: auto-detect Steam)")
    args = parser.parse_args()
    try:
        compat_dir = resolve_compat_dir(args.dir)
    except OSError as exc:
        print(f"Cannot use install folder: {exc}")
        return 1
    try:
        root_menu(compat_dir, detect_cpu_level())
    except (KeyboardInterrupt, EOFError):
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
