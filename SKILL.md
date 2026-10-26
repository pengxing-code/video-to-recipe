---
name: video-to-recipe
description: Convert one cooking video, multiple local cooking videos, a video folder, or downloaded cloud-share videos into separate searchable Chinese recipe files. Use when Codex needs to transcribe cooking videos locally with faster-whisper, extract or re-extract evidence frames with FFmpeg, recover ingredients and quantities from speech and on-screen text, resume a large batch safely, build a source-based progress ledger, process only 3-4 recipes per batch, or audit that videos, transcripts, and recipes map one-to-one.
---

# 视频转菜谱

在本机转写烹饪视频，并将每个来源视频整理成一份独立中文菜谱。批量任务必须以磁盘现状为准，使用来源编号建立进度，不依赖旧对话中的完成数量。

## 环境

- Python 3.10+：使用当前环境可用的 `python`，依赖见 [README.md](README.md)。
- faster-whisper 模型：默认 `models/faster-whisper-medium`，可用 `--model` 或 `FASTER_WHISPER_MODEL` 指向已下载的本地模型目录。
- 推理：`cpu`、`int8`
- FFmpeg：依次从参数、`FFMPEG_PATH`、`PATH` 和工作区 `tools` 目录发现

路径变化时传入参数覆盖默认值，不要求用户重装。先确认模型与FFmpeg可用；不自动下载用户视频或上传转写。下列 `D:\videos`、`D:\video-work` 等均为示例路径，使用用户提供的实际位置。

## 批量协议

### 1. 准备转写与关键帧

对整个视频目录只启动一次 `scripts/prepare_videos.py`，让模型只加载一次：

```powershell
python "<skill-dir>\scripts\prepare_videos.py" `
  "D:\videos" --output-dir "D:\video-work" --recursive
```

脚本会逐视频更新 `manifest.json` 和 `preparation-heartbeat.json`，复用完整的转写与关键帧，并记录失败项。运行期间每60秒以内读取一次新输出；不要把仍在运行且持续有心跳的任务误判为卡死。

### 2. 每次都重新扫描磁盘

开始生成菜谱前，以及每批完成后，运行：

```powershell
python "<skill-dir>\scripts\reconcile_progress.py" `
  "D:\videos" `
  --transcripts "D:\video-work\transcripts" `
  --recipes "C:\recipe-output" `
  --output "C:\recipe-progress.csv" `
  --batch-size 4
```

只相信本次生成的 CSV 和 `*.next-batch.json`。不要用“菜谱文件总数”代替来源映射，也不要沿用旧对话中的进度描述。

### 3. 每批只处理3-4个来源

读取 `*.next-batch.json` 中列出的来源。正常批次处理3或4个；最后不足3个时处理实际余数。不得一次读取或推理全部剩余视频。

对本批依次执行：

1. 读取完整转写。
2. 查看已有关键帧。
3. 遇到克数、比例、配方表、火候、温度或短暂字幕时，按对应时间补抽证据帧。
4. 写入一份独立 Markdown 菜谱。
5. 立即重新运行 `reconcile_progress.py`。
6. 报告本批新增文件名、完成数和剩余数，再开始下一批。

如果5分钟内没有工具调用、命令输出、心跳或文件变化，停止当前动作并报告具体阻塞点。不要用长时间纯推理代替文件处理。

### 4. 精确补抽证据帧

默认10秒间隔可能漏掉短暂配方字幕。按时间点补抽：

```powershell
python "<skill-dir>\scripts\extract_evidence_frames.py" `
  "D:\videos\example.mp4" --output-dir "C:\evidence" --times 19 20 21 22
```

按时间段生成逐帧图片和联系表：

```powershell
python "<skill-dir>\scripts\extract_evidence_frames.py" `
  "D:\videos\example.mp4" --output-dir "C:\evidence" `
  --start 15 --duration 20 --fps 2 --contact-sheet
```

## 来源与进度规则

- 来源编号必须使用视频文件的精确文件名主干，保留空格，不擅自改成下划线。
- 不同文件夹或扩展名的视频也必须有唯一主干；重名时先重命名再处理，不能覆盖转写或关键帧。
- 每份菜谱必须同时写入来源编号和原视频绝对路径。
- 扫描时可兼容旧菜谱里的空格/下划线差异，但出现歧义必须标为异常，不能猜测。
- 一个来源只有在“视频1个、转写1份、菜谱1份”时才是 `done`。
- 重复菜谱、孤立菜谱、空菜谱、缺失转写和无法解析来源都必须单独报告。

## 证据规则

- 精确数字只能来自语音或画面；无法确认时写“视频未明确”。
- 结合画面修正明显的语音同音错字，但不要凭常见做法补克数、时间或温度。
- 删除广告、福利码、无关聊天、重复感叹和推广口播。
- 标题与实际技法不一致时保留视频菜名，并在备注中说明实际操作。

## 菜谱格式

每份文件使用以下顺序：

```markdown
# 菜名

## 来源
- 来源编号：`视频文件名主干`
- 原视频：`D:\完整路径\视频文件.mp4`
- 时长：视频明确时长
- 转写模型：faster-whisper medium

## 食材
## 做法
## 关键点
## 成菜特点
```

文件名默认使用菜名；重名时使用 `菜名__来源编号.md`。确保步骤中的食材都出现在食材表中。

## 最终验收

全部生成后运行严格审计：

```powershell
python "<skill-dir>\scripts\reconcile_progress.py" `
  "D:\videos" --transcripts "D:\video-work\transcripts" `
  --recipes "C:\recipe-output" --output "C:\recipe-progress.csv" --strict
```

只有脚本退出成功，并且视频数、转写来源数、菜谱来源数、`done` 数完全一致，同时缺失、重复、孤立、空文件和未映射项均为0，才可报告全部完成。
