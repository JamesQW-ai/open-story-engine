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
- [Demo Story 设计：雨夜候车室](docs/demo-story-design.md)：首个预置故事的世界、节点、状态与结局设计。
- [小说母本输入基线 v0.1](docs/source-novel-input-v0.1.md)：标准小说母本与后续异常输入处理边界。
- [产品方向与演进边界](docs/product-direction.md)：规范原著体验与未来私有同人共创模式的边界。
- [原著标注](content/annotations/rainy-waiting-room.v0.1.json)：小说母本到规范剧情锚点的可追溯标注。
- [StoryPackage 候选稿](content/packages/rainy-waiting-room/0.1.0.json)：首个机器可读的规范主线故事包。

## 开发顺序

1. 将标准小说母本转为标注与可校验的机器可读故事包。已完成候选内容。
2. 初始化 TypeScript、故事包 schema、确定性规则检定与自动化测试。已完成。
3. 增加 SQLite 事件写入、状态重建与本地 CLI 试玩。已完成。
4. 用 mock 行动解析器、剧情图、叙事器和高层剧情方向列表跑通端到端试玩与自动化测试。已完成。
5. 增加 `SessionStoryContract`、动态 `BranchNode` 与 Mock Planner，验证共创树的持久化和分叉。已完成基础切片。
6. 接入真实 LLM Planner/叙事适配器，并在规则不受模型影响的前提下评估方向与正文质量。

在第 4 步通过之前，不提前开发 Web 界面或小说导入能力。

开发试玩可运行 `npm run play`。每段剧情后会列出当前可达的高层方向；可输入方向编号，或直接输入自定义的自然语言行动。完整说明见[连贯叙事推进 v0.1](docs/narrative-progression-v0.1.md)。

动态树基础可运行 `npm run co-create`；该命令会自动读取项目根目录的 `.env`。默认使用 Mock Planner 验证契约和分支节点写入，也可输入自然语言方向。`.env` 仅供本机使用且被 Git 忽略，`.env.example` 是可共享的配置模板。

要启用真实 OpenAI-compatible Planner，在 `.env` 中设置：

```dotenv
STORY_PLANNER=openai
STORY_LLM_API_KEY=...
STORY_LLM_MODEL=...
STORY_LLM_BASE_URL=https://你的中转站地址/v1
```

`STORY_LLM_BASE_URL` 必须是 OpenAI-compatible API 前缀，不要包含 `/chat/completions`。共创 CLI 会请求 SSE 流式响应，并在 JSON 结构、引用范围校验通过且分支已落库后逐段呈现正文；中转站忽略流式请求时会自动回退到普通响应。模型返回不完整 JSON 或 schema 不通过时，Planner 会以更严格的紧凑格式要求重试一次；两次均失败则不会创建分支，可用 `llm-audits` 查看本次会话的调用结果。

真实模型回归不包含在默认测试中。显式设置 `STORY_LIVE_EVALUATION=1` 后运行 `npm run evaluate:live`，它会以受限调用次数在内存会话中验证自由文本、锁闭状态、受控汇合和请求幂等性；详见[真实 LLM 评估 v0.1](docs/live-llm-evaluation-v0.1.md)。
