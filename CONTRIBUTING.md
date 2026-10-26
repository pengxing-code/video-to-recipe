# 贡献说明

请优先提交能够复现的问题：操作系统、Python版本、脚本命令（去掉私有路径）、报错，以及期望/实际结果。来源映射问题可用空白占位视频文件和虚构文件名复现，不必公开真实视频。

修改脚本后运行：

```bash
python -m unittest discover -s tests -v
python scripts/prepare_videos.py --help
python scripts/extract_evidence_frames.py --help
python scripts/reconcile_progress.py --help
```

测试不下载模型、不使用云端AI、不处理个人媒体。涉及实际转写质量的变更，请额外说明使用的模型、语言和人工核对结果；不要把占位测试当作模型效果证明。

提交PR时说明具体问题、行为变化与验证范围。贡献须与MIT许可兼容，不提交凭据、私有来源路径、受版权限制的素材或大型模型。
