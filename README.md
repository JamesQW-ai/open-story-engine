# open-story-engine

基于预置故事包运行的高自由度互动叙事引擎。玩家通过自然语言表达行动；系统在可审计的规则、状态与剧情边界内推进故事，并生成可持续阅读的下一段叙事。

当前产品为桌面端官方长篇互动阅读：官方先冻结十万汉字及以上小说，玩家以未知原著的视角选择预设身份，通过自然语言行动形成自己的路线。不提供玩家上传、已读节点介入或从零创作整部小说。以 [产品方向](docs/product-direction.md)、[验收标准](docs/mvp-acceptance.md) 和 [开发计划](docs/next-development-plan-2026-09-15.md) 为准。

## 当前测试入口（2026-09-16）

功能测试和桌面夹具统一使用十万汉字长篇。《雨夜候车室》已停用，历史测试仅保留追溯，不能执行或计入当前通过数。清单自动扫描所有达标母本，并要求匹配官方运行包；当前唯一达标小说为《太虚遗录》第一部，102610 CJK、50 章。当前包已接通 7 个可玩身份入口；自然结局由故事包终点和场景完成结果自动登记，不需要玩家手动结束。

```sh
python3 -B -m test_support.run core
.venv-api/bin/python -B -m test_support.run api
cd web && npm test && npm run build
```

测试数据迁移不等于旧用例全部重新验收。当前未迁移模块和完成边界见 [长篇测试与路线结束记录](docs/longform-testing-2026-09-16.md)。不再运行对历史 `tests_py` / `tests_api` 的全目录 `unittest discover`。真实模型和人工阅读仍单独验收。

## 项目边界

- 当前玩家界面位于 `web/`，API 和 CLI 复用 Python 业务核心。
- LLM 理解行动和生成叙事；代码掌管权威状态、事实校验和故事包完整性。
- 历史存档可读取；书架、开局和续写只接纳当前官方模式。
- 自动测试使用临时数据库，不修改正式存档或冻结母本。

## 文档导航

- [MVP 技术方案](docs/architecture-mvp.md)：技术选型、模块边界、存储和 LLM 使用原则。
- [API 与 Web 架构选型 v0.1](docs/api-web-architecture-v0.1.md)：FastAPI、React + TypeScript + Vite、Ant Design 与 SQLite 方案及分批实施边界。
- [读取 API 服务 v0.1](docs/api-read-service-v0.1.md)：首批可运行的故事包、上下文及已有会话浏览；包含安装、启动、接口与验证步骤，尚未开放生成及状态写入。
- [写作与开局 API v0.1](docs/api-play-write-v0.1.md)：玩模式下的会话创建与回合续写；含启用方式、幂等契约与真实模型联调记录。
- [前端只读界面草案 v0.1](docs/frontend-read-ui-draft-v0.1.md)：第一版 React 只读工作台的范围、页面与数据加载顺序；实施代码位于 `web/`。
- [MVP 验收标准](docs/mvp-acceptance.md)：方向 2 的产品范围、必过场景与证据门槛，以及保留的早期 CLI 基础回归。
- [单回合协议](docs/turn-protocol.md)：一次玩家行动的输入、检定、持久化、叙事与失败处理契约。
- [StoryPackage v0.1](docs/story-package-v0.1.md)：引擎可加载的版本化故事内容契约。
- [运行期存储 v0.1](docs/runtime-storage-v0.1.md)：SQLite 会话、事件与状态重建契约。
- [连贯叙事推进 v0.1](docs/narrative-progression-v0.1.md)：剧情图、叙事锚点与跨回合承接契约。
- [私有共创基础 v0.1](docs/co-creation-foundation-v0.1.md)：共创会话契约、动态分支树与 Mock Planner 边界。
- [Python 迁移 v0.1](docs/python-migration-v0.1.md)：并行迁移范围、Python CLI 和替换前的验收门槛。
- [LLM 质量链路 v0.1](docs/llm-quality-pipeline-v0.1.md)：规划、草稿审阅、修订与权威状态边界。
- [历史 Demo 设计](docs/demo-story-design.md)：已停用，仅供追溯。
- [小说母本输入基线 v0.1](docs/source-novel-input-v0.1.md)：标准小说母本与后续异常输入处理边界。
- [产品方向与演进边界](docs/product-direction.md)：当前唯一产品方向与验收边界。
- [原著标注](content/annotations/rainy-waiting-room.v0.1.json)：小说母本到规范剧情锚点的可追溯标注。
- [长篇测试清单](test_support/longform.py)：按真实汉字数和冻结来源校验全部长篇，当前测试不加载旧短篇夹具。

## 历史 CLI 开发记录（不作为当前产品操作或测试指引）

1. 将标准小说母本转为标注与可校验的机器可读故事包。已完成候选内容。
2. 初始化 Python 运行时、故事包校验、确定性规则检定与自动化测试。已完成。
3. 增加 SQLite 事件写入、状态重建与本地 CLI 试玩。已完成。
4. 用 mock 行动解析器、剧情图、叙事器和高层剧情方向列表跑通端到端试玩与自动化测试。已完成。
5. 增加 `SessionStoryContract`、动态 `BranchNode` 与 Mock Planner，验证共创树的持久化和分叉。已完成基础切片。
6. 接入真实 LLM Planner/叙事适配器，并在规则不受模型影响的前提下评估方向与正文质量。Python CLI 是唯一运行时入口。

在第 4 步通过之前，不提前开发 Web 界面或小说导入能力。

标准 UTF-8 `.txt` 母本先运行 `python3 -m open_story_engine inspect-source <小说路径> --output <清单路径>`，再运行本地 Python 的 `analyze-source <小说路径> --output <语义候选路径>`。该步骤不会调用模型或发送母本文字。`build-story-package <小说路径> <语义候选路径> --id <包ID> --version <版本> --output <新包路径> --audit-output <审计路径>` 由 Python 生成角色、地点、物品、关系、时间线、剧情锚点、身份入口和宏观/章节方向；构建会在写入前强制通过审计。每个版本目录包含 `package.json`、`analysis.json`、`audit.json`、同源哈希绑定的本地 `reader.json` 和 `modules/` 模块目录。模块目录由 `package-index.json` 和 `runtime-index.json` 管理，离散保存世界、状态模型、主剧情图、场景节点、宏观方向、身份入口、剧情拍点、角色、地点、物品和关系；章节原文只在 `reader/` 模块。共创运行时从经哈希校验的模块投影加载规则与摘要，不读取根包中的原著节选；启动时只读取核心模块和索引，场景节点、宏观方向、身份入口和剧情拍点在会话实际需要时再按需加载。缺少模块目录的历史包保留单文件兼容加载。流程不会覆盖母本或既有故事包，完整契约见[小说母本输入基线 v0.1](docs/source-novel-input-v0.1.md)。

开发试玩运行 `python3 -m open_story_engine play`。每段剧情后会列出当前可达的高层方向；可输入方向编号，或直接输入自定义的自然语言行动。完整说明见[连贯叙事推进 v0.1](docs/narrative-progression-v0.1.md)。

动态树迁移版可运行 `python3 -m open_story_engine co-create`；它先按 `StoryPackage.story.entryModel` 选择既有角色或新建角色，再按身份选择可进入的关键剧情节点。运行时模型只获得该入口之前声明的时间线摘要、当前状态和已确认分支摘要，不读取母本 TXT 或原著节选。默认使用 Mock Planner 验证契约和分支节点写入，也可输入自然语言方向。Mock 只生成确定性的结构测试草稿，不能用于正文篇幅、连贯性或可读性验收；偏离原著的实际章节必须使用 `STORY_PLANNER=openai` 的真实模型运行验证。完成一个分支后可用 `derive <后续目标>` 创建会话隔离的 `DerivedStoryPackage`；它引用但不改写原始 `StoryPackage`。真实 Planner 只写小说正文，章节名、状态、后续菜单和跨回合实体均由本地规则决定；尚未登记的命名人物、地点或因果物品不能被正文当作后续可复用事实。世界约束与叙事禁则来自各自的 `StoryPackage.world`，Mock 仅提供固定验收样本。`.env` 仅供本机使用且被 Git 忽略，`.env.example` 是可共享的配置模板。完整迁移边界见[Python 迁移 v0.1](docs/python-migration-v0.1.md)。

要启用真实 OpenAI-compatible Planner，在 `.env` 中设置：

```dotenv
STORY_PLANNER=openai
STORY_PACKAGE_ID=taixu-relics-part1
STORY_PACKAGE_VERSION=0.1.2
STORY_LLM_API_KEY=...
STORY_LLM_MODEL=...
STORY_LLM_BASE_URL=https://你的中转站地址/v1
# CLI 默认使用 SSE，即时展示尚未提交的正文草稿。
STORY_LLM_STREAM=true
# 正文 SSE 首段等待上限，默认 30 秒；结构化 JSON 请求不使用 SSE。
STORY_LLM_FIRST_DELTA_TIMEOUT_SECONDS=30
# 保持自动 SSE/JSON 回退；若端点 SSE 不可靠，可与 STREAM=false 配合关闭。
STORY_LLM_TRANSPORT_FALLBACK=true
# 单次模型请求的超时上限，默认 30 秒。
STORY_LLM_TIMEOUT_SECONDS=30
# 单次正文模型响应的最大 token 预算，默认 8192；减小可控成本但可能不足以输出 2,000 字正文。
STORY_LLM_MAX_TOKENS=8192
# 可选：正文通过确定性校验后做审阅；审阅意见仅写入 audit，不会自动重写本回合。
STORY_LLM_QUALITY_REVIEW=false
```

`STORY_PACKAGE_ID` 与 `STORY_PACKAGE_VERSION` 固定本次运行加载的内容包，当前默认分别为 `taixu-relics-part1` 与 `0.1.2`；新规则必须新建包版本。运行目录仅保存用户实际导入或构建的故事，回归测试独立读取测试专用样本。CLI 试玩前须先构建故事包，或指定已导入包的 ID 与版本。`STORY_LLM_BASE_URL` 必须是 OpenAI-compatible API 前缀，不要包含 `/chat/completions`。正文 Planner 默认使用 SSE 展示未提交草稿，首段等待上限由 `STORY_LLM_FIRST_DELTA_TIMEOUT_SECONDS` 控制，传输失败时可按剩余时间回退到 JSON；每次请求默认 30 秒超时，正文最大 token 预算默认 8192。互动场景规划目标为 80 至 1,500 个中文字符，短而完整的行动不以填充文字满足最低长度；空正文、超长正文、状态事实冲突或未登记命名因果都会被拒绝且不会写入分支。可用 `llm-audits` 查看模型调用和拒绝记录。

以下 `evaluate-live` 是旧短篇评估命令，已停用，不再执行。历史配置记录：显式设置 `STORY_LIVE_EVALUATION=1` 后运行 `python3 -m open_story_engine evaluate-live --output /private/tmp/open-story-engine-python-live.json`，它会以受限调用次数在隔离内存会话中运行六个场景；真实模型场景固定使用 JSON、60 秒超时且不发生 JSON/SSE 传输降级。详见[真实 LLM 评估 v0.1](docs/live-llm-evaluation-v0.1.md)。
