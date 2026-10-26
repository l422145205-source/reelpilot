# 大模型应用与内容创作项目

这里汇总三个已公开的个人工程项目，便于从求职方向选择阅读入口。实现由 AI 编程工具辅助完成；各仓库区分模型实验、受控测试和未验证边界，没有企业上线或用户增长声明。

| 项目 | 解决的任务 | 优先检查的技术 |
|---|---|---|
| [ReelPilot](https://github.com/l422145205-source/reelpilot) | 从视频素材检索到来源约束的粗剪 | 多模态索引、计划编译、模型资源与查询合并、进程监督、真实崩溃恢复 |
| [ChipClaw](https://github.com/l422145205-source/chipclaw) | 芯片手册参数检索与工况核验 | BM25+E5、领域 MiniLM、教师学生对照、排序 LoRA、证据约束 |
| [VoxStage](https://github.com/l422145205-source/voxstage) | 游戏与动画的角色对白制作 | TTS/ASR 核对、时间槽、有界修复、单句增量重建 |

阅读 ReelPilot 时，可先看 README 的实际媒体结果，再看 [请求恢复设计](runtime-reliability.md) 与 [代码追问入口](interview-walkthrough.md)。原始质量实验样本较小且标签由代理辅助构造，不能当作独立人工评测；可靠性测试也不等同于生产系统规模验证。

本轮深挖的实现与实验入口：[运行控制设计](runtime-controls.md) · [故障实验说明](runtime-fault-lab.md)。不新增简历版本或重复包装成多个项目。

后续资源深化：[模型资源、驻留与退出恢复](model-resources.md)，明确区分真实微型模型接口实验、注入延迟对照和原始检索质量实验。
