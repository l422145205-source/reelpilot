# ReelPilot

**面向文娱素材的多模态检索与可恢复剪辑 Agent**

[![CPU CI](https://github.com/l422145205-source/reelpilot/actions/workflows/ci.yml/badge.svg)](https://github.com/l422145205-source/reelpilot/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/Python-3.11%2B-blue)
![License](https://img.shields.io/badge/code-MIT-green)

从「找出城市、机器人、人物交流的画面」到带来源时间戳的候选剪辑：模型拆解检索目标，程序编译和校验剪辑计划，FFmpeg 执行，SQLite 记录每一步以支持失败恢复。

适用于探索内容平台、影视宣发、游戏宣传素材的检索与粗剪工作流。当前是**本地运行的工程原型**，使用公开素材验证完整链路；没有企业上线或真实业务收益数据。

## 项目重点

v0.2 补齐规划前的持久请求回执、跨实例容量核对、失败追溯与过期请求保护；最终结果清单也采用所有权校验后的原子发布。[设计与可复现实验](docs/runtime-reliability.md) · [代码追问入口](docs/interview-walkthrough.md) · [项目索引](docs/portfolio.md)

- **时间对齐的多模态索引**：Whisper 词级时间戳、六秒窗口、中点帧；Qwen3-VL-Embedding-2B 构建画面、对白、联合内容三种索引，保留素材哈希、时间区间和授权来源。
- **有约束的 Agent 工具链**：Qwen3-0.6B + LM Format Enforcer 生成 Schema 约束目标；Beam Search 在时长上限、片段数和不重叠约束下编译 `EditPlan`，独立校验后才执行固定参数的 FFmpeg 工具。
- **可恢复执行**：SQLite 幂等请求、所有权租约、心跳与阶段日志；owner 校验和原子文件发布阻止过期 worker 覆盖结果，重试复用哈希验证后的产物。
- **有对照的方案选择**：比较 BM25、三种向量视图和 RRF，保留逐条排名和错误案例。模型加载严格检查 checkpoint keys，索引 fingerprint 不匹配时拒绝使用。
- **完整交互**：FastAPI 工作台支持画面/对白检索、原片定位、生成进度、结果播放与剪辑计划 JSON 导出。

## 看实际结果

![真实素材检索与剪辑示例](examples/retrieval-preview.jpg)

上图来自实际检索到的城市与大脑画面；[12 秒剪辑结果](examples/recovery-preview.mp4)来自故障恢复实验。素材为 **(CC) Blender Foundation | mango.blender.org，CC BY 3.0**，此处做了截帧、缩放和片段重组。[来源与授权](THIRD_PARTY.md)。

![本地工作台](examples/workbench.png)

## 五分钟运行：不需要模型或 GPU

Python 3.11+；推荐先在独立虚拟环境安装。此模式生成原创的四场景测试视频，使用明确目标和关键词检索，**不调用大模型**。

```bash
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -e '.[dev]'
reelpilot demo --collection data/demo
reelpilot serve --collection data/demo
```

打开 <http://127.0.0.1:8765>，保持「明确目标」与「字幕关键词」，输入 `forest; robot`。也可直接执行：

```bash
reelpilot edit --collection data/demo --jobs data/jobs --budget 8 'forest; robot'
pytest
```

FFmpeg 可执行文件由 `imageio-ffmpeg` 依赖提供，无需写入系统目录。应用默认只绑定本机地址。

## 运行真实模型与公开素材

本次验证环境：Windows、Python 3.13.9、PyTorch 2.11.0、Transformers 4.57.1、RTX 5070 Ti Laptop 12 GB。ASR 使用 CPU int8；嵌入与规划分阶段运行。检查点共约 6 GB，视频约 380 MB，处理缓存需额外空间。

先按你的硬件安装 PyTorch，然后：

```bash
python -m pip install -e '.[models,dev]'
python scripts/bootstrap_assets.py movie
python scripts/bootstrap_assets.py asr
python scripts/bootstrap_assets.py embedding
python scripts/bootstrap_assets.py planner
python scripts/prepare_open_movies.py --embed
python scripts/evaluate_pilot.py
python scripts/run_real_experiments.py
reelpilot serve --collection data/index --jobs data/jobs --embedding-model .runtime/models/qwen3-vl-embedding-2b --planner-model .runtime/models/qwen3-0.6b
```

下载脚本固定模型 revision 并校验下载大小与可用的 LFS SHA256。模型默认来自 Hugging Face；如需镜像，可通过 `REELPILOT_MODEL_ENDPOINT` 指定兼容端点。权重、完整影片、索引缓存和本地任务目录均不进入 Git。

自己的素材可用 `reelpilot ingest --help` 登记，必须提供来源与授权信息，再运行 `reelpilot embed` 构建三种索引。当前界面使用服务器已登记的素材库，不提供任意文件上传。

## 实验概况

2 份公开素材、约 13.1 分钟、132 个时间窗口；15 个场景意图对应 30 条中英文查询。标签由代理检查时间戳联系表后辅助编写，属于**开发性小样本**，不是独立人工测试集。

| 检索方法 | MRR@10 | 相关窗口集合 Recall@5 |
|---|---:|---:|
| BM25 / 字幕 | 0.1087 | 0.0620 |
| 画面向量 | **0.9319** | **0.7037** |
| 对白向量 | 0.1091 | 0.0722 |
| 画面 + 对白联合向量 | 0.7909 | 0.6005 |
| 联合向量 + BM25 / RRF | 0.6001 | 0.3907 |

这组问题以画面需求为主，无法据此判断对白路由的收益。默认采用按目标通道选视图的规则；不宣称统计显著或可泛化的提升。查询编码耗时中位数约 30.5 ms，**不含模型加载，不是完整问答或剪辑延迟**。

完成三次真实媒体运行，包括一次渲染后主动中断再恢复；该次日志记录渲染只执行一次，恢复命中缓存。v0.2 在原有 CPU 测试上补充请求容量、规划失败持久化、跨实例竞争和最终清单发布的恢复测试。测试结果见 Actions 与 [本地日志](reports/career_hardening_tests.txt)。详见[实验报告](docs/experiments.md)、[原始排名](reports/retrieval_pilot.json)、[真实运行日志](reports/real_runs.json)。

## 实现边界

固定窗口与单帧可能漏掉短动作或截断台词；当前不做镜头边界检测。小模型仍会扩写需求，真实预告片实验中城市目标召回了室内全息场景。结构验证保证来源和执行约束，**不证明画面语义、审美或叙事质量**。最终选片需要审片。

可恢复执行不是 exactly-once；进程崩溃可能重跑工具。当前是单实例本地服务，没有多租户、鉴权或公网部署加固。Ollama 后端已实现，但本次未进行服务实测。Docker 示例未在本机完成构建验证。

## 进一步阅读

- [技术架构与模块](docs/architecture.md)
- [实验设计、消融与失败分析](docs/experiments.md)
- [产品目标与验收范围](docs/product-brief.md)
- [面试讲解](docs/interview.md) · [简历条目草稿](docs/resume_excerpt.md)
- [第三方来源](THIRD_PARTY.md) · [代码许可](LICENSE)

代码由 AI 编程工具辅助实现；实验由本地真实运行产生，保留原始记录和明确边界。基础模型来自公开检查点，本项目没有训练或微调这些模型。
