#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path

STATE_ROOT = Path.home() / '.zeroclaw' / 'workspace' / 'state' / 'codex-tmux'
JOBS_ROOT = STATE_ROOT / 'jobs'
DEFAULT_SESSION = 'saki-codex'
DEFAULT_STALL_MINUTES = 20
DEFAULT_TAIL_LINES = 120
DEFAULT_QQ_RECIPIENT = os.environ.get('ZEROCLAW_QQ_RECIPIENT', '').strip()


def run(cmd: list[str], *, check: bool = True, capture: bool = True, text: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, capture_output=capture, text=text)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')


def sanitize_slug(value: str) -> str:
    value = value.strip().lower()
    cleaned = ''.join(ch if ch.isalnum() or ch in ('-', '_') else '-' for ch in value)
    cleaned = cleaned.strip('-_')
    while '--' in cleaned:
        cleaned = cleaned.replace('--', '-')
    return cleaned or 'job'


def signal_name_for_run(job_id: str, run_name: str) -> str:
    return f'codex-tmux-{sanitize_slug(job_id)}-{sanitize_slug(run_name)}-done'


def ensure_dirs() -> None:
    JOBS_ROOT.mkdir(parents=True, exist_ok=True)


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')


def read_text(path: Path, default: str = '') -> str:
    try:
        return path.read_text(encoding='utf-8').strip()
    except FileNotFoundError:
        return default


def spawn_waiter(job_id: str, run_name: str, *, notify_qq: bool = True) -> None:
    cmd = [sys.executable, str(Path(__file__).resolve()), 'wait', job_id, '--run', run_name]
    if notify_qq:
        cmd.append('--notify-qq')
    subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def current_run_name(job_dir: Path) -> str:
    value = read_text(job_dir / 'current_run.txt')
    if not value:
        raise SystemExit(f'missing current_run.txt in {job_dir}')
    return value


def run_dir_for(job_dir: Path, run_name: str | None = None) -> Path:
    if run_name:
        return job_dir / 'runs' / run_name
    return job_dir / 'runs' / current_run_name(job_dir)


def current_run_dir(job_dir: Path) -> Path:
    return run_dir_for(job_dir)


def next_run_name(job_dir: Path) -> str:
    runs_dir = job_dir / 'runs'
    runs_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(
        int(path.name.split('-')[1])
        for path in runs_dir.iterdir()
        if path.is_dir() and path.name.startswith('run-') and path.name.split('-')[1].isdigit()
    )
    next_idx = (existing[-1] + 1) if existing else 1
    return f'run-{next_idx:04d}'


def parse_thread_id(jsonl_path: Path) -> str:
    if not jsonl_path.exists():
        return ''
    for line in jsonl_path.read_text(encoding='utf-8', errors='replace').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get('type') == 'thread.started' and obj.get('thread_id'):
            return str(obj['thread_id']).strip()
    return ''


def wait_for_thread_id(jsonl_path: Path, timeout_secs: float = 8.0) -> str:
    deadline = time.time() + timeout_secs
    while time.time() < deadline:
        thread_id = parse_thread_id(jsonl_path)
        if thread_id:
            return thread_id
        time.sleep(0.25)
    return ''


def load_thread_id(job_dir: Path) -> str:
    thread_id = read_text(job_dir / 'thread_id.txt')
    if thread_id:
        return thread_id
    run_dir = current_run_dir(job_dir)
    thread_id = parse_thread_id(run_dir / 'events.jsonl')
    if not thread_id and read_text(run_dir / 'status.txt') == 'running':
        thread_id = wait_for_thread_id(run_dir / 'events.jsonl')
    if thread_id:
        write_text(job_dir / 'thread_id.txt', thread_id + '\n')
    return thread_id


def ensure_tmux_session(session: str) -> None:
    result = subprocess.run(['tmux', 'has-session', '-t', session], capture_output=True, text=True)
    if result.returncode == 0:
        return
    env_cmd = f"env PATH={shlex.quote(os.environ.get('PATH', ''))} HOME={shlex.quote(str(Path.home()))} SHELL={shlex.quote(os.environ.get('SHELL', '/bin/zsh'))} zsh -lc 'exec zsh'"
    subprocess.run(['tmux', 'new-session', '-d', '-s', session, '-n', 'scratch', env_cmd], check=True)


def make_window_name(job_id: str, run_name: str) -> str:
    base = f"{sanitize_slug(job_id)}-{run_name[-2:]}"
    return base[:28] or 'codex-job'


def write_args_file(path: Path, model: str | None, search: bool, reasoning: str | None) -> None:
    args: list[str] = []
    if search:
        args.append('--search')
    if model:
        args.extend(['-m', model])
    if reasoning:
        args.extend(['-c', f'model_reasoning_effort={json.dumps(reasoning)}'])
    write_text(path, '\n'.join(args) + ('\n' if args else ''))


def build_runner_script(
    script_path: Path,
    *,
    mode: str,
    cwd: Path,
    prompt_file: Path,
    args_file: Path,
    jsonl_file: Path,
    last_file: Path,
    status_file: Path,
    exit_file: Path,
    started_file: Path,
    finished_file: Path,
    thread_out_file: Path,
    resume_thread_id: str | None,
    signal_name: str,
) -> None:
    runner = f'''#!/usr/bin/env bash
set -euo pipefail
export PATH={shlex.quote(os.environ.get('PATH', ''))}
export HOME={shlex.quote(str(Path.home()))}
export SHELL={shlex.quote(os.environ.get('SHELL', '/bin/zsh'))}
export TERM=xterm-256color
CWD={shlex.quote(str(cwd))}
PROMPT_FILE={shlex.quote(str(prompt_file))}
ARGS_FILE={shlex.quote(str(args_file))}
JSONL_FILE={shlex.quote(str(jsonl_file))}
LAST_FILE={shlex.quote(str(last_file))}
STATUS_FILE={shlex.quote(str(status_file))}
EXIT_FILE={shlex.quote(str(exit_file))}
STARTED_FILE={shlex.quote(str(started_file))}
FINISHED_FILE={shlex.quote(str(finished_file))}
THREAD_OUT_FILE={shlex.quote(str(thread_out_file))}
RESUME_THREAD_ID={shlex.quote(resume_thread_id or '')}
MODE={shlex.quote(mode)}
TMUX_SIGNAL={shlex.quote(signal_name)}
cd "$CWD"
echo running > "$STATUS_FILE"
date -Iseconds > "$STARTED_FILE"
: > "$JSONL_FILE"
: > "$LAST_FILE"
mapfile -t EXTRA_ARGS < "$ARGS_FILE" || true
GLOBAL_ARGS=()
EXEC_ARGS=()
for arg in "${{EXTRA_ARGS[@]}}"; do
  case "$arg" in
    --search)
      GLOBAL_ARGS+=("$arg")
      ;;
    *)
      EXEC_ARGS+=("$arg")
      ;;
  esac
done
set +e
if [[ "$MODE" == "start" ]]; then
  codex "${{GLOBAL_ARGS[@]}}" exec "${{EXEC_ARGS[@]}}" --json --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox -C "$CWD" - -o "$LAST_FILE" < "$PROMPT_FILE" | tee "$JSONL_FILE"
  rc=${{PIPESTATUS[0]}}
else
  codex "${{GLOBAL_ARGS[@]}}" exec resume "${{EXEC_ARGS[@]}}" --json --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox "$RESUME_THREAD_ID" - -o "$LAST_FILE" < "$PROMPT_FILE" | tee "$JSONL_FILE"
  rc=${{PIPESTATUS[0]}}
fi
set -e
python3 - "$JSONL_FILE" "$THREAD_OUT_FILE" <<'PYEOF'
import json, sys
jsonl_path, out_path = sys.argv[1], sys.argv[2]
thread_id = ''
try:
    with open(jsonl_path, 'r', encoding='utf-8', errors='replace') as handle:
        for line in handle:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get('type') == 'thread.started' and obj.get('thread_id'):
                thread_id = str(obj['thread_id']).strip()
                break
except FileNotFoundError:
    pass
if thread_id:
    with open(out_path, 'w', encoding='utf-8') as handle:
        print(thread_id, file=handle)
PYEOF
echo "$rc" > "$EXIT_FILE"
if [[ "$rc" == "0" ]]; then
  echo completed > "$STATUS_FILE"
else
  echo failed > "$STATUS_FILE"
fi
date -Iseconds > "$FINISHED_FILE"
tmux wait-for -S "$TMUX_SIGNAL" || true
printf '\\n[exit:%s]\\n' "$rc"
exit "$rc"
'''
    write_text(script_path, runner)
    script_path.chmod(0o755)


def job_paths(job_id: str) -> Path:
    return JOBS_ROOT / job_id


def collect_jobs() -> list[Path]:
    if not JOBS_ROOT.exists():
        return []
    return sorted([path for path in JOBS_ROOT.iterdir() if path.is_dir()])


def latest_activity_mtime(run_dir: Path) -> float:
    candidates = [run_dir / 'events.jsonl', run_dir / 'last.txt', run_dir / 'started_at.txt']
    mtimes = [path.stat().st_mtime for path in candidates if path.exists()]
    return max(mtimes) if mtimes else 0.0


def infer_status(job_dir: Path, stall_minutes: int, run_name: str | None = None) -> tuple[str, Path]:
    run_dir = run_dir_for(job_dir, run_name)
    raw = read_text(run_dir / 'status.txt', 'unknown')
    if raw == 'running':
        last_mtime = latest_activity_mtime(run_dir)
        if last_mtime and (time.time() - last_mtime) > stall_minutes * 60:
            return 'stale', run_dir
    return raw, run_dir


def summarize_text(text: str, limit: int = 240) -> str:
    collapsed = ' '.join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + '…'


def print_status(job_dir: Path, *, stall_minutes: int, verbose: bool, run_name: str | None = None) -> None:
    status, run_dir = infer_status(job_dir, stall_minutes, run_name)
    thread_id = load_thread_id(job_dir)
    cwd = read_text(job_dir / 'cwd.txt')
    current_run = run_dir.name
    last_text = summarize_text(read_text(run_dir / 'last.txt'))
    print(f'job: {job_dir.name}')
    print(f'status: {status}')
    print(f'cwd: {cwd}')
    print(f'run: {current_run}')
    recipient = read_text(job_dir / 'recipient.txt')
    if recipient:
        print(f'recipient: {recipient}')
    if thread_id:
        print(f'thread: {thread_id}')
    if verbose:
        window = read_text(run_dir / 'window.txt')
        session = read_text(job_dir / 'session.txt', DEFAULT_SESSION)
        print(f'session: {session}')
        if window:
            print(f'window: {window}')
        if last_text:
            print(f'last: {last_text}')
    print()


def tmux_capture(job_dir: Path, lines: int, run_name: str | None = None) -> str:
    run_dir = run_dir_for(job_dir, run_name)
    window = read_text(run_dir / 'window.txt')
    session = read_text(job_dir / 'session.txt', DEFAULT_SESSION)
    if not window:
        return ''
    proc = subprocess.run(
        ['tmux', 'capture-pane', '-p', '-t', f'{session}:{window}', '-S', f'-{lines}'],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return ''
    return proc.stdout.rstrip()


def sha1_text(text: str) -> str:
    return hashlib.sha1(text.encode('utf-8')).hexdigest()[:12]


def build_notice(job_dir: Path, *, stall_minutes: int, run_name: str | None = None) -> tuple[str, str]:
    status, run_dir = infer_status(job_dir, stall_minutes, run_name)
    if status not in {'completed', 'failed', 'stale'}:
        return '', ''
    last_text = read_text(run_dir / 'last.txt')
    current_run = run_dir.name
    if status == 'stale':
        key = f'{current_run}|stale'
        body = textwrap.dedent(f'''\
        Codex 后台任务似乎卡住了。
        - job: {job_dir.name}
        - run: {current_run}
        - cwd: {read_text(job_dir / 'cwd.txt')}
        - 建议: 现在检查一次状态，必要时给一个更小、更具体的 follow-up。
        ''').strip()
        return key, body
    label = '已完成' if status == 'completed' else '失败'
    summary = summarize_text(last_text or '(没有捕获到末条回复)', 360)
    key = f'{current_run}|{status}|{sha1_text(summary)}'
    body = textwrap.dedent(f'''\
    Codex 后台任务{label}。
    - job: {job_dir.name}
    - run: {current_run}
    - cwd: {read_text(job_dir / 'cwd.txt')}
    - 摘要: {summary}
    ''').strip()
    return key, body


def notify_qq(recipient: str, text: str) -> None:
    if not text.strip():
        return
    qq_notify = Path.home() / 'utils' / 'zeroclaw' / 'bin' / 'qq-notify'
    subprocess.run([str(qq_notify), '--recipient', recipient, '--text', text], check=True)


def build_saki_callback_prompt(job_dir: Path, run_dir: Path, status: str) -> str:
    prompt_text = read_text(run_dir / 'prompt.txt')
    last_text = read_text(run_dir / 'last.txt')
    exit_code = read_text(run_dir / 'exit_code.txt')
    job_id = job_dir.name
    run_name = run_dir.name
    cwd = read_text(job_dir / 'cwd.txt')
    started_at = read_text(run_dir / 'started_at.txt')
    finished_at = read_text(run_dir / 'finished_at.txt')
    prompt_excerpt = summarize_text(prompt_text, 1200) if prompt_text else '(none)'
    last_excerpt = summarize_text(last_text, 2400) if last_text else '(none)'
    status_label = {'completed': '已完成', 'failed': '失败', 'stale': '似乎卡住了'}.get(status, status)
    return textwrap.dedent(
        f"""\
[SYSTEM BACKGROUND CALLBACK]
这不是用户刚发来的消息，而是一条后台 Codex 任务完成回调。请把它当成系统事件，而不是新的用户请求。

你的任务：
- 面向当前 QQ 对话里的用户，用自然中文回推这次后台任务的结果。
- 先点明“之前后台运行的任务现在{status_label}”。
- 不要原样转发生硬的 job/run/cwd/摘要 模板。
- 重点概括：真正做成了什么、产物是什么、若有网页/文件/路径那是什么。
- 如果结果里包含本地路径，可以说“结果已整理在某路径”；不要说“已经发给用户”除非确实发送了附件。
- 若任务失败或卡住，要清楚说原因，并给一句下一步建议。
- 不要使用 ** 加粗。
- 保持简洁，默认 4~8 行。

以下是系统提供的后台任务元数据：
- job: {job_id}
- run: {run_name}
- status: {status}
- exit_code: {exit_code or '(none)'}
- cwd: {cwd}
- started_at: {started_at or '(unknown)'}
- finished_at: {finished_at or '(unknown)'}

原始后台任务目标（节选）：
{prompt_excerpt}

后台任务最终结果/末条输出（节选）：
{last_excerpt}
"""
    ).strip()


def render_saki_notice(job_dir: Path, run_dir: Path, status: str, fallback_body: str) -> str:
    prompt = build_saki_callback_prompt(job_dir, run_dir, status)
    internal_chat = Path.home() / 'utils' / 'zeroclaw' / 'bin' / 'saki-internal-chat'
    proc = subprocess.run(
        [str(internal_chat), '--fallback-cli'],
        input=prompt,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        stderr = (proc.stderr or '').strip()
        if stderr:
            print(f'[codex-tmux] saki summarize failed: {stderr}', file=sys.stderr)
        return fallback_body
    rendered = proc.stdout.strip()
    return rendered or fallback_body


def cmd_start(args: argparse.Namespace) -> int:
    ensure_dirs()
    prompt = args.prompt if args.prompt is not None else Path(args.prompt_file).read_text(encoding='utf-8')
    cwd = Path(args.cwd).expanduser().resolve()
    if not cwd.exists():
        raise SystemExit(f'cwd does not exist: {cwd}')
    slug = sanitize_slug(args.name or cwd.name)
    job_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{slug}"
    job_dir = job_paths(job_id)
    run_name = 'run-0001'
    run_dir = job_dir / 'runs' / run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    signal_name = signal_name_for_run(job_id, run_name)
    write_text(job_dir / 'created_at.txt', now_iso() + '\n')
    write_text(job_dir / 'cwd.txt', str(cwd) + '\n')
    write_text(job_dir / 'session.txt', (args.session or DEFAULT_SESSION) + '\n')
    write_text(job_dir / 'current_run.txt', run_name + '\n')
    if args.recipient:
        write_text(job_dir / 'recipient.txt', args.recipient + '\n')
    write_text(run_dir / 'prompt.txt', prompt.rstrip() + '\n')
    write_args_file(run_dir / 'args.txt', args.model, args.search, args.reasoning)
    write_text(run_dir / 'signal.txt', signal_name + '\n')
    window_name = make_window_name(job_id, run_name)
    write_text(run_dir / 'window.txt', window_name + '\n')
    build_runner_script(
        run_dir / 'runner.sh',
        mode='start',
        cwd=cwd,
        prompt_file=run_dir / 'prompt.txt',
        args_file=run_dir / 'args.txt',
        jsonl_file=run_dir / 'events.jsonl',
        last_file=run_dir / 'last.txt',
        status_file=run_dir / 'status.txt',
        exit_file=run_dir / 'exit_code.txt',
        started_file=run_dir / 'started_at.txt',
        finished_file=run_dir / 'finished_at.txt',
        thread_out_file=job_dir / 'thread_id.txt',
        resume_thread_id=None,
        signal_name=signal_name,
    )
    session = args.session or DEFAULT_SESSION
    ensure_tmux_session(session)
    runner_cmd = f"env PATH={shlex.quote(os.environ.get('PATH', ''))} HOME={shlex.quote(str(Path.home()))} SHELL={shlex.quote(os.environ.get('SHELL', '/bin/zsh'))} bash -lc {shlex.quote(str(run_dir / 'runner.sh') + '; exec zsh')}"
    subprocess.run(['tmux', 'new-window', '-d', '-t', session, '-n', window_name, runner_cmd], check=True)
    spawn_waiter(job_id, run_name, notify_qq=True)
    print(f'job={job_id}')
    print(f'session={session}')
    print(f'window={window_name}')
    print(f'cwd={cwd}')
    if args.recipient:
        print(f'recipient={args.recipient}')
    print(f'run={run_name}')
    print(f'state_dir={job_dir}')
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    job_dir = job_paths(args.job)
    if not job_dir.exists():
        raise SystemExit(f'job not found: {args.job}')
    thread_id = load_thread_id(job_dir)
    if not thread_id:
        raise SystemExit(f'job has no thread_id yet: {args.job}')
    prompt = args.prompt if args.prompt is not None else Path(args.prompt_file).read_text(encoding='utf-8')
    run_name = next_run_name(job_dir)
    run_dir = job_dir / 'runs' / run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    signal_name = signal_name_for_run(job_dir.name, run_name)
    write_text(job_dir / 'current_run.txt', run_name + '\n')
    if args.recipient:
        write_text(job_dir / 'recipient.txt', args.recipient + '\n')
    write_text(run_dir / 'prompt.txt', prompt.rstrip() + '\n')
    write_args_file(run_dir / 'args.txt', args.model, args.search, args.reasoning)
    write_text(run_dir / 'signal.txt', signal_name + '\n')
    session = read_text(job_dir / 'session.txt', DEFAULT_SESSION)
    window_name = make_window_name(job_dir.name, run_name)
    write_text(run_dir / 'window.txt', window_name + '\n')
    build_runner_script(
        run_dir / 'runner.sh',
        mode='resume',
        cwd=Path(read_text(job_dir / 'cwd.txt')).expanduser().resolve(),
        prompt_file=run_dir / 'prompt.txt',
        args_file=run_dir / 'args.txt',
        jsonl_file=run_dir / 'events.jsonl',
        last_file=run_dir / 'last.txt',
        status_file=run_dir / 'status.txt',
        exit_file=run_dir / 'exit_code.txt',
        started_file=run_dir / 'started_at.txt',
        finished_file=run_dir / 'finished_at.txt',
        thread_out_file=job_dir / 'thread_id.txt',
        resume_thread_id=thread_id,
        signal_name=signal_name,
    )
    ensure_tmux_session(session)
    runner_cmd = f"env PATH={shlex.quote(os.environ.get('PATH', ''))} HOME={shlex.quote(str(Path.home()))} SHELL={shlex.quote(os.environ.get('SHELL', '/bin/zsh'))} bash -lc {shlex.quote(str(run_dir / 'runner.sh') + '; exec zsh')}"
    subprocess.run(['tmux', 'new-window', '-d', '-t', session, '-n', window_name, runner_cmd], check=True)
    spawn_waiter(job_dir.name, run_name, notify_qq=True)
    print(f'job={job_dir.name}')
    print(f'session={session}')
    print(f'window={window_name}')
    print(f'thread={thread_id}')
    if args.recipient:
        print(f'recipient={args.recipient}')
    print(f'run={run_name}')
    print(f'state_dir={job_dir}')
    return 0


def cmd_wait(args: argparse.Namespace) -> int:
    ensure_dirs()
    job_dir = job_paths(args.job)
    if not job_dir.exists():
        raise SystemExit(f'job not found: {args.job}')
    run_dir = run_dir_for(job_dir, args.run)
    if not run_dir.exists():
        raise SystemExit(f'run not found: {run_dir.name}')
    signal_name = read_text(run_dir / 'signal.txt', signal_name_for_run(job_dir.name, run_dir.name))
    subprocess.run(['tmux', 'wait-for', signal_name], check=False)
    key, body = build_notice(job_dir, stall_minutes=args.stall_minutes, run_name=run_dir.name)
    if not key or not body:
        if not args.quiet_no_change:
            print('no background codex task changes')
        return 0
    status, _ = infer_status(job_dir, args.stall_minutes, run_dir.name)
    rendered = render_saki_notice(job_dir, run_dir, status, body)
    marker_file = run_dir / 'last_notice_key.txt'
    if read_text(marker_file) != key:
        write_text(marker_file, key + '\n')
        if args.notify_qq:
            recipient = read_text(job_dir / 'recipient.txt') or args.recipient or DEFAULT_QQ_RECIPIENT
            if recipient:
                notify_qq(recipient, rendered)
    if not args.quiet_no_change:
        print(rendered)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    ensure_dirs()
    if args.job:
        print_status(job_paths(args.job), stall_minutes=args.stall_minutes, verbose=True, run_name=args.run)
        return 0
    jobs = collect_jobs()
    if not jobs:
        print('no codex tmux jobs')
        return 0
    for job_dir in jobs:
        print_status(job_dir, stall_minutes=args.stall_minutes, verbose=args.verbose)
    return 0


def cmd_tail(args: argparse.Namespace) -> int:
    job_dir = job_paths(args.job)
    if not job_dir.exists():
        raise SystemExit(f'job not found: {args.job}')
    pane = tmux_capture(job_dir, args.lines, args.run)
    if pane:
        print(pane)
        return 0
    run_dir = run_dir_for(job_dir, args.run)
    jsonl_path = run_dir / 'events.jsonl'
    if jsonl_path.exists():
        lines = jsonl_path.read_text(encoding='utf-8', errors='replace').splitlines()[-args.lines:]
        print('\n'.join(lines))
        return 0
    print('no pane or events available')
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    ensure_dirs()
    rendered_notices: list[str] = []
    for job_dir in collect_jobs():
        key, body = build_notice(job_dir, stall_minutes=args.stall_minutes)
        if not key or not body:
            continue
        marker_file = current_run_dir(job_dir) / 'last_notice_key.txt'
        if read_text(marker_file) == key:
            continue
        write_text(marker_file, key + '\n')
        status, run_dir = infer_status(job_dir, args.stall_minutes)
        rendered_notices.append(render_saki_notice(job_dir, run_dir, status, body))
    if not rendered_notices:
        if not args.quiet_no_change:
            print('no background codex task changes')
        return 0
    payload = '\n\n'.join(rendered_notices)
    print(payload)
    if args.notify_qq:
        recipient = args.recipient or DEFAULT_QQ_RECIPIENT
        if recipient:
            notify_qq(recipient, payload)
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description='Manage background Codex jobs inside tmux for ZeroClaw.')
    sub = ap.add_subparsers(dest='cmd', required=True)

    start = sub.add_parser('start', help='Start a new background Codex job in tmux')
    start.add_argument('--name', help='Human-friendly job name')
    start.add_argument('--cwd', default='.', help='Working directory for Codex')
    prompt_group = start.add_mutually_exclusive_group(required=True)
    prompt_group.add_argument('--prompt', help='Inline prompt text')
    prompt_group.add_argument('--prompt-file', help='Read prompt text from file')
    start.add_argument('--model', help='Optional Codex model override')
    start.add_argument('--reasoning', choices=['low', 'medium', 'high', 'xhigh'], help='Optional model reasoning effort')
    start.add_argument('--search', action='store_true', help='Enable web search for this Codex job')
    start.add_argument('--session', help='tmux session name (default: saki-codex)')
    start.add_argument('--recipient', help='QQ recipient for completion push, e.g. user:<openid>')
    start.set_defaults(func=cmd_start)

    resume = sub.add_parser('resume', help='Resume an existing Codex thread in tmux')
    resume.add_argument('job', help='Job id from codex-tmux start/status')
    prompt_group = resume.add_mutually_exclusive_group(required=True)
    prompt_group.add_argument('--prompt', help='Inline follow-up prompt')
    prompt_group.add_argument('--prompt-file', help='Read follow-up prompt from file')
    resume.add_argument('--model', help='Optional model override for this follow-up')
    resume.add_argument('--reasoning', choices=['low', 'medium', 'high', 'xhigh'], help='Optional model reasoning effort for this follow-up')
    resume.add_argument('--search', action='store_true', help='Enable web search for this follow-up')
    resume.add_argument('--recipient', help='QQ recipient for completion push, e.g. user:<openid>')
    resume.set_defaults(func=cmd_resume)

    wait = sub.add_parser('wait', help='Block until a Codex tmux run finishes and then emit one completion/failure notice')
    wait.add_argument('job')
    wait.add_argument('--run', help='Specific run name like run-0001; defaults to current run')
    wait.add_argument('--stall-minutes', type=int, default=DEFAULT_STALL_MINUTES)
    wait.add_argument('--quiet-no-change', action='store_true', help='Emit nothing when there is no new change')
    wait.add_argument('--notify-qq', action='store_true', help='Push the notice to QQ as well')
    wait.add_argument('--recipient', help='Fallback QQ recipient like user:<openid> or group:<openid>')
    wait.set_defaults(func=cmd_wait)

    status = sub.add_parser('status', help='Show Codex tmux job status')
    status.add_argument('job', nargs='?', help='Specific job id')
    status.add_argument('--run', help='Specific run name like run-0001')
    status.add_argument('--stall-minutes', type=int, default=DEFAULT_STALL_MINUTES)
    status.add_argument('--verbose', action='store_true')
    status.set_defaults(func=cmd_status)

    tail = sub.add_parser('tail', help='Show the latest pane/events for a Codex tmux job')
    tail.add_argument('job')
    tail.add_argument('--run', help='Specific run name like run-0001')
    tail.add_argument('--lines', type=int, default=DEFAULT_TAIL_LINES)
    tail.set_defaults(func=cmd_tail)

    watch = sub.add_parser('watch', help='Manually sweep current runs for notable Codex job changes')
    watch.add_argument('--stall-minutes', type=int, default=DEFAULT_STALL_MINUTES)
    watch.add_argument('--quiet-no-change', action='store_true', help='Emit nothing when there is no new change')
    watch.add_argument('--notify-qq', action='store_true', help='Push the notice to QQ as well')
    watch.add_argument('--recipient', help='QQ recipient like user:<openid> or group:<openid>')
    watch.set_defaults(func=cmd_watch)

    return ap


def main() -> int:
    ensure_dirs()
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == '__main__':
    raise SystemExit(main())
