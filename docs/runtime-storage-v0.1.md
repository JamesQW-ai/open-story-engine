# 运行期存储 v0.1

## 边界

本契约只处理单机运行期数据。`StoryPackage` 仍是文件系统中固定、版本化的内容输入；SQLite 不复制世界设定、原著全文或 LLM 输入输出。一个 `game_sessions` 行保存当前可读快照，一个 `game_events` 行保存已经提交的规则回合。

## 表与事务

`game_sessions` 包含故事包 ID 与版本、状态版本、会话状态、`current_state_json` 和时间戳。`game_events` 按会话保存严格递增的 `sequence`、幂等键 `request_id`、玩家原始输入、`ActionIntent`、不含状态本体的 `Resolution`、状态补丁与该回合后的状态快照。`event_narrations` 以事件序号关联规则提交后的叙事文本与 `continuity_json`，避免为补写正文而改动规则事件。

共创基础另外使用 `session_story_contracts` 与 `branch_nodes`。前者每个会话仅保存一份原著来源、继承范围与进入节点契约；后者以父节点 ID 和递增序号保存追加式剧情树。它们不取代 `game_events`，也不允许规划器借由分支节点直接改变规则状态。完整契约见[私有共创基础 v0.1](co-creation-foundation-v0.1.md)。

一次有效规则回合在同一 SQLite 事务中完成：确认请求未提交、确认基础状态版本、计算状态补丁、追加事件、更新快照及终局状态。`blocked` 与已经终局的行动不会写入事件或改变状态。相同会话中的相同 `request_id` 返回原事件结果，不重新掷骰。

## 重建

重建从已加载故事包的 `initialState` 开始，按 `sequence` 应用每个 `state_patch_json`。它不读取会话的当前快照；测试与 CLI 的 `rebuild` 命令会将重建结果和快照比较。事件中的 `state_after_json` 仅用于请求幂等响应和诊断，不是重建输入。

## 当前 CLI

使用 `python3 -m open_story_engine play` 创建新会话，默认写入 `data/open-story-engine.sqlite`。CLI 接受许川的自然语言行动，例如“许川俯身检查十七号柜的铜牌”或“他请求姜序带路”。mock `ActionParser` 只会映射到故事包中既有的稳定 ID；它不确定时返回澄清，不会写入事件。

`status`、`history`、`rebuild` 和 `quit` 是开发命令。CLI 不提供固定动作菜单，也不是正式玩家界面。mock `Narrator` 从最近保存的叙事锚点和本回合已确认的 `Resolution` 生成一段新的第三人称文本；以后替换为 LLM 适配器也不能改变规则结果或选择未满足条件的剧情边。

默认骰点来自运行时随机源。开发或回归演示可设置 `STORY_FIXED_ROLL=6` 固定骰点；`RuleEngine` 仍会校验它是否落在故事包声明的随机范围内。
