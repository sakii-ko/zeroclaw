#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path

STATE_ROOT = Path.home() / '.zeroclaw' / 'workspace' / 'state' / 'manim-remote'
JOBS_ROOT = STATE_ROOT / 'jobs'
LOCAL_OUTPUT_ROOT = Path.home() / 'downloads' / 'processed' / 'manim'
DEFAULT_REMOTE_HOST = 'duan78'
DEFAULT_REMOTE_BASE = '/home/lff/agent-work/manim-jobs'
DEFAULT_REMOTE_CODEX_BIN = '/home/lff/opt/node-v24.14.0-linux-x64/bin/codex'
DEFAULT_REMOTE_NODE_BIN = '/home/lff/opt/node-v24.14.0-linux-x64/bin'
DEFAULT_REMOTE_MANIM_BIN = '/home/lff/miniconda3/envs/manim/bin/manim'
DEFAULT_REMOTE_PYTHON = '/home/lff/miniconda3/envs/manim/bin/python'
DEFAULT_SESSION = 'saki-manim'
DEFAULT_STALL_MINUTES = 25
DEFAULT_FINAL_QUALITY = 'm'
DEFAULT_QQ_RECIPIENT = os.environ.get('ZEROCLAW_QQ_RECIPIENT', '').strip()
REMOTE_SKILL_PATH = '/home/lff/.codex/skills/manim-render/SKILL.md'
LOCAL_REMOTE_SKILL_TEMPLATE = Path(__file__).resolve().parents[1] / 'templates' / 'remote_manim_skill.md'


def run(cmd: list[str], *, check: bool = True, capture: bool = True, text: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, capture_output=capture, text=text)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')


def ensure_dirs() -> None:
    JOBS_ROOT.mkdir(parents=True, exist_ok=True)
    LOCAL_OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')


def read_text(path: Path, default: str = '') -> str:
    try:
        return path.read_text(encoding='utf-8').strip()
    except FileNotFoundError:
        return default


def sanitize_slug(value: str) -> str:
    value = value.strip().lower()
    cleaned = ''.join(ch if ch.isalnum() or ch in ('-', '_') else '-' for ch in value)
    cleaned = cleaned.strip('-_')
    while '--' in cleaned:
        cleaned = cleaned.replace('--', '-')
    return cleaned or 'job'


def ssh_cmd(host: str, command: str, *, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ['ssh', host, f"bash -lc {shlex.quote(command)}"],
        check=check,
        capture_output=capture,
        text=True,
    )


def scp_to(host: str, local_path: Path, remote_path: str) -> None:
    subprocess.run(['scp', '-r', str(local_path), f'{host}:{remote_path}'], check=True)


def scp_from(host: str, remote_path: str, local_path: Path) -> None:
    local_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(['scp', f'{host}:{remote_path}', str(local_path)], check=True)


def job_dir(job_id: str) -> Path:
    return JOBS_ROOT / job_id


def signal_name_for_job(job_id: str) -> str:
    return f'manim-remote-{sanitize_slug(job_id)}-done'


def spawn_waiter(job_id: str, *, notify_qq: bool = True) -> None:
    cmd = [sys.executable, str(Path(__file__).resolve()), 'wait', job_id]
    if notify_qq:
        cmd.append('--notify-qq')
    subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def remote_state_dir(remote_job_dir: str) -> str:
    return f'{remote_job_dir}/state'


def remote_exports_dir(remote_job_dir: str) -> str:
    return f'{remote_job_dir}/exports'


def ensure_remote_skill(host: str) -> None:
    remote_dir = shlex.quote(str(Path(REMOTE_SKILL_PATH).parent))
    ssh_cmd(host, f'mkdir -p {remote_dir}')
    scp_to(host, LOCAL_REMOTE_SKILL_TEMPLATE, REMOTE_SKILL_PATH)


def remote_doctor(host: str, remote_base: str) -> str:
    py_code = shlex.quote(
        "import manim, sys; "
        "print('manim_python=' + sys.executable); "
        "print('manim_module=' + manim.__file__); "
        "print('manim_version=' + getattr(manim, '__version__', 'unknown'))"
    )
    cmd = textwrap.dedent(
        f'''\
        set -euo pipefail
        export PATH={shlex.quote(DEFAULT_REMOTE_NODE_BIN)}:/home/lff/miniconda3/envs/manim/bin:/usr/local/bin:/usr/bin:/bin
        mkdir -p {shlex.quote(remote_base)}
        printf 'host=%s\n' "$(hostname)"
        printf 'user=%s\n' "$(whoami)"
        printf 'codex=%s\n' {shlex.quote(DEFAULT_REMOTE_CODEX_BIN)}
        test -x {shlex.quote(DEFAULT_REMOTE_CODEX_BIN)}
        codex --version
        test -x {shlex.quote(DEFAULT_REMOTE_MANIM_BIN)}
        {shlex.quote(DEFAULT_REMOTE_MANIM_BIN)} --version
        test -x {shlex.quote(DEFAULT_REMOTE_PYTHON)}
        {shlex.quote(DEFAULT_REMOTE_PYTHON)} -c {py_code}
        printf 'ffmpeg=%s\n' "$(command -v ffmpeg || true)"
        printf 'pdflatex=%s\n' "$(command -v pdflatex || true)"
        printf 'skill=%s\n' {shlex.quote(REMOTE_SKILL_PATH)}
        test -f {shlex.quote(REMOTE_SKILL_PATH)}
        '''
    )
    proc = ssh_cmd(host, cmd)
    return proc.stdout.strip()


def render_helper_script(remote_job_dir: str) -> str:
    manim_bin = DEFAULT_REMOTE_MANIM_BIN
    return textwrap.dedent(
        f'''\
        #!/usr/bin/env bash
        set -euo pipefail
        SCENE_FILE=${{1:?scene file required}}
        SCENE_NAME=${{2:?scene name required}}
        QUALITY=${{3:-{DEFAULT_FINAL_QUALITY}}}
        OUT_PATH=${{4:-exports/final.mp4}}
        JOB_DIR={shlex.quote(remote_job_dir)}
        MANIM_BIN={shlex.quote(manim_bin)}
        export PATH={shlex.quote(DEFAULT_REMOTE_NODE_BIN)}:/home/lff/miniconda3/envs/manim/bin:/usr/local/bin:/usr/bin:/bin
        cd "$JOB_DIR"
        mkdir -p "$(dirname "$OUT_PATH")" "$JOB_DIR/media"
        "$MANIM_BIN" -q "$QUALITY" --disable_caching --media_dir "$JOB_DIR/media" "$SCENE_FILE" "$SCENE_NAME"
        RENDERED=$(find "$JOB_DIR/media" -type f -name '*.mp4' -printf '%T@ %p\n' | sort -nr | awk 'NR==1 {{print $2}}')
        if [[ -z "$RENDERED" || ! -f "$RENDERED" ]]; then
          echo 'rendered mp4 not found' >&2
          exit 20
        fi
        cp "$RENDERED" "$OUT_PATH"
        printf '%s\n' "$RENDERED" > exports/render_source.txt
        printf '%s\n' "$OUT_PATH" > exports/render_target.txt
        '''
    ).lstrip()


def prompt_template(brief: str, final_quality: str) -> str:
    return textwrap.dedent(
        f'''\
        Use the `manim-render` skill if it is available.

        Goal:
        Create one clean Manim Community math animation from the brief in `brief.md`.

        Brief:
        {brief.strip()}

        Hard requirements:
        1. Work only inside the current job directory.
        2. Read `brief.md` and the provided `scripts/render_manim.sh` helper before coding.
        3. Create or update `scene.py` with one main scene class.
        4. Use `scripts/render_manim.sh scene.py <SceneName> {final_quality} exports/final.mp4` for the final render.
        5. If the first render fails, debug and rerun until it succeeds.
        6. Write these exact output files:
           - `exports/final.mp4`
           - `exports/summary.md`
           - `exports/result.json`
        7. `exports/result.json` must be valid JSON and include at least:
           - `status` = `completed`
           - `scene`
           - `quality`
           - `video` = `exports/final.mp4`
           - `summary`
        8. Keep the animation self-contained. Prefer native Manim objects, `MathTex`, `Tex`, `Axes`, `NumberPlane`, lines, polygons, and simple color highlights.
        9. Do not install packages unless absolutely necessary.
        10. When done, stop.

        Quality guidance:
        - Use quality `{final_quality}` for the final output.
        - Keep runtime moderate unless the brief explicitly asks for a long animation.
        - Prefer clarity over flashy effects.
        '''
    ).strip() + '\n'


def build_runner_script(remote_job_dir: str, signal_name: str) -> str:
    state_dir = remote_state_dir(remote_job_dir)
    return textwrap.dedent(
        f'''\
        #!/usr/bin/env bash
        set -euo pipefail
        export PATH={shlex.quote(DEFAULT_REMOTE_NODE_BIN)}:/home/lff/miniconda3/envs/manim/bin:/usr/local/bin:/usr/bin:/bin
        export HOME=/home/lff
        export SHELL=/bin/zsh
        export TERM=xterm-256color
        JOB_DIR={shlex.quote(remote_job_dir)}
        STATE_DIR={shlex.quote(state_dir)}
        TMUX_SIGNAL={shlex.quote(signal_name)}
        cd "$JOB_DIR"
        mkdir -p "$STATE_DIR" exports scripts
        echo running > "$STATE_DIR/status.txt"
        date -Iseconds > "$STATE_DIR/started_at.txt"
        : > "$STATE_DIR/events.jsonl"
        : > "$STATE_DIR/last.txt"
        {shlex.quote(DEFAULT_REMOTE_CODEX_BIN)} exec --json --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox -C "$JOB_DIR" - -o "$STATE_DIR/last.txt" < "$JOB_DIR/prompt.txt" | tee "$STATE_DIR/events.jsonl"
        rc=${{PIPESTATUS[0]}}
        python3 - "$STATE_DIR/events.jsonl" "$STATE_DIR/thread_id.txt" <<'PYEOF'
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
        if [[ "$rc" == "0" ]]; then
          if [[ ! -s "$JOB_DIR/exports/final.mp4" ]]; then
            echo 'missing exports/final.mp4' > "$STATE_DIR/validation_error.txt"
            rc=91
          elif [[ ! -s "$JOB_DIR/exports/result.json" ]]; then
            echo 'missing exports/result.json' > "$STATE_DIR/validation_error.txt"
            rc=92
          elif [[ ! -s "$JOB_DIR/exports/summary.md" ]]; then
            echo 'missing exports/summary.md' > "$STATE_DIR/validation_error.txt"
            rc=93
          fi
        fi
        echo "$rc" > "$STATE_DIR/exit_code.txt"
        if [[ "$rc" == "0" ]]; then
          echo completed > "$STATE_DIR/status.txt"
        else
          echo failed > "$STATE_DIR/status.txt"
        fi
        date -Iseconds > "$STATE_DIR/finished_at.txt"
        tmux wait-for -S "$TMUX_SIGNAL" || true
        printf '\n[exit:%s]\n' "$rc"
        exit "$rc"
        '''
    ).lstrip()


def ensure_remote_tmux_session(host: str, session: str) -> None:
    cmd = textwrap.dedent(
        f'''\
        if tmux has-session -t {shlex.quote(session)} 2>/dev/null; then
          exit 0
        fi
        env PATH={shlex.quote(DEFAULT_REMOTE_NODE_BIN + ':/home/lff/miniconda3/envs/manim/bin:/usr/local/bin:/usr/bin:/bin')} HOME=/home/lff SHELL=/bin/zsh tmux new-session -d -s {shlex.quote(session)} -n scratch 'zsh -lc "exec zsh"'
        '''
    )
    ssh_cmd(host, cmd)


def latest_jobs() -> list[Path]:
    if not JOBS_ROOT.exists():
        return []
    return sorted(path for path in JOBS_ROOT.iterdir() if path.is_dir())


def local_output_dir(job_dir_path: Path) -> Path:
    return LOCAL_OUTPUT_ROOT / job_dir_path.name


def remote_file_text(host: str, remote_path: str, default: str = '') -> str:
    proc = ssh_cmd(host, f'if [ -f {shlex.quote(remote_path)} ]; then cat {shlex.quote(remote_path)}; fi', check=True)
    text = proc.stdout.strip()
    return text if text else default


def remote_latest_activity(host: str, remote_job_dir: str) -> float:
    cmd = textwrap.dedent(
        f'''\
        python3 - <<'PY'
from pathlib import Path
paths = [
    Path({remote_state_dir(remote_job_dir)!r}) / 'events.jsonl',
    Path({remote_state_dir(remote_job_dir)!r}) / 'last.txt',
    Path({remote_state_dir(remote_job_dir)!r}) / 'started_at.txt',
]
mtimes = [p.stat().st_mtime for p in paths if p.exists()]
print(max(mtimes) if mtimes else 0)
PY
        '''
    )
    proc = ssh_cmd(host, cmd)
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return 0.0


def infer_status(job_dir_path: Path, stall_minutes: int) -> tuple[str, str, str]:
    host = read_text(job_dir_path / 'remote_host.txt', DEFAULT_REMOTE_HOST)
    remote_job_dir = read_text(job_dir_path / 'remote_job_dir.txt')
    state_dir = remote_state_dir(remote_job_dir)
    raw = remote_file_text(host, f'{state_dir}/status.txt', 'unknown')
    if raw == 'running':
        last_mtime = remote_latest_activity(host, remote_job_dir)
        if last_mtime and (time.time() - last_mtime) > stall_minutes * 60:
            return 'stale', host, remote_job_dir
    return raw, host, remote_job_dir


def summarize_text(text: str, limit: int = 240) -> str:
    collapsed = ' '.join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + '…'


def print_status(job_dir_path: Path, *, stall_minutes: int) -> None:
    status, host, remote_job_dir = infer_status(job_dir_path, stall_minutes)
    thread_id = remote_file_text(host, f'{remote_state_dir(remote_job_dir)}/thread_id.txt')
    last_text = summarize_text(remote_file_text(host, f'{remote_state_dir(remote_job_dir)}/last.txt'))
    print(f'job: {job_dir_path.name}')
    print(f'status: {status}')
    print(f'remote_host: {host}')
    print(f'remote_dir: {remote_job_dir}')
    print(f'session: {read_text(job_dir_path / "session.txt", DEFAULT_SESSION)}')
    print(f'window: {read_text(job_dir_path / "window.txt")}')
    recipient = read_text(job_dir_path / 'recipient.txt')
    if recipient:
        print(f'recipient: {recipient}')
    if thread_id:
        print(f'thread: {thread_id}')
    if last_text:
        print(f'last: {last_text}')
    local_video = read_text(job_dir_path / 'local_video.txt')
    if local_video:
        print(f'local_video: {local_video}')
    print()


def fetch_outputs(job_dir_path: Path) -> tuple[Path, Path, Path]:
    host = read_text(job_dir_path / 'remote_host.txt', DEFAULT_REMOTE_HOST)
    remote_job_dir = read_text(job_dir_path / 'remote_job_dir.txt')
    output_dir = local_output_dir(job_dir_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    video_path = output_dir / 'final.mp4'
    summary_path = output_dir / 'summary.md'
    result_path = output_dir / 'result.json'
    scene_path = output_dir / 'scene.py'
    scp_from(host, f'{remote_job_dir}/exports/final.mp4', video_path)
    scp_from(host, f'{remote_job_dir}/exports/summary.md', summary_path)
    scp_from(host, f'{remote_job_dir}/exports/result.json', result_path)
    try:
        scp_from(host, f'{remote_job_dir}/scene.py', scene_path)
    except subprocess.CalledProcessError:
        pass
    write_text(job_dir_path / 'local_video.txt', str(video_path) + '\n')
    write_text(job_dir_path / 'local_summary.txt', str(summary_path) + '\n')
    write_text(job_dir_path / 'local_result.txt', str(result_path) + '\n')
    write_text(job_dir_path / 'fetched_at.txt', now_iso() + '\n')
    return video_path, summary_path, result_path


def build_notice(job_dir_path: Path, *, stall_minutes: int) -> tuple[str, str, Path | None]:
    status, host, remote_job_dir = infer_status(job_dir_path, stall_minutes)
    if status not in {'completed', 'failed', 'stale'}:
        return '', '', None
    if status == 'stale':
        key = f'{job_dir_path.name}|stale'
        body = textwrap.dedent(
            f'''\
            Manim 远端任务似乎卡住了。
            - job: {job_dir_path.name}
            - remote: {host}:{remote_job_dir}
            - 建议: 现在检查一次状态或 tail，并给更具体的 follow-up。
            '''
        ).strip()
        return key, body, None
    if status == 'failed':
        last_text = summarize_text(remote_file_text(host, f'{remote_state_dir(remote_job_dir)}/last.txt') or '(没有捕获到末条回复)', 360)
        key = f'{job_dir_path.name}|failed|{last_text}'
        body = textwrap.dedent(
            f'''\
            Manim 远端任务失败了。
            - job: {job_dir_path.name}
            - remote: {host}:{remote_job_dir}
            - 摘要: {last_text}
            '''
        ).strip()
        return key, body, None
    existing_video = Path(read_text(job_dir_path / 'local_video.txt')) if read_text(job_dir_path / 'local_video.txt') else None
    existing_summary = Path(read_text(job_dir_path / 'local_summary.txt')) if read_text(job_dir_path / 'local_summary.txt') else None
    existing_result = Path(read_text(job_dir_path / 'local_result.txt')) if read_text(job_dir_path / 'local_result.txt') else None
    if existing_video and existing_summary and existing_result and existing_video.exists() and existing_summary.exists() and existing_result.exists():
        video_path, summary_path, result_path = existing_video, existing_summary, existing_result
    else:
        video_path, summary_path, result_path = fetch_outputs(job_dir_path)
    summary = ''
    try:
        data = json.loads(result_path.read_text(encoding='utf-8'))
        summary = str(data.get('summary') or '').strip()
    except Exception:
        summary = ''
    if not summary:
        summary = summarize_text(summary_path.read_text(encoding='utf-8', errors='replace'), 360)
    key = f'{job_dir_path.name}|completed|{video_path}|{summary}'
    body = textwrap.dedent(
        f'''\
        Manim 动画已完成。
        - job: {job_dir_path.name}
        - video: {video_path}
        - summary: {summary}
        '''
    ).strip()
    return key, body, video_path


def notify_qq(recipient: str, text: str, video_path: Path | None) -> None:
    qq_notify = Path.home() / 'utils' / 'zeroclaw' / 'bin' / 'qq-notify'
    if text.strip():
        subprocess.run([str(qq_notify), '--recipient', recipient, '--text', text], check=True)
    if video_path is not None and video_path.exists():
        subprocess.run([str(qq_notify), '--recipient', recipient, '--video', str(video_path)], check=True)


def cmd_bootstrap(args: argparse.Namespace) -> int:
    ensure_dirs()
    ensure_remote_skill(args.host)
    print(remote_doctor(args.host, args.remote_base))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    ensure_dirs()
    print(remote_doctor(args.host, args.remote_base))
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    ensure_dirs()
    ensure_remote_skill(args.host)
    ensure_remote_tmux_session(args.host, args.session)
    brief = args.prompt if args.prompt is not None else Path(args.prompt_file).read_text(encoding='utf-8')
    slug = sanitize_slug(args.name or 'manim')
    job_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{slug}"
    local_job_dir = job_dir(job_id)
    remote_job_dir = f"{args.remote_base.rstrip('/')}/{job_id}"
    signal_name = signal_name_for_job(job_id)
    window_name = (f'{slug[:18]}-{job_id[-6:]}')[:28] or 'manim-job'
    write_text(local_job_dir / 'created_at.txt', now_iso() + '\n')
    write_text(local_job_dir / 'remote_host.txt', args.host + '\n')
    write_text(local_job_dir / 'remote_job_dir.txt', remote_job_dir + '\n')
    write_text(local_job_dir / 'session.txt', args.session + '\n')
    write_text(local_job_dir / 'window.txt', window_name + '\n')
    write_text(local_job_dir / 'signal.txt', signal_name + '\n')
    write_text(local_job_dir / 'brief.md', brief.strip() + '\n')
    write_text(local_job_dir / 'final_quality.txt', args.final_quality + '\n')
    recipient = args.recipient or ''
    if recipient:
        write_text(local_job_dir / 'recipient.txt', recipient + '\n')

    bundle_dir = local_job_dir / 'bundle'
    write_text(bundle_dir / 'brief.md', brief.strip() + '\n')
    write_text(bundle_dir / 'prompt.txt', prompt_template(brief, args.final_quality))
    write_text(bundle_dir / 'scripts' / 'render_manim.sh', render_helper_script(remote_job_dir))
    (bundle_dir / 'scripts' / 'render_manim.sh').chmod(0o755)
    write_text(bundle_dir / 'state' / 'runner.sh', build_runner_script(remote_job_dir, signal_name))
    (bundle_dir / 'state' / 'runner.sh').chmod(0o755)

    ssh_cmd(args.host, f'mkdir -p {shlex.quote(remote_job_dir)} && rm -rf {shlex.quote(remote_job_dir)}/*')
    scp_to(args.host, bundle_dir, remote_job_dir)
    ssh_cmd(args.host, f'cp -a {shlex.quote(remote_job_dir)}/bundle/. {shlex.quote(remote_job_dir)}/ && rm -rf {shlex.quote(remote_job_dir)}/bundle && chmod +x {shlex.quote(remote_job_dir)}/scripts/render_manim.sh {shlex.quote(remote_job_dir)}/state/runner.sh')
    runner_cmd = f'env PATH={shlex.quote(DEFAULT_REMOTE_NODE_BIN + ":/home/lff/miniconda3/envs/manim/bin:/usr/local/bin:/usr/bin:/bin")} HOME=/home/lff SHELL=/bin/zsh bash -lc {shlex.quote(remote_job_dir + "/state/runner.sh; exec zsh")}'
    ssh_cmd(args.host, f'tmux new-window -d -t {shlex.quote(args.session)} -n {shlex.quote(window_name)} {shlex.quote(runner_cmd)}')
    spawn_waiter(job_id, notify_qq=True)
    print(f'job={job_id}')
    print(f'remote_host={args.host}')
    print(f'remote_dir={remote_job_dir}')
    print(f'session={args.session}')
    print(f'window={window_name}')
    print(f'quality={args.final_quality}')
    if recipient:
        print(f'recipient={recipient}')
    print(f'state_dir={local_job_dir}')
    return 0


def cmd_wait(args: argparse.Namespace) -> int:
    ensure_dirs()
    local_job_dir = job_dir(args.job)
    if not local_job_dir.exists():
        raise SystemExit(f'job not found: {args.job}')
    host = read_text(local_job_dir / 'remote_host.txt', DEFAULT_REMOTE_HOST)
    signal_name = read_text(local_job_dir / 'signal.txt', signal_name_for_job(args.job))
    ssh_cmd(host, f'tmux wait-for {shlex.quote(signal_name)}', check=False)
    key, body, video_path = build_notice(local_job_dir, stall_minutes=args.stall_minutes)
    if not key or not body:
        if not args.quiet_no_change:
            print('no manim remote task changes')
        return 0
    marker_file = local_job_dir / 'last_notice_key.txt'
    if read_text(marker_file) != key:
        write_text(marker_file, key + '\n')
        if args.notify_qq:
            recipient = read_text(local_job_dir / 'recipient.txt') or args.recipient or DEFAULT_QQ_RECIPIENT
            if recipient:
                notify_qq(recipient, body, video_path)
    if not args.quiet_no_change:
        print(body)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    ensure_dirs()
    if args.job:
        print_status(job_dir(args.job), stall_minutes=args.stall_minutes)
        return 0
    jobs = latest_jobs()
    if not jobs:
        print('no manim remote jobs')
        return 0
    for path in jobs:
        print_status(path, stall_minutes=args.stall_minutes)
    return 0


def cmd_tail(args: argparse.Namespace) -> int:
    local_job_dir = job_dir(args.job)
    if not local_job_dir.exists():
        raise SystemExit(f'job not found: {args.job}')
    host = read_text(local_job_dir / 'remote_host.txt', DEFAULT_REMOTE_HOST)
    remote_job_dir = read_text(local_job_dir / 'remote_job_dir.txt')
    session = read_text(local_job_dir / 'session.txt', DEFAULT_SESSION)
    window = read_text(local_job_dir / 'window.txt')
    pane = ssh_cmd(host, f'tmux capture-pane -p -t {shlex.quote(session + ":" + window)} -S -{args.lines}', check=False)
    if pane.returncode == 0 and pane.stdout.strip():
        print(pane.stdout.rstrip())
        return 0
    fallback = ssh_cmd(host, f'tail -n {int(args.lines)} {shlex.quote(remote_state_dir(remote_job_dir) + "/events.jsonl")}', check=False)
    if fallback.stdout.strip():
        print(fallback.stdout.rstrip())
        return 0
    print('no pane or events available')
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    ensure_dirs()
    notices: list[str] = []
    video_to_send: dict[str, Path] = {}
    for local_job_dir in latest_jobs():
        key, body, video_path = build_notice(local_job_dir, stall_minutes=args.stall_minutes)
        if not key or not body:
            continue
        marker_file = local_job_dir / 'last_notice_key.txt'
        if read_text(marker_file) == key:
            continue
        write_text(marker_file, key + '\n')
        notices.append(body)
        if video_path is not None:
            video_to_send[local_job_dir.name] = video_path
        if args.notify_qq:
            recipient = read_text(local_job_dir / 'recipient.txt') or args.recipient or DEFAULT_QQ_RECIPIENT
            if recipient:
                notify_qq(recipient, body, video_path)
    if not notices:
        if not args.quiet_no_change:
            print('no manim remote task changes')
        return 0
    print('\n\n'.join(notices))
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description='Manage remote Manim render jobs via Codex on a remote host.')
    sub = ap.add_subparsers(dest='cmd', required=True)

    bootstrap = sub.add_parser('bootstrap', help='Ensure remote skill and print environment status')
    bootstrap.add_argument('--host', default=DEFAULT_REMOTE_HOST)
    bootstrap.add_argument('--remote-base', default=DEFAULT_REMOTE_BASE)
    bootstrap.set_defaults(func=cmd_bootstrap)

    doctor = sub.add_parser('doctor', help='Check remote Manim/Codex environment')
    doctor.add_argument('--host', default=DEFAULT_REMOTE_HOST)
    doctor.add_argument('--remote-base', default=DEFAULT_REMOTE_BASE)
    doctor.set_defaults(func=cmd_doctor)

    start = sub.add_parser('start', help='Start a new remote Manim render job in tmux')
    start.add_argument('--name', help='Human-friendly job name')
    prompt_group = start.add_mutually_exclusive_group(required=True)
    prompt_group.add_argument('--prompt', help='Inline creative brief')
    prompt_group.add_argument('--prompt-file', help='Read brief from file')
    start.add_argument('--host', default=DEFAULT_REMOTE_HOST)
    start.add_argument('--remote-base', default=DEFAULT_REMOTE_BASE)
    start.add_argument('--session', default=DEFAULT_SESSION)
    start.add_argument('--final-quality', default=DEFAULT_FINAL_QUALITY, choices=['l', 'm', 'h', 'p', 'k'])
    start.add_argument('--recipient', help='QQ recipient for completion push, e.g. user:<openid>')
    start.set_defaults(func=cmd_start)

    wait = sub.add_parser('wait', help='Block until a remote Manim job finishes and then emit one completion/failure notice')
    wait.add_argument('job')
    wait.add_argument('--stall-minutes', type=int, default=DEFAULT_STALL_MINUTES)
    wait.add_argument('--quiet-no-change', action='store_true')
    wait.add_argument('--notify-qq', action='store_true')
    wait.add_argument('--recipient', help='Fallback QQ recipient if the job did not store one')
    wait.set_defaults(func=cmd_wait)

    status = sub.add_parser('status', help='Show remote Manim job status')
    status.add_argument('job', nargs='?')
    status.add_argument('--stall-minutes', type=int, default=DEFAULT_STALL_MINUTES)
    status.set_defaults(func=cmd_status)

    tail = sub.add_parser('tail', help='Show latest tmux pane or events for a remote Manim job')
    tail.add_argument('job')
    tail.add_argument('--lines', type=int, default=120)
    tail.set_defaults(func=cmd_tail)

    watch = sub.add_parser('watch', help='Report only notable remote Manim job changes')
    watch.add_argument('--stall-minutes', type=int, default=DEFAULT_STALL_MINUTES)
    watch.add_argument('--quiet-no-change', action='store_true')
    watch.add_argument('--notify-qq', action='store_true')
    watch.add_argument('--recipient', help='Fallback QQ recipient if the job did not store one')
    watch.set_defaults(func=cmd_watch)

    return ap


def main() -> int:
    ensure_dirs()
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == '__main__':
    raise SystemExit(main())
