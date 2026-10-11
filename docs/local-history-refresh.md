# 本机识别与笔记生成

`local-history.command` 是 Mac 的启动入口，底层命令为 `python -m scripts.local_history_refresh`。从 GitHub 克隆 main 后可以在自己的电脑运行；当前本地后端支持 **Apple Silicon / MLX**，Windows CUDA 后端尚未接入。下载、VAD 和 Qwen3-ASR 在本机执行；课件 OCR、作业截图与精选配图沿用共享处理链，摘要、作业视觉读取及理论复核使用配置的云端服务。

## 安装

需要 Git、GitHub CLI、FFmpeg、Apple Silicon Mac 和 Python。本地实际验证环境为 Python 3.13；双块后端固定 `mlx-qwen3-asr==0.4.4`，避免内部解码接口随升级改变。共享模块还使用 Cairo/Pango 原生库；已有 Homebrew 时可先执行 `brew install ffmpeg gh cairo pango`。若遇到找不到已安装 Cairo 的错误，在当前终端设置 `export DYLD_FALLBACK_LIBRARY_PATH="$(brew --prefix)/lib"` 后再启动，不修改系统配置。依赖与项目的普通 CPU/GitHub 运行环境分开安装：

```bash
git clone https://github.com/Lucasuiii/iCourseNotes.git
cd iCourseNotes
python3 -m venv .venv-local
.venv-local/bin/python -m pip install -r requirements-local-mlx.txt
gh auth login
gh auth setup-git
cp .env.example .env.local
chmod 600 .env.local
```

在 `.env.local` 中填写校园账号、`COURSE_IDS`、正式 data 对应的 `DB_ENCRYPTION_KEY`，以及摘要/视觉服务的 API 配置。配图需要 `DEEPSEEK_API_KEY`；可选识别缺口补救使用 `DOUBAO_ASR_API_KEY`。不要为既有 data 重新生成密钥。无需填写 SMTP 配置，本地入口不发邮件。

提前准备好 Qwen3-ASR-1.7B **MLX bf16** 权重和 Silero VAD ONNX。入口不自动下载权重，也不修改它们。默认模型目录为 `~/Library/Application Support/iCourseQwen/models/qwen3-asr-1.7b-bf16`，默认 VAD 为仓库根目录 `silero_vad.onnx`；可以在子命令前用 `--model` 和 `--vad-model` 指定已有路径。使用已有 Python 环境时，设置 `LOCAL_HISTORY_PYTHON` 指向该环境的 Python；否则启动器优先使用 `.venv-local/bin/python`。

私密 env、密钥及 VAD 路径也可放进 `.local-history-refresh/settings.json`，避免每次传参；文件权限须为600，运行目录权限为700。这个设置文件和运行产物均被 Git 忽略：

```json
{
  "env_files": ["/private/login.env", "/private/providers.env"],
  "key_file": "/private/database.key",
  "vad_model": "/path/to/silero_vad.onnx"
}
```

上述路径是占位示例，替换为自己实际文件。没有 settings 时，按下方命令直接传入 `.env.local`。

## 先试一堂

全局参数放在子命令前，恢复时保持相同参数。下面使用双块解码；想用串行改为 `--mlx-batch-size 1`，并新建运行目录。

```bash
./local-history.command --env-file .env.local --mlx-batch-size 2 doctor
./local-history.command --env-file .env.local --mlx-batch-size 2 plan --limit 1
./local-history.command --env-file .env.local --mlx-batch-size 2 run --hours 2
./local-history.command --env-file .env.local --mlx-batch-size 2 status
./local-history.command --env-file .env.local --mlx-batch-size 2 review
```

`doctor` 只检查环境，不登录校园、不加载模型、不调用服务。`plan` 只读正式 data，选择已完成笔记并冻结课程、日期、源码、权重、依赖和输入配置。用 `plan --lecture-id <课次ID>` 精确选择历史课次，可重复；排除课次和私密课表规则继续生效。不可用时不会自动换成别堂。

新课使用明确的 `plan --new-lecture <课次ID> --course-id <课程ID> --date YYYY-MM-DD`。这种计划仅生成本地预览，运行时再次核对校园目录中的课次和日期，不允许历史 `apply`。

`run` 顺序获取课程资源，等待完整音频后执行 VAD 与识别，再生成候选笔记；每块独立保存加密检查点。双块解码使用一个 bf16 模型，音频编码仍串行，每条结果保持自己的顺序、时间位置和原有有界补救。[双块调度说明](local-mlx-batch.md)

重复 `run` 会复用已完成的块；失败课次默认跳过，显式 `--retry-failed` 才会重新进入失败课次。已有不明确的模型请求仍阻止自动重放。时间预算在阶段和块边界检查，正在执行的 API/OCR/GPU 算子可能超过预算。Mac 应接电并保持唤醒。

批量任务使用独立运行目录，每次命令都加同一个 `--run-dir .local-history-refresh/overnight`。`status` 可以在任务持锁时只读阶段和已保存块数；`review` 生成原/新笔记对照，导出摘要与入选图片。完整ASR及整理转录保留在私密候选和识别检查点，不由该入口自动清理。

## 获取与复核

默认 `--audio-mode aac_auto`：从原 MP4 索引定位 AAC 音轨，校验来源、范围、packet覆盖和原时间轴，解码为16 kHz单声道PCM。只有明确不支持的格式才在输出前沿同一来源回退MP4；认证、缺包、来源变化和不完整获取直接停止。可显式用 `--audio-mode mp4`。

默认 `--campus-mode auto` 先做无凭据校园直连探测，不可达才选WebVPN；登录后不自动切换。媒体恢复最多一次现有会话探测和一次新登录，并核对同一身份与来源。该本地升级不改变普通云端入口的恢复上限。

入口在校园登录前只读检查相关 Actions；发现活动任务时记为 `blocked`，任务结束后原命令可继续。用户明确允许同时登录时，对这次 `run` 加 `--allow-active-actions`；该参数不取消或修改云端任务。

默认 `--review-scope theory`：检查摘要的概念、公式、适用条件与推导错误，不要求六类报告、逐字证据或默认二次语音定位。课件OCR、连续/平台截图、作业视觉读取及完整作业图保留；可选图片失败时注明未核实。摘要及自审不显式指定输出额度，仍受服务默认与上下文上限约束；截断或不可解析的回复仍记为复核未完成。

需要旧逐句语音复核时使用 `--review-scope full`，并准备 `torch`、`qwen-asr`、`soundfile` 及本地 `Qwen3-ForcedAligner-0.6B`（由 `--aligner` 指定）。新范围不能直接套到旧失败检查点；更改源码、模型、范围或调度应建立独立新计划。识别缺口策略、题号证据和精选图规则与共享流程一致；未确认题号不会被当成已布置作业。

## 审核后覆盖 data

本地运行默认不发布。历史计划全完成后，`review` 生成私密 `review.md` 和完整覆盖指纹；只审核成功部分须明确使用 `review --completed-only`。阅读对照后才执行：

```bash
./local-history.command --env-file .env.local --mlx-batch-size 2 apply --approval <完整覆盖指纹>
```

`apply` 是唯一写入正式 data 的步骤：备份最新整库，验证所选课次基线及候选，保留其他课次和邮件回执，重新加密分片并整库回读核验，然后普通快进推送。不强推、不重发邮件；不明确的推送结果保留备份，不盲目重推。新课预览不能走此步骤。

源码、启动器和安装说明属于GitHub代码；模型、账号、API密钥、完整音频、明文候选/转录和私密日志属于本机运行资料。`.local-history-refresh/`、`.env.local`、`.venv-local/` 已忽略。运行目录不要放进源码追踪目录。

## 验证范围

已有真实校园课堂验证了Mac端完整音频获取和MLX识别；整堂双块对照与后续批量识别另有本机审核记录。完整流程包含云端服务，批量曾有自审截断、JSON和选图失败，不能把ASR完成等同于整条流水线通过。main接入验证已完成：861项全仓库回归通过，随后23项AAC测试通过（分别检查签名等待和慢响应读取的截止时间）。测试使用模拟模型/API与本地HTTP、SQLite、Git检验恢复和发布边界；本次未再跑课堂或发布旧结果。Windows本地GPU和新安装环境的整堂识别尚未实测。
