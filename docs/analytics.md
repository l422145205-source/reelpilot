# AI 任务运行数据：增量采集、SQL 指标与可追溯口径

ReelPilot v0.6 将已有运行日志接入独立 SQLite 分析库。它解决一个具体问题：同一用户请求经过重提、恢复和缓存复用后，如何计算请求数、尝试数和阶段提交次数，而不把它们混为一谈。

这是可执行的数据处理模块，不需要 GPU、模型或新的服务。它补充原有 Agent 执行能力，不改变请求调度、检索、媒体生成或模型输出。

## 运行

```bash
# 服务运行生成的 journal.sqlite 是输入，分析库必须是另一个文件。
reelpilot analytics ingest --source data/jobs/journal.sqlite --warehouse data/metrics.sqlite --source-id local-demo
reelpilot analytics report --warehouse data/metrics.sqlite

# 在新目录生成原创视频，执行失败恢复、成功重提等路径并核对 SQL 输出。
python scripts/validate_analytics.py
pytest tests/test_analytics.py
```

`source-id` 是一个数据库生命周期的非个人标识。同一数据库搬家后沿用该标识；换成另一个数据库或清空历史后应使用新标识。多库使用不同标识，避免相同 job ID 碰撞。分析库和输入库不能相同或指向同一个硬链接。

## 表与粒度

| 层 | 表 / 视图 | 每行代表什么 |
|---|---|---|
| 采集状态 | sources | 一份源库的事件序号、历史摘要和采集时刻 |
| 事件事实 | fact_event | 一个源库中的一条事件，主键 source_id + seq |
| 请求事实 | fact_request | 一个源库中的一个逻辑请求，API 与纯 CLI 分开标记 |
| 阶段事实 | fact_stage | 某请求某阶段当前累计的成功持久化执行次数 |
| 请求指标 | request_metrics | 事件、阶段分别预聚合后关联到请求，避免多对多放大 |
| 汇总 | daily_metrics | 按最早已观察事件的 UTC 日期、请求类型、provider、mode 分组 |
| 时长 | attempt_latency | 最新已确认终态 API 尝试的墙钟时长及 nearest-rank p50/p95 |

SQL 是包内的 [analytics.sql](../src/reelpilot/analytics.sql)，不是文档中的伪代码。事实层保留当前快照，汇总视图随状态修正而变化；**不能还原历史某时点的整张报表**。日期是观察 cohort，不能当作某天完成量或真实用户的留存 cohort。

纯 CLI 任务没有 API admission 计数，`attempts` 为 NULL；不拿阶段提交数补造请求尝试数。API 的 complete 表示回执完成，不能仅据此推断输出过视频。

## 增量与失败一致性

1. 用 `mode=ro`、`query_only` 和同一个显式读事务提取源库已提交快照。不会调用会创建/迁移源表的 Journal 构造器，也不修改源数据行或源库配置。WAL 读取仍遵守 SQLite 自身的辅助文件要求。
2. 事件按 **seq** 水位追加，而非 `at` 时间戳。新插入但时间较早的事件仍会被采到。
3. 请求与阶段是可变快照：扫描、计算行摘要，只有摘要变化才 UPSERT。即使没有新事件，状态变化也会进入分析库。
4. 事实写入和 sources 水位处于同一个 `BEGIN IMMEDIATE` 事务。进程异常或故障钩子在提交前中断时一起回滚；下次采集可以安全重试。
5. 用已采事件前缀摘要验证历史没有被删改；同时检查请求身份、计数下降、记录消失和较旧采集覆盖新快照。发生冲突时保留上次成功结果，要求明确处理数据源身份。

当前为本地小规模实现：为了审计历史，每轮读取全部事件前缀及当前快照，**读取量 O(N)**；增量指事实写入和事件追加，不是数据库日志 CDC，也没有宣称分布式或千万级吞吐。多个数据源分次提交，各源快照时刻可能不同。

设计依据：[SQLite 读快照隔离](https://www.sqlite.org/isolation.html)、[SQLite UPSERT](https://www.sqlite.org/lang_upsert.html)。读快照保证同一次采集的表一致；目的库事务保证同一次加载的事实和水位一致，它们解决不同问题。

## 指标解释

| 字段 | 可以说明什么 | 不能说明什么 |
|---|---|---|
| logical_requests | 当前快照的去重请求数 | 用户数、活跃用户数 |
| api_attempts | 已接受的请求尝试数，校验与 admitted 事件数相等 | 被容量拒绝的请求、HTTP 请求总数 |
| complete_after_resubmission | 曾重提且当前完成的逻辑请求数 | 失败恢复率；重提也可能是成功后的重放 |
| stage_commits / render_commits | 持久化成功的阶段/渲染次数 | 所有实际工具调用次数；崩溃前未提交的调用不可见 |
| cache_hits | 检查通过并复用已有阶段的事件数 | token 节省或服务端缓存命中率 |
| inferred_terminal | 原始行仍为活动态，但租约过期后推定的终态数量 | worker 已经退出或取消已经确认 |
| confirmed_terminal | 原始行已保存为终态的数量 | 语义质量达到人工标准 |
| last_attempt_seconds | 最近一次已确认终态尝试，queued→updated 的墙钟秒数 | 所有尝试总耗时、纯推理耗时或纯排队耗时 |

过期租约的推定顺序与 API 一致：先比较取消与 deadline 的先后，再决定 cancelled、timed_out 或 interrupted。推定终态不会进入已确认终态的时长统计。时钟倒退的尝试也排除，并在 quality 字段计数。

不输出统一“成功率”来混合 API/CLI、取消、失败和待处理任务。需要时应先明确分母、观察窗口和来源。当前没有 token、费用或实际模型调用埋点，不编造这些数字。

## 可复现实验

[原始 JSON](../reports/analytics_validation.json) 来自六条刻意覆盖不同路径的本地任务，不是真实业务样本：

| 情况 | API 尝试数 | 当前终态 | 实际媒体处理 |
|---|---:|---|---|
| 渲染提交后受控异常，再恢复 | 2 | complete | 真正执行一次 FFmpeg，重试复用 render |
| 正常完成后原样再提交 | 2 | complete | 真正执行一次 FFmpeg，再提交复用阶段 |
| 无可用证据 | 1 | failed | 编译失败，未生成视频 |
| 排队时请求取消 | 1 | cancelled | 未执行媒体阶段 |
| deadline 到期 | 1 | timed_out | 未执行媒体阶段 |
| 服务关闭 | 1 | interrupted | 保存关闭回执 |

SQL 合计为 **6 个逻辑请求、8 次 API 尝试、2 次 render 提交、7 条缓存复用事件**；两个视频均为真实生成且可探测的 640×360 输出，时长记录在报告。受控 ETL 中断后与旧报表逐字段相等；重采新增事件、请求写入和阶段写入均为 0。源库数据行摘要在采集前后相同。

该实验只验证数据契约和真实媒体执行路径。没有加载模型，不能作为模型质量、用户满意度或线上成功率。CI 在三个 Python/系统组合另行执行同一实验，各运行报告保存在 Actions artifacts。

## 数据最小化

分析库不保存 brief、台词、错误正文、媒体路径、owner token 或原始 job ID。仅保留已知 provider/mode/事件/阶段枚举；未知项归为 unknown/other。job key 由来源标识与 job ID 做摘要，用于关联；它是**伪名化键，不是保证无法猜测的匿名化方案**。完整源记录摘要仅用于一致性检查。

保留 quality 计数：未知事件、无当前请求对应的事件、终态时钟倒退。它们不能被静默解释成质量通过；admission 与尝试数不一致、历史删改等硬错误会直接阻止加载。原始数据库和生成视频不进入 Git；只发布汇总结果与原创媒体实验脚本。

## 面试讲解：四个值得展开的问题

**为什么不能 `requests JOIN stages JOIN events` 后 COUNT？** 一个请求有多阶段、多事件，直接关联会发生 fanout。先按 source/job 对两个事实表分别聚合，再关联到请求，确保每个请求仍只出现一次。测试故意制造 render 提交与 cache_hit，检查请求数始终为 1。

**为什么不能只记 `MAX(updated)`？** 心跳会更新状态行，事件时间可能迟到，多个记录也可能共用时间戳。当前事件用 seq，状态用行摘要，两种可变性分开处理；代价是前缀审计需要全量扫描。

**一个请求最终成功，为什么不一定算“失败恢复成功”？** 原本成功的请求也能被再次提交。现有日志缺少与 owner 强关联的完整历史尝试表，所以只报告“重提后完成”。强行用相邻事件拼历史尝试可能混入延迟追加的旧 worker 事件。

**为什么没有细分纯排队时间和 token 成本？** 当前日志没有足够埋点。下一步应在任务接收、执行开始和模型调用边界记录带 attempt ID 的可靠事件，再讨论时长分解与成本核算，不能用阶段标签填补数据缺失。

可据实用于面试或项目介绍的一句话：**为多模态 Agent 增加基于 SQLite 的运行数据分析链路，按请求/阶段/事件建模，结合序号水位、Hash UPSERT、事务检查点和数据质量校验实现可重跑采集，并通过 SQL 预聚合与窗口函数分析重试、缓存复用及终态时长。** 这描述实现能力，不附加未测得的业务提升。
