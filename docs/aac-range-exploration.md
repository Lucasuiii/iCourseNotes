# 内嵌 AAC 有界范围获取实验

2026-10-09；基线为本地 `60746ee`，仅在 `codex/aac-range-exploration` 工作树开发。

初始探索入口是 `src/runtime/aac_ranges.py` 和显式 CLI `scripts/explore_aac_ranges.py`。本文前缀部分记录初始实验方法。用户后续授权接入正式流程后，本分支新增 `src/runtime/aac_audio.py`，并在 `AudioDownloader` 的时间轴保留路径默认优先使用 AAC；具体行为见[正式接入说明](aac-production-integration.md)。Actions 未派发，数据库及模型实现未修改。

## 协议与提交边界

`AACRangeTransport` 继承现有 `SignedRangeRelay`，复用 `MediaSource` 的冻结身份、强 ETag／Last-Modified 条件请求、签名更新、会话隔离、单次身份恢复和旧连接池关闭。此实验不启动本地 relay 服务。索引、连续基线和 multipart 都共用同一冻结来源。

默认每批至多 64 个有序、不重叠范围，请求头上限 8 KiB，响应体 16 MiB，索引读取 16 MiB，扩展索引估算 128 MiB，来源总大小至多 64 GiB，样本最多一百万，单次窗口最多 900 秒，选中 AAC 载荷最多 32 MiB。索引按远端 box 头跳过视频 sample table，只读取选中音轨；解析前检查声明长度与预算，完整检查整轨表计数、时间总和与每个音频偏移所在的 mdat。

所有响应发送 identity 编码、禁止自动跟随跳转。来源/validator 在读取正文前核对；multipart 每个 part 必须具有准确范围、冻结总长度与精确载荷长度。允许 part 乱序和有界空白 MIME 前导/尾随；按声明长度切二进制，不按 payload 内类似 boundary 的字节拆分。重复、缺失、额外、合并、重叠、压缩、HTML、截断均拒绝，200/416 不读取意外视频正文。

仅完整验证的批次可以进入窗口缓存；失败批次整体丢弃再重试，先前已验证批次留在函数局部，不向调用者输出。请求结果未知时不追加部分字节，返回只发生在整个窗口通过 `PacketWindow` 原计划重新计算与逐包覆盖检查之后。调用者删除末包或改写时间戳不能生成一个“更短的成功”。没有持久断点/跨进程恢复；这是内存内、单次运行实验。

签名、网络故障和一次会话恢复遵循现有有限策略。来源变化或损坏不能通过回退绕过。实验传输使用 `raw.read1` 让缓慢滴流每次读取后仍能检查总时限；单个阻塞读取仍受 socket timeout 限制。CLI 另有 600 秒 Unix alarm，包含认证、索引、ABBA 与解码；认证请求期限 90 秒，解码子进程各至多 90 秒且不超过剩余期限。总 body 字节包含已读取失败批次，不含 HTTP/TLS 头、认证/API 正文或网络重传。

## 支持范围

| 支持 | 当前明确拒绝 |
| --- | --- |
| 非 fragmented MP4；32/64 位 box；moov 头/尾；stco/co64；stsz 固定/可变与 stz2 4/8/16 bit | moof/mvex/mfra、加密、非 AAC、多音轨、多 sample description、未知表版本/标志、坏边界/计数/偏移 |
| AAC-LC、标准频率表、单/双声道、1024 sample frame；AudioSpecificConfig 严格解析，包括已知 SBR-absent 扩展 | HE-AAC/SBR/PS、PCE、960 frame、显式/未知频率或扩展 |
| 时间基等于 AAC 采样率；正常帧 1024 ticks，最后一帧可为 1–1024 ticks | 非零 ctts、任意 composition 表、中间非常规帧时长/时间缺口、其他时间基 |
| 无 edit list；一个 rate=1、media_time=0 的区间；可在前面有一个 empty edit，偏移必须为整数音轨 tick | priming/非零 media_time、媒体剪接、多段音频编辑、非 1 播放率、非整数偏移 |
| 已知 roll sample group，distance=-1/0，计数完整；有 preroll 时仅从时间零获取前缀 | 需要 preroll 的中途随机起点、其他 sample group/distance |

简单编辑的窗口必须落在音轨与 edit 覆盖范围内。早期前缀阶段未获取整堂；后续授权的整堂参考验证见文末扩展，不声称跨堂字幕、VAD、PPT 或生产时间轴已验证。真实课堂媒体含 **21 ms 前置空白**，忽略它的 ADTS 拼接不能代表保留原时间轴。

## 独立验证方式与证据边界

1. 从同一来源读取 multipart AAC 和连续音视频字节跨度，按同一计划恢复 packet；顺序为 ABBA，无并发。每轮载荷比较一致。
2. 用原音轨 MP4 元数据、原偏移和已选 AAC 建立临时 sparse 文件，禁用缺失的视频轨。FFprobe 独立检查每个 packet 的 pos/size/PTS/DTS/duration/SHA-256；不是自编解析器自己证明自己正确。
3. FFprobe 核验后，仅为解码参考裁短 sample tables，保留原 packet 偏移/时长、AAC config、roll 信息和原 edit list，使参考解码器不会读取尚未获取的整堂尾部。该文件是前缀证据夹具，不是完整媒体。
4. MP4 参考解码使用 `-copyts`，避免禁用视频后音轨首时间被自动归零；ADTS 探针显式加入已解析的简单起点偏移，再使用 `aresample=async=1:first_pts=0`。对比 16 kHz 单声道 f32le PCM，排除较短结果末端 32 个样本的重采样滤波差异，并单独检查两边时长与 packet 覆盖。

这是受支持前缀的 native 时间轴验证。探索脚本仍报告 `timeline_preserved=false`：ADTS 没有 MP4 edit/sample timing。后续正式接入通过已验证索引恢复偏移、裁去末帧填充并检查 PCM 终点，不把 ADTS 本身当作时间容器。连续跨度基线也不是正式整堂下载端到端耗时；不能据此声称生产整堂提速。认证和索引、包装与重试的成本单列。

探索 CLI 的不支持格式由固定错误码交给调用者处理；后续正式接入只在输出前按明确格式／资源形态允许列表回退，并继续绑定同一来源与会话恢复预算。输出后不切换路径。

## 复现

仅在用户明确允许的校园直连条件下执行，参数中的课堂选择和凭据路径保持私密。没有默认课堂、自动扫描、WebVPN 或 Actions 入口。

```bash
PYTHONDONTWRITEBYTECODE=1 /path/to/test-env/bin/python scripts/explore_aac_ranges.py \
  --course '<私密课程参数>' --lecture '<私密课次参数>' \
  --credentials /private/path/.env --credential-loader /private/path/loader-directory \
  --maximum-seconds 900 --network-byte-budget 700000000 \
  --report docs/experiments/anonymous-result.json
```

`--maximum-seconds 900` 必须先通过 60 秒获取、packet 独立参考、解码与时间检查才进入 900 秒；失败退出码为 1，不自动更换源、不调用模型、不发布。凭据加载复用本机 0600/O_NOFOLLOW 加载器，不 source shell，不输出原异常、URL、validator、Cookie、用户名、课号或日期。

所有课堂媒体与 PCM 仅在内存和私密临时目录，退出后清理；仅保留代码、匿名 body 字节/请求/时间/固定错误与结构统计。离线测试使用自生成数据和回环 HTTP，绝不代表账号真实恢复已通过。

## 用户授权的整堂探索扩展

在后续明确授权“尝试获取全量音频”后增加 `scripts/explore_full_aac.py`。此前的 900 秒内存窗口限制保留；整堂使用单独流式计划，每批仍至多 64 范围、256 KiB AAC 载荷、4,096 个 packet，不将整轨 packet/PCM 展开到内存。每个批次验证完毕才写入调用者拥有的私密临时文件。整轨必须具备全部索引 packet、载荷字节数和最后一帧终点，否则不标成功。

整堂探索 CLI 预算为 30 分钟、400 MB 媒体 body、10,000 次范围请求、200 MB AAC 载荷。请求上限的可选最大值扩到 10,000；原默认 2,000 不变。这些探索预算与后续正式获取预算分开。探索数据写入原偏移的 sparse MP4 音轨参考文件、ADTS 和临时逐包哈希表。Sparse 文件虽只写音频，文件系统实际占用还含稀疏块分配，不能把它的磁盘占用等同于 AAC 载荷大小。

FFprobe 流式逐包检查整堂原生 PTS/DTS/duration 与 SHA-256，不将整个 JSON 存入内存。MP4 和 ADTS 分别流式解码，PCM 只做字节数和散列计算，不落盘，也不保留解码散列值。所有参考媒体、ADTS、逐包哈希及 stderr 文件均在本次 0700 临时目录内，并在成功/失败后删除。真实 ASR/LLM、WebVPN、Actions、正式数据和发布仍不在范围内。

整堂实测获取全部 627,068 个 packet，原始 AAC 156.14 MB，总媒体响应体 210.43 MB，总墙钟 383.531 秒。保留原整轨元数据的参考与显式补前置偏移的 ADTS 完整解码 PCM 一致，整堂时长差为 0；先前前缀参考的 21 ms 尾差此次未复现，具体裁短原因仍未定位。未下载完整 MP4 性能基线。详见[整堂实测报告](experiments/aac-full-result-zh.md)。
