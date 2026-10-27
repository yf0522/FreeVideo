# 分镜候选片脚本

`scripts/storyboard_mvp.py` 接收已经确认的分镜、逐镜首帧和可选 WAV 旁白。它通过 FreeVideo 官方 Python Client 逐镜生成画面，再用 FFmpeg 输出 24 fps、带旁白和烧录中文字幕的候选片。

首版固定每镜 5 秒、120 帧，最多 12 镜。它不制作首帧、不合成旁白、不自动替人审片，也不修改 FreeVideo 的模型、更新器或签名校验。

## 准备目录

脚本放在独立 fork checkout 中运行，输入和运行结果放在仓库之外。例如：

```text
/Users/me/work/FreeVideo/                 # fork checkout
/Users/me/video-work/safety-demo/
  input/
    storyboard.json
    frames/s01.png
    audio/s01.wav
  run/                                    # --workdir，脚本创建
```

复制 `docs/examples/storyboard_mvp.json` 到输入目录后再填写。所有素材路径必须是相对 manifest 的路径，不能越出该目录。无旁白镜头写 `"narration": null`。

开始前需要：

- Python 3.12；
- `ffmpeg` 和 `ffprobe`；
- 已安装并启动的 FreeVideo；
- 安装根目录中存在 `agent/endpoint.json` 和 `agent/token`。只把根目录传给脚本，不读取、复制或打印 token；
- 模型授权和实际输出用途符合 MiniMax H3 Community License。

Mac 官方应用会让用户选择安装位置，那个包含 `agent/` 的位置才是 `--freevideo-root`，不是本 fork，也不一定是 `/Applications/FreeVideo.app`。

## 命令

以下命令都从 fork 根目录执行。用实际 Python、FreeVideo 安装目录和外部项目目录替换前三行：

```bash
FV_PYTHON=/opt/homebrew/bin/python3.12
FV_INSTALL=/path/to/FreeVideo-install
FV_PROJECT=/Users/me/video-work/safety-demo

"$FV_PYTHON" scripts/storyboard_mvp.py validate \
  --manifest "$FV_PROJECT/input/storyboard.json"

"$FV_PYTHON" scripts/storyboard_mvp.py render \
  --manifest "$FV_PROJECT/input/storyboard.json" \
  --freevideo-root "$FV_INSTALL" \
  --workdir "$FV_PROJECT/run"

"$FV_PYTHON" scripts/storyboard_mvp.py status \
  --workdir "$FV_PROJECT/run" \
  --freevideo-root "$FV_INSTALL"
```

`validate` 会先检查整个 JSON、全部首帧、全部音频和真实音频时长，不连接服务。尺寸必须符合当前 FreeVideo schema；旁白必须不超过 4.7 秒。任一输入有误时不会提交任何镜头。

`render` 启动时读取服务的 capabilities、schema 和 guide。每个新镜头先 upload、plan，再持久化冻结请求和稳定 ref，最后 submit。提交响应丢失时只用同一 ref 和同一请求重放；已知 job 则始终查询原 job。

只处理一个缺失或变更镜头：

```bash
"$FV_PYTHON" scripts/storyboard_mvp.py render ... --only s02
```

同一输入人工要求重拍一次：

```bash
"$FV_PYTHON" scripts/storyboard_mvp.py render ... --only s02 --redo
```

每组相同画面输入最多一次初拍和一次 `--redo`。仍不合格时应改首帧、提示词、seed 或机位。修改首帧、提示词、seed、生成参数或服务版本只让相关镜头进入新版本；修改旁白文件或字幕文字不会提交画面任务。

失败或中断的上游任务不会自动恢复。明确恢复它：

```bash
"$FV_PYTHON" scripts/storyboard_mvp.py render ... --only s02 --retry-failed
```

按 `Ctrl-C` 会请求取消当前 job、等到终态、保存 `state.json`，并停止提交后续镜头。已经取消的任务不会自动恢复。轮询等待如需由外部调度限制，可加 `--wait-timeout 秒数`；超时只退出等待，不取消也不重提任务。

## 审片和导出

逐镜完整播放 `run/shots/<镜头>/v<版本>.mp4`。确认后在外部 workdir 创建 `review.json`：

```json
{
  "project_id": "safety-demo",
  "shots": {
    "s01": {
      "status": "approved",
      "version": 1,
      "sha256": "从 state.json 对应镜头的 output_sha256 原样复制"
    }
  }
}
```

每个镜头都必须有一条 `approved`。导出会重新核对当前画面输入指纹、镜头版本、磁盘文件 SHA-256 和选片记录；缺失、拒绝、旧版本或文件被替换都会停止，绝不使用旧片或静态占位。

```bash
"$FV_PYTHON" scripts/storyboard_mvp.py export \
  --manifest "$FV_PROJECT/input/storyboard.json" \
  --workdir "$FV_PROJECT/run" \
  --reviews "$FV_PROJECT/run/review.json"
```

导出对每镜准确取前 120 帧，等比缩放并补边到 1280×720，移除生成片原声，在每个 5 秒槽内加入旁白或静音，按音频真实时长写句级 SRT，并烧录中文字幕。优先使用 FFmpeg `subtitles`；Mac 的 FFmpeg 未带 libass 时，使用系统中文字体生成透明字幕图，再由 FFmpeg 叠加。导出不使用 `-shortest`。

主要结果：

- `state.json`：原子保存的项目、镜头版本、稳定 ref、job、hash 和媒体校验；
- `shots/<id>/v<版本>.mp4`：逐镜下载结果，旧版本保留；
- `candidate.mp4`：候选片；
- `subtitles.srt`：句级中文字幕；
- `timing.json`：服务连接、逐镜提交/生成/下载和导出耗时；
- `captions/`：仅在 FFmpeg 无 libass 时生成的字幕图。

同一 workdir 同时只允许一个 render/export 进程。运行数据不应提交到 Git。

## 验收顺序

必须依次进行，不能用 mock 或循环同一镜头冒充真实验收：

1. 先运行行为测试：`python3.12 -m unittest -v scripts.test_storyboard_mvp`。
2. 用一组真实首帧和可选旁白运行 1×5 秒，记录 `timing.json`、设备、FreeVideo 版本和实际可解码结果。
3. 用三个独立镜头完成生成、逐镜选片、配音、字幕、导出、重启恢复和完整播放。
4. 用十二个独立镜头导出 1440 帧候选片，逐镜及全片完整观看；人工检查人物数量、脸、发型、工装、安全帽、场景、动作方向、黑帧、动作尾部、声音和字幕边界。

mock 测试通过不能写成“真实生成通过”；上游 job succeeded 也不能写成“人工验收通过”。三种状态应分别记录。

## 故障处理

- `no_token`：`--freevideo-root` 不是实际安装根目录，或安装尚未完成。
- `unreachable`：启动 FreeVideo 后原命令重跑。脚本会复用 state 中的稳定 ref/job。
- `failed` / `interrupted`：先看 `status` 的错误，再对该镜使用 `--only ... --retry-failed`。
- `cancelled`：不会自动恢复；修改输入后生成新版本，或按当前服务策略人工处理。
- 文件 hash 不符或媒体损坏：脚本不会把它当缓存；有 succeeded job 时会重新下载，否则明确报错。
- 旧 review：从当前 `state.json` 更新版本和 hash，重新完整播放后再批准。
- 服务升级：先做新的 1×5 秒测试。服务版本、schema 或 guide 改变会进入画面输入指纹，旧选片不能批准新生成。

本脚本只调用官方 Agent API，不写 FreeVideo.app、engine-source、source-overlay 或更新包，因此官方更新源和签名校验保持不变。
