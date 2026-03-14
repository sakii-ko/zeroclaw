#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_MODEL_FALLBACK = "gemini-2.5-flash"
DEFAULT_MAX_CHARS = 500
DEFAULT_MAX_BYTES = 50 * 1024 * 1024
INLINE_RAW_MAX_BYTES = 14 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 180
POLL_INTERVAL_SECONDS = 2.0
POLL_TIMEOUT_SECONDS = 180.0
YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "youtu.be",
    "www.youtu.be",
}
VIDEO_MIME_BY_SUFFIX = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
    ".mpeg": "video/mpeg",
    ".mpg": "video/mpeg",
}


def current_default_model() -> str:
    load_env_defaults()
    return os.environ.get("GEMINI_VIDEO_MODEL", DEFAULT_MODEL_FALLBACK)


LEGACY_HELP_FLAGS = {"-h", "--help"}
KNOWN_COMMANDS = {"inspect", "describe", "ask", "summary", "summarize", "probe"}


def normalize_argv(argv: list[str]) -> list[str]:
    if len(argv) <= 1:
        return argv
    if len(argv) == 2 and argv[1] in LEGACY_HELP_FLAGS:
        return argv
    first = argv[1]
    if first in KNOWN_COMMANDS:
        return argv
    return [argv[0], "describe", *argv[1:]]


def load_env_defaults() -> None:
    if os.environ.get("GEMINI_API_KEY"):
        return
    for path in [Path.home() / ".zeroclaw" / "service.env", Path.home() / ".config" / "zeroclaw" / "env"]:
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
        if os.environ.get("GEMINI_API_KEY"):
            return


def bail(message: str, code: int = 2) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(code)


def is_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


def is_youtube_url(value: str) -> bool:
    if not is_url(value):
        return False
    parsed = urllib.parse.urlparse(value)
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host in {host.removeprefix("www.") for host in YOUTUBE_HOSTS}


def guess_mime(path: Path) -> str:
    guess, _ = mimetypes.guess_type(path.name)
    if guess:
        return guess
    return VIDEO_MIME_BY_SUFFIX.get(path.suffix.lower(), "video/mp4")


def parse_source(input_value: str) -> dict[str, Any]:
    if is_youtube_url(input_value):
        return {"kind": "youtube_url", "input": input_value, "gemini_native": True}
    if is_url(input_value):
        return {"kind": "remote_url", "input": input_value, "gemini_native": False}
    path = Path(input_value).expanduser().resolve()
    if not path.exists():
        bail(f"Input video does not exist: {path}")
    if not path.is_file():
        bail(f"Input path is not a file: {path}")
    return {
        "kind": "local_file",
        "input": str(path),
        "path": path,
        "size_bytes": path.stat().st_size,
        "mime_type": guess_mime(path),
    }


def choose_transport(source: dict[str, Any], requested: str, max_bytes: int) -> str:
    if source["kind"] == "youtube_url":
        if requested in {"auto", "youtube"}:
            return "youtube"
        bail("YouTube URL input only supports transport=auto or transport=youtube")
    if source["kind"] != "local_file":
        bail("Only local video files or public YouTube URLs are supported")
    if source["size_bytes"] > max_bytes:
        bail(f"Input video exceeds max-bytes limit ({source['size_bytes']} > {max_bytes})")
    if requested == "auto":
        return "inline" if source["size_bytes"] <= INLINE_RAW_MAX_BYTES else "files"
    if requested == "inline" and source["size_bytes"] > INLINE_RAW_MAX_BYTES:
        bail(f"Input video is too large for inline upload ({source['size_bytes']} bytes). Use --transport files or auto.")
    if requested in {"inline", "files"}:
        return requested
    bail(f"Unsupported transport for local file: {requested}")


def request_json(url: str, *, method: str = "GET", headers: dict[str, str] | None = None, body: bytes | None = None, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> tuple[dict[str, Any], dict[str, str]]:
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return (json.loads(raw) if raw.strip() else {}, {k.lower(): v for k, v in resp.headers.items()})
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} for {url}: {detail}") from exc


def upload_local_file(api_key: str, path: Path, mime_type: str, timeout: int) -> dict[str, Any]:
    start_url = f"https://generativelanguage.googleapis.com/upload/v1beta/files?key={urllib.parse.quote(api_key)}"
    _, headers = request_json(
        start_url,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(path.stat().st_size),
            "X-Goog-Upload-Header-Content-Type": mime_type,
        },
        body=json.dumps({"file": {"display_name": path.name}}).encode("utf-8"),
        timeout=timeout,
    )
    upload_url = headers.get("x-goog-upload-url")
    if not upload_url:
        raise RuntimeError("Gemini Files API did not return an upload URL")

    upload_req = urllib.request.Request(
        upload_url,
        data=path.read_bytes(),
        headers={
            "Content-Length": str(path.stat().st_size),
            "X-Goog-Upload-Offset": "0",
            "X-Goog-Upload-Command": "upload, finalize",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(upload_req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            payload = json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Gemini file upload failed: HTTP {exc.code}: {detail}") from exc

    file_info = payload.get("file", payload)
    name = file_info.get("name")
    if not name:
        raise RuntimeError("Gemini file upload response did not include file name")

    deadline = time.time() + POLL_TIMEOUT_SECONDS
    while time.time() < deadline:
        poll_url = f"https://generativelanguage.googleapis.com/v1beta/{urllib.parse.quote(name, safe='/')}?key={urllib.parse.quote(api_key)}"
        current, _ = request_json(poll_url, timeout=timeout)
        file_info = current.get("file", current)
        state = file_info.get("state")
        if isinstance(state, dict):
            state = state.get("name")
        if not state or state == "ACTIVE":
            return file_info
        if state == "FAILED":
            raise RuntimeError(f"Gemini file processing failed for {name}")
        time.sleep(POLL_INTERVAL_SECONDS)

    raise RuntimeError(f"Timed out waiting for Gemini file to become ACTIVE: {name}")


def delete_uploaded_file(api_key: str, file_name: str, timeout: int) -> None:
    try:
        request_json(
            f"https://generativelanguage.googleapis.com/v1beta/{urllib.parse.quote(file_name, safe='/')}?key={urllib.parse.quote(api_key)}",
            method="DELETE",
            timeout=timeout,
        )
    except Exception:
        pass


def build_video_part(source: dict[str, Any], transport: str, api_key: str, timeout: int, fps: float | None) -> tuple[dict[str, Any], str | None]:
    metadata: dict[str, Any] = {}
    if fps is not None:
        metadata["fps"] = fps

    if source["kind"] == "youtube_url":
        part: dict[str, Any] = {"file_data": {"file_uri": source["input"]}}
        if metadata:
            part["videoMetadata"] = metadata
        return part, None

    if transport == "inline":
        part = {
            "inline_data": {
                "mime_type": source["mime_type"],
                "data": base64.b64encode(source["path"].read_bytes()).decode("ascii"),
            }
        }
        if metadata:
            part["videoMetadata"] = metadata
        return part, None

    if transport == "files":
        uploaded = upload_local_file(api_key, source["path"], source["mime_type"], timeout)
        part = {"file_data": {"mime_type": source["mime_type"], "file_uri": uploaded["uri"]}}
        if metadata:
            part["videoMetadata"] = metadata
        return part, uploaded["name"]

    bail(f"Unsupported transport: {transport}")


def build_text_prompt(prompt: str | None, question: str | None, max_chars: int) -> str:
    if question:
        return f"Answer the following question about the video. Be concise and stay within about {max_chars} characters.\nQuestion: {question.strip()}"
    if prompt:
        return prompt.strip()
    return (
        "Describe the key events in this video, including both visual and audio details when relevant. "
        f"Keep the answer concise and within about {max_chars} characters. Mention timestamps only when useful."
    )


def extract_text_response(payload: dict[str, Any]) -> str:
    candidates = payload.get("candidates") or []
    if not candidates:
        raise RuntimeError(f"Gemini returned no candidates: {json.dumps(payload, ensure_ascii=False)}")
    parts = candidates[0].get("content", {}).get("parts", [])
    text = "\n".join(part.get("text", "").strip() for part in parts if isinstance(part, dict) and part.get("text")).strip()
    if not text:
        raise RuntimeError(f"Gemini returned no text: {json.dumps(payload, ensure_ascii=False)}")
    return text


def maybe_write_output(text: str, output: Path | None) -> None:
    if output is None:
        return
    output = output.expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")


def resolve_input_arg(args: argparse.Namespace) -> str:
    value = getattr(args, "input", None) or getattr(args, "input_positional", None)
    if not value:
        bail("Missing video input. Use --input <path-or-url> or provide it as the first positional argument.")
    return value


def resolve_question_arg(args: argparse.Namespace) -> str:
    value = getattr(args, "question", None) or getattr(args, "prompt", None) or getattr(args, "question_positional", None)
    if not value:
        bail("Missing question. Use --question '...' or --prompt '...'.")
    return value


def generate_response(source: dict[str, Any], *, transport: str, model: str, prompt: str | None, question: str | None, max_chars: int, timeout: int, fps: float | None, as_json: bool, output: Path | None) -> int:
    load_env_defaults()
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        bail("GEMINI_API_KEY is not set. Put it in ~/.zeroclaw/service.env or export it in the environment.")

    uploaded_name = None
    try:
        video_part, uploaded_name = build_video_part(source, transport, api_key, timeout, fps)
        payload = {
            "contents": [{"parts": [video_part, {"text": build_text_prompt(prompt, question, max_chars)}]}],
            "generationConfig": {
                "temperature": 0,
                "maxOutputTokens": max(128, min(1024, max_chars * 2)),
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }
        response, _ = request_json(
            f"https://generativelanguage.googleapis.com/v1beta/models/{urllib.parse.quote(model)}:generateContent?key={urllib.parse.quote(api_key)}",
            method="POST",
            headers={"Content-Type": "application/json"},
            body=json.dumps(payload).encode("utf-8"),
            timeout=timeout,
        )
        text = extract_text_response(response)
        if len(text) > max_chars:
            text = text[: max_chars - 1].rstrip() + "…"
        maybe_write_output(text, output)
        if as_json:
            print(json.dumps({
                "model": model,
                "transport": transport,
                "source_kind": source["kind"],
                "input": source["input"],
                "max_chars": max_chars,
                "fps": fps,
                "answer": text,
                "usage_metadata": response.get("usageMetadata"),
            }, ensure_ascii=False, indent=2))
        else:
            print(text)
        return 0
    finally:
        if uploaded_name:
            delete_uploaded_file(api_key, uploaded_name, timeout)


def command_inspect(args: argparse.Namespace) -> int:
    source = parse_source(resolve_input_arg(args))
    if source["kind"] == "remote_url":
        print(json.dumps({
            "input": source["input"],
            "kind": "remote_url",
            "supported": False,
            "reason": "Remote URLs are not directly supported here unless they are public YouTube URLs. Fetch the video to a local file first, or use a separate downloader skill.",
        }, ensure_ascii=False, indent=2))
        return 0
    if source["kind"] == "youtube_url":
        print(json.dumps({
            "input": source["input"],
            "kind": "youtube_url",
            "supported": True,
            "gemini_native": True,
            "recommended_transport": "youtube",
            "default_model": args.model,
            "default_max_chars": args.max_chars,
        }, ensure_ascii=False, indent=2))
        return 0
    print(json.dumps({
        "input": source["input"],
        "kind": "local_file",
        "supported": True,
        "size_bytes": source["size_bytes"],
        "mime_type": source["mime_type"],
        "recommended_transport": choose_transport(source, "auto", args.max_bytes),
        "default_model": args.model,
        "default_max_chars": args.max_chars,
        "max_bytes": args.max_bytes,
    }, ensure_ascii=False, indent=2))
    return 0


def add_input_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("input_positional", nargs="?", help="Local video path or public YouTube URL")
    p.add_argument("--input", dest="input", help="Local video path or public YouTube URL")


def add_common(p: argparse.ArgumentParser) -> None:
    add_input_arg(p)
    p.add_argument("--model", default=current_default_model())
    p.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    p.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    p.add_argument("--transport", choices=["auto", "inline", "files", "youtube"], default="auto")
    p.add_argument("--fps", type=float, default=None, help="Optional Gemini videoMetadata.fps hint")
    p.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    p.add_argument("--output", type=Path, default=None)
    p.add_argument("--json", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Analyze local video files or public YouTube URLs with Gemini")
    sub = parser.add_subparsers(dest="cmd", required=True)

    inspect_p = sub.add_parser("inspect", help="Inspect whether a video input is supported and how it will be sent")
    add_input_arg(inspect_p)
    inspect_p.add_argument("--model", default=current_default_model())
    inspect_p.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    inspect_p.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)

    probe_p = sub.add_parser("probe", help="Compatibility alias for inspect")
    add_input_arg(probe_p)
    probe_p.add_argument("--model", default=current_default_model())
    probe_p.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    probe_p.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)

    describe_p = sub.add_parser("describe", help="Describe or summarize a video")
    add_common(describe_p)
    describe_p.add_argument("--prompt", default=None, help="Custom prompt; defaults to a concise video summary prompt")

    summary_p = sub.add_parser("summary", help="Compatibility alias for describe")
    add_common(summary_p)
    summary_p.add_argument("--prompt", default=None, help="Custom prompt; defaults to a concise video summary prompt")

    ask_p = sub.add_parser("ask", help="Ask a specific question about a video")
    add_common(ask_p)
    ask_p.add_argument("question_positional", nargs="?", help="Compatibility positional question")
    ask_p.add_argument("--question", default=None, help="Question to ask about the video")
    ask_p.add_argument("--prompt", default=None, help="Compatibility alias for --question")

    summarize_p = sub.add_parser("summarize", help="Compatibility alias for describe")
    add_common(summarize_p)
    summarize_p.add_argument("--prompt", default=None, help="Custom prompt; defaults to a concise video summary prompt")

    return parser


def main() -> int:
    argv = normalize_argv(sys.argv)
    args = build_parser().parse_args(argv[1:])

    if args.cmd in {"inspect", "probe"}:
        return command_inspect(args)

    source = parse_source(resolve_input_arg(args))
    if source["kind"] == "remote_url":
        bail("Remote URLs are not directly supported here unless they are public YouTube URLs. Fetch the video to a local file first, or use a separate downloader skill.")
    transport = choose_transport(source, args.transport, args.max_bytes)

    if args.cmd in {"describe", "summary", "summarize"}:
        return generate_response(
            source,
            transport=transport,
            model=args.model,
            prompt=args.prompt,
            question=None,
            max_chars=args.max_chars,
            timeout=args.timeout_seconds,
            fps=args.fps,
            as_json=args.json,
            output=args.output,
        )

    if args.cmd == "ask":
        return generate_response(
            source,
            transport=transport,
            model=args.model,
            prompt=None,
            question=resolve_question_arg(args),
            max_chars=args.max_chars,
            timeout=args.timeout_seconds,
            fps=args.fps,
            as_json=args.json,
            output=args.output,
        )

    bail(f"Unknown command: {args.cmd}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
