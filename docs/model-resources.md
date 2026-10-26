# 模型资源管理与服务退出：v0.4

本轮深化模型调用周围的运行机制，不增加新项目或重写模型质量数字。主要实现是取消感知的 FIFO 模型入口、有界查询向量缓存、同查询并发合并、可选规划器驻留、生成步控制，以及服务退出后的原请求恢复。

## 从两个具体缺口出发

v0.3 的 LockedRetriever 对所有 search 都取得模型锁，字幕关键词检索也会被一个慢模型挡住。构造 dense retriever 和调用编码器的部分等待路径使用普通锁，等待时无法及时检查取消。QwenEncoder 的 query_cache 则是随不同查询持续增长的字典。

v0.4 将“查缓存”“等待模型”“运行模型”分开。lexical 检索不进入模型入口；dense 的缓存命中也不占模型执行权。真正需要模型的未命中查询、规划调用和独立模型 worker 才排队。测试在规划替身被阻塞期间验证 lexical 请求先返回，并验证 dense 排队任务取消后没有加载模型。

## FIFO 资源入口与限流

`resources.FairGate` 使用条件变量和 FIFO 队列，每次只允许一个本地模型操作进入；默认最多 16 个等待者。调用者等待时周期检查自己的 guard，检查不持有条件锁，避免 SQLite 查询阻塞整个队列。取消的队首退出后立即让后继继续，异常也会释放占用。

这里的 FIFO 指需要该资源的已排队操作，不包括缓存命中或不需要模型的 CPU 任务。它不是优先级调度，也不是跨进程 GPU 调度器。独立 worker 模式仍在父进程取得该入口后启动，持有到 worker 完成回收；该模式没有跨任务模型复用。

`/api/search` 资源等待或不同查询填充达到上限时返回 429 与 Retry-After；服务退出时返回 503。作业仍通过持久回执接收，执行时若模型入口已满，失败可见并可用原请求重试。没有隐藏的无限重试或无限等待队列。

## 有界 LRU 与 Single-flight

缓存键是 `(encoder fingerprint, exact query)`，fingerprint 包含模型 revision、指令、pooling 与输入格式，保持和旧索引的身份算法一致。不同模型空间不能共享向量；不对查询进行可能改变语义的模糊归一化，也不缓存模型回答或最终剪辑。

默认最多 256 个保留条目、16 MiB 键与数组计费字节、16 个不同查询的在途填充。写入前按 LRU 淘汰；单条超过预算则不保留。预算不等于 Python 进程 RSS：解释器、对象开销、在途结果、模型权重和 CUDA 显存不在该计费值内。

同一个键的第一个请求负责计算，其他请求等待同一 flight。每个等待者继续检查自己的取消/截止时间，取消一个等待者不会取消其他请求。若负责计算的请求取消，活跃等待者可重新竞争计算权，而不继承别人的取消状态。真正的计算错误向等待者传播，该 flight 清除后允许后续请求重试。

缓存内部保留不可写的数组，每个调用者拿独立副本，避免某个请求修改向量污染后续结果。错误向量不入缓存；失败、取消和正常完成均清除在途条目。退出时先等待在途填充结束，再清缓存、关闭模型，防止一边推理一边释放对象。

## 规划模型驻留与生成取消

线程后端可用 `--retain-planner` 保留已加载的规划模型和 tokenizer。默认关闭，延续按次加载/释放规划器的内存取舍。开启后可能同时驻留编码器与规划器，需要自行评估显存；当前没有实测其大模型显存峰值，也没有宣称固定 12 GB 预算保证。

驻留复用的是权重，不是上一条 QuerySpec。每次重新构建 LM Format Enforcer 的解码状态，避免跨请求保留 token-prefix 状态；用户本次的时长、素材与目标约束重新校验。取消一次生成后，后续请求仍可使用驻留模型。

本地 Transformers 生成通过 StoppingCriteria 在生成步检查 guard，取消异常直接传播，部分 JSON 不做修复也不接受为结果。生成前后均再检查。它不能抢占正在执行的一次 CPU/GPU forward、模型加载或一个不返回的底层调用；逐步检查还会引入控制检查成本，没有测量 Qwen 大模型上的吞吐影响。[Transformers 4.57.1 生成控制接口](https://huggingface.co/docs/transformers/v4.57.1/en/internal/generation_utils#transformers.StoppingCriteria)

## 退出、确认与重新接管

应用 lifespan 退出时设置本地 stop 标记，关闭模型等待入口，拒绝新工作，并让线程池中的既有回执走 guard，保存 `interrupted`。运行中的阶段提交也检查本地退出标记。线程池与模型关闭在异步生命周期中交给辅助线程等待，避免直接阻塞事件循环。

独立进程后端的监督器看到退出标记后，使用原有受控进程树终止与等待机制，再确认 interrupted。真实 FFmpeg 实验验证：worker、FFmpeg 退出，旧请求输出不可下载；新应用用相同原始请求重新提交，attempts=2 并完成剪辑。

不使用 `shutdown(cancel_futures=True)` 直接丢弃尚未执行的回执；否则 Future 虽被取消，其数据库终态与心跳仍需要另行处理。线程中不可抢占的调用仍可能延长退出时间，这不是有严格时限的 graceful shutdown。[Python Executor.shutdown 文档](https://docs.python.org/3.13/library/concurrent.futures.html#concurrent.futures.Executor.shutdown)

已经赢得最终提交的 complete 保持不变，退出信号不是跨系统的瞬时撤销。服务重新启动不会自动开始旧任务，需要客户端重复提交原始请求。远端 Ollama 的计算终止、同步 search 的客户端断连取消和多实例 GPU 协调均不在本轮范围内。

## 实验分层与结果

| 证据 | 实际做了什么 | 能支持的结论 |
|---|---|---|
| 完整 CPU/媒体测试 | 本地 103 项测试，含旧故障回归与新的真实 FFmpeg 退出/恢复 | 缓存、控制、状态与媒体执行的契约验证 |
| 实际 Transformers 控制 | 本地随机初始化 6384 参数 GPT2；正常生成 8 token，取消在第 4 个 forward 触发，部分输出不接受 | 真实 generate 循环与生产 planner 控制路径接通 |
| 实际小模型查询合并 | 随机 GPT2 hidden-state CPU forward；8 次基线计算，对比 8 个同键并发请求共用 1 次 forward | 在强制并发未命中的条件下合并真实计算 |
| 受控回调对照 | 注入 25 ms 编码延迟，5 轮 8 请求；规划器另注入构造与执行延迟 | 隔离调度、合并和驻留构造次数的机制对照 |
| 容量抖动 | 500 个不同查询，预算 8192 字节、32 条；计费字节峰值 8062，最终 29 条 | 该实验中条目数和计费字节都未越界 |

微型模型不下载权重，不具备预训练语义能力。生成探针采用受控 tokenizer，并关闭 JSON-prefix 约束以单独验证取消路径；不是 Qwen 检查点适配或 JSON 生成质量实测。查询合并探针使用同步屏障制造并发未命中，不做推理加速比例声明。受控回调延迟是注入值，不能当作真实 GPU 延迟。

原始记录：[资源验证](../reports/resource_validation.json)、[测试摘要](../reports/resource_depth_tests.txt)、[真实微型模型探针](../reports/generation_control.json)、[受控对照](../reports/model_resource_experiment.json)。v0.3 的 80 项冻结记录与原始检索质量报告保留，未替换成新数字。

## 运行与复现

```bash
python scripts/validate_resources.py
python scripts/measure_model_resources.py
# 需要已安装 torch、transformers、lm-format-enforcer；不下载模型
python scripts/validate_generation_control.py

reelpilot serve --collection data/demo --model-waiters 16 --query-cache-entries 256 --query-cache-mib 16
# 配置本地规划检查点后，可在 thread 后端额外开启 --retain-planner
```

`/api/health` 返回模型加载、队列等待、缓存命中/合并/淘汰的计数，不返回查询正文。进程后端不接受 retain-planner，因为每个 worker 的生命周期只有一次任务。模型参数未配置时，原创演示视频与 lexical 模式仍可独立运行。

## 测试自身的故障也要解释

一次全量复核的监督进程死亡实验未到检查点，数据库只记录 admitted、没有续租；原测试把 2 秒租约与解释器导入冷启动混在了一起。修正后先等待可信测试监督器就绪，再创建该实验的短租约，并在 worker 提前退出时反馈 stderr。专项连续三次及全量测试通过。没有放宽进程死亡后的子进程退出断言，也没有修改应用默认租约。

该调整使父进程死亡和短租约等待成为各自可解释的实验，不能据此掩盖真实部署中冷启动与租约设置不当的风险。

首次 Windows CI 还暴露了监督器的两次读取问题：先读“是否 complete”，再调用 guard 读所有权；完成恰好落在中间时，第二次读到 complete/lease=0，可能误报失联。新增确定性测试在第一次取行后强制提交完成，旧算法稳定报 BusyJob；改为 `Ticket.worker_state` 用同一行快照校验，下一轮再读取新终态，不把正常完成当成租约丢失。

恢复实验也将“崩溃前故意设为 1 秒以验证真实过期”和“恢复后正常工作”的租约配置分开，后者使用 30 秒，避免将共享 CI 的亚秒级线程调度当作本实验的额外前提。原来的实际过期等待、旧 owner 拒绝提交、渲染只提交一次等断言全部保留。
