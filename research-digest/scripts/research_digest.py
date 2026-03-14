#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import re
import textwrap
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ARXIV_API_URLS = [
    "https://export.arxiv.org/api/query",
    "https://arxiv.org/api/query",
]
HF_DAILY_PAPERS_API = "https://huggingface.co/api/daily_papers"
DEFAULT_TIMEOUT = 40
FETCH_RETRY_DELAYS = (2.0, 5.0, 10.0)
RETRYABLE_HTTP_STATUS = {408, 425, 429, 500, 502, 503, 504}
DEFAULT_WINDOW_DAYS = 7
DEFAULT_LIMIT = 20
TRANSLATION_TIMEOUT = 90
DEFAULT_OUTPUT_ROOT = Path.home() / "downloads" / "extracted" / "research-digest"
DEFAULT_TOPICS = ["llm", "3d", "video-generation", "world-model"]
DEFAULT_SOURCES = ["arxiv", "hf-daily"]
USER_AGENT = "zeroclaw-research-digest/0.1 (+https://github.com/zeroclaw-labs/zeroclaw)"

TOPIC_ALIASES = {
    "llm": "llm",
    "language-model": "llm",
    "language-models": "llm",
    "3d": "3d",
    "3d-generation": "3d",
    "video": "video-generation",
    "video-gen": "video-generation",
    "video-generation": "video-generation",
    "world": "world-model",
    "world-model": "world-model",
    "world-models": "world-model",
}

TOPIC_TITLES = {
    "llm": "LLM / 多模态语言模型",
    "3d": "3D / Neural Rendering / Gaussian Splatting",
    "video-generation": "Video Generation / Text-to-Video",
    "world-model": "World Model / Model-based RL",
}

TOPIC_KEYWORDS = {
    "llm": [
        "large language model", "llm", "language model", "multimodal language model",
        "reasoning model", "foundation model", "large multimodal model",
    ],
    "3d": [
        "3d", "gaussian splatting", "nerf", "neural rendering", "mesh", "point cloud",
        "3d generation", "3d reconstruction", "scene reconstruction", "avatar",
    ],
    "video-generation": [
        "video generation", "text-to-video", "image-to-video", "video diffusion",
        "video synthesis", "video model", "video editing", "generative video",
    ],
    "world-model": [
        "world model", "world models", "model-based reinforcement learning",
        "action-conditioned world model", "predictive world model", "dreamer",
        "learned simulator",
    ],
}

TOPIC_QUERY_CONFIG = {
    "llm": {
        "cats": ["cs.CL", "cs.AI", "cs.LG", "cs.IR", "stat.ML"],
        "terms": [
            ('ti', 'large language model'), ('abs', 'large language model'),
            ('ti', 'multimodal language model'), ('abs', 'multimodal language model'),
            ('ti', 'reasoning model'), ('abs', 'reasoning model'),
            ('ti', 'foundation model'), ('abs', 'foundation model'),
            ('ti', 'llm'), ('abs', 'llm'),
        ],
    },
    "3d": {
        "cats": ["cs.CV", "cs.GR", "cs.AI", "eess.IV"],
        "terms": [
            ('ti', '3d generation'), ('abs', '3d generation'),
            ('ti', '3d reconstruction'), ('abs', '3d reconstruction'),
            ('ti', 'gaussian splatting'), ('abs', 'gaussian splatting'),
            ('ti', 'nerf'), ('abs', 'nerf'),
            ('ti', 'neural rendering'), ('abs', 'neural rendering'),
        ],
    },
    "video-generation": {
        "cats": ["cs.CV", "cs.AI", "eess.IV"],
        "terms": [
            ('ti', 'video generation'), ('abs', 'video generation'),
            ('ti', 'text-to-video'), ('abs', 'text-to-video'),
            ('ti', 'image-to-video'), ('abs', 'image-to-video'),
            ('ti', 'video diffusion'), ('abs', 'video diffusion'),
            ('ti', 'video synthesis'), ('abs', 'video synthesis'),
        ],
    },
    "world-model": {
        "cats": ["cs.AI", "cs.LG", "cs.RO", "cs.CV"],
        "terms": [
            ('ti', 'world model'), ('abs', 'world model'),
            ('ti', 'world models'), ('abs', 'world models'),
            ('ti', 'model-based reinforcement learning'), ('abs', 'model-based reinforcement learning'),
            ('ti', 'action-conditioned world model'), ('abs', 'action-conditioned world model'),
            ('ti', 'predictive world model'), ('abs', 'predictive world model'),
            ('ti', 'dreamer'), ('abs', 'dreamer'),
        ],
    },
}

KNOWN_COMMANDS = {"collect", "digest"}
HELP_FLAGS = {"-h", "--help"}

ATOM_NS = {
    'a': 'http://www.w3.org/2005/Atom',
    'arxiv': 'http://arxiv.org/schemas/atom',
}


@dataclass
class PaperItem:
    key: str
    title: str
    authors: list[str]
    published_at: str
    summary: str
    url: str
    pdf_url: str | None = None
    topic_tags: list[str] = field(default_factory=list)
    source_tags: list[str] = field(default_factory=list)
    source_rank: float = 0.0
    arxiv_id: str | None = None
    hf_paper_url: str | None = None
    github_url: str | None = None
    upvotes: int | None = None
    importance_note: str | None = None


def bail(msg: str, code: int = 2) -> None:
    raise SystemExit(msg)


def normalize_topic(raw: str) -> str:
    key = raw.strip().lower()
    if key not in TOPIC_ALIASES:
        bail(f"Unsupported topic: {raw}")
    return TOPIC_ALIASES[key]


def parse_topics(raw: str | None) -> list[str]:
    if not raw:
        return list(DEFAULT_TOPICS)
    out: list[str] = []
    for part in raw.split(','):
        topic = normalize_topic(part)
        if topic not in out:
            out.append(topic)
    return out


def parse_sources(raw: str | None) -> list[str]:
    if not raw:
        return list(DEFAULT_SOURCES)
    allowed = {'arxiv', 'hf-daily', 'x'}
    out: list[str] = []
    for part in raw.split(','):
        key = part.strip().lower()
        if key not in allowed:
            bail(f"Unsupported source: {part}")
        if key not in out:
            out.append(key)
    return out


def normalize_ws(text: str) -> str:
    return re.sub(r'\s+', ' ', text or '').strip()


def compact_summary(text: str, max_sentences: int = 2, max_chars: int = 360) -> str:
    cleaned = normalize_ws(text)
    if not cleaned:
        return ''
    parts = re.split(r'(?<=[.!?。！？])\s+', cleaned)
    picked = ' '.join(part.strip() for part in parts[:max_sentences] if part.strip())
    if not picked:
        picked = cleaned
    if len(picked) > max_chars:
        picked = picked[: max_chars - 1].rstrip() + '…'
    return picked


def slugify(text: str) -> str:
    text = normalize_ws(text).lower()
    text = re.sub(r'[^a-z0-9]+', '-', text)
    return text.strip('-') or 'item'


def strip_arxiv_version(arxiv_id: str) -> str:
    return re.sub(r'v\d+$', '', arxiv_id)


def describe_fetch_error(exc: Exception) -> str:
    text = normalize_ws(str(exc)) or exc.__class__.__name__
    if len(text) > 180:
        text = text[:179].rstrip() + '…'
    if exc.__class__.__name__ in text:
        return text
    return f'{exc.__class__.__name__}: {text}'


def is_retryable_fetch_error(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in RETRYABLE_HTTP_STATUS
    return isinstance(exc, (urllib.error.URLError, TimeoutError, OSError))


def fetch_bytes(url: str, *, headers: dict[str, str] | None = None, timeout: int = DEFAULT_TIMEOUT) -> bytes:
    merged_headers = {"User-Agent": USER_AGENT, **(headers or {})}
    errors: list[str] = []
    attempts = len(FETCH_RETRY_DELAYS) + 1
    for attempt_idx in range(attempts):
        try:
            req = urllib.request.Request(url, headers=merged_headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as exc:
            errors.append(f'attempt {attempt_idx + 1}/{attempts}: {describe_fetch_error(exc)}')
            if not is_retryable_fetch_error(exc) or attempt_idx >= attempts - 1:
                break
            time.sleep(FETCH_RETRY_DELAYS[attempt_idx])
    raise RuntimeError(f'fetch failed for {url}: ' + '; '.join(errors))


def fetch_json(url: str, *, headers: dict[str, str] | None = None, timeout: int = DEFAULT_TIMEOUT) -> Any:
    return json.loads(fetch_bytes(url, headers=headers, timeout=timeout).decode('utf-8'))


def fetch_text(url: str, *, headers: dict[str, str] | None = None, timeout: int = DEFAULT_TIMEOUT) -> str:
    return fetch_bytes(url, headers=headers, timeout=timeout).decode('utf-8', errors='replace')


def load_codex_translation_config() -> dict[str, str]:
    config_path = Path.home() / '.codex' / 'config.toml'
    auth_path = Path.home() / '.codex' / 'auth.json'
    cfg = tomllib.loads(config_path.read_text(encoding='utf-8'))
    auth = json.loads(auth_path.read_text(encoding='utf-8'))
    provider_name = str(cfg.get('model_provider') or '').strip()
    provider = (cfg.get('model_providers') or {}).get(provider_name) or {}
    base_url = normalize_ws(str(provider.get('base_url') or ''))
    model = normalize_ws(str(cfg.get('model') or ''))
    api_key = normalize_ws(str(auth.get('OPENAI_API_KEY') or ''))
    if not base_url or not model or not api_key:
        raise RuntimeError('Missing codex translation config or OPENAI_API_KEY')
    return {'base_url': base_url, 'model': model, 'api_key': api_key}


def build_responses_url(base_url: str) -> str:
    base = base_url.rstrip('/')
    if base.endswith('/responses'):
        return base
    if base.endswith('/chat/completions'):
        return base[:-len('/chat/completions')] + '/responses'
    return base + '/responses'


def extract_response_text(payload: Any) -> str:
    if isinstance(payload, dict):
        top = payload.get('output_text')
        if isinstance(top, str) and top.strip():
            return top
        for item in payload.get('output') or []:
            if not isinstance(item, dict):
                continue
            for content in item.get('content') or []:
                if not isinstance(content, dict):
                    continue
                text = content.get('text')
                kind = content.get('type') or content.get('kind')
                if isinstance(text, str) and text.strip() and kind in {'output_text', 'text', None}:
                    return text
    raise RuntimeError('No output_text found in responses payload')


def parse_sse_output_text(body: str) -> str:
    saw_delta = False
    deltas: list[str] = []
    fallback = ''
    for chunk in body.split('\n\n'):
        data_lines = []
        for line in chunk.splitlines():
            if line.startswith('data:'):
                data_lines.append(line[len('data:'):].strip())
        if not data_lines:
            continue
        data = '\n'.join(data_lines).strip()
        if not data or data == '[DONE]':
            continue
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            continue
        event_type = event.get('type')
        if event_type == 'response.output_text.delta':
            delta = event.get('delta')
            if isinstance(delta, str) and delta:
                saw_delta = True
                deltas.append(delta)
            continue
        if event_type == 'response.output_text.done' and not saw_delta:
            text = event.get('text')
            if isinstance(text, str) and text.strip():
                fallback = text
            continue
        if event_type in {'response.completed', 'response.done'} and not fallback:
            response = event.get('response')
            if isinstance(response, dict):
                try:
                    fallback = extract_response_text(response)
                except Exception:
                    pass
    final = ''.join(deltas).strip() or fallback.strip()
    if not final:
        raise RuntimeError('No text found in SSE response')
    return final


def parse_json_loose(text: str) -> Any:
    cleaned = text.strip()
    if cleaned.startswith('```'):
        cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned)
        cleaned = re.sub(r'\s*```$', '', cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    for opener, closer in [('[', ']'), ('{', '}')]:
        start = cleaned.find(opener)
        end = cleaned.rfind(closer)
        if start == -1 or end == -1 or end <= start:
            continue
        snippet = cleaned[start : end + 1]
        try:
            return json.loads(snippet)
        except json.JSONDecodeError:
            continue
    raise ValueError('Could not parse JSON from model output')


def translate_summaries_to_zh(items: list[PaperItem]) -> dict[str, str]:
    payload_items = [
        {
            'key': item.key,
            'title': item.title,
            'summary_en': item.summary,
        }
        for item in items
        if normalize_ws(item.summary)
    ]
    if not payload_items:
        return {}

    cfg = load_codex_translation_config()
    instructions = (
        'You translate machine learning paper summaries into concise, faithful Simplified Chinese. '
        'Preserve paper names, model names, acronyms, and technical terms when that is clearer. '
        'Do not add facts. Return JSON only: an array of objects, each with keys key and summary_zh.'
    )
    prompt = (
        'Translate the following English paper summaries into Simplified Chinese. '
        'Keep each translation to about 1-2 sentences and preserve the original meaning. '
        'Return JSON only.\n\n'
        + json.dumps(payload_items, ensure_ascii=False)
    )
    req_body = {
        'model': cfg['model'],
        'instructions': instructions,
        'input': [{'role': 'user', 'content': prompt}],
        'stream': True,
    }
    req = urllib.request.Request(
        build_responses_url(cfg['base_url']),
        data=json.dumps(req_body).encode('utf-8'),
        headers={
            'Authorization': f"Bearer {cfg['api_key']}",
            'Content-Type': 'application/json',
            'OpenAI-Beta': 'responses=experimental',
            'User-Agent': 'codex-cli/1.0',
            'Accept': 'text/event-stream',
        },
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=TRANSLATION_TIMEOUT) as resp:
        raw = resp.read().decode('utf-8', errors='replace')
    model_text = parse_sse_output_text(raw)
    rows = parse_json_loose(model_text)
    if not isinstance(rows, list):
        raise RuntimeError('Translation response is not a JSON array')
    translated: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = normalize_ws(str(row.get('key') or ''))
        summary_zh = normalize_ws(str(row.get('summary_zh') or ''))
        if key and summary_zh:
            translated[key] = summary_zh
    if not translated:
        raise RuntimeError('Translation response did not include any summaries')
    return translated


def apply_chinese_summaries(selected: list[PaperItem], notes: list[str]) -> None:
    try:
        translated = translate_summaries_to_zh(selected)
    except Exception as exc:
        notes.append(f'摘要翻译失败，本次保留英文摘要：{summarize_error(exc)}')
        return
    for item in selected:
        if item.key in translated:
            item.summary = translated[item.key]


def detect_topics(*texts: str) -> list[str]:
    haystack = ' '.join(normalize_ws(x).lower() for x in texts if x)
    matched: list[str] = []
    for topic in DEFAULT_TOPICS:
        if any(keyword in haystack for keyword in TOPIC_KEYWORDS[topic]):
            matched.append(topic)
    return matched


def normalize_argv(argv: list[str]) -> list[str]:
    if len(argv) <= 1:
        return argv
    if len(argv) == 2 and argv[1] in HELP_FLAGS:
        return argv
    if argv[1] in KNOWN_COMMANDS:
        return argv
    return [argv[0], "digest", *argv[1:]]


def parse_since(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        if len(raw) == 10:
            return datetime.fromisoformat(raw + "T00:00:00+00:00").astimezone(timezone.utc)
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        bail(f"Invalid --since value: {value}")


def filter_since(items: list[PaperItem], since: datetime | None) -> list[PaperItem]:
    if since is None:
        return items
    kept: list[PaperItem] = []
    for item in items:
        try:
            if iso_to_dt(item.published_at) >= since:
                kept.append(item)
        except Exception:
            continue
    return kept


def build_arxiv_query(topic: str) -> str:
    cfg = TOPIC_QUERY_CONFIG[topic]
    cat_expr = ' OR '.join(f'cat:{cat}' for cat in cfg['cats'])
    term_expr = ' OR '.join(f'{field}:"{value}"' for field, value in cfg['terms'])
    return f'({cat_expr}) AND ({term_expr})'


def iso_to_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)


def format_day(value: str) -> str:
    try:
        return iso_to_dt(value).strftime('%Y-%m-%d')
    except Exception:
        return value[:10]


def candidate_score(item: PaperItem, now: datetime) -> float:
    score = item.source_rank
    try:
        age_days = max(0.0, (now - iso_to_dt(item.published_at)).total_seconds() / 86400)
    except Exception:
        age_days = 30.0
    score += max(0.0, 30.0 - age_days * 3.0)
    score += min(12.0, len(item.topic_tags) * 2.0)
    if item.github_url:
        score += 2.5
    if item.upvotes:
        score += min(12.0, float(item.upvotes))
    if 'hf-daily' in item.source_tags:
        score += 12.0
    if 'arxiv' in item.source_tags:
        score += 4.0
    return score


def merge_items(base: PaperItem, incoming: PaperItem) -> PaperItem:
    for topic in incoming.topic_tags:
        if topic not in base.topic_tags:
            base.topic_tags.append(topic)
    for source in incoming.source_tags:
        if source not in base.source_tags:
            base.source_tags.append(source)
    base.source_rank = max(base.source_rank, incoming.source_rank)
    if not base.summary or len(incoming.summary) > len(base.summary):
        base.summary = incoming.summary
    if not base.pdf_url and incoming.pdf_url:
        base.pdf_url = incoming.pdf_url
    if not base.hf_paper_url and incoming.hf_paper_url:
        base.hf_paper_url = incoming.hf_paper_url
    if not base.github_url and incoming.github_url:
        base.github_url = incoming.github_url
    if incoming.upvotes is not None:
        base.upvotes = max(base.upvotes or 0, incoming.upvotes)
    if incoming.importance_note and not base.importance_note:
        base.importance_note = incoming.importance_note
    return base


def fetch_hf_daily(topics: list[str], limit: int) -> list[PaperItem]:
    data = fetch_json(f'{HF_DAILY_PAPERS_API}?limit={max(limit, 20)}')
    out: list[PaperItem] = []
    for row in data:
        paper = row.get('paper') or {}
        title = normalize_ws(paper.get('title', ''))
        summary_text = paper.get('ai_summary') or paper.get('summary') or ''
        matched = detect_topics(title, summary_text)
        if topics and not set(matched).intersection(topics):
            continue
        arxiv_id = strip_arxiv_version(str(paper.get('id', '')).strip()) or None
        authors = [a.get('name', '').strip() for a in paper.get('authors') or [] if a.get('name')]
        url = f'https://arxiv.org/abs/{arxiv_id}' if arxiv_id else f'https://huggingface.co/papers/{slugify(title)}'
        hf_paper_url = f'https://huggingface.co/papers/{arxiv_id}' if arxiv_id else None
        item = PaperItem(
            key=arxiv_id or slugify(title),
            title=title,
            authors=authors,
            published_at=paper.get('publishedAt') or paper.get('submittedOnDailyAt') or '',
            summary=compact_summary(summary_text, max_sentences=2, max_chars=360),
            url=url,
            pdf_url=f'https://arxiv.org/pdf/{arxiv_id}.pdf' if arxiv_id else None,
            topic_tags=sorted(set(matched) or set(topics)),
            source_tags=['hf-daily'],
            source_rank=22.0,
            arxiv_id=arxiv_id,
            hf_paper_url=hf_paper_url,
            github_url=paper.get('githubRepo') or None,
            upvotes=paper.get('upvotes'),
            importance_note='Hugging Face Daily Papers 收录',
        )
        out.append(item)
    return out


def fetch_arxiv(topics: list[str], per_topic: int, window_days: int) -> list[PaperItem]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    out: list[PaperItem] = []
    for topic in topics:
        query = build_arxiv_query(topic)
        params = {
            'search_query': query,
            'start': '0',
            'max_results': str(max(per_topic, 10)),
            'sortBy': 'submittedDate',
            'sortOrder': 'descending',
        }
        root = None
        arxiv_errors: list[str] = []
        for base_url in ARXIV_API_URLS:
            url = f"{base_url}?{urllib.parse.urlencode(params)}"
            try:
                root = ET.fromstring(fetch_text(url))
                break
            except Exception as exc:
                arxiv_errors.append(f'{base_url}: {summarize_error(exc)}')
        if root is None:
            raise RuntimeError(f'arXiv query failed for topic {topic}: ' + ' | '.join(arxiv_errors))
        for entry in root.findall('a:entry', ATOM_NS):
            published = entry.findtext('a:published', default='', namespaces=ATOM_NS)
            if not published:
                continue
            published_dt = iso_to_dt(published)
            if published_dt < cutoff:
                continue
            title = normalize_ws(entry.findtext('a:title', default='', namespaces=ATOM_NS))
            summary_text = normalize_ws(entry.findtext('a:summary', default='', namespaces=ATOM_NS))
            matched = detect_topics(title, summary_text)
            if topic not in matched:
                matched.append(topic)
            authors = [normalize_ws(a.findtext('a:name', default='', namespaces=ATOM_NS)) for a in entry.findall('a:author', ATOM_NS)]
            entry_id = normalize_ws(entry.findtext('a:id', default='', namespaces=ATOM_NS))
            arxiv_id = strip_arxiv_version(entry_id.rsplit('/', 1)[-1]) if entry_id else None
            pdf_url = None
            for link in entry.findall('a:link', ATOM_NS):
                if link.attrib.get('title') == 'pdf':
                    pdf_url = link.attrib.get('href')
                    break
            item = PaperItem(
                key=arxiv_id or slugify(title),
                title=title,
                authors=[a for a in authors if a],
                published_at=published,
                summary=compact_summary(summary_text, max_sentences=2, max_chars=360),
                url=f'https://arxiv.org/abs/{arxiv_id}' if arxiv_id else entry_id,
                pdf_url=pdf_url,
                topic_tags=sorted(set(matched)),
                source_tags=['arxiv'],
                source_rank=10.0,
                arxiv_id=arxiv_id,
            )
            out.append(item)
    return out


def fetch_x_placeholders() -> dict[str, Any]:
    return {
        'enabled': False,
        'reason': 'X source is not included by default. Official X recent-search access normally requires a separately configured bearer token, so this skill currently focuses on arXiv + Hugging Face Daily Papers.',
    }


def summarize_error(exc: Exception) -> str:
    text = normalize_ws(str(exc)) or exc.__class__.__name__
    if len(text) > 180:
        text = text[:179].rstrip() + '…'
    if exc.__class__.__name__ in text:
        return text
    return f'{exc.__class__.__name__}: {text}'


def gather_candidates(topics: list[str], sources: list[str], limit: int, window_days: int) -> tuple[list[PaperItem], dict[str, Any]]:
    merged: dict[str, PaperItem] = {}
    meta: dict[str, Any] = {'sources': sources, 'notes': [], 'source_errors': {}}

    def merge_batch(source_name: str, items: list[PaperItem]) -> None:
        for item in items:
            merged[item.key] = merge_items(merged[item.key], item) if item.key in merged else item

    if 'hf-daily' in sources:
        try:
            merge_batch('hf-daily', fetch_hf_daily(topics, max(limit * 2, 20)))
        except Exception as exc:
            msg = summarize_error(exc)
            meta['source_errors']['hf-daily'] = msg
            meta['notes'].append(f'数据源 hf-daily 拉取失败，本次已跳过：{msg}')

    if 'arxiv' in sources:
        per_topic = max(12, limit)
        try:
            merge_batch('arxiv', fetch_arxiv(topics, per_topic=per_topic, window_days=window_days))
        except Exception as exc:
            msg = summarize_error(exc)
            meta['source_errors']['arxiv'] = msg
            meta['notes'].append(f'数据源 arxiv 拉取失败，本次已跳过：{msg}')

    if 'x' in sources:
        meta['x'] = fetch_x_placeholders()
        meta['notes'].append(meta['x']['reason'])

    items = list(merged.values())
    now = datetime.now(timezone.utc)
    for item in items:
        item.source_rank = candidate_score(item, now)
    items.sort(key=lambda x: (-x.source_rank, x.published_at), reverse=False)
    if not items and meta['source_errors']:
        meta['notes'].append('本次可用上游源全部失败，因此没有生成论文条目。')
    return items, meta


def select_items(items: list[PaperItem], topics: list[str], limit: int) -> list[PaperItem]:
    if not items:
        return []
    quotas: dict[str, int] = {}
    base = limit // max(1, len(topics))
    rem = limit % max(1, len(topics))
    for idx, topic in enumerate(topics):
        quotas[topic] = base + (1 if idx < rem else 0)
    selected: list[PaperItem] = []
    seen: set[str] = set()
    for topic in topics:
        need = quotas[topic]
        topic_items = [item for item in items if topic in item.topic_tags and item.key not in seen]
        topic_items.sort(key=lambda x: (-x.source_rank, x.published_at), reverse=False)
        for item in topic_items:
            if need <= 0:
                break
            selected.append(item)
            seen.add(item.key)
            need -= 1
    if len(selected) < limit:
        for item in sorted(items, key=lambda x: (-x.source_rank, x.published_at), reverse=False):
            if item.key in seen:
                continue
            selected.append(item)
            seen.add(item.key)
            if len(selected) >= limit:
                break
    return selected[:limit]


def format_links(item: PaperItem) -> str:
    parts = [item.url]
    if item.pdf_url:
        parts.append(item.pdf_url)
    if item.hf_paper_url:
        parts.append(item.hf_paper_url)
    if item.github_url:
        parts.append(item.github_url)
    dedup: list[str] = []
    for part in parts:
        if part and part not in dedup:
            dedup.append(part)
    return ' | '.join(dedup)


def render_markdown(selected: list[PaperItem], topics: list[str], sources: list[str], window_days: int, generated_at: datetime, notes: list[str]) -> str:
    by_topic: dict[str, list[PaperItem]] = {topic: [] for topic in topics}
    leftovers: list[PaperItem] = []
    for item in selected:
        placed = False
        for topic in topics:
            if topic in item.topic_tags and len(by_topic[topic]) < math.ceil(len(selected) / max(1, len(topics))) + 2:
                by_topic[topic].append(item)
                placed = True
                break
        if not placed:
            leftovers.append(item)
    lines = [
        f'# Daily Research Digest - {generated_at.strftime("%Y-%m-%d")}',
        '',
        f'- 生成时间：{generated_at.strftime("%Y-%m-%d %H:%M:%S %Z")}',
        f'- 数据源：{", ".join(sources)}',
        f'- 主题：{", ".join(topics)}',
        f'- arXiv 窗口：最近 {window_days} 天',
        f'- 条目数：{len(selected)}',
    ]
    for note in notes:
        lines.append(f'- 备注：{note}')
    lines.append('')
    for topic in topics:
        items = by_topic.get(topic) or []
        if not items:
            continue
        lines.append(f'## {TOPIC_TITLES.get(topic, topic)}')
        lines.append('')
        for idx, item in enumerate(items, 1):
            source_text = ', '.join(item.source_tags)
            topic_text = ', '.join(item.topic_tags)
            authors = ', '.join(item.authors[:6]) + (' 等' if len(item.authors) > 6 else '')
            lines.extend([
                f'{idx}. {item.title}',
                f'   - 日期：{format_day(item.published_at)}',
                f'   - 作者：{authors or "未知"}',
                f'   - 主题标签：{topic_text}',
                f'   - 来源：{source_text}',
                f'   - 摘要：{item.summary}',
                f'   - 链接：{format_links(item)}',
            ])
            if item.importance_note:
                lines.append(f'   - 备注：{item.importance_note}')
            lines.append('')
    if leftovers:
        lines.append('## 其他补充')
        lines.append('')
        for idx, item in enumerate(leftovers, 1):
            lines.extend([
                f'{idx}. {item.title}',
                f'   - 日期：{format_day(item.published_at)}',
                f'   - 主题标签：{", ".join(item.topic_tags)}',
                f'   - 摘要：{item.summary}',
                f'   - 链接：{format_links(item)}',
                '',
            ])
    return '\n'.join(lines).rstrip() + '\n'


def output_file_stem(generated_at: datetime) -> str:
    local_dt = generated_at.astimezone()
    return f"daily-research-digest-{local_dt.strftime('%Y%m%d')}"


def write_outputs(markdown: str, payload: dict[str, Any], output_dir: Path, generated_at: datetime) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = output_file_stem(generated_at)
    md_path = output_dir / f'{stem}.md'
    json_path = output_dir / f'{stem}.json'
    md_path.write_text(markdown, encoding='utf-8')
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    return md_path, json_path


def make_payload(selected: list[PaperItem], topics: list[str], sources: list[str], meta: dict[str, Any], generated_at: datetime, window_days: int) -> dict[str, Any]:
    return {
        'generated_at': generated_at.isoformat(),
        'topics': topics,
        'sources': sources,
        'window_days': window_days,
        'notes': meta.get('notes', []),
        'items': [asdict(item) for item in selected],
        'source_meta': {k: v for k, v in meta.items() if k != 'notes'},
    }


def command_collect(args: argparse.Namespace) -> int:
    topics = parse_topics(args.topics)
    sources = parse_sources(args.sources)
    items, meta = gather_candidates(topics, sources, args.limit, args.window_days)
    items = filter_since(items, parse_since(getattr(args, 'since', None)))
    payload = make_payload(items[: args.limit], topics, sources, meta, datetime.now(timezone.utc), args.window_days)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def command_digest(args: argparse.Namespace) -> int:
    topics = parse_topics(args.topics)
    sources = parse_sources(args.sources)
    generated_at = datetime.now(timezone.utc)
    items, meta = gather_candidates(topics, sources, args.limit, args.window_days)
    items = filter_since(items, parse_since(getattr(args, 'since', None)))
    selected = select_items(items, topics, args.limit)
    notes = meta.setdefault('notes', [])
    apply_chinese_summaries(selected, notes)
    payload = make_payload(selected, topics, sources, meta, generated_at, args.window_days)
    markdown = render_markdown(selected, topics, sources, args.window_days, generated_at, notes)
    if getattr(args, 'out', None):
        md_path = Path(args.out).expanduser()
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(markdown, encoding='utf-8')
        json_path = md_path.with_suffix('.json') if md_path.suffix else Path(str(md_path) + '.json')
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    else:
        output_dir = Path(args.output_dir).expanduser() if args.output_dir else DEFAULT_OUTPUT_ROOT / generated_at.strftime('%Y-%m-%d')
        md_path, json_path = write_outputs(markdown, payload, output_dir, generated_at)
    if args.json:
        out = dict(payload)
        out['markdown_path'] = str(md_path)
        out['json_path'] = str(json_path)
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(markdown)
        print(f'\nOUTPUT_MARKDOWN={md_path}')
        print(f'OUTPUT_JSON={json_path}')
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Build a daily research digest from arXiv and Hugging Face Daily Papers')
    sub = parser.add_subparsers(dest='cmd', required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument('--topics', '--domains', dest='topics', default=','.join(DEFAULT_TOPICS), help='Comma-separated topics: llm,3d,video-generation,world-model')
        p.add_argument('--sources', default=','.join(DEFAULT_SOURCES), help='Comma-separated sources: arxiv,hf-daily[,x]')
        p.add_argument('--limit', '--count', dest='limit', type=int, default=DEFAULT_LIMIT, help='Number of final items to return')
        p.add_argument('--window-days', '--days', dest='window_days', type=int, default=DEFAULT_WINDOW_DAYS, help='Recent-day window for arXiv filtering')
        p.add_argument('--since', default=None, help='Optional ISO date lower bound (YYYY-MM-DD)')

    collect_p = sub.add_parser('collect', help='Collect raw candidates as JSON')
    add_common(collect_p)

    digest_p = sub.add_parser('digest', help='Generate a curated daily digest')
    add_common(digest_p)
    digest_p.add_argument('--output-dir', default=None, help='Directory to store markdown/json outputs')
    digest_p.add_argument('--out', default=None, help='Compatibility alias for an explicit markdown output path')
    digest_p.add_argument('--json', action='store_true', help='Print JSON instead of markdown')

    return parser


def main() -> int:
    argv = normalize_argv(__import__('sys').argv)
    args = build_parser().parse_args(argv[1:])
    if args.cmd == 'collect':
        return command_collect(args)
    if args.cmd == 'digest':
        return command_digest(args)
    bail(f'Unknown command: {args.cmd}')
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
