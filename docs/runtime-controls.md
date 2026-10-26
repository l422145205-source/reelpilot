# 长任务控制：取消、截止时间与进程隔离

v0.3 在 v0.2 的持久请求和阶段恢复之上，解决正在运行的任务如何停止，以及停止后谁还能提交结果。没有增加新的模型、数据集或检索质量声明。核心实现位于 `control.py`、`admission.py`、`journal.py`、`execution.py`、`supervisor.py`、`processes.py`。

v0.4 进一步加入有界模型入口、查询合并、生成步检查和服务退出的 interrupted 确认；模型复用与最新退出行为见 [资源设计](model-resources.md)。下文保留 v0.3 的控制协议和平台边界。

## 为什么不能只调用 Future.cancel

线程已经进入推理或 FFmpeg 时，取消排队 Future 不能终止正在运行的工作。只把页面标成“已取消”也不够：后台仍可能写出结果，释放的名额还可能不断接收新请求。v0.3 将控制意图、结果提交权限和进程生命周期分别处理。

| 层次 | 负责什么 | 明确不保证什么 |
|---|---|---|
| 请求回执 | 原始请求指纹、容量、取消意图、尝试截止时间 | 不保证外部工具只执行一次 |
| 阶段租约 | 绑定当前请求 owner，保护阶段与正式产物提交 | 不强制终止一个卡住的 Python 线程 |
| 线程执行器 | 复用本地模型，遇到检查点协作停止 | 不能在阻塞模型调用中立即抢占 |
| 独立进程执行器 | 每个任务一个受控 worker，可终止其本地工具树 | 不保证远端 Ollama 服务停止推理，也不是安全沙箱 |

## 请求与提交不变量

1. 原请求与 job ID 的绑定保持不变。改变 brief、检索模式、provider 或显式 timeout 后，必须使用新 ID。
2. 阶段记录、产物替换和最终提交都检查 pipeline owner、关联 request owner、租约、取消标记与 deadline。旧工作进程即使计算完成，也不能提交。
3. 取消意图持久化。取消后服务在确认前退出，同一 ID 仍不能恢复；新建任务需要新 ID。这里修复了“取消标记被重试清空”的漏洞。
4. pipeline 完成和请求回执 complete 在同一 SQLite 写事务内提交。取消接口使用同一个写锁：先提交取消，则 finalization 拒绝；先完成 finalization，则取消返回 `requested: false`。
5. 新请求 owner 在通过回执校验后，可以替换仍持有旧 pipeline 租约的 worker。恢复无需额外等待旧 pipeline 的 120 秒租约；旧 owner 的后续发布被拒绝。

`Journal.finalize` 的顺序是：`BEGIN IMMEDIATE → 校验 → 替换 result.json → 再校验 → 两张表写 complete → COMMIT`。文件系统改名不能随 SQLite 回滚。如果进程恰在改名后、数据库提交前死亡，文件可能存在，但 API 根据未完成的数据库状态拒绝提供结果。恢复时重验各阶段哈希并重新提交最终清单。

这条路径验证的是**进程崩溃后的恢复行为**。没有实现跨文件系统/数据库的断电事务，也没有声称磁盘损坏、掉电或网络文件系统上的一致性。

## 截止时间与取消确认

默认每次尝试 180 秒，可通过服务参数调整；请求 `timeout_seconds` 范围为 `(0, 3600]`，拒绝 NaN/Infinity。计时从接收请求开始，包含排队、等待模型锁、加载模型、检索、渲染和提交。超时重试得到新的尝试截止时间；显式超时值作为原请求的一部分保持不变。

取消与 deadline 同时出现时，按持久化的取消时刻和截止时刻判断先后。排队或运行中的取消请求先显示 `phase=cancelling`，继续占用容量，直到工作线程处理该控制或监督器完成终止、回收后确认。不能在取消 HTTP 请求刚返回时就说后台已经停止。超时也不通过提前释放容量绕过仍存在的执行工作。

持久截止时间使用墙上时钟，进程内部兜底等待使用 monotonic 时钟；系统校时可能影响持久 deadline，因此它不是实时系统的硬时限。线程模式会等待不可抢占的模型调用返回；进程创建、数据库等待和 OS 调度也有额外耗时。

如果尚未完成时 OS 终止或退出确认失败，记录 `failed` 与 ProcessCleanupError，不把它伪装成已确认的 `cancelled`。取消标记仍保留以阻止同 ID 恢复。若结果已经提交完成，后续清理错误记录为 `postcommit_error`，不会追溯改写已发布的完成状态。这类错误需要检查进程状态。测试用故障替身覆盖这些分支，不能据此声称在所有 OS 故障下都能清理成功。

## 可选进程后端

```bash
reelpilot demo --collection data/demo
reelpilot serve --collection data/demo --execution-backend process --request-timeout 60
```

监督器启动 worker 后，先建立生命周期边界，再通过 stdin 发送受限 JSON 请求。worker 在收到启动消息前不导入模型、不启动工具；所有权凭据不放进进程命令行。worker 再核对原始回执指纹和素材库哈希。stdout 只传完成回执，模型日志转 stderr；两路日志均并行排空，每路只保留最后 64 KiB，并记录总字节数。

Windows 使用私有、不可继承句柄的 Job Object 与 `KILL_ON_JOB_CLOSE`。显式停止时调用 TerminateJobObject，检查 active process 数量归零，等待直接 worker 退出；监督器自身被杀时，最后一个 Job 句柄关闭触发清理。Job 关联失败时拒绝执行，不退回无隔离方式。[Microsoft Job Objects 文档](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)

POSIX 使用独立 session/process group；正常控制路径向该组发送终止信号并 wait 直接子进程。父进程意外死亡由 worker 的 Python 守护线程检查父 PID 后终止本组，存在轮询窗口和 Python 调度限制；它不具备 Windows Job Object 的同等内核保证。非直接子进程的 zombie 由其系统收养者回收。工具是可信的固定 FFmpeg 调用，没有针对恶意进程脱离组或逃逸设计安全边界。

Python 官方指出子进程管道读写有阻塞风险，捕获输出也需要考虑内存规模；本项目采用两个独立排空线程和有界尾部缓冲。[subprocess 文档](https://docs.python.org/3.13/library/subprocess.html) SQLite 连接另外使用显式 close，避免误以为 Connection 上下文退出会关闭连接。[sqlite3 文档](https://docs.python.org/3.13/library/sqlite3.html#how-to-use-the-connection-context-manager)

## 为什么保留默认线程模式

进程后端每个任务冷加载模型，不能复用线程后端的编码器缓存。在单 GPU 上，当前应用实例仍用模型锁串行化需要模型的任务，以限制同时加载；没有跨实例 GPU 调度，也没有常驻模型服务。CPU 小样本开销对照见 [原始测量](../reports/runtime_backend_benchmark.json)，不应把 CPU 渲染对照换算成 GPU 推理吞吐。

没有为这个本地原型引入 Redis/Celery 或多主机调度。SQLite 已能提供所需的低容量请求事务；新增中间件并不能替代工具终止、owner fencing 或最终提交的一致性设计。若以后需要复用模型与强终止并存，可评估独立常驻推理服务，但需要额外处理远端取消、资源计量与服务恢复。

`/api/jobs` 支持上述控制；同步 `/api/search` 和直接 `reelpilot edit` 尚不纳入请求取消协议。服务关闭仍等待其线程执行器结束，线程中的不可抢占调用可能延迟退出。自动恢复执行、多租户鉴权、磁盘配额、失败尝试目录清理不在本轮实现范围。
