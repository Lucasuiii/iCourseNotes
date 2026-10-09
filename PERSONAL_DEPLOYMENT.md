# 个人部署说明（保留的 GitHub Actions 配置）

本独立仓库迁移时已关闭 Actions，尚未启用每日课堂处理。本文介绍保留的
Ubuntu Runner 配置；先阅读[迁移说明](docs/standalone-migration.md)，确认执行位置
和平台允许的用途，再考虑运行。仓库及 `data` 分支中的加密文件公开可访问，
凭证只能存入对应运行环境的私密配置，不能写入代码或提交。

## 部署前边界

- 仅处理本人有权访问的课程资料。
- GitHub Pages 前端仅在当前标签页的 `sessionStorage` 中保存 PAT 和
  `DB_ENCRYPTION_KEY`，关闭标签页后失效；不要在公共电脑上使用。
- 前端 PAT 只授予当前仓库的 Actions/Secrets 读写和 Contents 只读权限。
- 不公开或转发录播、转录、PPT OCR 与课程摘要。
- 上游更新不会自动进入本独立仓库；合并前应人工审查网络请求和 workflow 变更。

## 需要配置的 Secrets

进入自己的仓库：`Settings -> Secrets and variables -> Actions`，添加：

| Secret | 内容 |
|---|---|
| `STUID` | 复旦学号 |
| `UISPSW` | UIS 密码 |
| `COURSE_IDS` | 每日订阅课程 ID，多个用英文逗号分隔 |
| `COURSE_SESSION_RULES` | 可选；每行一门课程的课次白名单，例如 `12345=周一第1-2节\|周三第6-8节` |
| `COURSE_SESSION_EXCLUSIONS` | 可选；固定排除时段，例如 `12345=周一第6-10节`；优先于白名单、日期例外和定向重跑 |
| `COURSE_SESSION_OVERRIDE_DATES` | 可选；调课/补课日期例外，逗号分隔，例如 `2026-09-20,2026-10-01` |
| `DEEPSEEK_API_KEY` | DeepSeek API Key；首次只配置这一个模型服务即可 |
| `DOUBAO_ASR_API_KEY` | 可选；豆包语音新版控制台的 API Key。仅复核 Qwen 疑难短片段，整课共享 900 秒／40 段；按服务规则消耗额度或计费。 |
| `TAVILY_API_KEY` | 可选。仅当笔记标出可公开核查的术语缺口时使用；每节课最多 2 次基础搜索，不上传整段课堂材料。未配置时不联网检索。 |
| `SMTP_EMAIL` | QQ 发件邮箱 |
| `SMTP_PASSWORD` | QQ 邮箱 SMTP 授权码，不是邮箱登录密码 |
| `RECEIVER_EMAILS` | 接收摘要的邮箱；多个地址用英文逗号、分号或换行分隔 |
| `RECEIVER_EMAIL` | 兼容旧部署；未设置 `RECEIVER_EMAILS` 时使用的单个邮箱 |
| `DB_ENCRYPTION_KEY` | 独立随机数据库密钥，不能与 UIS 密码相同 |

在本地终端生成数据库密钥：

```bash
openssl rand -hex 32
```

只把输出粘贴到 `DB_ENCRYPTION_KEY` Secret。不要把输出发给别人，也不要写入
`.env` 后提交。丢失该密钥将无法解密已有数据库；更换它之前应先做好迁移。

启用云端识别还需在[豆包语音控制台](https://console.volcengine.com/speech/new/)
开通“录音文件识别模型 2.0 标准版”（资源 ID `volc.seedasr.auc`），创建新版
API Key，并将它仅填入 `DOUBAO_ASR_API_KEY` Secret。体验中心的试用额度不等于
API 已开通或 API 账单一定免费；首次运行后请在控制台核对用量与费用。

当前迁移库的 Actions 保持关闭。迁移代码或改为独立仓库不等于获得继续原负载的许可；
确认平台允许的用途及运行方式后，再配置对应环境，不能通过换仓库绕过暂停。

`COURSE_SESSION_RULES` 支持每行一个课程。没有出现在该 Secret 中的课程会处理全部
可播放课次；出现的课程只处理列出的星期和节次。多条规则使用 `|` 分隔，也可填写
`课程ID=全部`。格式错误时任务会在登录和调用模型前停止，且公开日志不会打印规则
内容。

若临时调课落在白名单之外，可把实际上课日期加入
`COURSE_SESSION_OVERRIDE_DATES`。该日期会对所有已配置课程放行；课程处理成功后保留
这个日期也不会重复生成摘要。日期例外不绕过 `COURSE_SESSION_EXCLUSIONS`。

每日任务统一使用 Qwen3-ASR-1.7B 在 Runner 本地识别。Silero VAD 跳过长时间无讲话区间，
保留原时间戳后切为约两分钟块，默认共享队列识别；自动术语默认关闭。
豆包仅接收选中的疑难短音频，整课共享 900 秒／40 段，不上传整个录播或登录签名 URL。
未配置豆包或复核失败时保留本地转写；官方字幕仅作辅助。
正式分片链在识别前校验媒体获取、解码错误及实际时长，不能把显著截断的输入当作整课成功。
每门课程单独发送邮件，正文为 HTML，附件默认 PDF，PDF 生成失败时回退 `.md`。
流程及恢复细节见 [正式处理链](docs/parallel-course-pilot.md)。

每日自动任务只在北京时间 17:07 运行，不再安排晚间补跑。需要额外检查时，
可在 Actions 中手动启动 **iCourse Check**。
已处理课次会由数据库自动跳过；GitHub cron 可能因队列繁忙而延迟，不能作为精确到
分钟的定时器。

同一课次连续失败三次后会暂停自动重试，并向 `RECEIVER_EMAILS` 发送一次私人失败
摘要；摘要只包含课程、课次、失败阶段和次数，不包含异常原文、签名 URL 或凭证。
提醒发送失败时不会标记为已通知，下次任务会继续尝试发送。需要重新处理时，打开
`Actions -> Single Run -> Run workflow`，勾选
`Retry all paused failed lectures`。这是布尔开关，不需要在公开的 workflow 参数里
填写课程或课次 ID。

## 首次试跑

1. 在 `COURSE_IDS` 中暂时只填一门课程，并至少配置一个摘要模型。
2. 打开 `Actions -> iCourse Parallel Pilot -> Run workflow`，选择 `main`。
3. 填写已订阅课程的 `validation_course_id`，保持 `validation_lecture_rank=1`，
   验证最新实际可播放课次。课程 ID 是公开输入，登录信息只填 Secrets。
4. 保持 `publish_results=false`、`send_email=false`；检查选定日期、所有块完成、
   非空转录和摘要、复核额度及模型平台用量。此运行不写正式库、不发邮件。
5. 确认后运行 `iCourse Check` 或等待定时任务，再核对正式邮件与账单；
   无异常后加入其他课程。

未填写单课验证参数时，默认会处理订阅课程中符合筛选条件、尚未处理的已有录播，
不是只处理最新一堂。假期或调休没有实际录播的课次正常跳过；失败课次按上述规则
最多自动尝试三次。GitHub 的 cron 不保证准点执行。

`main` 的本地 ASR 不需要额外语音 API；豆包仅复核疑难片段，整课最多 10 分钟、
20 段，不要求用满。正式链的准备音频与识别检查点加密保留 7 天用于恢复，不能把
Runner 退出理解为所有音频已立即删除。
课程标题、转录/OCR 文本和摘要提示会发送给你配置的模型服务商；生成的摘要会发送
给邮箱服务商。请按课程资料的使用规则和对应服务商的隐私条款决定是否使用。

## 可选：导出或删除数据

在前端课程页可按课次导出或删除。课次选择会先写入一次性 GitHub Secret，公开的
workflow 参数只包含随机请求 ID。删除会清除摘要、转录和 PPT OCR，但保留一个不含
课程内容的永久忽略标记，因此后续每日任务不会重新生成或补发该课次。

## 停用

先在仓库 `Actions` 中禁用 workflows，再撤销模型 API Key 和 SMTP 授权码。若
出现 UIS 异地登录或异常认证重试，应立即停用 workflow 并更改 UIS 密码。
