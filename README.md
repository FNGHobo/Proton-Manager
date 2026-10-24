# Proton-Manager

A small menu-driven terminal tool for browsing, installing and removing custom
Proton builds for Steam on Linux. No dependencies beyond Python's standard library.

The program is a single file, `protonmgr.py`.

Supported branches:

- **GE-Proton** (GloriousEggroll)
- **Proton-CachyOS**
- **DW Proton** (Dawn Winery)

## Features

- **Arrow-key interface:** move with the arrow keys, select with Space, confirm
  with Enter. No typing numbers.
- Lists what you have installed, with the size of each version on disk.
- Browses each branch's online releases in one scrolling list (more load as you
  reach the bottom) and marks the ones you already have with `[installed]`.
- **Multi-select download queue:** pick several releases with Space. The queue is
  kept while you scroll, filter, or jump between branches, then installs all at
  once.
- **Check for updates:** shows the newest stable release of each branch and
  whether you have it, and can queue the missing ones in one key.
- **Filter** releases by name, show or hide pre-releases, and read each
  release's **notes** before installing.
- **Cleanup:** select several installed versions and delete them in one go, or
  let it pre-select the older ones (everything except the newest two of each
  branch; folders it doesn't recognise are never selected).
- Verifies the published checksum (sha512/sha256) before installing, and
  extracts into a staging folder first, so a failed install never leaves a
  half-unpacked version behind.
- Picks the right CachyOS build (`x86_64`, `x86_64_v3`, `x86_64_v4`) for your CPU.
- Scriptable: `--list` and `--latest BRANCH` work without the interface.

## Requirements

protonmgr has **no third-party Python packages**. Everything it imports ships
with Python itself, so there is nothing to `pip install`.

| Needed | Why | Notes |
| --- | --- | --- |
| Linux on x86_64 | Proton builds are for 64-bit x86 Linux | ARM and "signed" builds are skipped |
| A normal terminal | The arrow-key interface uses Python's built-in `curses` module | Any common Linux terminal works. It needs to be a real terminal, so it won't run from a pipe or cron job (use `--list` / `--latest` for those) |
| Python 3.8 or newer | Runs the program | Tested on 3.11, 3.12 and 3.13; the code only uses features that exist in 3.8, but older versions haven't been run. Python must include the standard `lzma` module (normal on distro packages), because CachyOS and DW Proton ship `.tar.xz` files |
| Steam | Where the installed Proton versions get used | Native or Flatpak. Steam does not need to be running, and the `compatibilitytools.d` folder is created if it is missing |
| Internet access | Fetches release lists and downloads | Needs `api.github.com` and GitHub's release downloads. `dawn.wine` is tried first for DW Proton, with GitHub as the fallback |
| `pgrep` (from `procps`) | Optional | Only used to tell you whether Steam is running and needs a restart |
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
The available keys are always listed at the bottom of the screen.

**Everywhere**

| Key | Does |
| --- | --- |
| Up / Down (or `k` / `j`) | Move |
| PgUp / PgDn, Home / End | Jump |
| Enter | Open or confirm |
| Esc (or `q`, Left) | Go back |

**Browsing a branch**

| Key | Does |
| --- | --- |
| Space | Select or unselect a version (it joins the queue) |
| Enter | Install the queue, or the highlighted version if nothing is selected |
| `i` or Right | Read the release notes |
| `/` | Filter by name (Esc clears it) |
| `a` | Select every version on screen that isn't installed yet |
| `p` | Show or hide pre-releases |
| `v` | View the queue |
| `r` | Refresh the list |

**Installed versions**

| Key | Does |
| --- | --- |
| Space | Select a version |
| `a` | Select all |
| `o` | Select old versions (all but the newest two of each branch) |
| `d` | Delete the selected versions, or the highlighted one if none are selected |

Installing asks for confirmation first and shows the total download size. While
files download, the interface steps aside so you can see normal progress output,
then returns. After installing, restart Steam and choose the version under a
game's Properties > Compatibility.

## Command line

| Command | Does |
| --- | --- |
| `protonmgr` | Open the interface |
| `protonmgr --list` | Print the installed versions and exit |
| `protonmgr --latest ge` | Install the newest stable release of a branch (`ge`, `cachyos` or `dw`) and exit. Does nothing if you already have it |
| `protonmgr --dir PATH` | Manage a different `compatibilitytools.d` folder |
| `protonmgr --version` | Print the version |

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
