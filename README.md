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

- Linux, x86_64 (ARM builds are skipped)
- Python 3.8 or newer (3.12+ recommended)
- `zstd` only if you install a release that ships solely as `.tar.zst`

## Usage

```sh
git clone https://github.com/FNGHobo/Proton-Manager.git
cd Proton-Manager
python3 protonmgr.py
```

To run it as `protonmgr` from anywhere:

```sh
chmod +x protonmgr.py
ln -s "$PWD/protonmgr.py" ~/.local/bin/protonmgr
```

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
