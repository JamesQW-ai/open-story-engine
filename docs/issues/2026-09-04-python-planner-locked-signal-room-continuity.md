# Python Planner 在锁闭信号室场景改写既定事实

## 状态

Open。Python `co-create` 的真实模型人工验收未通过；运行时已安全拒绝错误草稿，未产生错误分支。

## 复现

使用独立数据库运行：

```sh
STORY_PLANNER=openai \
STORY_LLM_STREAM=true \
STORY_DATABASE_PATH=/private/tmp/open-story-engine-python-sse-retest-5.sqlite \
python3 -m open_story_engine co-create
```

依次输入 `1` 和 `先救唐栖，暂不取证`。

方向判定正确确认了“救援优先”，但 `branch_planner` 的两次草稿都违背既定状态：第一次将仍然锁闭的信号室写成可直接开启，并断言唐栖已从检修口离开；第二次让唐栖从室内自行打开门锁。当前已确认状态要求唐栖仍在锁闭的信号室中，尚未有任何能改变该状态的受控动作。

最终校验拒绝草稿并显示：

```text
剧情正文与已确认状态矛盾：信号室仍锁闭，唐栖不能自行离开或出现在设备间。
```

随后检查该 SQLite 数据库，`branch_nodes` 仅有 `source_entry` 和 `direction_find_token` 两个节点；失败方向未写入。

## 影响与边界

- 这是模型正文的连续性与阅读体验问题，不是 StoryPackage 固定内容、状态存储或事件重建的损坏。
- 守卫和草稿-提交边界按预期工作：错误正文不会进入历史或成为后续上下文。
- 该场景的真实模型 Python CLI 验收尚不能标记通过。

## 关闭标准

1. 在全新的 SQLite 数据库中按上述输入完成同一场景，并出现“剧情已确认”。
2. 在唐栖获救的受控状态变更前，正文不得让她离开、出现在其他地点，或自行打开仍锁闭的信号室。
3. 接续下一回合时，已确认的地点、人物和锁闭状态不应被重复或改写。
4. 保持现有拒绝策略；不得为通过测试而放宽状态守卫或改写原始 StoryPackage。
