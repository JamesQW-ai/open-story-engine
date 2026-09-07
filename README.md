# open-story-engine

基于预置故事包运行的高自由度互动叙事引擎。玩家通过自然语言表达行动；系统在可审计的规则、状态与剧情边界内推进故事，并生成可持续阅读的下一段叙事。

当前处于 MVP 基础实现阶段。首个交付物不是 Web 产品，而是一个本地 CLI MVP，用来验证“上下文装载 -> 玩家行动 -> 权威检定 -> 状态更新 -> 叙事呈现”的完整闭环。

## 项目边界

- MVP 只支持一个人工创作或已获授权的预置 `StoryPackage`。
- 预置故事为原创现代悬疑《雨夜候车室》。它用于验证引擎，不是最终内容题材限制。
- CLI 是开发和试玩工具；正式玩家端将采用小说阅读器式交互，不复用命令行界面。
- LLM 可理解玩家意图和生成叙事，但不能直接修改游戏状态或改写世界事实。
- 用户上传小说、自动解析、图片、多用户和云端部署均不在本次 MVP 范围内。私有共创树现作为开发试玩能力提供，尚不是正式玩家端功能。

## 文档导航

- [MVP 技术方案](docs/architecture-mvp.md)：技术选型、模块边界、存储和 LLM 使用原则。
- [MVP 验收标准](docs/mvp-acceptance.md)：首个实现切片的范围、测试场景和完成门槛。
- [单回合协议](docs/turn-protocol.md)：一次玩家行动的输入、检定、持久化、叙事与失败处理契约。
- [StoryPackage v0.1](docs/story-package-v0.1.md)：引擎可加载的版本化故事内容契约。
- [运行期存储 v0.1](docs/runtime-storage-v0.1.md)：SQLite 会话、事件与状态重建契约。
- [连贯叙事推进 v0.1](docs/narrative-progression-v0.1.md)：剧情图、叙事锚点与跨回合承接契约。
- [私有共创基础 v0.1](docs/co-creation-foundation-v0.1.md)：共创会话契约、动态分支树与 Mock Planner 边界。
- [Python 迁移 v0.1](docs/python-migration-v0.1.md)：并行迁移范围、Python CLI 和替换前的验收门槛。
- [LLM 质量链路 v0.1](docs/llm-quality-pipeline-v0.1.md)：规划、草稿审阅、修订与权威状态边界。
- [Demo Story 设计：雨夜候车室](docs/demo-story-design.md)：首个预置故事的世界、节点、状态与结局设计。
- [小说母本输入基线 v0.1](docs/source-novel-input-v0.1.md)：标准小说母本与后续异常输入处理边界。
- [产品方向与演进边界](docs/product-direction.md)：规范原著体验与未来私有同人共创模式的边界。
- [原著标注](content/annotations/rainy-waiting-room.v0.1.json)：小说母本到规范剧情锚点的可追溯标注。
- [StoryPackage 候选稿](content/packages/rainy-waiting-room/0.1.0.json)：首个机器可读的规范主线故事包。

## 开发顺序

1. 将标准小说母本转为标注与可校验的机器可读故事包。已完成候选内容。
2. 初始化 Python 运行时、故事包校验、确定性规则检定与自动化测试。已完成。
3. 增加 SQLite 事件写入、状态重建与本地 CLI 试玩。已完成。
4. 用 mock 行动解析器、剧情图、叙事器和高层剧情方向列表跑通端到端试玩与自动化测试。已完成。
5. 增加 `SessionStoryContract`、动态 `BranchNode` 与 Mock Planner，验证共创树的持久化和分叉。已完成基础切片。
6. 接入真实 LLM Planner/叙事适配器，并在规则不受模型影响的前提下评估方向与正文质量。Python CLI 是唯一运行时入口。

在第 4 步通过之前，不提前开发 Web 界面或小说导入能力。

开发试玩运行 `python3 -m open_story_engine play`。每段剧情后会列出当前可达的高层方向；可输入方向编号，或直接输入自定义的自然语言行动。完整说明见[连贯叙事推进 v0.1](docs/narrative-progression-v0.1.md)。

动态树迁移版可运行 `python3 -m open_story_engine co-create`；默认使用 Mock Planner 验证契约和分支节点写入，也可输入自然语言方向。完成一个分支后可用 `derive <后续目标>` 创建会话隔离的 `DerivedStoryPackage`；它引用但不改写原始 `StoryPackage`。无论处于原故事分支还是衍生包，真实 Planner 都可在单回合中登记分支私有地点、人物、物品或延后揭示的身份；这些实体只写入该分支状态，后续回合可持续引用，固定故事包不会被改写。世界约束与叙事禁则来自各自的 `StoryPackage.world`，Mock 仅提供固定验收样本。`.env` 仅供本机使用且被 Git 忽略，`.env.example` 是可共享的配置模板。完整迁移边界见[Python 迁移 v0.1](docs/python-migration-v0.1.md)。

要启用真实 OpenAI-compatible Planner，在 `.env` 中设置：

```dotenv
STORY_PLANNER=openai
STORY_LLM_API_KEY=...
STORY_LLM_MODEL=...
STORY_LLM_BASE_URL=https://你的中转站地址/v1
# CLI 默认使用 SSE，即时展示尚未提交的正文草稿。
STORY_LLM_STREAM=true
# 单次模型请求的超时上限，默认 30 秒。
STORY_LLM_TIMEOUT_SECONDS=30
# 单次正文模型响应的最大 token 预算，默认 4096；增大可能提高成本。
STORY_LLM_MAX_TOKENS=4096
# 可选：正文通过确定性校验后做审阅；审阅意见仅写入 audit，不会自动重写本回合。
STORY_LLM_QUALITY_REVIEW=false
```

`STORY_LLM_BASE_URL` 必须是 OpenAI-compatible API 前缀，不要包含 `/chat/completions`。共创 CLI 默认让正文 Planner 优先使用 SSE：模型 JSON 的 `narrativeText` 到达时会立即显示为“尚未提交”的剧情草稿，完整 JSON 通过结构、引用范围和叙事事实校验且分支落库后才显示“剧情已确认”和后续方向。自由文本输入先执行非流式方向判定，CLI 会显示“正在判定自由方向”；判定通过后才显示“正在生成正文草稿”。动态正文提示目标为 2,200 至 2,800 个中文字符；首稿少于 2,000 个非空白字符时，Planner 最多请求一次只追加正文的受控续写，合并后仍不足或违反状态约束才拒绝，且不会写入分支。每次请求默认携带 `max_tokens=4096`，可通过 `STORY_LLM_MAX_TOKENS` 在 1,024 至 8,192 之间调整；提高该值可能增加成本。除短稿的受控续写外，一次成功的正文草稿只会接受一次模型结果：结构完整但摘要、章节信息或菜单局部无效时，运行时会做可审计的本地归一化；有效的新地点、人物、物品和方向会连同状态一起写入分支。草稿出现不可安全修复的 JSON、叙事事实或状态冲突时会被拒绝，既不会写入分支，也不会自动重试。每次模型请求默认 30 秒超时，可通过 `STORY_LLM_TIMEOUT_SECONDS` 设置为 5 至 120 秒。`STORY_LLM_STREAM=true` 并非 SSE-only：若 SSE 在连接或首段正文返回前保持静默，CLI 最多等待 15 秒，再以本回合剩余时间改用兼容 JSON 请求；因此一次正文 Planner 回合在传输降级时可能产生一次 SSE 和一次 JSON 模型请求。该降级不是对已返回草稿的重写；同一正文 Planner 会熔断失效的 SSE，后续回合直接使用 JSON，避免重复等待。传输失败、SSE 中断或草稿未通过校验时，CLI 会明确标记草稿未采纳；这些草稿不会改变状态或写入分支。将 `STORY_LLM_STREAM=false` 可临时让正文也改为普通 JSON；可用 `llm-audits` 查看本次会话的调用、归一化和拒绝记录。

真实模型回归不包含在默认测试中。显式设置 `STORY_LIVE_EVALUATION=1` 后运行 `python3 -m open_story_engine evaluate-live --scenario broad_goal_starts_current_phase --output /private/tmp/open-story-engine-python-live.json`，它会以受限调用次数在内存会话中验证宽泛自由文本能锚定到当前阶段；详见[真实 LLM 评估 v0.1](docs/live-llm-evaluation-v0.1.md)。
