# 本地与云端共用识别流程

本地应用和 GitHub Actions 共用内容处理规则。MLX 与 CPU 适配器分别负责模型执行，任务调度和存储由各自运行环境管理。

## 共用调用链

1. 使用 `create_media_client` 创建带受限恢复能力的媒体客户端。
2. 使用 `configure_audio_downloader` 配置 AAC、反相声道检查和会话恢复。
3. 使用现有 VAD 生成约两分钟的检查点块，并冻结音频指纹、边界和术语快照。
4. CPU、MLX 串行和 MLX 双路入口通过 `recognize_with_context` 执行相同的质量规则。内部推理片段由 `split_inference_audio` 统一产生，长度至多30秒，切点靠近低能量位置。
5. 子块拼接前检查提示词回显，记录采样边界与指纹。检测到回显后，在剩余时间内执行一次完整原块的无提示补救。仍然异常时进入已有的有界整块重试、短片段重试和缺口记录。
6. 两端通过 `finalization_material` 将已验证结果交给 `LectureRunner`，继续执行摘要、作业证据、配图、理论审核和一次修订。

## 一处维护的规则

| 内容 | 实现位置 |
|---|---|
| 回显片段识别 | `src/ai/qwen_quality.py` |
| 术语整理、输出预算、无提示补救和子块检查 | `src/ai/recognition_policy.py` |
| VAD 外层块与内部推理切分 | `src/ai/qwen_segmentation.py` |
| 有界重试、检查点块输出 | `src/ai/qwen_transcriber.py` |
| MLX 模型与双路解码适配 | `scripts/local_history/backends.py` |
| 历史术语证据及日期隔离 | `src/ai/automatic_glossary.py` |
| 媒体恢复配置 | `src/api/media_client.py` |
| 音频获取配置与完整性验证 | `src/runtime/audio_preparation.py` |
| 汇总材料边界 | `src/pipeline/prepared_lecture.py` |
| 课件与作业图恢复 | `src/pipeline/summary_figures.py` |

普通识别与无提示补救使用相同的每推理片段2048 token上限。CPU 保留实际生成 token 审计，MLX 保留运行库的截断检查。补救共享本块剩余时间，并受原有累计时限和调用次数限制。

日常云端、手动云端和历史预览新任务默认启用自动术语，与本地应用一致。显式关闭选项仍有效。术语快照优先放入有证据的历史确认术语，再用基础术语填充，最多30项，排除当前课次、未来课次和已删除课次。候选术语的来源核验由 `AutomaticGlossary` 统一负责。

媒体恢复两端共用60秒读取超时、180秒恢复总时限。临时认证失败最多启动两次新流程。单次下载连续恢复预算为3，累计上限为8，沿用经过验证的下载进展才能恢复预算的规则。直连和 WebVPN 分别传入访问方式。

平台课件图与作业图写入现有 `frames_only` 检查点，跨任务后可继续使用。合并缓存按图像指纹、作业线索及时间去重，优先保留作业证据。已有选图请求和完成结果继续使用原来的恢复规则。

## 入口与兼容

- 本地 CLI 使用 `scripts.local_history_refresh`，本地应用通过活动源码目录调用它。
- 云端日常入口为 `check.yml`，进入 `parallel_pilot.yml` 和 production 各阶段。
- 旧冻结任务继续引用其原源码与原计划。新版本启用时创建独立源码目录。
- 旧结果行和检查点格式仍可读取。新增的子块记录是附加审计字段。
- 缺少显式审核范围的历史材料继续按原账本判定，已记录的模型请求沿用原恢复记录。
- 发布和邮件仍由入口的交付配置控制。

## 验证

完整回归通过 `scripts/ci_local.sh` 执行。`test_recognition_policy` 覆盖三个识别入口的一致决策、完整输入补救、子块边界与超时处理。其余测试覆盖实际 CPU 生成审计、MLX 双路补救、日期隔离术语、媒体恢复和跨任务图片恢复。

真实整堂识别的文字质量还需结合课程音频、板书与人工审阅判断。自动化测试验证控制流和数据一致性。
