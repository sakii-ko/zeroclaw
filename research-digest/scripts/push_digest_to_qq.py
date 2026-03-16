#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sqlite3
import subprocess
import textwrap
import time
import tomllib
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

QQ_AUTH_URL = "https://bots.qq.com/app/getAppAccessToken"
QQ_API_BASE = "https://api.sgroup.qq.com"
DEFAULT_RECIPIENT = os.environ.get("ZEROCLAW_QQ_RECIPIENT", "").strip()
DEFAULT_LIMIT = 20
DEFAULT_TOPICS = "llm,3d,video-generation,world-model"
DEFAULT_WINDOW_DAYS = 7
TEXT_CHUNK_LIMIT = 3500
QQ_HTTP_TIMEOUT_SECS = 12
DIGEST_COMMAND_TIMEOUT_SECS = 95
DIGEST_RETRY_DELAYS = (20.0, 60.0)
DELIVERY_STATE_KEEP_DAYS = 45
DELIVERY_DB_PATH = Path.home() / '.zeroclaw' / 'workspace' / 'state' / 'research_digest_delivery.db'


def load_config() -> dict:
    path = Path.home() / '.zeroclaw' / 'config.toml'
    data = tomllib.loads(path.read_text(encoding='utf-8'))
    qq = ((data.get('channels_config') or {}).get('qq') or {})
    app_id = qq.get('app_id')
    app_secret = qq.get('app_secret')
    if not app_id or not app_secret:
        raise SystemExit('QQ channel is not configured in ~/.zeroclaw/config.toml')
    return {'app_id': app_id, 'app_secret': app_secret}


def sanitize_user_id(raw: str) -> str:
    return ''.join(c for c in raw if c.isalnum() or c == '_')


def message_url(recipient: str) -> str:
    if recipient.startswith('group:'):
        return f"{QQ_API_BASE}/v2/groups/{recipient.split(':',1)[1]}/messages"
    raw = recipient.split(':', 1)[1] if recipient.startswith('user:') else recipient
    return f"{QQ_API_BASE}/v2/users/{sanitize_user_id(raw)}/messages"


def file_url(recipient: str) -> str:
    if recipient.startswith('group:'):
        return f"{QQ_API_BASE}/v2/groups/{recipient.split(':',1)[1]}/files"
    raw = recipient.split(':', 1)[1] if recipient.startswith('user:') else recipient
    return f"{QQ_API_BASE}/v2/users/{sanitize_user_id(raw)}/files"


def post_json(url: str, payload: dict, *, token: str | None = None) -> dict:
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = f'QQBot {token}'
    req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), headers=headers, method='POST')
    with urllib.request.urlopen(req, timeout=QQ_HTTP_TIMEOUT_SECS) as resp:
        raw = resp.read().decode('utf-8', errors='replace')
    return json.loads(raw) if raw.strip() else {}


def fetch_token(app_id: str, app_secret: str) -> str:
    data = post_json(QQ_AUTH_URL, {'appId': app_id, 'clientSecret': app_secret})
    token = data.get('access_token')
    if not token:
        raise SystemExit(f'Missing access_token in QQ response: {data}')
    return token


def send_text(recipient: str, content: str, token: str) -> None:
    if not content.strip():
        return
    post_json(message_url(recipient), {'content': content, 'msg_type': 0}, token=token)


def send_document(recipient: str, path: Path, token: str) -> None:
    payload = {
        'file_type': 4,
        'srv_send_msg': False,
        'file_data': base64.b64encode(path.read_bytes()).decode('ascii'),
        'file_name': path.name,
    }
    media = post_json(file_url(recipient), payload, token=token)
    post_json(message_url(recipient), {'msg_type': 7, 'media': media}, token=token)


def build_summary(data: dict) -> str:
    items = data.get('items') or []
    lines = [f"今日 research digest（{len(items)} 篇）："]
    if not items:
        lines.append('今天没有成功收集到可展示的论文条目。')
    else:
        for idx, item in enumerate(items, 1):
            lines.append(f"{idx}. {item['title']}")
    lines.append('完整中文摘要、链接和分类见附件 Markdown。')
    return '\n'.join(lines).strip() + '\n'


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


def all_requested_sources_failed(data: dict) -> bool:
    items = data.get('items') or []
    if items:
        return False
    requested_sources = [s for s in (data.get('sources') or []) if s in {'arxiv', 'hf-daily'}]
    source_meta = data.get('source_meta') or {}
    source_errors = source_meta.get('source_errors') or {}
    return bool(requested_sources) and all(source_errors.get(source) for source in requested_sources)


def make_fallback_digest(limit: int, topics: str, window_days: int, failures: list[str]) -> dict:
    generated_at = datetime.now(timezone.utc)
    output_dir = Path.home() / 'downloads' / 'extracted' / 'research-digest' / generated_at.strftime('%Y-%m-%d')
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"daily-research-digest-{generated_at.astimezone().strftime('%Y%m%d')}"
    md_path = output_dir / f'{stem}.md'
    json_path = output_dir / f'{stem}.json'
    topics_list = [part.strip() for part in topics.split(',') if part.strip()]
    notes = ['上游源连续失败，本次先发送 0 条结果占位，不中断 saki 的流程。']
    notes.extend(failures[-4:])
    markdown_lines = [
        f'# Daily Research Digest - {generated_at.strftime("%Y-%m-%d")}',
        '',
        f'- 生成时间：{generated_at.strftime("%Y-%m-%d %H:%M:%S %Z")}',
        '- 数据源：arxiv, hf-daily',
        f'- 主题：{", ".join(topics_list)}',
        f'- arXiv 窗口：最近 {window_days} 天',
        '- 条目数：0',
    ]
    for note in notes:
        markdown_lines.append(f'- 备注：{note}')
    markdown = '\n'.join(markdown_lines) + '\n'
    payload = {
        'generated_at': generated_at.isoformat(),
        'topics': topics_list,
        'sources': ['arxiv', 'hf-daily'],
        'window_days': window_days,
        'notes': notes,
        'items': [],
        'source_meta': {
            'sources': ['arxiv', 'hf-daily'],
            'source_errors': {'fallback': ' | '.join(failures[-4:]) or 'unknown upstream failure'},
        },
        'markdown_path': str(md_path),
        'json_path': str(json_path),
    }
    md_path.write_text(markdown, encoding='utf-8')
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    return payload


def resolve_markdown_path(data: dict) -> Path:
    raw = str(data.get('markdown_path') or '').strip()
    if raw:
        return Path(raw).expanduser()
    generated_at = str(data.get('generated_at') or '').strip()
    if generated_at:
        try:
            dt = datetime.fromisoformat(generated_at.replace('Z', '+00:00')).astimezone()
        except ValueError:
            dt = datetime.now().astimezone()
    else:
        dt = datetime.now().astimezone()
    output_dir = Path.home() / 'downloads' / 'extracted' / 'research-digest' / dt.strftime('%Y-%m-%d')
    stem = f"daily-research-digest-{dt.strftime('%Y%m%d')}"
    return output_dir / f'{stem}.md'


def run_digest(limit: int, topics: str, window_days: int) -> dict:
    cmd = [
        str(Path.home() / 'utils' / 'zeroclaw' / 'bin' / 'research-digest'),
        'digest',
        '--topics', topics,
        '--limit', str(limit),
        '--window-days', str(window_days),
        '--json',
    ]
    failures: list[str] = []
    last_data: dict | None = None
    attempts = len(DIGEST_RETRY_DELAYS) + 1
    for attempt_idx in range(attempts):
        try:
            raw = subprocess.check_output(
                cmd,
                text=True,
                stderr=subprocess.STDOUT,
                timeout=DIGEST_COMMAND_TIMEOUT_SECS,
            )
            data = json.loads(raw)
        except Exception as exc:
            failures.append(f'attempt {attempt_idx + 1}/{attempts}: {exc}')
            if attempt_idx >= attempts - 1:
                break
            time.sleep(DIGEST_RETRY_DELAYS[attempt_idx])
            continue
        if not all_requested_sources_failed(data):
            return data
        last_data = data
        failures.append(
            f"attempt {attempt_idx + 1}/{attempts}: all upstream sources failed: "
            + json.dumps((data.get('source_meta') or {}).get('source_errors') or {}, ensure_ascii=False)
        )
        if attempt_idx >= attempts - 1:
            break
        time.sleep(DIGEST_RETRY_DELAYS[attempt_idx])
    if last_data is not None:
        notes = list(last_data.get('notes') or [])
        notes.append('上游连续失败；已保留 0 条结果并继续推送，避免中断 saki。')
        last_data['notes'] = notes
        return last_data
    return make_fallback_digest(limit, topics, window_days, failures)


def open_delivery_db() -> sqlite3.Connection:
    DELIVERY_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DELIVERY_DB_PATH)
    conn.execute(
        '''
        CREATE TABLE IF NOT EXISTS deliveries (
            delivery_key TEXT PRIMARY KEY,
            recipient TEXT NOT NULL,
            digest_stamp TEXT NOT NULL,
            markdown_path TEXT NOT NULL,
            text_sent INTEGER NOT NULL DEFAULT 0,
            attachment_sent INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )
        '''
    )
    conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_deliveries_updated_at ON deliveries(updated_at)'
    )
    conn.commit()
    return conn


def prune_delivery_db(conn: sqlite3.Connection) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=DELIVERY_STATE_KEEP_DAYS)).isoformat()
    conn.execute('DELETE FROM deliveries WHERE updated_at < ?', (cutoff,))
    conn.commit()


def digest_stamp_for(data: dict, markdown_path: Path) -> str:
    generated_at = str(data.get('generated_at') or '').strip()
    if generated_at:
        try:
            dt = datetime.fromisoformat(generated_at.replace('Z', '+00:00'))
            return dt.astimezone().strftime('%Y%m%d')
        except ValueError:
            pass
    match = re.search(r'(\d{8})', markdown_path.stem)
    if match:
        return match.group(1)
    return datetime.now().astimezone().strftime('%Y%m%d')


def delivery_key_for(recipient: str, data: dict, markdown_path: Path) -> tuple[str, str]:
    stamp = digest_stamp_for(data, markdown_path)
    return (f'{recipient}|{stamp}|{markdown_path.name}', stamp)


def get_delivery_state(conn: sqlite3.Connection, delivery_key: str) -> tuple[bool, bool]:
    row = conn.execute(
        'SELECT text_sent, attachment_sent FROM deliveries WHERE delivery_key = ?',
        (delivery_key,),
    ).fetchone()
    if not row:
        return (False, False)
    return (bool(row[0]), bool(row[1]))


def update_delivery_state(
    conn: sqlite3.Connection,
    delivery_key: str,
    recipient: str,
    digest_stamp: str,
    markdown_path: Path,
    *,
    text_sent: bool | None = None,
    attachment_sent: bool | None = None,
) -> tuple[bool, bool]:
    current_text, current_attachment = get_delivery_state(conn, delivery_key)
    new_text = current_text if text_sent is None else text_sent
    new_attachment = current_attachment if attachment_sent is None else attachment_sent
    conn.execute(
        '''
        INSERT INTO deliveries (
            delivery_key, recipient, digest_stamp, markdown_path,
            text_sent, attachment_sent, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(delivery_key) DO UPDATE SET
            recipient = excluded.recipient,
            digest_stamp = excluded.digest_stamp,
            markdown_path = excluded.markdown_path,
            text_sent = excluded.text_sent,
            attachment_sent = excluded.attachment_sent,
            updated_at = excluded.updated_at
        ''',
        (
            delivery_key,
            recipient,
            digest_stamp,
            str(markdown_path),
            int(new_text),
            int(new_attachment),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    return (new_text, new_attachment)


def main() -> int:
    ap = argparse.ArgumentParser(description='Generate the daily research digest and push it to QQ')
    ap.add_argument('--recipient', default=DEFAULT_RECIPIENT, help='QQ recipient, e.g. user:<openid> or group:<openid>')
    ap.add_argument('--topics', default=DEFAULT_TOPICS)
    ap.add_argument('--limit', type=int, default=DEFAULT_LIMIT)
    ap.add_argument('--window-days', type=int, default=DEFAULT_WINDOW_DAYS)
    ap.add_argument('--no-attach', action='store_true', help='Skip sending the markdown attachment')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--force', action='store_true', help='Ignore per-day delivery dedupe and send again')
    args = ap.parse_args()

    data = run_digest(args.limit, args.topics, args.window_days)
    summary = build_summary(data)
    markdown_path = resolve_markdown_path(data)
    delivery_key, digest_stamp = delivery_key_for(args.recipient, data, markdown_path)
    state_conn = open_delivery_db()
    prune_delivery_db(state_conn)
    already_text_sent, already_attachment_sent = ((False, False) if args.force else get_delivery_state(state_conn, delivery_key))

    if args.dry_run:
        print(summary)
        print(f'CHUNKS={len(chunk_text(summary))}')
        print(f'DELIVERY_KEY={delivery_key}')
        print(f'ALREADY_TEXT_SENT={int(already_text_sent)}')
        print(f'ALREADY_ATTACHMENT_SENT={int(already_attachment_sent)}')
        print(f'MARKDOWN={markdown_path}')
        return 0

    needs_attachment = (not args.no_attach) and markdown_path.exists()
    if not args.force and already_text_sent and (not needs_attachment or already_attachment_sent):
        print('ALREADY_SENT=1')
        print(f'MARKDOWN={markdown_path}')
        return 0

    config = load_config()
    token = fetch_token(config['app_id'], config['app_secret'])

    if not already_text_sent:
        for chunk in chunk_text(summary):
            send_text(args.recipient, chunk, token)
        update_delivery_state(
            state_conn,
            delivery_key,
            args.recipient,
            digest_stamp,
            markdown_path,
            text_sent=True,
        )
    else:
        print('SKIP_TEXT_ALREADY_SENT=1')

    if needs_attachment and not already_attachment_sent:
        send_document(args.recipient, markdown_path, token)
        update_delivery_state(
            state_conn,
            delivery_key,
            args.recipient,
            digest_stamp,
            markdown_path,
            attachment_sent=True,
        )
    elif needs_attachment:
        print('SKIP_ATTACHMENT_ALREADY_SENT=1')

    print(f'SENT_TO={args.recipient}')
    print(f'MARKDOWN={markdown_path}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
