# 正式处理链代码结构

本轮只调整职责和依赖，选课范围、音频完整性门禁、识别重试、加密格式、队列协议、复核额度与邮件规则保持原有行为。

## 核心与运行入口

`src` 不再导入 `scripts`。公共算法独立于 Actions 传输和命令行入口：

| 模块 | 职责 |
| --- | --- |
| `src/ai/qwen_model.py` | 已冻结的识别模型、revision 和采样率 |
| `src/ai/qwen_segmentation.py` | VAD 分块和边界文本去重 |
| `src/ai/qwen_quality.py` | 保守质量筛选、生成上限与复核定位 |
| `src/ai/qwen_audio_alignment.py` | 独立 CPU 对齐阶段，模型延迟加载 |
| `src/pipeline/qwen_plan.py` | 不可变原块计划、指纹和严格结果校验 |
| `src/pipeline/runner_budget.py` | 全局与单堂名额、工作量估算 |

原 `scripts/qwen_sharding.py`、`qwen_segmentation.py`、`qwen_quality.py`、`qwen_audio_alignment.py` 是兼容导入路径，指向同一个核心实现，避免旧调用者和检查工具失效。新核心代码直接导入 `src` 路径。

## 正式阶段

`python -m scripts.production_qwen <mode>` 命令保持不变。入口只转交给 `scripts.production.runtime`；原导入路径也保持可用。

| 文件 | 职责 |
| --- | --- |
| `runtime.py` | CLI、错误脱敏、加密/产物传输公共服务及旧 API 适配 |
| `planning.py` | 正式枚举与冻结、受限验证选择 |
| `preparation.py` | 输入恢复、课件和完整音频准备、失败检查点 |
| `recognition.py` | 识别入口、跨 Worker 原结果读取与恢复 |
| `finalization.py` | 缺口兜底、复核及正常 LectureRunner 汇总 |
| `publishing.py` | 单堂增量发布、批次收尾和邮件回执 |
| `recovery.py` | 历史耗时估算、汇总尝试与额度检查点门禁 |

每个阶段接收明确的 `runtime` 服务参数；不同阶段通过这些服务调用，不把函数复制进共享全局空间。传输仍在运行适配层，核心模块不依赖 CLI。30 个阶段函数从原文件提取，除上下文绑定外保留原始语句、调用顺序和注释。

## 显式检查点

`src/data/checkpoint_database.py` 的 `CheckpointDatabase` 对指定状态变更采用显式覆盖：先执行 SQLite 提交，再触发该实例的回调。检查点错误会传回调用方；不回滚已提交的 SQLite 状态，也不影响其他实例。

转录、摘要、处理状态和错误更新会触发课堂检查点；邮件与失败通知的批量成功标记触发回执检查点。普通 metadata 写入不触发回调，避免递归。

邮件发送使用显式 `database_factory` 注入，原默认路径继续使用普通 Database。运行期间不修改 Database 类方法，也不通过 `__getattribute__` 拦截方法。SMTP 已接收但回执未保存时仍可能重复投递，本轮没有引入恰好一次发信承诺。

## Actions 共享配置

- `qwen-cpu-model`：集中固定 CPU torch、qwen-asr 及可选识别依赖的安装。
- `qwen-asr-runtime`：动态与固定 Worker 共用基础依赖、CPU 安装和原缓存。已完成输入的 `SHARD_ID=-1` 继续跳过模型安装。
- `qwen-pilot-runtime`：准备和汇总的完整环境，按需调用 CPU 安装。
- Pool stage 的两个执行步骤合并，保留先验证预约身份再安装推理依赖/提供凭据的顺序。汇总阶段显示名称仍是 `Finalize through LectureRunner with saved quota`，恢复门禁依赖这个名称。

日程仅台北时间 18:00（UTC+8）一次。共享队列、owner、先预约后派发、不重放未知派发和每堂 900 秒/40 段云复核限制没有调整。旧 benchmark 等独立实验入口保留原环境，未开展大范围重写。

## 验证与边界

原有本地 Git/SQLite/加密集成、音频完整性、队列终态、失败恢复与额度回归继续执行。新增边界测试验证核心导入不依赖脚本或初始化模型、旧导入兼容、显式提交回调、CLI 清理范围和 Actions 认证顺序。

这类结构回归不替代真实校园、SMTP 或模型推理验证。本轮不创建新课堂试跑，不改变 Secrets、正式数据或邮件接收人；重构 PR 合并前，main 及已冻结课堂任务继续使用原代码。
