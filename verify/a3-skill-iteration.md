# A3.2 skill 迭代复现：oar 案例（2026-11-22）

用户意图：「找一个用一个接口管住多种编码 agent runtime 的库」，事先不知道名字与关键词。

- 第 1 轮查询：`coding agent orchestration tool`（宽搜，含上位词 orchestration）
  - 读描述发现新术语：**harnesses**（来自 bradAGI/awesome-cli-coding-agents 的 "the harnesses that orchestrate them"；另见 ModelEngine-Group/nexent、zhnt/loushang 的 harness）
- 第 2 轮查询：`agent harnesses interface`（用学到的术语构造）
  - **命中 botiverse/oar** — "oar (Open Agent Runtime): programming interface for all agent harnesses"，排第 1
  - 同时带出 HarnessRouter/harnessrouter（同类方案，排第 2）

结论：按 skill 流程执行两轮即命中基准案例，覆盖度达标（A2）。相关项目及命中依据：oar（第 2 轮 / harnesses）、HarnessRouter（第 2 轮 / harnesses，参考）。
