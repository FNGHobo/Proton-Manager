# Proton-Manager

A small menu-driven terminal tool for browsing, installing and removing custom
Proton builds for Steam on Linux. No dependencies beyond Python's standard library.

The program is a single file, `protonmgr.py`.

Supported branches:

- **GE-Proton** (GloriousEggroll)
- **Proton-CachyOS**
- **DW Proton** (Dawn Winery)

## Features

- Lists the Proton versions you already have installed.
- Browses each branch's online releases, page by page, and marks the ones you
  already have with `[installed]`.
- Multi-select: pick several releases at once (`1 3 5-7`) into a download queue
  that is kept while you change pages or switch branches, then install the whole
  queue in one go.
- Verifies the published checksum (sha512/sha256) before installing, and
  extracts into a staging folder first so a failed install never leaves a
  half-unpacked version behind.
- Picks the right CachyOS build (`x86_64`, `x86_64_v3`, `x86_64_v4`) for your CPU.
- Removes installed versions, with a typed confirmation.

## Requirements

protonmgr has **no third-party Python packages**. Everything it imports ships
with Python itself, so there is nothing to `pip install`.

| Needed | Why | Notes |
| --- | --- | --- |
| Linux on x86_64 | Proton builds are for 64-bit x86 Linux | ARM and "signed" builds are skipped |
| Python 3.8 or newer | Runs the program | Developed and tested on 3.13. Python must include the standard `lzma` module (normal on distro packages), because CachyOS and DW Proton ship `.tar.xz` files |
| Steam | Where the installed Proton versions get used | Native or Flatpak. Steam does not need to be running, and the `compatibilitytools.d` folder is created if it is missing |
| Internet access | Fetches release lists and downloads | Needs `api.github.com` and GitHub's release downloads. `dawn.wine` is tried first for DW Proton, with GitHub as the fallback |
| `git` | Only to clone this repo | Not needed if you download the file directly |
| `zstd` and `tar` | Only for a release that ships solely as `.tar.zst` | Most releases use `.tar.gz` or `.tar.xz` and never need it. If it is missing, the tool tells you |

Disk space: each version is roughly 500 MB to download and over 1 GB once
extracted (CachyOS builds are around 1.4 GB), so keep a few GB free. Downloads
go to `~/.cache/protonmgr` and are deleted after each install.

## Installation

Install the few system packages first (skip any you already have):

```sh
# Arch / CachyOS / Manjaro
sudo pacman -S python git zstd

# Debian / Ubuntu / Mint
sudo apt install python3 git zstd

# Fedora
sudo dnf install python3 git zstd
```

Then get the program:

```sh
git clone https://github.com/FNGHobo/Proton-Manager.git
cd Proton-Manager
python3 protonmgr.py
```

Or download just the one file and run it:

```sh
curl -LO https://raw.githubusercontent.com/FNGHobo/Proton-Manager/main/protonmgr.py
python3 protonmgr.py
```

To run it as `protonmgr` from anywhere (assumes `~/.local/bin` is on your `PATH`):

```sh
chmod +x protonmgr.py
ln -s "$PWD/protonmgr.py" ~/.local/bin/protonmgr
```

To update later, run `git pull` inside the cloned folder.

## Usage

Start it with `python3 protonmgr.py` (or `protonmgr` if you linked it as above).

Menu keys: type a number to open an entry, `b` goes back, `q` quits. In a
release list, type numbers or ranges to toggle them, `a` toggles everything not
yet installed on the page, `n`/`p` change page, `v` views the queue, `i`
installs it, and `r` refreshes the list.

After installing, restart Steam and choose the version under a game's
Properties > Compatibility.

## Where it installs

The first existing Steam folder from this list is used, with
`compatibilitytools.d` inside it:

1. `~/.local/share/Steam`
2. `~/.steam/steam`
3. `~/.steam/root`
4. `~/.var/app/com.valvesoftware.Steam/data/Steam` (Flatpak Steam)

Override with `--dir PATH` or the `PROTONMGR_DIR` environment variable (the path
must be the `compatibilitytools.d` folder itself).

## Environment variables

| Variable | Purpose |
| --- | --- |
| `PROTONMGR_DIR` | Install folder, same as `--dir` |
| `PROTONMGR_CPU_LEVEL` | Force the CachyOS build level: `2`, `3` or `4` |
| `GITHUB_TOKEN` | Optional; raises GitHub's anonymous API rate limit (60 requests/hour) |

## Notes

- dawn.wine can reject scripted requests with a bot check. If it does, DW Proton
  releases are fetched from the official GitHub mirror
  (`dawn-winery/dwproton-mirror`) automatically, and a note is shown.
- The tool keeps a small record of what it installed in
  `~/.config/protonmgr/manifest.json`. It only helps the `[installed]` marker;
  deleting it is harmless.

## Adding another branch

Add a `Source(...)` entry to `SOURCES` in `protonmgr.py`. Any GitHub or Gitea
(Forgejo) releases endpoint that ships `.tar.gz`, `.tar.xz` or `.tar.zst`
assets works.

## License

MIT, see [LICENSE](LICENSE).
