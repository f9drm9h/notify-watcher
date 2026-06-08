"""Build data/music_seed.json from a local music folder (run locally, not in CI).

The cloud watcher can't see your music folder, so this script scans it here and
writes a small taste-profile manifest (a list of artist names) that the discovery
feature in notify_watcher/topics/music.py reads. Re-run it whenever you add music
and commit the updated data/music_seed.json.

Artist resolution, in order of preference per file:
  1. The file's ID3 'artist' tag, if `mutagen` is installed.
  2. Otherwise the Deezer search API (no key): the cleaned filename is queried
     and the best-matching track's artist is taken. Many filenames here are
     title-only ("Blinding Lights.mp3"), so this is the reliable path.

Only audio files are scanned; .html/.pdf/.exe/.jpg/.docx/.xlsx etc. are skipped.

Usage:
  python tools/scan_music.py ["<folder path>"]
  (defaults to ~/Desktop/New music for iTunes 2026)
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

import requests

AUDIO_EXTS = {".mp3", ".m4a", ".flac", ".wav", ".aac", ".ogg", ".opus", ".wma"}
DEFAULT_FOLDER = Path.home() / "Desktop" / "New music for iTunes 2026"
OUT_PATH = Path(__file__).resolve().parent.parent / "data" / "music_seed.json"
HEADERS = {"User-Agent": "notify-watcher/1.0 (+https://github.com/) personal-use"}

# YouTube-rip noise to strip from filenames before searching.
_JUNK = re.compile(
    r"\[[^\]]*\]|\([^)]*\)|\b(official|video|audio|lyric[s]?|letra|hd|hq|4k|1080p|720p|"
    r"vevo|visualizer|mv|m/v|cover|remaster(ed)?|explicit)\b",
    re.IGNORECASE,
)


def _clean(name: str) -> str:
    stem = Path(name).stem
    stem = _JUNK.sub(" ", stem)
    # Drop a trailing " - <uploader>" segment when there are 3+ dash parts.
    parts = [p.strip() for p in stem.split(" - ") if p.strip()]
    if len(parts) >= 3:
        stem = " - ".join(parts[:-1])
    return re.sub(r"\s+", " ", stem).strip(" -_")


def _artist_from_tag(path: Path) -> str | None:
    try:
        from mutagen import File as MutagenFile  # type: ignore
    except Exception:  # noqa: BLE001 - mutagen optional
        return None
    try:
        audio = MutagenFile(path, easy=True)
        if audio and audio.get("artist"):
            return str(audio["artist"][0]).strip() or None
    except Exception:  # noqa: BLE001
        return None
    return None


def _artist_from_deezer(query: str) -> str | None:
    if not query:
        return None
    try:
        resp = requests.get("https://api.deezer.com/search", params={"q": query, "limit": 1},
                            headers=HEADERS, timeout=20)
        resp.raise_for_status()
        data = resp.json().get("data") or []
        if data:
            return (data[0].get("artist") or {}).get("name") or None
    except Exception:  # noqa: BLE001
        return None
    return None


def main() -> int:
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_FOLDER
    if not folder.is_dir():
        print(f"folder not found: {folder}", file=sys.stderr)
        return 1

    files = [p for p in sorted(folder.iterdir())
             if p.is_file() and p.suffix.lower() in AUDIO_EXTS]
    print(f"scanning {len(files)} audio file(s) in {folder}")

    counts: Counter[str] = Counter()
    for i, path in enumerate(files, 1):
        artist = _artist_from_tag(path) or _artist_from_deezer(_clean(path.name))
        if artist:
            counts[artist.strip()] += 1
        if i % 25 == 0:
            print(f"  ...{i}/{len(files)}")
        if not _artist_from_tag(path):  # only throttle the network path
            time.sleep(0.15)  # be gentle with Deezer's rate limit

    artists = sorted(counts)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(
        json.dumps({"generated": _dt.date.today().isoformat(),
                    "source": str(folder), "artists": artists}, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(artists)} unique artist(s) -> {OUT_PATH}")
    top = ", ".join(f"{a} ({n})" for a, n in counts.most_common(12))
    print(f"top: {top}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
