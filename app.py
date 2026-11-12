import base64
import csv
import hmac
import json
import logging
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
from collections import deque
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import gradio as gr
import requests
from dotenv import load_dotenv

load_dotenv()

MODEL_CHOICES = ("wan3.0-video-prime", "wan3.0-video")
MODEL = "wan3.0-video-prime"
SYNTHESIS_URL = os.getenv(
    "DASHSCOPE_VIDEO_URL",
    "https://llm-0nr9mznstg4wrz4h.cn-beijing.maas.aliyuncs.com/"
    "api/v1/services/aigc/video-generation/video-synthesis",
)
TASK_URL_TEMPLATE = os.getenv(
    "DASHSCOPE_TASK_URL_TEMPLATE",
    "https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}",
)
POLL_INTERVAL = max(1, int(os.getenv("POLL_INTERVAL_SECONDS", "5")))
TASK_TIMEOUT = max(60, int(os.getenv("TASK_TIMEOUT_SECONDS", "1200")))
OUTPUT_DIR = Path(os.getenv("VIDEO_OUTPUT_DIR", "generated_videos"))
LOG_FILE = Path(os.getenv("VIDEO_LOG_FILE", "logs/generation.log"))
PROJECT_DIR = Path(__file__).resolve().parent
USER_FILE = PROJECT_DIR / "user.json"
OPERATION_LOG_FILE = PROJECT_DIR / "operation_log.csv"
SCENE_COUNT = 5
MAX_CONCURRENT_BATCHES = 3
MAX_OPERATION_LOG_ROWS = 500
OPERATION_LOG_FIELDS = (
    "time",
    "username",
    "model",
    "api_key_masked",
    "task_id",
    "prompt",
    "video_path",
    "duration_seconds",
    "scene_number",
    "orientation",
    "audio",
    "result",
    "error",
)
OPERATION_LOG_HEADERS = (
    "时间",
    "账号",
    "模型",
    "API Key（脱敏）",
    "视频 ID",
    "视频生成提示词",
    "视频绝对路径",
    "视频长度（秒）",
    "分镜序号",
    "画幅",
    "音轨",
    "结果",
    "错误信息",
)
SERVER_HOST = os.getenv("GRADIO_SERVER_NAME", "0.0.0.0")
SERVER_PORT = int(os.getenv("GRADIO_SERVER_PORT", "7860"))

_user_store_lock = threading.RLock()
_active_user_lock = threading.Lock()
_active_generation_users: set[str] = set()
_scene_files_lock = threading.Lock()
_user_scene_files: dict[str, dict[int, Path]] = {}
_operation_log_lock = threading.Lock()

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("video_generator")


def _mask_api_key(api_key: str) -> str:
    api_key = (api_key or "").strip()
    if len(api_key) <= 8:
        return "*" * len(api_key)
    return f"{api_key[:4]}{'*' * (len(api_key) - 8)}{api_key[-4:]}"


def _compose_video_prompt(prompt: str, dialogue: str) -> str:
    prompt = (prompt or "").strip()
    dialogue = (dialogue or "").strip()
    if not dialogue:
        return prompt
    return (
        f"{prompt}\n\n对白与声音要求：\n{dialogue}\n"
        "请按上述内容生成自然、清晰的中文语音；如画面中有人物说话，"
        "尽量使口型与对应对白同步。"
    )


def _ensure_operation_log() -> None:
    OPERATION_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _operation_log_lock:
        if not OPERATION_LOG_FILE.exists() or OPERATION_LOG_FILE.stat().st_size == 0:
            with OPERATION_LOG_FILE.open("w", encoding="utf-8-sig", newline="") as log_file:
                writer = csv.DictWriter(log_file, fieldnames=OPERATION_LOG_FIELDS)
                writer.writeheader()
        os.chmod(OPERATION_LOG_FILE, 0o600)


def _append_operation_log(record: dict[str, object]) -> None:
    try:
        _ensure_operation_log()
        with _operation_log_lock:
            with OPERATION_LOG_FILE.open("a", encoding="utf-8", newline="") as log_file:
                writer = csv.DictWriter(log_file, fieldnames=OPERATION_LOG_FIELDS)
                writer.writerow(
                    {field: record.get(field, "") for field in OPERATION_LOG_FIELDS}
                )
    except Exception:
        logger.exception("failed to append video operation log")


def _read_operation_log(limit: int | None = None) -> list[dict[str, str]]:
    _ensure_operation_log()
    with _operation_log_lock:
        with OPERATION_LOG_FILE.open("r", encoding="utf-8-sig", newline="") as log_file:
            rows = csv.DictReader(log_file)
            if limit is None:
                return list(rows)
            return list(deque(rows, maxlen=limit))


def _operation_log_table_data() -> list[list[str]]:
    rows = _read_operation_log(limit=MAX_OPERATION_LOG_ROWS)
    return [
        [row.get(field, "") for field in OPERATION_LOG_FIELDS]
        for row in reversed(rows)
    ]


def _admin_operation_log_table(request: gr.Request) -> list[list[str]]:
    _require_admin(request)
    return _operation_log_table_data()


def export_operation_log_excel(request: gr.Request) -> str:
    _require_admin(request)
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
    except ImportError as exc:
        raise gr.Error("Excel 导出依赖 openpyxl，请运行 pip install -r requirements.txt。") from exc

    rows = _read_operation_log()
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "视频生成操作日志"
    worksheet.append(OPERATION_LOG_HEADERS)
    for cell in worksheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill(fill_type="solid", fgColor="335B78")
        cell.alignment = Alignment(vertical="top", wrap_text=True)

    for row in rows:
        values = []
        for field in OPERATION_LOG_FIELDS:
            value = str(row.get(field, "") or "")
            if field in {"duration_seconds", "scene_number"} and value.isdigit():
                values.append(int(value))
            else:
                values.append(value[:32767])
        worksheet.append(values)
        for cell in worksheet[worksheet.max_row]:
            if cell.column not in {8, 9}:
                cell.data_type = "s"
            cell.alignment = Alignment(vertical="top", wrap_text=True)

    worksheet.freeze_panes = "A2"
    worksheet.auto_filter.ref = worksheet.dimensions
    widths = (22, 18, 24, 28, 30, 64, 54, 16, 12, 16, 12, 12, 64)
    for index, width in enumerate(widths, start=1):
        worksheet.column_dimensions[worksheet.cell(1, index).column_letter].width = width

    fd, filename = tempfile.mkstemp(
        prefix="operation_log_", suffix=".xlsx", dir=str(PROJECT_DIR)
    )
    os.close(fd)
    try:
        workbook.save(filename)
    except Exception:
        Path(filename).unlink(missing_ok=True)
        raise
    return str(Path(filename).resolve())


def _default_user_store() -> dict:
    return {
        "users": {
            "admin": {
                "password": "1qaz2wsx",
                "role": "admin",
                "model": MODEL,
                "api_key": "",
                "synthesis_url": SYNTHESIS_URL,
                "task_url_template": TASK_URL_TEMPLATE,
            }
        }
    }


def _write_user_store(data: dict) -> None:
    USER_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=".user_", suffix=".tmp", dir=str(USER_FILE.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output_file:
            json.dump(data, output_file, ensure_ascii=False, indent=2)
            output_file.write("\n")
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, USER_FILE)
        os.chmod(USER_FILE, 0o600)
    except Exception:
        Path(temp_name).unlink(missing_ok=True)
        raise


def _ensure_user_store() -> None:
    with _user_store_lock:
        if not USER_FILE.exists():
            _write_user_store(_default_user_store())
        else:
            os.chmod(USER_FILE, 0o600)


def _read_user_store() -> dict:
    _ensure_user_store()
    with _user_store_lock:
        try:
            data = json.loads(USER_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise gr.Error(f"无法读取账号文件 {USER_FILE.name}：{exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("users"), dict):
        raise gr.Error("user.json 格式异常，必须包含 users 对象。")
    return data


def _save_user_store(data: dict) -> None:
    with _user_store_lock:
        _write_user_store(data)


def _get_user_record(username: str) -> dict | None:
    store = _read_user_store()
    return store["users"].get(username)


def _authenticate(username: str, password: str) -> bool:
    username = username.strip()
    user = _get_user_record(username)
    if not user or not isinstance(user.get("password"), str):
        return False
    return hmac.compare_digest(user["password"], password)


def _request_username(request: gr.Request) -> str:
    username = getattr(request, "username", None)
    if not username or not _get_user_record(username):
        raise gr.Error("登录状态无效，请退出后重新登录。")
    return username


def _require_admin(request: gr.Request) -> str:
    username = _request_username(request)
    user = _get_user_record(username)
    if user.get("role") != "admin":
        raise gr.Error("只有超级管理员可以执行此操作。")
    return username


def _claim_generation_slot(username: str) -> None:
    with _active_user_lock:
        if username in _active_generation_users:
            raise gr.Error("你的账号已有一个批量任务正在运行，请稍后再提交。")
        _active_generation_users.add(username)


def _release_generation_slot(username: str) -> None:
    with _active_user_lock:
        _active_generation_users.discard(username)


def _set_user_scene_file(username: str, scene_number: int, path: Path | None) -> None:
    with _scene_files_lock:
        user_files = _user_scene_files.setdefault(username, {})
        if path is None:
            user_files.pop(scene_number, None)
        else:
            user_files[scene_number] = path.resolve()


def _user_output_dir(username: str) -> Path:
    if username in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", username):
        raise gr.Error("账号名不符合安全的目录名称格式。")
    return OUTPUT_DIR / username


def _validate_endpoint_urls(synthesis_url: str, task_url_template: str) -> None:
    if not synthesis_url.startswith("https://"):
        raise gr.Error("视频生成接口地址必须以 https:// 开头。")
    if not task_url_template.startswith("https://"):
        raise gr.Error("任务查询地址必须以 https:// 开头。")
    if "{task_id}" not in task_url_template:
        raise gr.Error("任务查询地址模板必须包含 {task_id}。")


def _authorization_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _image_data_uri(image_path: str) -> str:
    path = Path(image_path)
    mime_type = mimetypes.guess_type(path.name)[0] or "image/png"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _response_error(response: requests.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        body = response.text[:1000]
    return f"HTTP {response.status_code}: {body}"


def _task_id_from_response(response: requests.Response) -> str:
    try:
        data = response.json()
    except ValueError as exc:
        raise gr.Error("视频接口返回的不是有效 JSON。") from exc

    task_id = data.get("output", {}).get("task_id") or data.get("task_id")
    if not task_id:
        raise gr.Error(f"接口没有返回 task_id：{data}")
    return str(task_id)


def _task_url(task_id: str, task_url_template: str) -> str:
    return task_url_template.format(task_id=quote(task_id, safe=""))


def _download_video(video_url: str, api_key: str, output_dir: Path) -> str:
    output_dir.mkdir(parents=True, exist_ok=True)
    response = requests.get(
        video_url,
        headers=_authorization_headers(api_key),
        stream=True,
        timeout=(15, 120),
    )
    if not response.ok:
        raise gr.Error(f"下载生成视频失败：{_response_error(response)}")

    fd, filename = tempfile.mkstemp(
        prefix="video_", suffix=".mp4", dir=str(output_dir)
    )
    try:
        with os.fdopen(fd, "wb") as output_file:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    output_file.write(chunk)
    except Exception:
        Path(filename).unlink(missing_ok=True)
        raise
    return filename


def _scene_video(
    image_path: str,
    prompt: str,
    dialogue: str,
    generate_audio: bool,
    orientation: str,
    duration: int,
    scene_number: int,
    api_key: str,
    model: str,
    synthesis_url: str,
    task_url_template: str,
    output_dir: Path,
    username: str,
    operation_record: dict[str, object],
):
    prompt = _compose_video_prompt(prompt, dialogue)
    operation_record["prompt"] = prompt

    ratio = {"横屏 16:9": "16:9", "竖屏 9:16": "9:16"}[orientation]
    payload = {
        "model": model,
        "input": {
            "prompt": prompt,
            "media": [
                {"type": "first_frame", "url": _image_data_uri(image_path)}
            ],
        },
        "parameters": {
            "resolution": os.getenv("VIDEO_RESOLUTION", "480P"),
            "ratio": ratio,
            "duration": int(duration),
            "audio": bool(generate_audio),
            "prompt_extend": True,
        },
    }

    try:
        response = requests.post(
            synthesis_url,
            headers={
                **_authorization_headers(api_key),
                "X-DashScope-Async": "enable",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=(15, 120),
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"提交视频生成任务失败：{exc}") from exc
    if not response.ok:
        raise RuntimeError(f"提交视频生成任务失败：{_response_error(response)}")

    task_id = _task_id_from_response(response)
    operation_record["task_id"] = task_id
    yield None, f"任务已提交（{task_id}），等待生成……"

    deadline = time.monotonic() + TASK_TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL)
        try:
            status_response = requests.get(
                _task_url(task_id, task_url_template),
                headers=_authorization_headers(api_key),
                timeout=(15, 60),
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"查询任务状态失败：{exc}") from exc
        if not status_response.ok:
            raise RuntimeError(f"查询任务状态失败：{_response_error(status_response)}")

        try:
            result = status_response.json()
        except ValueError as exc:
            raise RuntimeError("任务状态接口返回的不是有效 JSON。") from exc

        output = result.get("output", {})
        status = output.get("task_status") or result.get("task_status", "")
        if status == "SUCCEEDED":
            video_url = output.get("video_url")
            if not video_url:
                raise RuntimeError(f"任务成功，但响应中没有 video_url：{result}")
            video_path = _download_video(video_url, api_key, output_dir)
            logger.info(
                "user=%s scene=%s task_id=%s video_path=%s",
                username,
                scene_number,
                task_id,
                Path(video_path).resolve(),
            )
            yield video_path, "生成完成"
            return
        if status in {"FAILED", "CANCELED", "UNKNOWN"}:
            message = output.get("message") or output.get("code") or result
            raise RuntimeError(f"视频生成失败（{status}）：{message}")

        yield None, f"视频生成中，任务状态：{status or '处理中'}……"

    raise RuntimeError(
        f"等待超时（{TASK_TIMEOUT} 秒）。任务可能仍在云端运行，可稍后查询任务 ID：{task_id}"
    )


def generate_batch(request: gr.Request, *values):
    username = _request_username(request)
    user = _get_user_record(username)
    api_key = str(user.get("api_key", "")).strip()
    model = str(user.get("model", "")).strip()
    synthesis_url = str(user.get("synthesis_url", "")).strip()
    task_url_template = str(user.get("task_url_template", "")).strip()
    if not api_key:
        raise gr.Error("请先在“账号与百炼配置”中填写并保存 API Key。")
    if model not in MODEL_CHOICES:
        raise gr.Error("请在“账号与百炼配置”中选择支持的模型并保存。")
    _validate_endpoint_urls(synthesis_url, task_url_template)

    scene_values = values[: SCENE_COUNT * 6]
    previous_videos = values[SCENE_COUNT * 6 :]
    scenes = [
        scene_values[index : index + 6]
        for index in range(0, len(scene_values), 6)
    ]
    if len(scenes) != SCENE_COUNT or len(previous_videos) != SCENE_COUNT:
        raise gr.Error("分镜表单数据异常，请刷新页面后重试。")

    active = []
    for scene_number, scene in enumerate(scenes, start=1):
        image_path, prompt, dialogue, audio, orientation, duration = scene
        if not image_path:
            continue
        if not prompt or not prompt.strip():
            raise gr.Error(f"分镜 {scene_number} 已上传首帧，请填写视频提示词。")
        active.append((scene_number, scene))

    states = []
    for scene, previous_video in zip(scenes, previous_videos):
        if scene[0]:
            states.append((None, "排队中"))
        elif previous_video:
            states.append((previous_video, "保留已有成功结果"))
        else:
            states.append((None, "未上传，已跳过"))

    def outputs():
        flattened = []
        for video_path, message in states:
            flattened.extend([video_path, message])
        return tuple(flattened + [None, None])

    if not active and not any(previous_videos):
        raise gr.Error("请至少上传一个分镜首帧图片。")
    output_dir = _user_output_dir(username)
    _claim_generation_slot(username)
    try:
        for scene_number, _scene in active:
            _set_user_scene_file(username, scene_number, None)
        yield outputs()
        for scene_number, scene in active:
            image_path, prompt, dialogue, audio, orientation, duration = scene
            operation_record: dict[str, object] = {
                "time": datetime.now().astimezone().isoformat(timespec="seconds"),
                "username": username,
                "model": model,
                "api_key_masked": _mask_api_key(api_key),
                "task_id": "",
                "prompt": "",
                "video_path": "",
                "duration_seconds": int(duration),
                "scene_number": scene_number,
                "orientation": orientation,
                "audio": "是" if audio else "否",
                "result": "",
                "error": "",
            }
            try:
                for video_path, message in _scene_video(
                    image_path,
                    prompt,
                    dialogue or "",
                    bool(audio),
                    orientation,
                    int(duration),
                    scene_number,
                    api_key,
                    model,
                    synthesis_url,
                    task_url_template,
                    output_dir,
                    username,
                    operation_record,
                ):
                    if video_path:
                        _set_user_scene_file(
                            username, scene_number, Path(video_path)
                        )
                        operation_record["video_path"] = str(
                            Path(video_path).resolve()
                        )
                        operation_record["result"] = "成功"
                        states[scene_number - 1] = (video_path, message)
                    else:
                        states[scene_number - 1] = (None, message)
                    yield outputs()
                if operation_record["video_path"]:
                    operation_record["result"] = "成功"
                else:
                    operation_record["result"] = "失败"
                    operation_record["error"] = "任务未返回视频文件。"
            except Exception as exc:
                error_message = str(exc)
                if api_key:
                    error_message = error_message.replace(
                        api_key, _mask_api_key(api_key)
                    )
                logger.error(
                    "user=%s scene=%s generation failed: %s",
                    username,
                    scene_number,
                    error_message,
                )
                operation_record["result"] = "失败"
                operation_record["error"] = error_message
                states[scene_number - 1] = (None, f"生成失败：{error_message}")
            finally:
                if not operation_record["result"]:
                    operation_record["result"] = "中断"
                    operation_record["error"] = "生成任务在完成前停止。"
                _append_operation_log(operation_record)
            if operation_record["result"] != "成功":
                yield outputs()
    finally:
        _release_generation_slot(username)


def _local_video_entries(values: tuple, username: str) -> list[tuple[int, Path]]:
    entries = []
    root = _user_output_dir(username).resolve()
    with _scene_files_lock:
        user_files = dict(_user_scene_files.get(username, {}))
    for scene_number, value in enumerate(values, start=1):
        if isinstance(value, dict):
            value = value.get("path") or value.get("name")
        path = user_files.get(scene_number)
        if value and path and path.is_file() and path.is_relative_to(root):
            entries.append((scene_number, path))
    return entries


def download_scene_videos(request: gr.Request, *values):
    username = _request_username(request)
    output_dir = _user_output_dir(username)
    entries = _local_video_entries(values, username)
    if not entries:
        raise gr.Error("当前没有可下载的成功分镜视频。")
    output_dir.mkdir(parents=True, exist_ok=True)
    archive_path = output_dir / f"scenes_{time.strftime('%Y%m%d_%H%M%S')}.zip"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for scene_number, path in entries:
            archive.write(path, arcname=f"scene_{scene_number:02d}{path.suffix}")
    return str(archive_path.resolve())


def _has_audio(path: Path) -> bool:
    probe = shutil.which("ffprobe")
    if not probe:
        raise gr.Error("合并视频需要 FFmpeg/ffprobe，请先安装 FFmpeg 并加入 PATH。")
    result = subprocess.run(
        [
            probe,
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise gr.Error(f"读取视频音轨失败：{result.stderr.strip()}")
    return bool(result.stdout.strip())


def _video_dimensions(path: Path) -> tuple[int, int]:
    result = subprocess.run(
        [
            shutil.which("ffprobe") or "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise gr.Error(f"读取视频尺寸失败：{result.stderr.strip()}")
    try:
        stream = json.loads(result.stdout)["streams"][0]
        return int(stream["width"]), int(stream["height"])
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise gr.Error(f"无法识别视频尺寸：{path.name}") from exc


def merge_scene_videos(request: gr.Request, *values):
    username = _request_username(request)
    output_dir = _user_output_dir(username)
    entries = _local_video_entries(values, username)
    if not entries:
        raise gr.Error("当前没有可合并的成功分镜视频。")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not shutil.which("ffprobe"):
        raise gr.Error("一键合并需要安装 FFmpeg 和 ffprobe，并确保它们位于 PATH 中。")

    output_dir.mkdir(parents=True, exist_ok=True)
    merged_path = (output_dir / f"merged_{time.strftime('%Y%m%d_%H%M%S')}.mp4").resolve()
    with tempfile.TemporaryDirectory(prefix="merge_", dir=output_dir) as temp_dir:
        temp_path = Path(temp_dir)
        normalized_paths = []
        first_width, first_height = _video_dimensions(entries[0][1])
        width, height = (
            (1280, 720) if first_width >= first_height else (720, 1280)
        )

        for index, (scene_number, source) in enumerate(entries):
            normalized = temp_path / f"part_{index:02d}.mp4"
            has_audio = _has_audio(source)
            command = [ffmpeg, "-y", "-i", str(source)]
            if not has_audio:
                command.extend(
                    ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
                )
            command.extend(
                [
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a:0" if has_audio else "1:a:0",
                    "-vf",
                    (
                        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30"
                    ),
                    "-c:v",
                    "libx264",
                    "-preset",
                    "medium",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                ]
            )
            if has_audio:
                command.extend(["-af", "apad"])
            command.extend(["-shortest", str(normalized)])
            result = subprocess.run(command, capture_output=True, text=True, check=False)
            if result.returncode:
                raise gr.Error(
                    f"标准化分镜 {scene_number} 失败：{result.stderr[-2000:]}"
                )
            normalized_paths.append(normalized)

        concat_list = temp_path / "concat.txt"
        concat_lines = []
        for path in normalized_paths:
            escaped_path = str(path).replace("'", "'\\''")
            concat_lines.append(f"file '{escaped_path}'\n")
        concat_list.write_text("".join(concat_lines), encoding="utf-8")
        result = subprocess.run(
            [
                ffmpeg,
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_list),
                "-c",
                "copy",
                str(merged_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            raise gr.Error(f"合并分镜失败：{result.stderr[-2000:]}")

    return str(merged_path)


def load_account_settings(request: gr.Request):
    username = _request_username(request)
    user = _get_user_record(username)
    is_admin = user.get("role") == "admin"
    configured = bool(str(user.get("api_key", "")).strip())
    model = str(user.get("model") or MODEL)
    unsupported_model = model not in MODEL_CHOICES
    if unsupported_model:
        model = MODEL
    role_label = "超级管理员" if is_admin else "普通用户"
    status = (
        f"当前账号：`{username}`（{role_label}）。此前保存的模型已不在可选列表中，请选择模型并保存。"
        if unsupported_model
        else (
            f"当前账号：`{username}`（{role_label}）。百炼配置已完成。"
            if configured
            else f"当前账号：`{username}`（{role_label}）。首次使用前请填写模型、API Key 和接口地址。"
        )
    )
    return (
        model,
        str(user.get("synthesis_url") or SYNTHESIS_URL),
        str(user.get("task_url_template") or TASK_URL_TEMPLATE),
        status,
        gr.update(visible=is_admin),
        _format_user_list(_read_user_store()) if is_admin else "",
        gr.update(visible=is_admin),
        _operation_log_table_data() if is_admin else [],
    )


def save_account_settings(
    model: str,
    api_key: str,
    synthesis_url: str,
    task_url_template: str,
    request: gr.Request,
):
    username = _request_username(request)
    model = (model or "").strip()
    synthesis_url = (synthesis_url or "").strip()
    task_url_template = (task_url_template or "").strip()
    if model not in MODEL_CHOICES:
        raise gr.Error("请选择支持的百炼模型。")
    _validate_endpoint_urls(synthesis_url, task_url_template)

    with _user_store_lock:
        store = _read_user_store()
        user = store["users"].get(username)
        if not user:
            raise gr.Error("当前账号不存在，请重新登录。")
        saved_api_key = (api_key or "").strip() or str(user.get("api_key", "")).strip()
        if not saved_api_key:
            raise gr.Error("请填写百炼 API Key。")
        user.update(
            {
                "model": model,
                "api_key": saved_api_key,
                "synthesis_url": synthesis_url,
                "task_url_template": task_url_template,
            }
        )
        _save_user_store(store)
    return "百炼配置已保存。API Key 留空时会保留已保存的 Key。"


def change_own_password(
    current_password: str,
    new_password: str,
    confirm_password: str,
    request: gr.Request,
):
    username = _request_username(request)
    if len(new_password or "") < 8:
        raise gr.Error("新密码至少需要 8 个字符。")
    if new_password != confirm_password:
        raise gr.Error("两次输入的新密码不一致。")
    with _user_store_lock:
        store = _read_user_store()
        user = store["users"].get(username)
        if not user or not hmac.compare_digest(
            str(user.get("password", "")), current_password or ""
        ):
            raise gr.Error("当前密码不正确。")
        user["password"] = new_password
        _save_user_store(store)
    return "登录密码已更新。"


def _format_user_list(store: dict) -> str:
    rows = ["| 用户名 | 权限 | 百炼配置 |", "|---|---|---|"]
    for username, user in sorted(store["users"].items()):
        role = "超级管理员" if user.get("role") == "admin" else "普通用户"
        configured = "已配置" if str(user.get("api_key", "")).strip() else "待配置"
        rows.append(f"| `{username}` | {role} | {configured} |")
    return "\n".join(rows)


def admin_user_list(request: gr.Request):
    _require_admin(request)
    return _format_user_list(_read_user_store())


def admin_create_user(username: str, password: str, request: gr.Request):
    _require_admin(request)
    username = (username or "").strip()
    if username in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", username):
        raise gr.Error("用户名只能包含英文字母、数字、点、下划线和连字符，长度为 1–32。")
    if username == "admin":
        raise gr.Error("admin 是保留的超级管理员账号。")
    if len(password or "") < 8:
        raise gr.Error("初始密码至少需要 8 个字符。")

    with _user_store_lock:
        store = _read_user_store()
        if username in store["users"]:
            raise gr.Error("该用户名已存在。")
        store["users"][username] = {
            "password": password,
            "role": "user",
            "model": MODEL,
            "api_key": "",
            "synthesis_url": SYNTHESIS_URL,
            "task_url_template": TASK_URL_TEMPLATE,
        }
        _save_user_store(store)
    return f"账号 `{username}` 已创建；首次登录时需自行填写百炼配置。", _format_user_list(store)


def admin_reset_password(username: str, password: str, request: gr.Request):
    _require_admin(request)
    username = (username or "").strip()
    if len(password or "") < 8:
        raise gr.Error("新密码至少需要 8 个字符。")
    with _user_store_lock:
        store = _read_user_store()
        user = store["users"].get(username)
        if not user:
            raise gr.Error("找不到该账号。")
        user["password"] = password
        _save_user_store(store)
    return f"账号 `{username}` 的密码已重置。", _format_user_list(store)


def admin_delete_user(username: str, request: gr.Request):
    _require_admin(request)
    username = (username or "").strip()
    if username in {"", "admin"}:
        raise gr.Error("不能删除 admin 超级管理员账号。")
    with _active_user_lock:
        if username in _active_generation_users:
            raise gr.Error("该账号当前有生成任务运行，请任务结束后再删除。")
    with _user_store_lock:
        store = _read_user_store()
        if username not in store["users"]:
            raise gr.Error("找不到该账号。")
        del store["users"][username]
        _save_user_store(store)
    with _scene_files_lock:
        _user_scene_files.pop(username, None)
    return f"账号 `{username}` 已删除；已有视频文件不会自动删除。", _format_user_list(store)


with gr.Blocks(title="阿里云百炼批量分镜视频生成") as demo:
    with gr.Row():
        with gr.Column(scale=12):
            gr.Markdown(
                "# 阿里云百炼批量分镜视频生成\n"
                "分别填写最多 5 个分镜；未上传首帧的分镜会自动跳过。"
                f"任务按序号逐个生成；全站最多同时运行 {MAX_CONCURRENT_BATCHES} 个批次，"
                "每个账号最多运行 1 个批次。"
            )
        with gr.Column(scale=1, min_width=80):
            gr.Button("退出", link="/logout", variant="secondary")
    with gr.Accordion("账号与百炼配置", open=True):
        account_status = gr.Markdown()
        with gr.Row():
            model_input = gr.Dropdown(
                label="百炼模型名称",
                choices=MODEL_CHOICES,
                value=MODEL,
                multiselect=False,
                allow_custom_value=False,
                filterable=False,
            )
            api_key_input = gr.Textbox(
                label="百炼 API Key",
                type="password",
                placeholder="首次配置时填写；留空则保留已保存的 Key",
            )
        synthesis_url_input = gr.Textbox(
            label="视频生成接口地址",
            value=SYNTHESIS_URL,
            lines=2,
        )
        task_url_input = gr.Textbox(
            label="任务查询地址模板",
            value=TASK_URL_TEMPLATE,
            info="地址中必须包含 {task_id}，并与模型和 API Key 所属地域匹配。",
        )
        save_account_button = gr.Button("保存百炼配置")
        config_save_status = gr.Markdown()
        save_account_button.click(
            fn=save_account_settings,
            inputs=[model_input, api_key_input, synthesis_url_input, task_url_input],
            outputs=config_save_status,
        )

        gr.Markdown("### 修改登录密码")
        with gr.Row():
            current_password_input = gr.Textbox(label="当前密码", type="password")
            new_password_input = gr.Textbox(
                label="新密码（至少 8 个字符）", type="password"
            )
            confirm_password_input = gr.Textbox(
                label="确认新密码", type="password"
            )
        change_password_button = gr.Button("更新登录密码")
        password_change_status = gr.Markdown()
        change_password_button.click(
            fn=change_own_password,
            inputs=[
                current_password_input,
                new_password_input,
                confirm_password_input,
            ],
            outputs=password_change_status,
        )

    with gr.Group(visible=False) as admin_panel:
        gr.Markdown("## 超级管理员：账号管理")
        admin_user_table = gr.Markdown()
        refresh_users_button = gr.Button("刷新账号列表")
        refresh_users_button.click(
            fn=admin_user_list,
            inputs=[],
            outputs=admin_user_table,
        )
        with gr.Accordion("创建普通用户", open=False):
            with gr.Row():
                create_username_input = gr.Textbox(label="用户名")
                create_password_input = gr.Textbox(
                    label="初始密码（至少 8 个字符）", type="password"
                )
            create_user_button = gr.Button("创建账号")
            create_user_status = gr.Markdown()
            create_user_button.click(
                fn=admin_create_user,
                inputs=[create_username_input, create_password_input],
                outputs=[create_user_status, admin_user_table],
            )
        with gr.Accordion("重置用户密码", open=False):
            with gr.Row():
                reset_username_input = gr.Textbox(label="用户名")
                reset_password_input = gr.Textbox(
                    label="新密码（至少 8 个字符）", type="password"
                )
            reset_password_button = gr.Button("重置密码")
            reset_password_status = gr.Markdown()
            reset_password_button.click(
                fn=admin_reset_password,
                inputs=[reset_username_input, reset_password_input],
                outputs=[reset_password_status, admin_user_table],
            )
        with gr.Accordion("删除普通用户", open=False):
            delete_username_input = gr.Textbox(label="用户名")
            delete_user_button = gr.Button("删除账号")
            delete_user_status = gr.Markdown()
            delete_user_button.click(
                fn=admin_delete_user,
                inputs=delete_username_input,
                outputs=[delete_user_status, admin_user_table],
            )

    scene_inputs = []
    scene_videos = []
    scene_statuses = []
    for scene_number in range(1, SCENE_COUNT + 1):
        with gr.Accordion(f"分镜 {scene_number}", open=scene_number == 1):
            with gr.Row():
                with gr.Column():
                    image = gr.Image(
                        label="首帧图片",
                        type="filepath",
                        sources=["upload"],
                        image_mode="RGB",
                    )
                    prompt = gr.Textbox(
                        label="视频提示词",
                        placeholder="描述画面中的人物动作、场景变化和镜头运动",
                        lines=4,
                    )
                    dialogue = gr.Textbox(
                        label="对白 / 旁白（可选）",
                        placeholder="输入本分镜需要生成的对白或旁白",
                        lines=3,
                    )
                    generate_audio = gr.Checkbox(
                        label="生成视频音轨",
                        value=True,
                        info="语音内容及口型同步可能存在误差。",
                    )
                    orientation = gr.Radio(
                        choices=["横屏 16:9", "竖屏 9:16"],
                        value="竖屏 9:16",
                        label="画面方向",
                    )
                    duration = gr.Slider(
                        minimum=2,
                        maximum=30,
                        step=1,
                        value=5,
                        label="视频时长（秒）",
                    )
                with gr.Column():
                    scene_videos.append(gr.Video(label="生成结果"))
                    scene_statuses.append(
                        gr.Textbox(label="任务状态", interactive=False)
                    )
            scene_inputs.extend(
                [image, prompt, dialogue, generate_audio, orientation, duration]
            )

    generate_all = gr.Button("批量生成分镜视频", variant="primary")
    with gr.Row():
        download_zip_button = gr.Button("批量下载分镜视频（ZIP）")
        merge_button = gr.Button("一键下载合并视频")
    zip_file = gr.File(label="分镜视频压缩包")
    merged_file = gr.File(label="合并后完整视频")

    with gr.Group(visible=False) as operation_log_panel:
        gr.Markdown(
            "## 视频生成操作日志\n"
            f"表格显示最近 {MAX_OPERATION_LOG_ROWS} 条记录；Excel 导出包含完整日志。"
        )
        operation_log_table = gr.Dataframe(
            value=[],
            headers=list(OPERATION_LOG_HEADERS),
            datatype=["str"] * len(OPERATION_LOG_HEADERS),
            interactive=False,
            wrap=True,
        )
        with gr.Row():
            refresh_operation_log_button = gr.Button("刷新操作日志")
            export_operation_log_button = gr.Button("导出完整日志 Excel")
        operation_log_file = gr.File(label="操作日志 Excel 文件")
        refresh_operation_log_button.click(
            fn=_admin_operation_log_table,
            inputs=[],
            outputs=operation_log_table,
        )
        export_operation_log_button.click(
            fn=export_operation_log_excel,
            inputs=[],
            outputs=operation_log_file,
        )

    batch_outputs = []
    for scene_video, scene_status in zip(scene_videos, scene_statuses):
        batch_outputs.extend([scene_video, scene_status])
    batch_outputs.extend([zip_file, merged_file])

    generate_all.click(
        fn=generate_batch,
        inputs=scene_inputs + scene_videos,
        outputs=batch_outputs,
        concurrency_limit=MAX_CONCURRENT_BATCHES,
    )
    download_zip_button.click(
        fn=download_scene_videos,
        inputs=scene_videos,
        outputs=zip_file,
    )
    merge_button.click(
        fn=merge_scene_videos,
        inputs=scene_videos,
        outputs=merged_file,
    )
    demo.load(
        fn=load_account_settings,
        inputs=[],
        outputs=[
            model_input,
            synthesis_url_input,
            task_url_input,
            account_status,
            admin_panel,
            admin_user_table,
            operation_log_panel,
            operation_log_table,
        ],
    )


if __name__ == "__main__":
    _ensure_user_store()
    _ensure_operation_log()
    demo.queue().launch(
        server_name=SERVER_HOST,
        server_port=SERVER_PORT,
        auth=_authenticate,
        blocked_paths=[
            str(USER_FILE.resolve()),
            str(OPERATION_LOG_FILE.resolve()),
        ],
    )
