# 评估与校准（Eval）

校准「任务识别 + 推理强度分档」的基准（见 ADR-006）。

- 最小 eval 集：确定性 mock 模式、零 token 成本、可反复跑、纳入 pytest 回归
- 对照实验：`demo_agent.experiment`，多维 judge（完整/准确/简洁）+ low/medium/high 三档成本/质量对比
- 覆盖易/难/真机（Blender execute 级）任务
