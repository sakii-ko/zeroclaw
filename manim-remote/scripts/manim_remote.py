#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
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
PRIVATE_REMOTE_CONFIG_PATH = Path.home() / '.zeroclaw' / 'manim-remote.json'


def load_private_remote_config() -> dict[str, str]:
    data: dict[str, str] = {}
    try:
        loaded = json.loads(PRIVATE_REMOTE_CONFIG_PATH.read_text(encoding='utf-8'))
    except FileNotFoundError:
        loaded = {}
    except json.JSONDecodeError as exc:
        raise SystemExit(f'invalid private remote config: {PRIVATE_REMOTE_CONFIG_PATH}: {exc}')
    if isinstance(loaded, dict):
        for key, value in loaded.items():
            if value is None:
                continue
            data[str(key)] = str(value).strip()
    env_map = {
        'host': 'ZEROCLAW_MANIM_REMOTE_HOST',
        'home': 'ZEROCLAW_MANIM_REMOTE_HOME',
        'base': 'ZEROCLAW_MANIM_REMOTE_BASE',
        'node_bin': 'ZEROCLAW_MANIM_REMOTE_NODE_BIN',
        'manim_bin': 'ZEROCLAW_MANIM_REMOTE_MANIM_BIN',
        'python_bin': 'ZEROCLAW_MANIM_REMOTE_PYTHON_BIN',
        'shell': 'ZEROCLAW_MANIM_REMOTE_SHELL',
    }
    for key, env_name in env_map.items():
        raw = os.environ.get(env_name, '').strip()
        if raw:
            data[key] = raw
    return data


def remote_setting(key: str, default: str) -> str:
    value = PRIVATE_REMOTE_CONFIG.get(key, '').strip()
    return value or default


def remote_shell_name() -> str:
    return Path(DEFAULT_REMOTE_SHELL).name or 'sh'


def remote_path_env() -> str:
    entries = [
        DEFAULT_REMOTE_NODE_BIN,
        str(Path(DEFAULT_REMOTE_MANIM_BIN).parent),
        '/usr/local/bin',
        '/usr/bin',
        '/bin',
    ]
    unique: list[str] = []
    for entry in entries:
        entry = entry.strip()
        if entry and entry not in unique:
            unique.append(entry)
    return ':'.join(unique)


PRIVATE_REMOTE_CONFIG = load_private_remote_config()
DEFAULT_REMOTE_HOST = remote_setting('host', 'remote-manim-host')
DEFAULT_REMOTE_HOME = remote_setting('home', '/home/remoteuser')
DEFAULT_REMOTE_BASE = remote_setting('base', f'{DEFAULT_REMOTE_HOME}/agent-work/manim-jobs')
DEFAULT_REMOTE_NODE_BIN = remote_setting('node_bin', f'{DEFAULT_REMOTE_HOME}/.local/bin')
DEFAULT_REMOTE_MANIM_BIN = remote_setting('manim_bin', f'{DEFAULT_REMOTE_HOME}/miniconda3/envs/manim/bin/manim')
DEFAULT_REMOTE_PYTHON = remote_setting('python_bin', f'{DEFAULT_REMOTE_HOME}/miniconda3/envs/manim/bin/python')
DEFAULT_REMOTE_SHELL = remote_setting('shell', '/bin/zsh')
DEFAULT_SESSION = 'saki-manim'
DEFAULT_STALL_MINUTES = 25
DEFAULT_FINAL_QUALITY = 'm'
DEFAULT_QQ_RECIPIENT = os.environ.get('ZEROCLAW_QQ_RECIPIENT', '').strip()
_raw_local_codex_bin = os.environ.get('ZEROCLAW_LOCAL_CODEX_BIN', 'codex').strip() or 'codex'
DEFAULT_LOCAL_CODEX_BIN = shutil.which(_raw_local_codex_bin) or _raw_local_codex_bin


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


def local_state_dir(job_dir_path: Path) -> Path:
    return job_dir_path / 'state'


def local_output_dir(job_dir_path: Path) -> Path:
    return LOCAL_OUTPUT_ROOT / job_dir_path.name


def local_state_text(job_dir_path: Path, name: str, default: str = '') -> str:
    return read_text(local_state_dir(job_dir_path) / name, default)


def local_latest_activity(job_dir_path: Path) -> float:
    state_dir = local_state_dir(job_dir_path)
    paths = [
        state_dir / 'events.jsonl',
        state_dir / 'last.txt',
        state_dir / 'remote_render.log',
        state_dir / 'started_at.txt',
    ]
    mtimes = [p.stat().st_mtime for p in paths if p.exists()]
    return max(mtimes) if mtimes else 0.0


def tail_file(path: Path, lines: int) -> str:
    try:
        content = path.read_text(encoding='utf-8', errors='replace').splitlines()
    except FileNotFoundError:
        return ''
    if not content:
        return ''
    return '\n'.join(content[-lines:])


def summarize_text(text: str, limit: int = 240) -> str:
    collapsed = ' '.join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + '…'


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


def local_codex_doctor() -> str:
    proc = subprocess.run(
        [DEFAULT_LOCAL_CODEX_BIN, '--version'],
        check=False,
        capture_output=True,
        text=True,
    )
    detail = (proc.stdout or proc.stderr).strip()
    if proc.returncode != 0:
        raise SystemExit(f'local codex unavailable: {detail or proc.returncode}')
    lines = [f'local_codex={DEFAULT_LOCAL_CODEX_BIN}']
    if detail:
        lines.append(detail)
    return '\n'.join(lines)


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
        export PATH={shlex.quote(remote_path_env())}
        export HOME={shlex.quote(DEFAULT_REMOTE_HOME)}
        export SHELL={shlex.quote(DEFAULT_REMOTE_SHELL)}
        mkdir -p {shlex.quote(remote_base)}
        printf 'host=%s\n' "$(hostname)"
        printf 'user=%s\n' "$(whoami)"
        printf 'remote_base=%s\n' {shlex.quote(remote_base)}
        printf 'manim=%s\n' {shlex.quote(DEFAULT_REMOTE_MANIM_BIN)}
        test -x {shlex.quote(DEFAULT_REMOTE_MANIM_BIN)}
        {shlex.quote(DEFAULT_REMOTE_MANIM_BIN)} --version
        test -x {shlex.quote(DEFAULT_REMOTE_PYTHON)}
        {shlex.quote(DEFAULT_REMOTE_PYTHON)} -c {py_code}
        printf 'ffmpeg=%s\n' "$(command -v ffmpeg || true)"
        printf 'pdflatex=%s\n' "$(command -v pdflatex || true)"
        '''
    )
    proc = ssh_cmd(host, cmd)
    return proc.stdout.strip()


def render_helper_script(remote_job_dir: str) -> str:
    lines = [
        '#!/usr/bin/env bash',
        'set -euo pipefail',
        'SCENE_FILE=${1:?scene file required}',
        'SCENE_NAME=${2:?scene name required}',
        f'QUALITY=${{3:-{DEFAULT_FINAL_QUALITY}}}',
        'OUT_PATH=${4:-exports/final.mp4}',
        f'JOB_DIR={shlex.quote(remote_job_dir)}',
        f'MANIM_BIN={shlex.quote(DEFAULT_REMOTE_MANIM_BIN)}',
        f'export PATH={shlex.quote(remote_path_env())}',
        f'export HOME={shlex.quote(DEFAULT_REMOTE_HOME)}',
        f'export SHELL={shlex.quote(DEFAULT_REMOTE_SHELL)}',
        'cd "$JOB_DIR"',
        'mkdir -p "$(dirname "$OUT_PATH")" "$JOB_DIR/media"',
        '"$MANIM_BIN" -q "$QUALITY" --disable_caching --media_dir "$JOB_DIR/media" "$SCENE_FILE" "$SCENE_NAME"',
        "RENDERED=$(find \"$JOB_DIR/media\" -type f -name '*.mp4' -printf '%T@ %p\\n' | sort -nr | awk 'NR==1 {print $2}')",
        'if [[ -z "$RENDERED" || ! -f "$RENDERED" ]]; then',
        "  echo 'rendered mp4 not found' >&2",
        '  exit 20',
        'fi',
        'cp "$RENDERED" "$OUT_PATH"',
        'echo "$RENDERED" > exports/render_source.txt',
        'echo "$OUT_PATH" > exports/render_target.txt',
    ]
    return '\n'.join(lines) + '\n'


def prompt_template(brief: str, final_quality: str) -> str:
    return textwrap.dedent(
        f'''\
        Use the `manim-render` skill if it is available.

        Goal:
        Create one clean Manim Community math animation from the brief in `brief.md`.

        Context:
        - You are authoring this job locally.
        - After you finish authoring, this job directory will be synced to a remote render host.
        - The final full render will happen remotely, not on the local machine.

        Brief:
        {brief.strip()}

        Hard requirements:
        1. Work only inside the current job directory.
        2. Read `brief.md` and the provided `scripts/render_manim.sh` helper before coding.
        3. Create or update `scene.py` with one main scene class.
        4. Write the final scene class name into `state/scene_name.txt` using only the class name.
        5. Prepare these files before stopping:
           - `scene.py`
           - `state/scene_name.txt`
           - `exports/summary.md`
           - `exports/result.json`
        6. Do not do the final full Manim render locally. The final remote step will later run `scripts/render_manim.sh scene.py <SceneName> {final_quality} exports/final.mp4` after sync.
        7. If you want validation, keep it lightweight: syntax checks, import checks, or tiny sanity checks only. Do not spend effort on a heavy local render.
        8. `exports/result.json` must be valid JSON and include at least:
           - `status` = `prepared`
           - `scene`
           - `quality`
           - `video` = `exports/final.mp4`
           - `summary`
        9. Keep the animation self-contained. Prefer native Manim objects, `MathTex`, `Tex`, `Axes`, `NumberPlane`, lines, polygons, and simple color highlights.
        10. Do not install packages unless absolutely necessary.
        11. When done, stop.

        Quality guidance:
        - Use quality `{final_quality}` for the final remote output.
        - Keep runtime moderate unless the brief explicitly asks for a long animation.
        - Prefer clarity over flashy effects.
        '''
    ).strip() + '\n'


def build_runner_script(local_job_dir: Path, remote_job_dir: str, host: str, final_quality: str) -> str:
    state_dir = local_state_dir(local_job_dir)
    bundle_dir = local_job_dir / 'bundle'
    output_dir = local_output_dir(local_job_dir)
    remote_prepare_cmd = (
        f"mkdir -p {shlex.quote(remote_job_dir)} && "
        f"find {shlex.quote(remote_job_dir)} -mindepth 1 -maxdepth 1 -exec rm -rf -- {{}} +"
    )
    remote_post_sync_cmd = (
        f"mkdir -p {shlex.quote(remote_job_dir + '/exports')} {shlex.quote(remote_job_dir + '/state')} && "
        f"chmod +x {shlex.quote(remote_job_dir + '/scripts/render_manim.sh')}"
    )
    remote_render_cmd = (
        f"export PATH={shlex.quote(remote_path_env())}; "
        f"export HOME={shlex.quote(DEFAULT_REMOTE_HOME)}; "
        f"export SHELL={shlex.quote(DEFAULT_REMOTE_SHELL)}; "
        f"cd {shlex.quote(remote_job_dir)}; "
        f"bash {shlex.quote(remote_job_dir + '/scripts/render_manim.sh')} scene.py \"$(cat state/scene_name.txt)\" {shlex.quote(final_quality)} exports/final.mp4"
    )
    local_codex_path_entries = [str(Path(DEFAULT_LOCAL_CODEX_BIN).parent), *os.environ.get('PATH', '').split(':'), '/usr/local/bin', '/usr/bin', '/bin']
    local_codex_path_env = ':'.join(dict.fromkeys(entry for entry in local_codex_path_entries if entry))
    local_codex_shell_command = (
        f"export PATH={shlex.quote(local_codex_path_env)}; "
        f"set -o pipefail; {shlex.quote(DEFAULT_LOCAL_CODEX_BIN)} exec --json --skip-git-repo-check "
        f"--dangerously-bypass-approvals-and-sandbox -C {shlex.quote(str(bundle_dir))} - "
        f"-o {shlex.quote(str(state_dir / 'last.txt'))} < {shlex.quote(str(bundle_dir / 'prompt.txt'))} "
        f"| tee {shlex.quote(str(state_dir / 'events.jsonl'))}"
    )
    thread_id_py = textwrap.dedent(r'''
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
''').strip()
    prepare_result_py = textwrap.dedent(r'''
import json, sys
from pathlib import Path
path = Path(sys.argv[1])
scene = sys.argv[2]
quality = sys.argv[3]
try:
    data = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
except Exception:
    data = {}
summary = str(data.get('summary') or '').strip()
if not summary:
    summary_path = path.parent / 'summary.md'
    if summary_path.exists():
        summary = summary_path.read_text(encoding='utf-8', errors='replace').strip()
data['status'] = 'prepared'
data['scene'] = scene
data['quality'] = quality
data['video'] = 'exports/final.mp4'
data['summary'] = summary
path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
''').strip()
    complete_result_py = textwrap.dedent(r'''
import json, sys
from pathlib import Path
src = Path(sys.argv[1])
dst = Path(sys.argv[2])
scene = sys.argv[3]
quality = sys.argv[4]
try:
    data = json.loads(src.read_text(encoding='utf-8'))
except Exception:
    data = {}
summary = str(data.get('summary') or '').strip()
if not summary:
    summary_path = src.parent / 'summary.md'
    if summary_path.exists():
        summary = summary_path.read_text(encoding='utf-8', errors='replace').strip()
data['status'] = 'completed'
data['scene'] = scene
data['quality'] = quality
data['video'] = 'exports/final.mp4'
data['summary'] = summary
payload = json.dumps(data, ensure_ascii=False, indent=2) + '\n'
src.write_text(payload, encoding='utf-8')
dst.write_text(payload, encoding='utf-8')
''').strip()
    fail_result_py = textwrap.dedent(r'''
import json, sys
from pathlib import Path
log_path = Path(sys.argv[1])
out_path = Path(sys.argv[2])
result_path = Path(sys.argv[3])
scene = sys.argv[4]
quality = sys.argv[5]
text = log_path.read_text(encoding='utf-8', errors='replace').strip() if log_path.exists() else ''
summary = ' '.join(text.split())
if len(summary) > 360:
    summary = summary[:359].rstrip() + '…'
out_path.write_text((summary or 'Remote render failed without log output.') + '\n', encoding='utf-8')
try:
    data = json.loads(result_path.read_text(encoding='utf-8')) if result_path.exists() else {}
except Exception:
    data = {}
data['status'] = 'failed'
data['scene'] = scene
data['quality'] = quality
data['video'] = 'exports/final.mp4'
data['summary'] = str(data.get('summary') or '').strip() or (summary or 'Remote render failed.')
result_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
''').strip()

    lines = [
        '#!/usr/bin/env bash',
        'set -euo pipefail',
        f'LOCAL_JOB_DIR={shlex.quote(str(local_job_dir))}',
        f'JOB_DIR={shlex.quote(str(bundle_dir))}',
        f'STATE_DIR={shlex.quote(str(state_dir))}',
        f'OUTPUT_DIR={shlex.quote(str(output_dir))}',
        f'REMOTE_HOST={shlex.quote(host)}',
        f'REMOTE_JOB_DIR={shlex.quote(remote_job_dir)}',
        f'REMOTE_PREP_CMD={shlex.quote(remote_prepare_cmd)}',
        f'REMOTE_POST_SYNC_CMD={shlex.quote(remote_post_sync_cmd)}',
        f'REMOTE_RENDER_CMD={shlex.quote(remote_render_cmd)}',
        f'CODEX_SHELL_COMMAND={shlex.quote(local_codex_shell_command)}',
        f'QUALITY={shlex.quote(final_quality)}',
        'SCENE_PATH="$JOB_DIR/scene.py"',
        'SCENE_NAME_PATH="$JOB_DIR/state/scene_name.txt"',
        'SUMMARY_PATH="$JOB_DIR/exports/summary.md"',
        'RESULT_PATH="$JOB_DIR/exports/result.json"',
        'REMOTE_RENDER_LOG="$STATE_DIR/remote_render.log"',
        '',
        'mkdir -p "$STATE_DIR" "$JOB_DIR/exports" "$JOB_DIR/state" "$OUTPUT_DIR"',
        'echo running > "$STATE_DIR/status.txt"',
        'echo authoring > "$STATE_DIR/stage.txt"',
        'date -Iseconds > "$STATE_DIR/started_at.txt"',
        ': > "$STATE_DIR/events.jsonl"',
        ': > "$STATE_DIR/last.txt"',
        ': > "$REMOTE_RENDER_LOG"',
        'echo "Local Codex authoring started." > "$STATE_DIR/last.txt"',
        '',
        'setsid bash -lc "$CODEX_SHELL_COMMAND" &',
        'CODEX_WRAPPER_PID=$!',
        'echo "$CODEX_WRAPPER_PID" > "$STATE_DIR/local_codex_pid.txt"',
        '',
        'while kill -0 "$CODEX_WRAPPER_PID" 2>/dev/null; do',
        '  if [[ -s "$SCENE_PATH" && -s "$SCENE_NAME_PATH" && -s "$SUMMARY_PATH" && -s "$RESULT_PATH" ]]; then',
        '    last_activity=0',
        '    for tracked_path in "$STATE_DIR/events.jsonl" "$STATE_DIR/last.txt" "$SCENE_PATH" "$SCENE_NAME_PATH" "$SUMMARY_PATH" "$RESULT_PATH"; do',
        '      if [[ -e "$tracked_path" ]]; then',
        "        tracked_mtime=$(stat -c '%Y' \"$tracked_path\")",
        '        if (( tracked_mtime > last_activity )); then',
        '          last_activity=$tracked_mtime',
        '        fi',
        '      fi',
        '    done',
        '    now_ts=$(date +%s)',
        '    if (( now_ts - last_activity >= 15 )); then',
        '      echo "Required files are ready; stopping local Codex and continuing to remote render." > "$STATE_DIR/last.txt"',
        '      kill -TERM -- "-$CODEX_WRAPPER_PID" 2>/dev/null || true',
        '      break',
        '    fi',
        '  fi',
        '  sleep 2',
        'done',
        '',
        'rc=0',
        'if wait "$CODEX_WRAPPER_PID"; then',
        '  rc=0',
        'else',
        '  rc=$?',
        'fi',
        'echo "$rc" > "$STATE_DIR/local_codex_exit.txt"',
        '',
        "python3 - \"$STATE_DIR/events.jsonl\" \"$STATE_DIR/thread_id.txt\" <<'PYEOF'",
    ]
    lines.extend(thread_id_py.splitlines())
    lines.extend([
        'PYEOF',
        '',
        'if [[ "$rc" != "0" && ( ! -s "$SCENE_PATH" || ! -s "$SCENE_NAME_PATH" || ! -s "$SUMMARY_PATH" || ! -s "$RESULT_PATH" ) ]]; then',
        '  echo "local codex authoring failed" > "$STATE_DIR/failure_reason.txt"',
        '  echo failed > "$STATE_DIR/status.txt"',
        '  echo authoring-failed > "$STATE_DIR/stage.txt"',
        '  date -Iseconds > "$STATE_DIR/finished_at.txt"',
        '  exit "$rc"',
        'fi',
        'if [[ "$rc" != "0" ]]; then',
        '  echo "Local Codex exited after preparing the required files; continuing to remote render." > "$STATE_DIR/last.txt"',
        'fi',
        '',
        'if [[ ! -s "$SCENE_PATH" ]]; then',
        '  echo "missing scene.py" > "$STATE_DIR/validation_error.txt"',
        '  echo "missing scene.py" > "$STATE_DIR/failure_reason.txt"',
        '  echo "missing scene.py" > "$STATE_DIR/last.txt"',
        '  echo failed > "$STATE_DIR/status.txt"',
        '  echo validation-failed > "$STATE_DIR/stage.txt"',
        '  date -Iseconds > "$STATE_DIR/finished_at.txt"',
        '  exit 81',
        'fi',
        'if [[ ! -s "$SCENE_NAME_PATH" ]]; then',
        '  echo "missing state/scene_name.txt" > "$STATE_DIR/validation_error.txt"',
        '  echo "missing state/scene_name.txt" > "$STATE_DIR/failure_reason.txt"',
        '  echo "missing state/scene_name.txt" > "$STATE_DIR/last.txt"',
        '  echo failed > "$STATE_DIR/status.txt"',
        '  echo validation-failed > "$STATE_DIR/stage.txt"',
        '  date -Iseconds > "$STATE_DIR/finished_at.txt"',
        '  exit 82',
        'fi',
        'if [[ ! -s "$SUMMARY_PATH" ]]; then',
        '  echo "missing exports/summary.md" > "$STATE_DIR/validation_error.txt"',
        '  echo "missing exports/summary.md" > "$STATE_DIR/failure_reason.txt"',
        '  echo "missing exports/summary.md" > "$STATE_DIR/last.txt"',
        '  echo failed > "$STATE_DIR/status.txt"',
        '  echo validation-failed > "$STATE_DIR/stage.txt"',
        '  date -Iseconds > "$STATE_DIR/finished_at.txt"',
        '  exit 83',
        'fi',
        "SCENE_NAME=$(tr -d '\\r\\n' < \"$SCENE_NAME_PATH\")",
        'if [[ -z "$SCENE_NAME" ]]; then',
        '  echo "empty state/scene_name.txt" > "$STATE_DIR/validation_error.txt"',
        '  echo "empty state/scene_name.txt" > "$STATE_DIR/failure_reason.txt"',
        '  echo "empty state/scene_name.txt" > "$STATE_DIR/last.txt"',
        '  echo failed > "$STATE_DIR/status.txt"',
        '  echo validation-failed > "$STATE_DIR/stage.txt"',
        '  date -Iseconds > "$STATE_DIR/finished_at.txt"',
        '  exit 84',
        'fi',
        "python3 - \"$RESULT_PATH\" \"$SCENE_NAME\" \"$QUALITY\" <<'PYEOF'",
    ])
    lines.extend(prepare_result_py.splitlines())
    lines.extend([
        'PYEOF',
        'date -Iseconds > "$STATE_DIR/author_done_at.txt"',
        '',
        'echo syncing > "$STATE_DIR/stage.txt"',
        'echo "Local Codex authoring finished; syncing to remote render host." > "$STATE_DIR/last.txt"',
        'ssh "$REMOTE_HOST" "$REMOTE_PREP_CMD"',
        'scp -r "$JOB_DIR"/. "$REMOTE_HOST:$REMOTE_JOB_DIR/"',
        'ssh "$REMOTE_HOST" "$REMOTE_POST_SYNC_CMD"',
        'date -Iseconds > "$STATE_DIR/sync_done_at.txt"',
        '',
        'echo rendering > "$STATE_DIR/stage.txt"',
        'echo "Remote render started." > "$STATE_DIR/last.txt"',
        'if ssh "$REMOTE_HOST" "$REMOTE_RENDER_CMD" > "$REMOTE_RENDER_LOG" 2>&1; then',
        '  scp "$REMOTE_HOST:$REMOTE_JOB_DIR/exports/final.mp4" "$OUTPUT_DIR/final.mp4"',
        '  cp "$SCENE_PATH" "$OUTPUT_DIR/scene.py"',
        '  cp "$SUMMARY_PATH" "$OUTPUT_DIR/summary.md"',
        "  python3 - \"$RESULT_PATH\" \"$OUTPUT_DIR/result.json\" \"$SCENE_NAME\" \"$QUALITY\" <<'PYEOF'",
    ])
    lines.extend(complete_result_py.splitlines())
    lines.extend([
        'PYEOF',
        '  echo "$OUTPUT_DIR/final.mp4" > "$LOCAL_JOB_DIR/local_video.txt"',
        '  echo "$OUTPUT_DIR/summary.md" > "$LOCAL_JOB_DIR/local_summary.txt"',
        '  echo "$OUTPUT_DIR/result.json" > "$LOCAL_JOB_DIR/local_result.txt"',
        '  date -Iseconds > "$LOCAL_JOB_DIR/fetched_at.txt"',
        '  date -Iseconds > "$STATE_DIR/render_done_at.txt"',
        '  echo "Remote render completed; outputs fetched locally." > "$STATE_DIR/last.txt"',
        '  echo completed > "$STATE_DIR/status.txt"',
        '  echo completed > "$STATE_DIR/stage.txt"',
        '  date -Iseconds > "$STATE_DIR/finished_at.txt"',
        '  exit 0',
        'fi',
        '',
        'render_rc=$?',
        "python3 - \"$REMOTE_RENDER_LOG\" \"$STATE_DIR/last.txt\" \"$RESULT_PATH\" \"$SCENE_NAME\" \"$QUALITY\" <<'PYEOF'",
    ])
    lines.extend(fail_result_py.splitlines())
    lines.extend([
        'PYEOF',
        'echo "remote render failed" > "$STATE_DIR/failure_reason.txt"',
        'echo failed > "$STATE_DIR/status.txt"',
        'echo render-failed > "$STATE_DIR/stage.txt"',
        'date -Iseconds > "$STATE_DIR/finished_at.txt"',
        'exit "$render_rc"',
    ])
    return '\n'.join(lines) + '\n'


def infer_status(job_dir_path: Path, stall_minutes: int) -> tuple[str, str, str]:
    host = read_text(job_dir_path / 'remote_host.txt', DEFAULT_REMOTE_HOST)
    remote_job_dir = read_text(job_dir_path / 'remote_job_dir.txt')
    raw = local_state_text(job_dir_path, 'status.txt', 'unknown')
    if raw == 'running':
        last_mtime = local_latest_activity(job_dir_path)
        if last_mtime and (time.time() - last_mtime) > stall_minutes * 60:
            return 'stale', host, remote_job_dir
    return raw, host, remote_job_dir


def print_status(job_dir_path: Path, *, stall_minutes: int) -> None:
    status, host, remote_job_dir = infer_status(job_dir_path, stall_minutes)
    stage = local_state_text(job_dir_path, 'stage.txt')
    thread_id = local_state_text(job_dir_path, 'thread_id.txt')
    last_text = summarize_text(local_state_text(job_dir_path, 'last.txt'))
    print(f'job: {job_dir_path.name}')
    print(f'status: {status}')
    if stage:
        print(f'stage: {stage}')
    print(f'remote_host: {host}')
    print(f'remote_dir: {remote_job_dir}')
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
    bundle_dir = job_dir_path / 'bundle'
    scp_from(host, f'{remote_job_dir}/exports/final.mp4', video_path)
    if (bundle_dir / 'exports' / 'summary.md').exists():
        summary_path.write_text((bundle_dir / 'exports' / 'summary.md').read_text(encoding='utf-8'), encoding='utf-8')
    if (bundle_dir / 'exports' / 'result.json').exists():
        result_path.write_text((bundle_dir / 'exports' / 'result.json').read_text(encoding='utf-8'), encoding='utf-8')
    if (bundle_dir / 'scene.py').exists():
        scene_path.write_text((bundle_dir / 'scene.py').read_text(encoding='utf-8'), encoding='utf-8')
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
        last_text = summarize_text(local_state_text(job_dir_path, 'last.txt') or '(没有捕获到末条回复)', 360)
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
    print(local_codex_doctor())
    print()
    print(remote_doctor(args.host, args.remote_base))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    ensure_dirs()
    print(local_codex_doctor())
    print()
    print(remote_doctor(args.host, args.remote_base))
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    ensure_dirs()
    brief = args.prompt if args.prompt is not None else Path(args.prompt_file).read_text(encoding='utf-8')
    slug = sanitize_slug(args.name or 'manim')
    job_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{slug}"
    local_job_dir = job_dir(job_id)
    remote_job_dir = f"{args.remote_base.rstrip('/')}/{job_id}"
    signal_name = signal_name_for_job(job_id)
    write_text(local_job_dir / 'created_at.txt', now_iso() + '\n')
    write_text(local_job_dir / 'remote_host.txt', args.host + '\n')
    write_text(local_job_dir / 'remote_job_dir.txt', remote_job_dir + '\n')
    write_text(local_job_dir / 'signal.txt', signal_name + '\n')
    write_text(local_job_dir / 'workflow.txt', 'local-codex-remote-render\n')
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

    runner_path = local_state_dir(local_job_dir) / 'runner.sh'
    write_text(runner_path, build_runner_script(local_job_dir, remote_job_dir, args.host, args.final_quality))
    runner_path.chmod(0o755)

    proc = subprocess.Popen(
        [str(runner_path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    write_text(local_job_dir / 'local_runner_pid.txt', str(proc.pid) + '\n')
    spawn_waiter(job_id, notify_qq=True)

    print(f'job={job_id}')
    print(f'remote_host={args.host}')
    print(f'remote_dir={remote_job_dir}')
    print(f'quality={args.final_quality}')
    print(f'local_runner_pid={proc.pid}')
    if recipient:
        print(f'recipient={recipient}')
    print(f'state_dir={local_job_dir}')
    return 0


def cmd_wait(args: argparse.Namespace) -> int:
    ensure_dirs()
    local_job_dir = job_dir(args.job)
    if not local_job_dir.exists():
        raise SystemExit(f'job not found: {args.job}')
    while True:
        key, body, video_path = build_notice(local_job_dir, stall_minutes=args.stall_minutes)
        if key and body:
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
        time.sleep(5)


def cmd_status(args: argparse.Namespace) -> int:
    ensure_dirs()
    if args.job:
        print_status(job_dir(args.job), stall_minutes=args.stall_minutes)
        return 0
    jobs = sorted(path for path in JOBS_ROOT.iterdir() if path.is_dir()) if JOBS_ROOT.exists() else []
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
    state_dir = local_state_dir(local_job_dir)
    stage = read_text(state_dir / 'stage.txt')
    candidates: list[Path] = []
    if stage in {'syncing', 'rendering', 'render-failed'}:
        candidates.append(state_dir / 'remote_render.log')
    candidates.extend([
        state_dir / 'events.jsonl',
        state_dir / 'last.txt',
    ])
    for path in candidates:
        text = tail_file(path, int(args.lines))
        if text.strip():
            print(text)
            return 0
    print('no local runner output available')
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    ensure_dirs()
    notices: list[str] = []
    for local_job_dir in sorted(path for path in JOBS_ROOT.iterdir() if path.is_dir()) if JOBS_ROOT.exists() else []:
        key, body, video_path = build_notice(local_job_dir, stall_minutes=args.stall_minutes)
        if not key or not body:
            continue
        marker_file = local_job_dir / 'last_notice_key.txt'
        if read_text(marker_file) == key:
            continue
        write_text(marker_file, key + '\n')
        notices.append(body)
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
    ap = argparse.ArgumentParser(description='Manage remote Manim render jobs via local Codex authoring and remote rendering.')
    sub = ap.add_subparsers(dest='cmd', required=True)

    bootstrap = sub.add_parser('bootstrap', help='Check local Codex and remote render environment')
    bootstrap.add_argument('--host', default=DEFAULT_REMOTE_HOST)
    bootstrap.add_argument('--remote-base', default=DEFAULT_REMOTE_BASE)
    bootstrap.set_defaults(func=cmd_bootstrap)

    doctor = sub.add_parser('doctor', help='Check local Codex and remote render environment')
    doctor.add_argument('--host', default=DEFAULT_REMOTE_HOST)
    doctor.add_argument('--remote-base', default=DEFAULT_REMOTE_BASE)
    doctor.set_defaults(func=cmd_doctor)

    start = sub.add_parser('start', help='Start a new Manim authoring job locally and render it remotely')
    start.add_argument('--name', help='Human-friendly job name')
    prompt_group = start.add_mutually_exclusive_group(required=True)
    prompt_group.add_argument('--prompt', help='Inline creative brief')
    prompt_group.add_argument('--prompt-file', help='Read brief from file')
    start.add_argument('--host', default=DEFAULT_REMOTE_HOST)
    start.add_argument('--remote-base', default=DEFAULT_REMOTE_BASE)
    start.add_argument('--session', default=DEFAULT_SESSION, help='Retained for CLI compatibility; currently unused')
    start.add_argument('--final-quality', default=DEFAULT_FINAL_QUALITY, choices=['l', 'm', 'h', 'p', 'k'])
    start.add_argument('--recipient', help='QQ recipient for completion push, e.g. user:<openid>')
    start.set_defaults(func=cmd_start)

    wait = sub.add_parser('wait', help='Block until a Manim job finishes and then emit one completion/failure notice')
    wait.add_argument('job')
    wait.add_argument('--stall-minutes', type=int, default=DEFAULT_STALL_MINUTES)
    wait.add_argument('--quiet-no-change', action='store_true')
    wait.add_argument('--notify-qq', action='store_true')
    wait.add_argument('--recipient', help='Fallback QQ recipient if the job did not store one')
    wait.set_defaults(func=cmd_wait)

    status = sub.add_parser('status', help='Show Manim job status')
    status.add_argument('job', nargs='?')
    status.add_argument('--stall-minutes', type=int, default=DEFAULT_STALL_MINUTES)
    status.set_defaults(func=cmd_status)

    tail = sub.add_parser('tail', help='Show latest local Codex or remote render log for a job')
    tail.add_argument('job')
    tail.add_argument('--lines', type=int, default=120)
    tail.set_defaults(func=cmd_tail)

    watch = sub.add_parser('watch', help='Report only notable Manim job changes')
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
