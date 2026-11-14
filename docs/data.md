# 来源资料约定

pages.json 中 now、last_visit、interval 与 max_staleness 的单位均为虚拟天数，daily_budget 表示当日允许发出的访问数。last_visit 为 null 的地址没有历史。来源的 activity 有两版正文，manual 内容稳定，broken 返回 503。http_source 可复现条件请求和手动推进来源版本。

## 自适应计划使用的字段

- `importance`：业务重要度，参与排序时按 10 封顶；
- `max_staleness`：可接受陈旧时长（虚拟天），用于截止压力与饥饿窗口；
- `last_visit`：旧格式兼容字段，只登记为 `legacy` 观测，不进入变化率证据；
- `observations`：可选的历史观测列表，元素含 `time`、`kind`
  （`304` / `unchanged` / `changed` / `failure` / `redirect` / `legacy`）、
  `digest`、`status` 等；重定向观测必须带 `target`；
- `first_seen`：地址进入计划的虚拟天，缺省取计划器当前时间，用于无内容
  证据地址的饥饿窗口归一化。

## 一周演练剧本（simulation.build_week_scenario）

固定虚拟时钟走第 0..6 天，每日预算 2：

- `/activity`（重要度 10，陈旧上限 2）：第 3 天改版，验证变化只能定位到
  删失区间、发现延迟按真实改版时刻评估；
- `/manual`（重要度 2，陈旧上限 30）：整周稳定，验证低压力页面让位；
- `/broken`（重要度 5，陈旧上限 3）：连续 7 天 503，验证失败不更新内容
  时间、指数退避和未满足目标审计；
- `/new`（重要度 3）：无历史，第 5 天后正文摘要与 `/manual` 相同但 ETag
  不同，验证探索机会与"摘要相同不合并历史"；
- `/old-doc`：第 2 天永久重定向到 `/docs-new`，验证退休与历史继承；
- `/quiet`（重要度 1）：冷门地址，验证防饿死的有界等待窗口。

演练来源的真实改版时刻只用于事后评估，计划器只能通过抓取结果认识世界。
