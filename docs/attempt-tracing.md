# 每次尝试独立归因：重试、执行记录与不完整观测

v0.7 在原有可恢复执行与 SQL 数据集市之上，补充真正绑定每次尝试的运行记录。解决的问题是：同一个 job ID 先失败、再恢复、成功后又被原样提交时，不能把所有事件混成一次执行。

## 新增行为

- 每次成功 admission 创建新的随机 `attempt_id`，并保存该 job 的递增 ordinal。请求回执、admitted 事件和尝试记录在同一个事务中创建。
- 规划、检索器准备以及 compile/verify/render/probe 阶段绑定原始尝试。子进程根据 durable owner 的摘要查回同一 attempt，不依靠“最新 job 状态”猜测归属。
- 操作记录开始时先持久化 open；正常返回保存 success 或 cache_hit，异常保存 error/fenced/cancelled/timed_out。不保存输入文本、输出内容或错误正文。
- SQL 用 `LAG` 对比同一 job 连续 ordinal 的结果，区分“上一次已记录失败后完成”与“上一次成功后重放完成”。缺失旧尝试时不拼接出虚构的历史。
- 本地接口 `GET /api/jobs/{job_id}/attempts` 返回分次尝试与操作详情，省略 owner、owner_key、用户需求与错误正文。它延续本机原型的信任边界，不是多租户鉴权系统。
- 详情接口的尝试与操作各有独立游标（after_attempt / after_span），每页每类最多 50 条；按 ordinal 和插入序号分页，不依赖可能倒退的时间戳。next_after_attempt / next_after_span 分别表示后续页，避免一次读出无限历史。
- 分页提供读取时的快照，不是更新订阅。已经读到 open 的操作若稍后结束，需要重读该页才能看到闭合结果，不能仅靠追加游标获取旧行的状态变化。

## 运行与代码入口

```bash
# 安装和运行服务的方法见主 README。
reelpilot trace-report --source data/jobs/journal.sqlite

# 全量运行契约测试并收集四类真实/受控实验记录；每次创建新 scratch。
python scripts/validate_tracing.py
```

实现：[tracing.py](../src/reelpilot/tracing.py)、[admission.py](../src/reelpilot/admission.py)、[journal.py](../src/reelpilot/journal.py)、[execution.py](../src/reelpilot/execution.py)。报表包含 accepted/recorded/legacy coverage、三种重提转移、启动等待和按 operation/outcome 汇总的观测时长。

`trace-report` 通过只读连接、单个读事务查询源库，输出无任务 ID、原始路径、邮箱或内容的聚合 JSON。[原有 analytics](analytics.md) 保留请求粒度的当前快照分析；本模块补充尝试粒度的历史与诊断，没有把新表伪装成旧表已存在的历史数据。

## 数据契约

| 数据 | 粒度 | 关键约束 |
|---|---|---|
| request_attempts | 一个 job 的一次已接受尝试 | attempt_id 唯一；job+ordinal 唯一；job+owner 摘要唯一 |
| operation_spans | 该次尝试中的一次操作观察 | 记录 attempt_id，不使用 job 的最新 owner 来重新归因 |
| submissions | 一个 job 当前的请求回执 | 原有授权与容量依据；追踪 ID 不能替代 owner |
| stages | 一个 job 某阶段当前的可复用结果 | 仍需要输入摘要、产物 SHA256 和所有权检查 |

`Journal.finalize` 在同一写事务内确认 pipeline、submission 和 attempt 的 complete；最终提交中断则全部回滚。完成后的清理错误仍进入既有 postcommit_error 日志，不反向改变已经完成的尝试。

旧 worker 晚返回时，可关闭它自己原来的操作观察，但不能提交阶段或修改新尝试的完成状态。追踪归因与执行授权是两件事：前者保留实际发生的旧操作，后者必须继续由当前 owner、租约与请求控制裁决。

## 进程死亡和未知结果

新尝试接管尚无终态的旧尝试时，旧记录标为 `superseded`。这只说明所有权已替换，**不能证明旧进程已经退出**，也不自动等于一次已确认失败。

如果 OS 直接杀死 worker，Python 的 finally 不会执行，已经保存的操作记录仍为 open，elapsed_seconds 为 NULL。报表保留观测数量，测量数量为 0，均值/总时长为 NULL。不能把它当成“耗时 0 秒”，也不能在下一次启动时拿当前时间减去旧开始时间充当真实运行耗时。

历史上的尝试没有可靠记录时，`unrecorded_legacy_attempts` 保留缺口；例如升级前已有第 1 次请求，第 2 次才开始追踪，不能仅据“第 2 次成功”断言失败恢复。转移指标只接受相邻且都有真实记录的 ordinal。

## 时间口径

操作时长由**同一进程**进入/退出操作的 `perf_counter` 差值计算；该时钟不受系统时间向后调整影响。墙钟时间仍作为展示字段。[Python 官方时间 API 说明](https://docs.python.org/3/library/time.html#time.perf_counter)

admitted_at→首次 phase 的等待使用墙钟差值，时钟倒退的记录不进入等待统计。线程后端与进程后端的启动边界不同：进程后端的这段等待可能包含模型入口排队和 worker 冷启动。因此它不是可直接比较的纯线程池排队耗时。规划和阶段时长也包含其内部等待、工具执行和部分记录开销，不能写成纯 GPU 计算时间。

目前没有 token、费用、GPU 核函数耗时或独立埋点开销实验。不是 OpenTelemetry 协议实现，也没有网络 exporter；所有运行内容保留在本机 SQLite。

## 实验与可核验结果

[tracing_validation.json](../reports/tracing_validation.json) 收集四个案例，每次 CI 在新目录生成原始数据：

1. **真实恢复与成功重放**：原创素材，显式规划与关键词检索；一条请求在真实渲染提交后受控异常，再恢复；另一条正常完成后重放。共 4 次尝试、2 次 render 提交。SQL 分别识别 1 次已记录失败后完成、1 次已记录成功后完成；六种操作都有实际记录。
2. **操作中 OS 强杀**：真实子进程进入受控 planning 操作后被杀死，留下 open 和未知时长。等待实际租约到期后重提，归类为 supersession 后完成，不假报已记录失败恢复。这个操作是受控休眠，不是模型推理或真实规划性能实验。
3. **真实 FFmpeg 后强杀**：在 render 已提交后杀死 worker，恢复使用新的 attempt ID，render 提交仍为 1；旧 owner 无法恢复授权。
4. **最终发布事务内强杀**：文件替换后、SQL 提交前杀死 worker，未确认结果不对外提供；三层完成状态不被提前写成 complete。恢复后绑定新尝试并复用已验证渲染。

这些是 CPU 执行、归因与一致性证据，没有加载预训练模型，也不是业务成功率或语义质量提升实验。源数据库、marker 文件、用户内容及机器路径不会发布；公开报告只有聚合结果和实验范围。

## 面试时能沿代码展开的问题

**为什么已有 job ID 还需要 attempt ID？** job 表示请求身份，原样重提沿用它；attempt 表示一次实际接收。旧执行的晚返回与新执行可能交错，按 job 的当前状态回填会串线。

**为什么 owner 和 attempt_id 不能共用？** owner 是授权能力，不能在诊断接口公开；attempt_id 是非授权的关联标识。数据库只用 owner 摘要查回关联，不把原始 owner 复制进分析记录。

**为什么不把被接管的旧尝试都计为失败？** 没有收到可靠终态。它可能已经完成了部分工具调用，也可能仍活着。superseded 与 open 是必要的未知状态，缺失不能通过默认值掩盖。

**新增追踪是否会破坏原有恢复？** 完成确认与原事务一致，stage 缓存仍需原有哈希检查；归因失败会让该次执行失败而不是静默缺记录，下一次可以复用已经提交的阶段。它不是独立、无损、零开销的遥测系统。

可以据实描述的项目能力：**实现带 owner 隔离的尝试级运行追踪与 SQL 故障归因，贯通线程/子进程执行，将重试、缓存复用、过期接管与不完整时长分开记录；通过真实媒体和 OS 强杀验证晚返回隔离及完成状态的事务一致性。**
