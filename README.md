# iCourseNotes

> **项目来源与作者许可**：本项目基于 [LeafCreeper/Fudan_iCourse_Subscriber](https://github.com/LeafCreeper/Fudan_iCourse_Subscriber) 独立维护。原作者已许可保留显著来源链接后迁移；请遵守其传播限制，不在树洞、班级群、大群或社交媒体等可能引起校方注意的渠道发布或推广。详情见[上游许可说明](docs/upstream-permission.md)。

自动检查复旦大学 iCourse 的课程更新，将录播语音、课件和板书整理成中文课程笔记，按课程发送邮件，并提供加密数据查看器。

当前 `main` 已包含 Qwen 本地识别、共享识别队列、作业多帧读图和课程术语库，并保留本地执行及 GitHub Actions 工作流配置。**Actions 仅开放代码检查和前端部署，课堂处理及每日任务保持暂停。** 代码迁移不会解除平台对运行用途的限制；运行位置、凭据与恢复边界见[独立仓库迁移说明](docs/standalone-migration.md)。

个人加密数据查看器：[打开 iCourseNotes](https://lucasuiii.github.io/iCourseNotes/)。页面会识别当前仓库，查看公开仓库的加密笔记只需数据库密钥，也可选择本机密钥文件；密钥仅在浏览器中用于解密。管理操作另行填写仅授权本仓库的个人访问令牌。

仅用于本人有权访问的课程和个人学习。账号、模型密钥、邮箱信息及数据库密钥通过 Actions Secrets 配置；不要公开传播录播、转录或课程笔记。部署前请阅读 [个人部署说明](PERSONAL_DEPLOYMENT.md)。

## 功能与默认行为

| 功能 | 当前行为 |
| --- | --- |
| 课程订阅 | 按课程 ID 扫描；支持课次白名单、固定课时排除和调课日期例外 |
| 本地识别 | **Qwen3-ASR-1.7B**；Silero VAD 跳过长时间无讲话区间，再切成约两分钟的块 |
| 单堂课并行 | 默认共享队列，按预计识别耗时分配；单堂最多 6 台、5 个活跃课次、含调度器合计 15 台 |
| 疑难复核 | 豆包仅处理疑难短片段；每堂课共享 **900 秒／40 段**，不要求用满 |
| 课件与作业 | PPT OCR；作业提示附近多帧直接读图，失败时回退本地 OCR |
| 课程笔记 | 按讲授脉络组织分级标题、正文和公式；保留可靠的作业要求、题号及页码 |
| 术语提示 | 使用人工基础词表；自动候选／确认词库**默认关闭**，同堂冻结词表 |
| 保存与恢复 | 加密数据库、音频哈希和检查点；正式处理链重试复用成功块及复核额度 |
| 历史课程更新 | [先隔离预览，再单独确认覆盖](docs/historical-refresh.md)；加密备份、旧结果校验与邮件回执保留 |
| 邮件 | 每门课程分别发送；HTML 正文及 PDF 附件，PDF 失败时回退 Markdown 附件 |

官方字幕仅作辅助，不替代 Qwen 转录。SenseVoice、FireRed 和 Zipformer 已不作为本地识别后端。

## 处理流程

```mermaid
flowchart LR
    A[扫描与筛选课次] --> B[获取音频并校验完整性]
    B --> C[VAD 与原始分块]
    C --> D[多个 Runner 识别]
    D --> E[本堂全部块完成]
    E --> F[疑难复核与作业读图]
    F --> G[生成笔记并保存]
    G --> H[按开关发布与发信]
```

每堂课完成后独立汇总，无需等待其他课程识别结束；邮件在本批课次处理结束后按课程发送。各块保留源时间戳，汇总统一排序和边界去重。正式音频获取会更新媒体签名、校验字节范围和源标识；存在读取错误或显著时长缺口时停止，避免把截断录播作为完整课堂发布。

正式音频准备默认优先从 MP4 按范围获取内嵌 AAC，逐批验证后流式解码，保留原时间轴；明确不支持的格式在输出前回退到现有 MP4 路径。可设置 `AUDIO_ACQUISITION=mp4` 显式选回旧路径。黑板识别仍按语音线索访问原视频取帧。实现、预算及当前实测边界见 [AAC 正式接入说明](docs/aac-production-integration.md)。

## 快速开始

### 1. 获取代码并确认运行方式

本仓库是独立维护版本，可克隆用于本地开发。以下配置说明介绍保留的 Actions 接口，**不表示本仓库已启用运行**；先阅读[迁移说明](docs/standalone-migration.md)，确定运行位置和平台允许的用途，再配置凭据。首次处理可能包括所有符合筛选条件且尚未处理的历史录播，建议先只订阅一门课程。

### 2. 配置凭据和课程

进入 **Settings → Secrets and variables → Actions**，添加 Repository secrets。

| Secret | 用途 |
| --- | --- |
| `STUID` | 复旦学号 |
| `UISPSW` | UIS 统一身份认证密码 |
| `COURSE_IDS` | 订阅课程 ID，使用英文逗号分隔，如 `12345,23456` |
| `DB_ENCRYPTION_KEY` | 独立随机数据库密钥，用于保存与查看加密数据 |

数据库密钥可在本地终端生成：

```bash
openssl rand -hex 32
```

将输出保存到 Secret，并自行妥善备份。不要提交到仓库，也不要用 UIS 密码代替；丢失密钥后无法读取已有数据库，更换前需要迁移数据。

登录 [iCourse](https://icourse.fudan.edu.cn)，进入课程页面，从页面 URL 获取课程 ID。

![课程 ID 在课程页面 URL 中的位置](docs/courseid.png)

### 3. 配置摘要模型

至少配置以下一个 Secret。文字模型按 [运行配置](src/runtime/config.py) 中的顺序尝试已配置的服务商。

| Secret | 对应服务 | 获取入口 |
| --- | --- | --- |
| `DASHSCOPE_API_KEY` | 此项目中用于 ModelScope 文字模型，名称为历史兼容项 | [ModelScope](https://modelscope.cn/my/myaccesstoken) |
| `DEEPSEEK_API_KEY` | DeepSeek 文字模型及可选作业图片理解 | [DeepSeek Platform](https://platform.deepseek.com/) |
| `GEMINI_API_KEY` | Gemini 文字模型 | [Google AI Studio](https://aistudio.google.com/) |

Qwen ASR 在 Runner 本地执行，不需要语音 API Key。模型可用性、调用额度和费用以对应服务商为准，仓库不承诺免费额度。

作业图片理解使用独立的 DeepSeek 图像路径，不把图片发送到文字模型回退链；未配置或调用失败时使用本地 OCR。自定义 `DASHSCOPE_BASE_URL`、`DEEPSEEK_BASE_URL`、`GEMINI_BASE_URL` 时须确认接口兼容；增加服务商还需同步可复用工作流的密钥声明。

### 4. 配置邮件

日常订阅发信需要以下 Secrets；仅做隔离验证时不会调用 SMTP。

| Secret | 用途 |
| --- | --- |
| `SMTP_EMAIL` | QQ 发件邮箱 |
| `SMTP_PASSWORD` | QQ 邮箱 SMTP 授权码，**不是邮箱登录密码** |
| `RECEIVER_EMAILS` | 收件邮箱；支持逗号、分号或换行分隔 |
| `RECEIVER_EMAIL` | 兼容旧部署的单一收件邮箱；未设置 `RECEIVER_EMAILS` 时使用 |

在 [QQ 邮箱](https://mail.qq.com) 的账户设置中启用 SMTP 并获取授权码。邮件按课程拆分，公式在本地渲染，不通过外部公式图片服务发送笔记内容。

### 5. 先做隔离验证，再启用日常订阅

在 **Actions → iCourse Parallel Pilot → Run workflow** 中选择 `main`。名称保留了 Pilot，但它也是当前日常订阅调用的正式处理入口。

首次验证可填写已订阅课程的 `validation_course_id`，保持 `validation_lecture_rank=1`，选择最新实际可播放录播。该模式跳过未来课次、无录播的假期／调休记录和排除时段，使用独立空课堂数据库执行识别。课程 ID 是可见的工作流输入；账号和密钥仍只填 Secrets。

保持 `publish_results=false`、`send_email=false`，运行后核对选定日期、计划块完成数、转录／摘要状态和复核额度。隔离模式会调用配置的识别与摘要服务、保存加密产物并验证数据库合并，但不写正式 `data` 分支、不发邮件。**未填写单课验证参数时，仍会按订阅范围规划课次**，不要将其误认为只处理最新一堂。

确认配置后，可手动运行 **iCourse Check**，或等待日常定时任务：

- **每天 17:07（北京时间）**：每日主任务，不再安排晚间补跑。
- 如需额外运行，可在 Actions 中手动启动 **iCourse Check**。

`iCourse Check` 开启正式发布和邮件。GitHub 定时任务可能延迟；已完成摘要不会因再次扫描或识别后端更新而自动重算。

## 可选配置

### 课次筛选

| Secret | 示例 | 行为 |
| --- | --- | --- |
| `COURSE_SESSION_RULES` | `12345=周一第1-2节\|周三第6-8节` | 课次白名单；未列出的课程不受白名单限制 |
| `COURSE_SESSION_EXCLUSIONS` | `12345=周一第6-10节` | 固定排除时段，与课次有重叠即排除 |
| `COURSE_SESSION_OVERRIDE_DATES` | `2026-09-20,2026-10-01` | 指定日期绕过白名单，适用于调课；不绕过排除规则 |

白名单和排除规则均支持每行一门课程。排除规则优先于日期例外和定向重跑；配置格式错误会停止任务，不公开打印规则内容。

### 识别与复核

下表列出工作流默认值和密钥配置方式，不表示当前仓库的实际配置状态。API 密钥应添加到 **Settings → Secrets and variables → Actions → Repository secrets**；已配置时无需重复添加。

| 选项 | 默认值／配置方式 | 说明 |
| --- | --- | --- |
| `shard_mode` | `shared` | 全局调度共享 Worker；准备、识别、汇总和发布共用 Runner 额度 |
| `shard_mode=2` | 可选 | 保留原固定两路流程；块数不足时减少 Runner |
| `shard_mode=auto` | 可选 | 待识别音频超过 60 分钟时用三路，否则两路 |
| `shard_mode=shared` | 默认 | 根据原块时长及历史／本堂推理速度估算工作量，目标约 75 分钟 ASR，单堂最多 6 台 |
| `automatic_terms` | `false` | 启用自动术语候选和确认库；仅确认词可供更晚日期的课堂使用 |
| `DOUBAO_ASR_API_KEY` | 可选 Secret | 配置后按需启用疑难音频及作业重点复核；所有分片和重试共享每课 900 秒／40 段 |
| `TAVILY_API_KEY` | 可选 Secret | 配置后按需检索公共知识疑点，每课最多两次，不上传完整课堂材料；未配置也能完成识别与摘要 |

这里的音频量是 **VAD 后原始块的累计时长，包含块间重叠**，不是录播总长度。块队列仍按单堂课隔离；空出的 Runner 名额可以分给其他课次。全局同时最多 1 台调度器和 14 个准备／识别／汇总／发布任务，最多 5 个活跃课次。75 分钟是调度目标，不是完成时限；15 台预算不包含无关 CI 作业。完整规则见 [共享队列](docs/shared-asr-queue.md) 和 [作业复核](docs/homework-review.md)。

人工基础词表位于 [course_glossary.json](prompts/course_glossary.json)。同堂 Qwen、豆包和疑难筛选使用冻结词表，不在识别后用新候选重跑同堂音频。词表是上下文提示，不是全局替换规则，也不证明课堂实际讲过该词。自动词库的证据门槛见 [课程术语库](docs/automatic-course-glossary.md)。

## 保存、恢复与历史数据

正式数据库以加密分片保存于独立 `data` 分支。发布按单堂课范围合并，保留历史摘要、处理状态、删除标记和邮件回执；发生竞争时重新读取远端并普通快进推送，不强制覆盖。

正式分片链的准备音频、输入哈希、识别结果和复核检查点通过加密 Actions artifacts 保留 **7 天**。失败恢复先复用成功块，只补未完成部分；摘要失败不会重跑已完成 ASR，云端调用的预留额度不会因重试重置。检查点缺失、过期、输入不符或额度未知时明确停止，避免默认为一节新课。

共享模式另建 `codex/asr-queue-<run_id>-<task_slot>` 保存加密块队列；`codex/runner-pool-<run_id>` 保存加密派发记录，`codex/runner-pool-owner` 防止旧子任务未结束就开启新批次。`codex/runner-source-<run_id>` 冻结执行代码。这些分支不是正式数据库，当前不会自动清理；清理前须确认任务结束、恢复窗口及所需产物已保留。不要根据“分支不是 main”就直接删除活动队列。

连续失败三次的课次会暂停自动重试，并按邮件配置发送一次失败提醒。**Single Run** 保留定向重跑、导出、删除及暂停课次重试等入口，采用单 Runner 流程，不能当作正式分片链的块级恢复入口。删除课次会清除摘要、转录和 PPT OCR，并保留永久忽略标记；不会在下一次扫描中自动重新识别。

SMTP 不提供恰好一次投递保证；若服务端已收信但客户端未能保存回执，仍可能重复投递。保存与恢复细节见 [正式处理链](docs/parallel-course-pilot.md) 和 [全局 Runner 调度](docs/global-runner-pool.md)。

## 前端查看器

![课程与笔记查看器](docs/frontend.png)

前端是静态页面，读取 `data` 分支的加密数据库，使用用户输入的 `DB_ENCRYPTION_KEY` 在浏览器内解密，不需要 UIS 凭证。PAT 和数据库密钥只保存在当前标签页的 `sessionStorage`；关闭标签页后失效。支持课程订阅编辑、课次查看、导出与删除。

GitHub Pages 部署是可选且**仅手动触发**：配置 Pages 后运行 **Deploy Frontend**。处理课程和接收邮件不依赖 Pages。仓库公开不等于课堂内容可公开；API Key、明文数据库和课程文件不能提交到仓库。

## 验证范围与限制

当前正式链已完成一堂约 109 分钟录播的隔离验证：三个共享 Worker 完成全部 62 块，复核、非空转录和摘要、`LectureRunner` 保存及隔离发布均通过。后续摘要表达、提醒去重和子题号保留有回归测试与 CI 覆盖。[整课运行记录](https://github.com/Lucasuiii/Fudan_iCourse_Subscriber/actions/runs/37410686897)与 [PR #23](https://github.com/Lucasuiii/Fudan_iCourse_Subscriber/pull/23)保留验证证据。

这些结果证明流程连通，不能当作识别准确率听审或严格同输入性能对照。作业截图中的可靠单项会保留到最终摘要，但画面有题号不等于已经布置整份作业，清单完整性和模糊数字仍需核对。课末通知的跨块重点复核、跨课术语升确认和队列自动清理仍有改进空间；本次课堂验证没有执行正式发信或故障注入。

## 文档导航

开发者可先阅读[处理链代码结构](docs/pipeline-architecture.md)，了解核心模块、运行入口与检查点边界。

| 文档 | 内容 |
| --- | --- |
| [个人部署说明](PERSONAL_DEPLOYMENT.md) | Secrets、权限、私密筛选、数据操作和停用 |
| [正式处理链](docs/parallel-course-pilot.md) | 规划、独立汇总、数据库保存、恢复和发布 |
| [Qwen 运行说明](docs/qwen-only-runtime.md) | 本地识别后端、依赖、VAD 与推理边界 |
| [共享 ASR 队列](docs/shared-asr-queue.md) | 动态领块、协调分支、故障恢复 |
| [作业与课务复核](docs/homework-review.md) | 重点音频、多帧读图、题号佐证和摘要保留 |
| [课程术语库](docs/automatic-course-glossary.md) | 基础库、候选库、确认库及同堂冻结 |
| [媒体签名与范围续传](docs/signed-media-transport.md) | 获取完整音频、源标识与读取故障 |
| [隔离分片对照](docs/qwen-sharded-pilot.md) | 保留的实验入口，与正式订阅流程分开 |
| [早期 V2 设计记录](docs/legacy-design.md) | 历史调度与识别方案，不代表当前实现 |

## 开发

Python 依赖在 [requirements.txt](requirements.txt)，本地公式渲染依赖在 [package.json](package.json)。ASR 所需的 CPU 版 PyTorch 和 `qwen-asr` 由 [Actions 运行环境](.github/actions/qwen-pilot-runtime/action.yml) 单独安装；仅执行 `pip install -r requirements.txt` 不构成完整识别环境。

配置测试所需依赖后，可运行隔离测试：

```bash
python -m unittest discover -s tests
```

PDF／邮件渲染测试还需要系统字体、Pango 和 Cairo；环境配置参考 Actions。测试使用临时数据库、合成输入及模拟服务，不等于实际课程调用。修改正式处理逻辑后应区分隔离测试、完整课堂验证和真实发布验证，并保留旧输入与检查点。
