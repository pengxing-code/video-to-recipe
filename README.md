# 视频转菜谱 · Video to Recipe

把本地烹饪视频整理成**一视频一菜谱、可搜索、可追溯**的中文 Markdown 知识库。

这是一个可供支持 `SKILL.md` 的 AI 助手使用的技能，附带本地转写、证据抽帧、进度重建脚本。脚本负责准备证据与校验映射；菜谱由助手结合转写和画面编写，**不是运行一条命令就自动完成所有菜谱的独立应用**。

Local cooking videos → local transcripts + evidence frames → source-backed Chinese recipes. A reusable AI skill with resumable preparation and one-to-one progress auditing.

## 能做什么

- 单个视频或目录批量转写：`faster-whisper` 模型只加载一次，默认CPU/int8。
- 定时抽帧、按秒补抽、片段联系表，便于核对短暂字幕中的克数、比例与火候。
- `manifest.json` 和运行心跳显示处理状态；已有完整转写与元数据可复用。
- 每批3–4个来源生成独立菜谱，不把所有视频拼成一份长文。
- 每次按磁盘现状重建CSV和下一批清单，识别缺失、重复、孤立、空文件与来源歧义。
- 用 `--strict` 检查视频、转写、菜谱是否一一对应。结构审计通过不代表所有食材与步骤已正确识别，仍需对照证据检查。

## 安装

需要 Python **3.10+**、可运行的 **FFmpeg**，以及一个已下载的 faster-whisper 模型目录。

```bash
git clone https://github.com/pengxing-code/video-to-recipe.git
cd video-to-recipe
python -m venv .venv
# Windows PowerShell: .\.venv\Scripts\Activate.ps1
# macOS / Linux: source .venv/bin/activate
python -m pip install -r requirements.txt
```

从 FFmpeg 官方分发或系统包管理器安装 FFmpeg，并加入PATH；也可用 `--ffmpeg` 指定路径。项目不打包FFmpeg、模型或原始视频。

已有本地模型时，使用 `--model /path/to/faster-whisper-medium`。首次准备模型可在确认下载后运行：

```bash
python -c "from huggingface_hub import snapshot_download; snapshot_download('Systran/faster-whisper-medium', local_dir='models/faster-whisper-medium')"
```

模型较大，需要网络与本地空间。准备脚本只读取本地模型，不会自动联网下载模型。可通过 `FASTER_WHISPER_MODEL` 环境变量更改默认目录。

作为技能使用时，将本仓库中 `SKILL.md`、`agents/`、`scripts/` 一起放入助手的 `video-to-recipe` 技能目录；或让支持读取技能文件的助手直接读取仓库中的 `SKILL.md`。不同宿主的技能安装位置由宿主决定。

## 快速开始

以下命令从仓库根目录运行，路径均可替换。先建立 `recipe-output/` 目录，放入你有权处理的视频到 `videos/`。

**1. 准备转写和关键帧**

```bash
python scripts/prepare_videos.py ./videos --output-dir ./video-work --recursive
```

默认每10秒抽取一帧。处理状态见 `video-work/manifest.json`，运行心跳见 `video-work/preparation-heartbeat.json`。不指定 `--overwrite` 时复用已有输出；来源文件改变后应换输出目录或显式重做，脚本不使用内容指纹判断变化。

**2. 重建进度，取下一批**

```bash
python scripts/reconcile_progress.py ./videos --transcripts ./video-work/transcripts --recipes ./recipe-output --output ./recipe-progress.csv --batch-size 4 --recursive
```

读取 `recipe-progress.next-batch.json` 的 `next_batch`。由AI助手按 `SKILL.md`，逐项读完整转写、看关键帧、补查字幕，写一份独立菜谱。每批通常3–4份；末批按实际余数处理。

**3. 缺少关键证据时补抽**

```bash
python scripts/extract_evidence_frames.py ./videos/example.mp4 --output-dir ./evidence/example --times 19 20 21 22
python scripts/extract_evidence_frames.py ./videos/example.mp4 --output-dir ./evidence/example-range --start 15 --duration 20 --fps 2 --contact-sheet
```

不同片段使用不同输出目录，避免把上次的缓存图片当成当前片段。联系表可能有多页。

**4. 每批完成后重新扫描，最后严格验收**

```bash
python scripts/reconcile_progress.py ./videos --transcripts ./video-work/transcripts --recipes ./recipe-output --output ./recipe-progress.csv --recursive --strict
```

退出码：`0`=扫描完成（严格模式下全部一一对应）；`1`=处理失败或严格审计未通过；`2`=输入、环境或参数问题。普通扫描模式退出0不表示菜谱全部完成，必须查看输出进度。

## 菜谱长什么样

```markdown
# 菜名

## 来源
- 来源编号：`example`
- 原视频：`/your/local/videos/example.mp4`
- 时长：视频明确时长
- 转写模型：faster-whisper medium

## 食材
- 按语音或画面记录；不明确的用量写“视频未明确”

## 做法
1. 按视频实际顺序整理。

## 关键点
## 成菜特点
```

具体范式见 [菜谱模板](examples/recipe-template.md)。此模板不是一份真实视频的提取结果。

## 不编造配方

精确数字只能来自语音或画面；没有证据就写“视频未明确”。可以修正有画面依据的语音同音错字，但不能凭常见做法补克数、时间或温度。删除广告、福利码与无关闲聊，保留视频菜名及实际技法差异说明。

来源编号使用视频文件名主干，保留空格。不同子目录或扩展名的视频也不能重名，否则脚本会停止，避免输出覆盖。菜谱重名使用 `菜名__来源编号.md`。

## 数据与隐私

转写、抽帧与审计脚本在本机运行，不上传视频、音频或转写。若使用云端AI助手来读转写或图片，相关数据的处理取决于该助手，请只提供你有权分享的内容。

生成的清单与菜谱会记录本机来源路径。公开分享结果之前，请自行去除私有路径和不宜公开的素材。仓库忽略常见视频、模型与输出目录；不要把个人媒体、账号凭据或工作台运行数据提交进来。

不包含网盘下载器、登录绕过、DRM处理或自动OCR。云盘素材需由用户先完成合法下载；无口述配方的视频需要助手逐帧查看，无法确认的信息保持未明确。

## 项目文件

```text
SKILL.md                         助手执行流程与证据规则
agents/openai.yaml               技能展示信息
scripts/prepare_videos.py        可恢复本地转写与初步抽帧
scripts/extract_evidence_frames.py  精确抽帧与联系表
scripts/reconcile_progress.py    来源核对、进度CSV与下一批
tests/                          无模型下载的回归测试
```

```bash
python -m unittest discover -s tests -v
```

测试使用临时假素材检查来源映射、重名保护、严格审计与参数行为，不替代真实视频转写验收。开源整理版本目前在 Windows / Python 3.13 验证；CI另外覆盖多系统和多Python版本，实际结果以仓库Actions为准。

## 贡献与许可

欢迎提交可复现的转写、抽帧或来源映射问题，见 [贡献说明](CONTRIBUTING.md)。请勿上传不具备公开权限的原始视频。

本仓库代码与技能说明使用 [MIT License](LICENSE)。FFmpeg、faster-whisper及模型遵循各自许可；MIT许可不授予任何第三方视频或内容的传播权。
