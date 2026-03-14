#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import textwrap
import tomllib
import urllib.request
from pathlib import Path

QQ_AUTH_URL = 'https://bots.qq.com/app/getAppAccessToken'
QQ_API_BASE = 'https://api.sgroup.qq.com'
TEXT_CHUNK_LIMIT = 1100
FILE_TYPES = {
    'image': 1,
    'video': 2,
    'voice': 3,
    'document': 4,
}


def load_config() -> dict[str, str]:
    path = Path.home() / '.zeroclaw' / 'config.toml'
    data = tomllib.loads(path.read_text(encoding='utf-8'))
    qq = ((data.get('channels_config') or {}).get('qq') or {})
    app_id = qq.get('app_id')
    app_secret = qq.get('app_secret')
    if not app_id or not app_secret:
        raise SystemExit('QQ channel is not configured in ~/.zeroclaw/config.toml')
    return {'app_id': app_id, 'app_secret': app_secret}


def sanitize_user_id(raw: str) -> str:
    return ''.join(ch for ch in raw if ch.isalnum() or ch == '_')


def message_url(recipient: str) -> str:
    if recipient.startswith('group:'):
        return f"{QQ_API_BASE}/v2/groups/{recipient.split(':', 1)[1]}/messages"
    raw = recipient.split(':', 1)[1] if recipient.startswith('user:') else recipient
    return f"{QQ_API_BASE}/v2/users/{sanitize_user_id(raw)}/messages"


def file_url(recipient: str) -> str:
    if recipient.startswith('group:'):
        return f"{QQ_API_BASE}/v2/groups/{recipient.split(':', 1)[1]}/files"
    raw = recipient.split(':', 1)[1] if recipient.startswith('user:') else recipient
    return f"{QQ_API_BASE}/v2/users/{sanitize_user_id(raw)}/files"


def post_json(url: str, payload: dict, *, token: str | None = None) -> dict:
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = f'QQBot {token}'
    req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), headers=headers, method='POST')
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = resp.read().decode('utf-8', errors='replace')
    return json.loads(raw) if raw.strip() else {}


def fetch_token(app_id: str, app_secret: str) -> str:
    payload = {'appId': app_id, 'clientSecret': app_secret}
    data = post_json(QQ_AUTH_URL, payload)
    token = data.get('access_token')
    if not token:
        raise SystemExit(f'Missing access_token in QQ response: {data}')
    return token


def send_text(recipient: str, content: str, token: str) -> None:
    if not content.strip():
        return
    post_json(message_url(recipient), {'content': content, 'msg_type': 0}, token=token)


def chunk_text(text: str, limit: int = TEXT_CHUNK_LIMIT) -> list[str]:
    paras = text.split('\n\n')
    chunks: list[str] = []
    current = ''
    for para in paras:
        part = para.strip()
        if not part:
            continue
        candidate = part if not current else current + '\n\n' + part
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        if len(part) <= limit:
            current = part
            continue
        wrapped = textwrap.wrap(part, width=limit, break_long_words=False, break_on_hyphens=False)
        if not wrapped:
            continue
        chunks.extend(wrapped[:-1])
        current = wrapped[-1]
    if current:
        chunks.append(current)
    return chunks


def target_looks_like_gif(target: str) -> bool:
    lowered = target.lower()
    if lowered.startswith('https://'):
        core = lowered.split('?', 1)[0].split('#', 1)[0]
        return core.endswith('.gif')
    return Path(target).expanduser().suffix.lower() == '.gif'


def normalize_media_kind(kind: str, target: str) -> str:
    if kind == 'video' and target_looks_like_gif(target):
        return 'image'
    return kind


def send_rich_media(recipient: str, token: str, kind: str, target: str) -> None:
    kind = normalize_media_kind(kind, target)
    file_type = FILE_TYPES[kind]
    upload_payload: dict[str, object] = {
        'file_type': file_type,
        'srv_send_msg': False,
    }
    if target.startswith('https://'):
        upload_payload['url'] = target
    else:
        path = Path(target).expanduser().resolve()
        if not path.exists():
            raise SystemExit(f'Attachment path does not exist: {path}')
        data = path.read_bytes()
        if not data:
            raise SystemExit(f'Attachment path is empty: {path}')
        upload_payload['file_data'] = base64.b64encode(data).decode('ascii')
        if kind == 'document':
            upload_payload['file_name'] = path.name or 'attachment.bin'
        elif kind == 'image' and 'file_name' not in upload_payload:
            guessed = mimetypes.guess_type(path.name)[0] or ''
            if guessed:
                upload_payload['file_name'] = path.name
    media = post_json(file_url(recipient), upload_payload, token=token)
    post_json(message_url(recipient), {'msg_type': 7, 'media': media}, token=token)


def main() -> int:
    ap = argparse.ArgumentParser(description='Send QQ notifications and optional rich media using the configured QQ bot channel')
    ap.add_argument('--recipient', required=True, help='QQ recipient, e.g. user:<openid> or group:<openid>')
    ap.add_argument('--text', help='Inline notification text; if omitted, read from stdin unless media-only')
    ap.add_argument('--image', action='append', default=[], help='Local image path or https URL to send')
    ap.add_argument('--video', action='append', default=[], help='Local video path or https URL to send')
    ap.add_argument('--document', action='append', default=[], help='Local file path or https URL to send')
    args = ap.parse_args()

    text = args.text if args.text is not None else __import__('sys').stdin.read()
    cfg = load_config()
    token = fetch_token(cfg['app_id'], cfg['app_secret'])
    if text.strip():
        for chunk in chunk_text(text.strip() + '\n'):
            send_text(args.recipient, chunk, token)
    for image in args.image:
        send_rich_media(args.recipient, token, 'image', image)
    for video in args.video:
        send_rich_media(args.recipient, token, 'video', video)
    for document in args.document:
        send_rich_media(args.recipient, token, 'document', document)
    if not text.strip() and not args.image and not args.video and not args.document:
        return 0
    print(f'SENT_TO={args.recipient}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
