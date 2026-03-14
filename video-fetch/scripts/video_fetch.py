#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

DEFAULT_OUTPUT_DIR = Path.home() / 'downloads' / 'fetched'
DEFAULT_TEMP_DIR = Path.home() / 'downloads' / '.tmp-video-fetch'
DEFAULT_MAX_SUBS = 20
DEFAULT_MAX_HEIGHT = 720

VIDEO_EXTS = {'.mp4', '.mkv', '.webm', '.mov', '.avi', '.m4v', '.flv', '.mpeg', '.mpg'}
TEXT_EXTS = {'.json', '.info.json', '.description', '.txt', '.srt', '.vtt', '.ass', '.lrc'}


def bail(message: str, code: int = 2) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(code)


def root_dir() -> Path:
    return Path.home() / 'utils' / 'zeroclaw' / 'video-fetch'


def python_bin() -> Path:
    return root_dir() / '.venv' / 'bin' / 'python'


def ffmpeg_bin() -> str | None:
    direct = shutil.which('ffmpeg')
    if direct:
        return direct
    py = python_bin()
    if not py.exists():
        return None
    proc = subprocess.run([str(py), '-c', 'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())'], capture_output=True, text=True)
    path = proc.stdout.strip() if proc.returncode == 0 else ''
    return path or None


def ensure_runtime() -> None:
    py = python_bin()
    if not py.exists():
        bail('Missing video-fetch runtime. Run: ~/utils/zeroclaw/bin/video-fetch-bootstrap')


def url_kind(url: str) -> str:
    lowered = url.lower()
    if 'youtube.com/' in lowered or 'youtu.be/' in lowered:
        return 'youtube'
    if 'bilibili.com/' in lowered or 'b23.tv/' in lowered:
        return 'bilibili'
    return 'generic'


def list_new_files(before: set[Path], base_dir: Path) -> list[Path]:
    after = {p for p in base_dir.rglob('*') if p.is_file()}
    return sorted(after - before)


def file_manifest(files: list[Path]) -> dict[str, Any]:
    videos = [str(p) for p in files if p.suffix.lower() in VIDEO_EXTS]
    texts = [str(p) for p in files if p.suffix.lower() in TEXT_EXTS]
    return {
        'video_files': videos,
        'text_sidecars': texts,
        'all_files': [str(p) for p in files],
    }


def collect_existing_output_files(base_dir: Path, info: dict[str, Any]) -> list[Path]:
    video_id = str(info.get('id') or '').strip()
    if not video_id:
        return []
    matched = []
    needle = f'[{video_id}]'
    for path in base_dir.rglob('*'):
        if not path.is_file():
            continue
        if needle in path.name:
            matched.append(path)
    return sorted(matched)


def compact_lang_map(mapping: Any) -> dict[str, int] | None:
    if not isinstance(mapping, dict):
        return None
    return {lang: len(entries or []) for lang, entries in mapping.items()}


def parse_json_from_mixed_output(text: str) -> dict[str, Any]:
    for line in reversed([line.strip() for line in text.splitlines() if line.strip()]):
        if not line.startswith('{'):
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    raise ValueError('No JSON object found in yt-dlp output')


def sanitize_info(info: dict[str, Any]) -> dict[str, Any]:
    keep = [
        'id', 'title', 'fulltitle', 'webpage_url', 'original_url', 'extractor', 'extractor_key',
        'uploader', 'channel', 'duration', 'ext', 'format', 'format_id', 'width', 'height',
        'fps', 'protocol', 'vcodec', 'acodec', 'filesize', 'filesize_approx', 'release_timestamp',
        'upload_date', 'description', 'thumbnail', 'tags', 'categories',
        'availability', 'age_limit', 'live_status', 'was_live',
    ]
    out = {k: info.get(k) for k in keep if k in info}
    if isinstance(out.get('description'), str) and len(out['description']) > 600:
        out['description'] = out['description'][:599].rstrip() + '…'
    if isinstance(out.get('tags'), list) and len(out['tags']) > 20:
        out['tags'] = out['tags'][:20]
        out['tag_count'] = len(info.get('tags') or [])
    subtitles = compact_lang_map(info.get('subtitles'))
    auto_captions = compact_lang_map(info.get('automatic_captions'))
    if subtitles:
        out['subtitle_languages'] = sorted(subtitles.keys())[:DEFAULT_MAX_SUBS]
        out['subtitle_language_count'] = len(subtitles)
    if auto_captions:
        out['auto_caption_languages'] = sorted(auto_captions.keys())[:DEFAULT_MAX_SUBS]
        out['auto_caption_language_count'] = len(auto_captions)
    if info.get('requested_subtitles'):
        out['requested_subtitles'] = list(info['requested_subtitles'].keys())
    out['kind'] = url_kind(info.get('webpage_url') or info.get('original_url') or '')
    return out


def make_ydl_opts(args: argparse.Namespace, *, download: bool) -> dict[str, Any]:
    outdir = Path(args.output_dir).expanduser()
    tempdir = Path(args.temp_dir).expanduser()
    outdir.mkdir(parents=True, exist_ok=True)
    tempdir.mkdir(parents=True, exist_ok=True)
    ffmpeg = ffmpeg_bin()

    opts: dict[str, Any] = {
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'paths': {'home': str(outdir), 'temp': str(tempdir)},
        'outtmpl': {'default': '%(extractor_key)s/%(title)s [%(id)s].%(ext)s'},
        'restrictfilenames': False,
        'windowsfilenames': False,
        'ignoreerrors': False,
        'skip_download': not download,
        'writeinfojson': download,
        'writedescription': bool(args.write_description) if hasattr(args, 'write_description') else False,
        'writesubtitles': bool(args.write_subs) if hasattr(args, 'write_subs') else False,
        'writeautomaticsub': bool(args.write_auto_subs) if hasattr(args, 'write_auto_subs') else False,
        'subtitleslangs': ['all', '-live_chat'] if getattr(args, 'write_subs', False) or getattr(args, 'write_auto_subs', False) else [],
    }

    if download:
        if ffmpeg:
            opts['ffmpeg_location'] = ffmpeg
            opts['merge_output_format'] = 'mp4'
            opts['format'] = f"bv*[height<={args.max_height}]+ba/b[height<={args.max_height}]/b"
        else:
            opts['format'] = f"best[height<={args.max_height}][ext=mp4][vcodec!=none][acodec!=none]/best[height<={args.max_height}][vcodec!=none][acodec!=none]/best"

    if getattr(args, 'cookies', None):
        opts['cookiefile'] = str(Path(args.cookies).expanduser())

    return opts


def probe(args: argparse.Namespace) -> int:
    ensure_runtime()
    py = python_bin()
    code = (
        'import json, sys\n'
        'from yt_dlp import YoutubeDL\n'
        'opts = json.loads(sys.stdin.read())\n'
        'url = opts.pop("_url")\n'
        'with YoutubeDL(opts) as ydl:\n'
        '    info = ydl.extract_info(url, download=False, process=False)\n'
        '    print(json.dumps(ydl.sanitize_info(info), ensure_ascii=False))\n'
    )
    opts = make_ydl_opts(args, download=False)
    opts['_url'] = args.input
    proc = subprocess.run([str(py), '-c', code], input=json.dumps(opts), capture_output=True, text=True)
    if proc.returncode != 0:
        bail(proc.stderr.strip() or proc.stdout.strip() or 'yt-dlp probe failed')
    raw = parse_json_from_mixed_output(proc.stdout)
    data = sanitize_info(raw)
    data['supported'] = True
    data['input'] = args.input
    data['ffmpeg_available'] = bool(ffmpeg_bin())
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def fetch(args: argparse.Namespace) -> int:
    ensure_runtime()
    py = python_bin()
    outdir = Path(args.output_dir).expanduser()
    outdir.mkdir(parents=True, exist_ok=True)
    before = {p for p in outdir.rglob('*') if p.is_file()}

    code = (
        'import json, sys\n'
        'from yt_dlp import YoutubeDL\n'
        'opts = json.loads(sys.stdin.read())\n'
        'url = opts.pop("_url")\n'
        'with YoutubeDL(opts) as ydl:\n'
        '    info = ydl.extract_info(url, download=True)\n'
        '    print(json.dumps(ydl.sanitize_info(info), ensure_ascii=False))\n'
    )
    opts = make_ydl_opts(args, download=True)
    opts['_url'] = args.input
    proc = subprocess.run([str(py), '-c', code], input=json.dumps(opts), capture_output=True, text=True)
    if proc.returncode != 0:
        bail(proc.stderr.strip() or proc.stdout.strip() or 'yt-dlp fetch failed')

    raw = parse_json_from_mixed_output(proc.stdout)
    data = sanitize_info(raw)
    new_files = list_new_files(before, outdir)
    if not new_files:
        new_files = collect_existing_output_files(outdir, raw)
    manifest = file_manifest(new_files)
    data.update({
        'supported': True,
        'input': args.input,
        'output_dir': str(outdir),
        'ffmpeg_available': bool(ffmpeg_bin()),
        'downloaded': manifest,
    })

    if args.write_summary:
        summary_path = Path(args.write_summary).expanduser()
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')

    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Fetch local video files from supported public pages with yt-dlp')
    sub = parser.add_subparsers(dest='cmd', required=True)

    probe_p = sub.add_parser('probe', help='Inspect whether a URL is fetchable and show basic metadata')
    probe_p.add_argument('--input', required=True, help='Public video page URL, typically YouTube or bilibili')
    probe_p.add_argument('--output-dir', default=str(DEFAULT_OUTPUT_DIR))
    probe_p.add_argument('--temp-dir', default=str(DEFAULT_TEMP_DIR))
    probe_p.add_argument('--cookies', default=None, help='Optional Netscape cookies.txt path for restricted content')
    probe_p.add_argument('--max-height', type=int, default=DEFAULT_MAX_HEIGHT, help='Preview download preference height; does not force probe output')

    fetch_p = sub.add_parser('fetch', help='Download a local video file for later processing')
    fetch_p.add_argument('--input', required=True, help='Public video page URL, typically YouTube or bilibili')
    fetch_p.add_argument('--output-dir', default=str(DEFAULT_OUTPUT_DIR))
    fetch_p.add_argument('--temp-dir', default=str(DEFAULT_TEMP_DIR))
    fetch_p.add_argument('--cookies', default=None, help='Optional Netscape cookies.txt path for restricted content')
    fetch_p.add_argument('--write-subs', action='store_true', help='Write normal subtitles if available')
    fetch_p.add_argument('--write-auto-subs', action='store_true', help='Write automatic subtitles if available')
    fetch_p.add_argument('--write-description', action='store_true', help='Write page description sidecar if available')
    fetch_p.add_argument('--write-summary', default=None, help='Optional path to write the JSON result')
    fetch_p.add_argument('--max-height', type=int, default=DEFAULT_MAX_HEIGHT, help='Preferred maximum video height for downloads')

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.cmd == 'probe':
        return probe(args)
    if args.cmd == 'fetch':
        return fetch(args)
    bail(f'Unknown command: {args.cmd}')
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
