# 阿里云百炼批量分镜视频生成器

当前版本：**v1.03**

基于 Gradio 和阿里云百炼 Wan3.0 视频生成接口。最多可填写 5 个分镜，每个分镜可单独设置首帧图片、视频提示词、对白/旁白、音轨、横竖屏和时长。生成后可下载分镜 ZIP，或按分镜顺序合并为一个视频。

## 功能

- 每次最多批量生成 5 个分镜，未上传首帧的分镜会跳过。
- 分镜任务按顺序执行；单个分镜失败会记录错误并继续后续分镜。
- 支持为每个分镜单独设置对白/旁白和是否生成音轨。
- 用户登录后可分别配置百炼模型、API Key 和接口地址。
- 模型下拉框提供 `wan3.0-video-prime`（默认）和 `wan3.0-video`。
- 超级管理员可创建、删除普通账号并重置普通账号密码。
- 超级管理员可查看视频生成操作日志，并导出完整 Excel 文件。
- 全站最多同时运行 3 个批量任务；每个账号最多运行 1 个批量任务。
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

### 3. 初始化账号和百炼配置

首次启动会在项目目录创建 `user.json`，并内置一个超级管理员账号。用户登录后再在网页中填写自己的模型、API Key 和接口地址。

如需调整新账号的默认模型或地域，可复制示例配置文件：

```bash
cp .env.example .env
```

用 macOS 自带的文本编辑器 `nano` 打开：

```bash
nano .env
```

模型在网页中通过下拉框选择，默认是 `wan3.0-video-prime`，也可选择 `wan3.0-video`。`.env` 中的视频生成和任务查询地址只作为新账号的初始默认值；用户可以分别修改自己的模型、API Key 和接口地址。

首次启动后使用以下默认超级管理员账号登录：

```text
用户名：admin
密码：1qaz2wsx
```

登录后建议先在“账号与百炼配置”中设置自己的百炼信息并修改初始密码。页面顶部的“退出”按钮会结束当前登录并返回登录页面。超级管理员可在账号管理面板创建普通用户；新用户首次登录时需要填写自己的百炼配置。

`user.json` 保存明文密码和 API Key，已加入 `.gitignore`。不要把该文件提交到 GitHub、截图或分享给其他人。若其中的密钥曾被公开，应立即在百炼控制台撤销并重新创建。

模型、API Key、视频生成接口地址和任务查询地址必须属于兼容的地域及工作空间。

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

如无法连接，请检查 macOS 防火墙是否阻止 Python 接受传入连接，并确认客户端和 Mac 处于同一个局域网。应用需要账号密码登录，但默认 HTTP 连接不加密；只在可信网络中使用，不要将端口转发到公网。若要跨不可信网络使用，请通过 HTTPS 反向代理提供服务。

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

### 多用户与任务队列

- 多个用户可以同时登录并填写任务。
- 全站最多同时运行 3 个批量任务；超过全局名额的请求进入 Gradio 队列等待。
- 同一账号最多同时运行 1 个批量任务；已有任务运行时再次提交会提示稍后重试。
- 每个批次内的分镜仍按序号逐个生成。
- 每个账号的视频、ZIP 和合并结果保存到 `generated_videos/<用户名>/`。
- 账号和任务按用户名区分，适合可信团队内部使用；Gradio 的文件服务不提供完整的逐用户文件权限隔离，不应当作强隔离的多租户平台。
- 用户名限英文字母、数字、点、下划线和连字符；密码至少 8 个字符。

## 视频生成操作日志

- 超级管理员登录后可在页面底部查看最近 500 条视频生成记录，并手动刷新表格。
- 点击“导出完整日志 Excel”会导出全部记录，不受表格 500 条显示上限影响。
- 每次分镜生成尝试记录时间、账号、模型、脱敏 API Key、百炼任务 ID、实际提交的提示词、本地绝对路径、请求时长、分镜序号、画幅、音轨、结果和错误信息。失败的任务也会记录，未获得任务 ID 或文件路径时对应字段留空。
- “视频长度”记录提交给模型的目标秒数，不是用媒体分析工具测得的实际时长。
- API Key 长于 8 个字符时显示前 4 位、星号和后 4 位；长度为 8 位或更短时全部遮蔽。完整 API Key 不写入操作日志。
- CSV 日志保存在项目根目录 `operation_log.csv`；Excel 导出文件也保存在项目根目录，并已加入 `.gitignore`。

提示词和视频绝对路径会写入日志。该日志只在应用界面中向超级管理员开放；使用时仍应将项目目录视为敏感数据。

## 配置变量

`.env` 支持以下变量：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DASHSCOPE_VIDEO_URL` | 北京地域示例地址 | 新账号的视频生成接口默认值 |
| `DASHSCOPE_TASK_URL_TEMPLATE` | DashScope 默认任务地址 | 新账号的任务查询地址默认值 |
| `VIDEO_RESOLUTION` | `480P` | 视频分辨率 |
| `POLL_INTERVAL_SECONDS` | `5` | 任务轮询间隔（秒） |
| `TASK_TIMEOUT_SECONDS` | `1200` | 单个分镜最长等待时间（秒） |
| `VIDEO_OUTPUT_DIR` | `generated_videos` | 视频及下载文件目录 |
| `VIDEO_LOG_FILE` | `logs/generation.log` | 成功分镜的本地日志 |
| `GRADIO_SERVER_NAME` | `0.0.0.0` | Gradio 监听地址 |
| `GRADIO_SERVER_PORT` | `7860` | Gradio 监听端口 |

账号配置保存在项目根目录的 `user.json`。上传的图片按 Wan3.0 的 `input.media` / `first_frame` 格式传递。对白/旁白会并入生成提示词；勾选音轨时请求 `parameters.audio=true`。

模型下拉框提供 `wan3.0-video-prime` 和 `wan3.0-video` 两个选项。

## 视频合并依赖

合并功能需要 `ffmpeg` 和 `ffprobe`。macOS 可运行 `brew install ffmpeg` 安装。没有安装 FFmpeg 时，单个分镜生成和 ZIP 下载仍可使用，但一键合并不可用。

## 文件保存位置

- 原始分镜视频、ZIP 和合并视频：`generated_videos/<用户名>/`
- 用户账号和百炼配置：项目根目录 `user.json`
- 视频生成操作日志：项目根目录 `operation_log.csv`
- 成功分镜日志：`logs/generation.log`

`operation_log.csv` 记录所有分镜生成尝试；`logs/generation.log` 记录应用运行和成功结果。`user.json`、操作日志、导出文件、输出目录及 `.env` 均已加入 `.gitignore`。

## 发布到 GitHub

发布前确认账号文件、密钥、视频和日志都没有进入暂存区：

```bash
git check-ignore -v .env
git check-ignore -v user.json
git check-ignore -v operation_log.csv
git status --short
```

仅提交项目代码和占位配置，不要使用可能把本地账号、密钥、视频或日志一起添加的宽泛操作。首次创建 Git 仓库时，可按以下方式显式添加文件：

```bash
git init
git add README.md app.py requirements.txt .gitignore .env.example
git status --short
git commit -m "Initial project files"
git branch -M main
git remote add origin https://github.com/<你的用户名>/<你的仓库名>.git
git push -u origin main
```

如果 GitHub 仓库已经创建并克隆，则不需要重复运行 `git init`、`git branch` 或 `git remote add`。推送前再次检查 Git 暂存区，确认没有 `.env`、`user.json`、`operation_log.csv`、Excel 导出、真实 API Key、`generated_videos/` 或 `logs/`。
