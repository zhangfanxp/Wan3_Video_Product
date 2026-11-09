# 阿里云百炼批量分镜视频生成器

当前版本：**v1.02**

基于 Gradio 和阿里云百炼 Wan3.0 视频生成接口。最多可填写 5 个分镜，每个分镜可单独设置首帧图片、视频提示词、对白/旁白、音轨、横竖屏和时长。生成后可下载分镜 ZIP，或按分镜顺序合并为一个视频。

## 功能

- 每次最多批量生成 5 个分镜，未上传首帧的分镜会跳过。
- 分镜任务按顺序执行；单个分镜失败会记录错误并继续后续分镜。
- 支持为每个分镜单独设置对白/旁白和是否生成音轨。
- 批量下载成功分镜，或按序合并成功的视频。
- 每次成功的分镜会记录任务 ID 和本地视频绝对路径。
- 可在本机运行，也可供可信局域网内的设备访问。

## macOS 安装与运行

### 1. 安装系统依赖

需要 Python 3.10 或更新版本。视频合并功能还需要 FFmpeg。

如尚未安装 Homebrew，请先按 [Homebrew 官网](https://brew.sh/) 的说明安装，然后在终端运行：

```bash
brew install python ffmpeg
python3 --version
ffmpeg -version
ffprobe -version
```

### 2. 获取项目并创建虚拟环境

从 GitHub 克隆项目（将地址替换为你自己的仓库地址），或在终端切换到项目目录：

```bash
git clone https://github.com/<你的用户名>/<你的仓库名>.git
cd <你的仓库名>
```

创建并启用隔离的 Python 虚拟环境，然后安装 Python 依赖：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

以后每次打开新终端运行项目，都需要先进入项目目录并执行：

```bash
source .venv/bin/activate
```

### 3. 配置阿里云百炼 API Key

复制示例配置文件：

```bash
cp .env.example .env
```

用 macOS 自带的文本编辑器 `nano` 打开：

```bash
nano .env
```

将 `DASHSCOPE_API_KEY=your_new_api_key_here` 中等号右侧替换为你在阿里云百炼控制台创建的 API Key。保存并退出：按 `Control+O`、回车保存，再按 `Control+X` 退出。

**不要把真实 API Key 写入代码、README、截图或 GitHub。** `.env` 已被 Git 忽略；`.env.example` 只能保留占位符。若 API Key 曾经发布到聊天、截图或代码仓库，应立即在阿里云控制台撤销并重新创建，再更新本机 `.env`。

确认模型、Endpoint URL 和 API Key 属于同一地域。默认配置是项目现有的北京地域示例；若你的模型或工作空间使用其他地域，请将 `.env` 里的 `DASHSCOPE_VIDEO_URL` 和 `DASHSCOPE_TASK_URL_TEMPLATE` 更新为百炼控制台对应地域的地址。

### 4. 启动应用

```bash
python app.py
```

本机浏览器访问 <http://127.0.0.1:7860>。按 `Control+C` 可停止服务。

## 局域网访问

默认服务监听 `0.0.0.0:7860`。让其他设备连接到同一可信局域网，再在浏览器打开：

```text
http://运行应用的Mac电脑局域网IP:7860
```

macOS 可在“系统设置 → Wi-Fi → 当前网络详情”中查看局域网 IP，也可在终端运行：

```bash
ipconfig getifaddr en0
```

如无法连接，请检查 macOS 防火墙是否阻止 Python 接受传入连接，并确认客户端和 Mac 处于同一个局域网。应用默认**没有登录认证**，访问者可以提交生成任务并消耗你的百炼额度，也可能访问该应用生成的文件。只在可信网络中使用，不要将端口转发到公网。

监听地址和端口可在 `.env` 中设置：

```dotenv
GRADIO_SERVER_NAME=0.0.0.0
GRADIO_SERVER_PORT=7860
```

## 使用批量分镜功能

1. 在每个分镜卡片中上传首帧，填写视频提示词；对白/旁白可留空。
2. 分别选择是否生成音轨、横屏/竖屏和视频时长。
3. 点击“批量生成分镜视频”。任务按序号逐个生成，失败会显示错误但不会中断其他分镜。
4. 点击“批量下载分镜视频（ZIP）”下载成功的视频，压缩包内保留分镜序号。
5. 点击“一键下载合并视频”，将成功分镜按原序号合并。画幅统一为第一个成功分镜的横屏或竖屏比例，不同画幅会缩放并填充；没有音轨的片段以静音补齐。

模型生成的中文语音和口型同步可能不准确，请检查输出；用于正式宣传时可在视频剪辑软件中替换配音或调整字幕。

## 配置变量

`.env` 支持以下变量：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DASHSCOPE_API_KEY` | 无 | 阿里云百炼 API Key，必须在本机 `.env` 设置 |
| `DASHSCOPE_MODEL` | `wan3.0-video-prime` | 视频生成模型 |
| `DASHSCOPE_VIDEO_URL` | 北京地域示例地址 | 异步视频生成接口 |
| `DASHSCOPE_TASK_URL_TEMPLATE` | DashScope 默认任务地址 | 任务状态查询地址，需匹配所用地域 |
| `VIDEO_RESOLUTION` | `480P` | 视频分辨率 |
| `POLL_INTERVAL_SECONDS` | `5` | 任务轮询间隔（秒） |
| `TASK_TIMEOUT_SECONDS` | `1200` | 单个分镜最长等待时间（秒） |
| `VIDEO_OUTPUT_DIR` | `generated_videos` | 视频及下载文件目录 |
| `VIDEO_LOG_FILE` | `logs/generation.log` | 成功分镜的本地日志 |
| `GRADIO_SERVER_NAME` | `0.0.0.0` | Gradio 监听地址 |
| `GRADIO_SERVER_PORT` | `7860` | Gradio 监听端口 |

上传的图片按 Wan3.0 的 `input.media` / `first_frame` 格式传递。对白/旁白会并入生成提示词；勾选音轨时请求 `parameters.audio=true`。

## 视频合并依赖

合并功能需要 `ffmpeg` 和 `ffprobe`。macOS 可运行 `brew install ffmpeg` 安装。没有安装 FFmpeg 时，单个分镜生成和 ZIP 下载仍可使用，但一键合并不可用。

## 文件保存位置

- 原始分镜视频、ZIP 和合并视频：`generated_videos/`
- 成功分镜日志：`logs/generation.log`

日志记录成功时间、分镜序号、任务 ID 和视频绝对路径。以上输出目录及 `.env` 均已加入 `.gitignore`。

## 发布到 GitHub

发布前检查 `.env` 中没有真实密钥以外的误提交内容，并确认它确实被忽略：

```bash
git check-ignore -v .env
git status --short
```

仅提交项目代码和占位配置，不要使用可能把本地密钥、视频或日志一起添加的宽泛操作。首次创建 Git 仓库时，可按以下方式显式添加文件：

```bash
git init
git add README.md app.py requirements.txt .gitignore .env.example
git status --short
git commit -m "Initial project files"
git branch -M main
git remote add origin https://github.com/<你的用户名>/<你的仓库名>.git
git push -u origin main
```

如果 GitHub 仓库已经创建并克隆，则不需要重复运行 `git init`、`git branch` 或 `git remote add`。推送前再次检查 Git 暂存区，确认没有 `.env`、真实 API Key、`generated_videos/` 或 `logs/`。
