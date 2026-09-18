## 本轮范围

在当前唯一达标官方长篇《太虚遗录》第一部（102610 CJK，`taixu-relics-part1@0.1.2`）上补齐剧情问题账本的两个结构字段：`priority` 和 `recoveryWindow`。本轮不修改正文生成策略，不调用真实模型，不使用《雨夜候车室》，不改写正式存档。

## 已实现

- 新开局问题明确保存 `priority` 与 `recoveryWindow`，没有依据时均为 `unknown`。
- 规划更新只接受 `critical|high|normal|low|unknown` 和 `immediate|near|mid|late|unknown`；旧问题省略字段表示继承，不能静默清空或改变。
- 新问题默认保持未知，不能从 `open`、人物缺席、目标状态或菜单消失推断优先级和回收时间。
- 分支提交把字段写入问题历史；局部大纲和步骤投影同步展示，并将字段纳入来源证据绑定。
- 开局账本、旧记录和被篡改的历史如果元数据不一致，会保留 `unknown`，不会继续沿用原始开局证据。
- 问题规划与独立复核提示词已声明字段语义：没有正文或公开账本依据时必须填写 `unknown`。

## 验证

```text
.venv-api/bin/python -B -m unittest tests_api.test_reader_threads tests_api.test_reader_consequences tests_api.test_longform_route_outline tests_api.test_longform_route_closure tests_api.test_longform_item_dependencies
```

结果：67 项通过。覆盖新字段合法性、更新继承、正文证据提交、旧开局篡改降级、路线大纲投影、终局清单和道具依赖交互。`route-outline` 契约版本更新为 `/3`。

## 保留边界

当前没有为长篇每条问题人工填写优先级或回收窗口；未确认值继续显示 `unknown`。本轮字段可记录、可校验、可沿分支隔离，但还没有根据它们自动安排剧情或改变终局条件。NQ-001、END-001、SRC-001 继续按计划暂缓。
