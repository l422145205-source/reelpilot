# 真实故障实验与复现

本轮不扩大项目数量，集中验证 ReelPilot 的长任务执行机制。测试媒体由程序生成，检索使用 explicit + lexical，不加载模型。原有多模态开发评测保持原样；这里的通过数是工程测试结果，不是大模型准确率。

## 一键复现

```bash
python -m pip install -e '.[dev]'
python scripts/validate_runtime.py
python scripts/measure_runtime.py --samples 5
```

验证器在 scratch 下创建新的 UUID 测试目录，保留原始 JUnit、完整日志、SQLite 与测试媒体；不会删除已有指定目录。公开汇总见 [故障证据](../reports/runtime_fault_validation.json)、[测试摘要](../reports/runtime_depth_tests.txt) 和 [开销对照](../reports/runtime_backend_benchmark.json)。每次本地重跑会覆盖 reports 中对应的汇总；这是单次实验记录，不是长期统计仪表盘。

## 故障矩阵

| 场景 | 注入方式 | 观察和断言 | 代码入口 |
|---|---|---|---|
| 独立进程正常完成 | 真实 worker、HTTP handler、FFmpeg | 结果可解码，完成后有 worker 回收事件 | `test_real_process_backend_completes` |
| 渲染已提交时崩溃 | checkpoint 标记后 OS 强杀 worker，无 Python 清理 | 等真实请求租约过期；重试完成，render 提交次数仍为 1 | `test_hard_kill_recovers...` / `render_committed` |
| 清单已替换但 SQL 未提交时崩溃 | SQLite 写事务内停住后 OS 强杀 | result.json 存在但接口返回 409；SQL 回滚；重试复用渲染 | 同上 / `result_replaced_before_commit` |
| 取消正在运行的工具 | 可信测试 worker 启动真实实时 FFmpeg；调用取消 API | 控制确认后 worker 和 FFmpeg 都已退出，输出不可下载 | `test_supervisor_reaps_running_ffmpeg_before_control_ack` |
| 工具执行中超过截止时间 | FFmpeg 确认启动后将 deadline 设为 now+0.25 秒，真实等待过期 | 状态 timed_out；终止 worker 与工具；输出返回 409 | 同上 / `timeout` |
| 监督进程自身崩溃 | 仅强杀直接监督进程；测试外层不做进程树清理 | worker 与 FFmpeg 随其生命周期机制退出 | `test_actual_supervisor_death...` |
| stdout/stderr 大量输出 | 两路各写 5 MiB | 两路完整排空，各只保留 64 KiB，不因管道堵塞超时 | `test_pipe_flood...` |

强杀目标和存活检查只针对测试直接创建、通过本地标记获得的 PID，不枚举或终止系统中同名进程。Windows 检查使用只具同步权限的进程句柄；没有用可能产生平台差异的 `os.kill(pid, 0)` 检查 Windows 进程。

故障 worker 是仓库中的可信测试程序，通过内部 checkpoint 暂停或启动长时间 FFmpeg；API 和模型不能指定该程序，也不能传 checkpoint 参数。进程创建、stdin 启动门、OS 生命周期控制、guard、回收和 API 终态使用生产实现。它验证的是确定位置的故障机制，不能冒充自然发生的生产故障率。

## 额外的确定性契约测试

- 取消发生在运算内部：结果已经算出也不能提交阶段记录。
- 取消发生在服务退出之前、取消确认之前：持久化取消标记不会被重试清空。
- 完成与取消同时竞争：同一个 SQLite 写锁决定先后；运行 20 组竞争，不允许“取消已接受却又提交最终结果”。
- 旧请求 owner 的失效与新 pipeline owner 的接管：不等待旧 pipeline 租约，旧 publisher 仍被隔离。
- 排队取消：名额保留到确认，防止无限积累被标记取消的 Future。
- 阻塞模型替身：线程模式不假装能强杀模型；返回后不得进入 pipeline。
- 排队超时：使用真实经过的 0.05 秒期限，不修改数据库时钟。
- v0.2 数据库迁移、无效 deadline、已完成任务取消、未知任务取消。

其中部分 owner/租约单元测试会直接设置数据库状态，以稳定覆盖竞争分支；它们与前述真实进程死亡、真实租约等待实验分开记录。

## 开销实验如何读

原始报告逐次保存两个后端的完成时间。每种后端排除一次预热，再交替顺序各测 5 次；任务为同一段 8 秒原创视频、两个明确检索目标、单工作线程。测量从 HTTP 提交到数据库确认 complete，独立记录进程 worker 回收的观察时间；不包含素材预生成和依赖安装。

进程模式引入 Python 冷启动、模块导入、序列化和监督成本。这个实验只支持“在该 CPU 工作负载下，进程隔离存在额外开销”的取舍，不能证明有 GPU 模型时延迟如何，也不是生产 SLA、并发吞吐或统计显著性实验。

## 面试时最值得解释的三个案例

1. **取消复活漏洞**：错误实现只检查已确认 cancelled 状态，重试会清空尚未确认的取消标记。修复后，无论取消是否完成确认，都按持久控制顺序处理。
2. **文件存在不等于任务完成**：主动制造改名后、数据库提交前的死亡，实际观察到文件存在而接口拒绝输出；恢复复用 render，而非重新剪辑。
3. **有成本的隔离**：线程能保留模型缓存，进程能终止本地工作树。用可复现开销对照说明为何提供可选后端，而非把“更多进程”当作无条件优化。

进一步的边界与实现取舍见 [控制设计](runtime-controls.md)。Windows 与 Ubuntu 的 CI 结果应分别检查，不能用一台机器上的通过记录替代跨平台验证。
