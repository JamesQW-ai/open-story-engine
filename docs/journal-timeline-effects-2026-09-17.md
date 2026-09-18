范围：桌面端故事手记的当前路线回顾，补齐 P4 要求的状态后果与分支时间线投影；不改变状态账本的写入和审查规则。

## 修复

时间轴效果现在从相邻分支状态做差异投影，新增以下已登记变化：

- 人物：存活、死亡、离队、失踪、受伤；永久死亡／离队保留永久标记。
- 道具：`destroyedPermanently` 从未确认变为 `true` 时显示永久损毁。
- 目标：完成、转化、放下和新增目标。
- 剧情问题：解决、放下和新增问题。

当前页 `feedback` 仍只保留前两项，避免手记摘要过长；完整效果保留在 `recap` 时间轴。未知、缺席、兄弟分支状态和没有登记的记录不会生成效果文字。

## 验证

- `.venv-api/bin/python -B -m unittest tests_api.test_journey_display -v`：5 项通过。
- `.venv-api/bin/python -B -m test_support.run api`：通过，清单显示《太虚遗录》第一部 102610 CJK。
- `python3 -B -m test_support.run core`：25 项通过。
- `cd web && npm test`：43 项通过；`cd web && npm run build`：通过。
- `git diff --check`：通过。

本轮没有调用真实模型、改写正式存档、修改正文或扩大插图任务；NQ-001、END-001、SRC-001 仍按既有边界暂缓。
