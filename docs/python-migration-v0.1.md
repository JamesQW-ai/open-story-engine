# Python 迁移 v0.1

## 目标与边界

运行时迁移到 Python 3.9+，但不改变 `StoryPackage`、权威状态、事件重建、共创契约或衍生故事包的语义。`content/packages/` 始终是版本化的只读输入；玩家选择产生的会话、事件、分支、审计与衍生修订仅写入 SQLite。

本次迁移采用并行策略：`open_story_engine/` 是 Python 实现，`src/` 中现有 TypeScript 继续保留为行为基线，直到 Python 的自动回归、真实模型验收和人工 CLI 阅读验收完成。迁移期间不得通过删除 TypeScript 或改写原故事包来“通过”测试。

## 命令

```bash
python3 -m open_story_engine validate
python3 -m open_story_engine play
python3 -m open_story_engine co-create
```

命令使用 `STORY_DATABASE_PATH` 指定 SQLite；未设置时使用 `data/open-story-engine.sqlite`。人工试玩必须使用新的临时数据库，避免混入历史会话。

```bash
STORY_PLANNER=mock \
STORY_DATABASE_PATH=/private/tmp/open-story-engine-python-manual.sqlite \
python3 -m open_story_engine co-create
```

Python CLI 启动时会读取项目根目录 `.env`，但不会覆盖已经由命令行设置的环境变量。因此可沿用现有 `STORY_PLANNER=openai`、`STORY_LLM_*` 配置，也可在单次人工验收中用命令行变量覆盖它们。

Python 标准库覆盖 JSON、SQLite、CLI 与 OpenAI-compatible HTTP；当前不要求安装第三方运行时依赖。`pyproject.toml` 提供后续打包入口。

## 等价能力清单

| 能力 | Python 模块 | 权威边界 |
| --- | --- | --- |
| StoryPackage 读取和交叉引用校验 | `content.py` | 无效内容不启动会话 |
| 普通试玩、随机检定、事件补丁、重建 | `play.py`、`state.py`、`storage.py` | `state.py` 是普通回合状态权威 |
| 共创树、规范原文复用、自由文本方向 | `cocreation.py` | 已公布方向和状态补丁先于模型正文 |
| 衍生故事包 | `cocreation.py`、`storage.py` | 固定来源，只允许追加修订 |
| SQLite 会话、分支与审计 | `storage.py` | 数据库不改写原始内容包 |
| OpenAI-compatible JSON/SSE | `llm.py` | SSE 只是草稿传输，不代表分支已提交 |
| 真实模型评估 | `cli.py evaluate-live` | 必须显式设置 `STORY_LIVE_EVALUATION=1` |

`MockPlanner` 是离线契约夹具，用于稳定验证路线、状态补丁、回归与衍生包；它不代表发布级的正文质量。人工阅读验收必须使用经授权的真实 Planner，并保留其 `llm-audits` 结果。

## 未完成迁移门槛

Python 版不得在下列验收前替换 TypeScript 命令或删除 TypeScript：完整 Python 自动回归、真实模型 `evaluate-live`、一次普通成功/失败路径人工验收、一次共创 SSE 人工阅读验收、一次衍生包人工验收。任何一项失败都只说明迁移尚未完成，不授权回退故事包或跳过状态校验。
