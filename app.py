import base64
import json
import logging
import mimetypes
import os
import shutil
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path
from urllib.parse import quote

import gradio as gr
import requests
from dotenv import load_dotenv

load_dotenv()

MODEL = os.getenv("DASHSCOPE_MODEL", "wan3.0-video-prime")
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
SCENE_COUNT = 5
SERVER_HOST = os.getenv("GRADIO_SERVER_NAME", "0.0.0.0")
SERVER_PORT = int(os.getenv("GRADIO_SERVER_PORT", "7860"))

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("video_generator")


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


def _task_url(task_id: str) -> str:
    return TASK_URL_TEMPLATE.format(task_id=quote(task_id, safe=""))


def _download_video(video_url: str, api_key: str) -> str:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    response = requests.get(
        video_url,
        headers=_authorization_headers(api_key),
        stream=True,
        timeout=(15, 120),
    )
    if not response.ok:
        raise gr.Error(f"下载生成视频失败：{_response_error(response)}")

    fd, filename = tempfile.mkstemp(
        prefix="video_", suffix=".mp4", dir=str(OUTPUT_DIR)
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
):
    prompt = prompt.strip()
    dialogue = dialogue.strip()
    if dialogue:
        prompt = (
            f"{prompt}\n\n对白与声音要求：\n{dialogue}\n"
            "请按上述内容生成自然、清晰的中文语音；如画面中有人物说话，"
            "尽量使口型与对应对白同步。"
        )

    ratio = {"横屏 16:9": "16:9", "竖屏 9:16": "9:16"}[orientation]
    payload = {
        "model": MODEL,
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
            SYNTHESIS_URL,
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
    yield None, f"任务已提交（{task_id}），等待生成……"

    deadline = time.monotonic() + TASK_TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL)
        try:
            status_response = requests.get(
                _task_url(task_id),
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
            video_path = _download_video(video_url, api_key)
            logger.info(
                "scene=%s task_id=%s video_path=%s",
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


def generate_batch(*values):
    api_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
    if not api_key or api_key == "your_new_api_key_here":
        raise gr.Error("请先在 .env 文件中配置 DASHSCOPE_API_KEY。")

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
    yield outputs()
    for scene_number, scene in active:
        image_path, prompt, dialogue, audio, orientation, duration = scene
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
            ):
                if video_path:
                    states[scene_number - 1] = (video_path, message)
                else:
                    states[scene_number - 1] = (None, message)
                yield outputs()
        except Exception as exc:
            error_message = str(exc)
            logger.exception("scene=%s generation failed: %s", scene_number, error_message)
            states[scene_number - 1] = (None, f"生成失败：{error_message}")
            yield outputs()


def _local_video_entries(*values) -> list[tuple[int, Path]]:
    entries = []
    for scene_number, value in enumerate(values, start=1):
        if isinstance(value, dict):
            value = value.get("path") or value.get("name")
        if value and Path(value).is_file():
            entries.append((scene_number, Path(value).resolve()))
    return entries


def download_scene_videos(*values):
    entries = _local_video_entries(*values)
    if not entries:
        raise gr.Error("当前没有可下载的成功分镜视频。")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    archive_path = OUTPUT_DIR / f"scenes_{time.strftime('%Y%m%d_%H%M%S')}.zip"
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


def merge_scene_videos(*values):
    entries = _local_video_entries(*values)
    if not entries:
        raise gr.Error("当前没有可合并的成功分镜视频。")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not shutil.which("ffprobe"):
        raise gr.Error("一键合并需要安装 FFmpeg 和 ffprobe，并确保它们位于 PATH 中。")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    merged_path = (OUTPUT_DIR / f"merged_{time.strftime('%Y%m%d_%H%M%S')}.mp4").resolve()
    with tempfile.TemporaryDirectory(prefix="merge_", dir=OUTPUT_DIR) as temp_dir:
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


with gr.Blocks(title="阿里云百炼批量分镜视频生成") as demo:
    gr.Markdown(
        "# 阿里云百炼批量分镜视频生成\n"
        "分别填写最多 5 个分镜；未上传首帧的分镜会自动跳过。"
        "任务按分镜序号逐个生成，失败的分镜不会阻止后续任务。"
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

    batch_outputs = []
    for scene_video, scene_status in zip(scene_videos, scene_statuses):
        batch_outputs.extend([scene_video, scene_status])
    batch_outputs.extend([zip_file, merged_file])

    generate_all.click(
        fn=generate_batch,
        inputs=scene_inputs + scene_videos,
        outputs=batch_outputs,
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


if __name__ == "__main__":
    demo.queue().launch(server_name=SERVER_HOST, server_port=SERVER_PORT)
