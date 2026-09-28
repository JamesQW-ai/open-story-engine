# open-story-engine

基于预置故事包运行的高自由度互动叙事引擎。玩家通过自然语言表达行动；系统在可审计的规则、状态与剧情边界内推进故事，并生成可持续阅读的下一段叙事。

当前产品为桌面端官方长篇互动阅读：官方先冻结十万汉字及以上小说，玩家以未知原著的视角选择预设身份，通过自然语言行动形成自己的路线。不提供玩家上传、已读节点介入或从零创作整部小说。以 [产品方向](docs/product-direction.md)、[验收标准](docs/mvp-acceptance.md) 和 [开发计划](docs/next-development-plan-2026-09-15.md) 为准。

## 当前交付入口（2026-09-28）

最新口径、可用记录与已知问题统一从 [交付导航](docs/delivery-index-2026-09-28.md) 阅读。当前用上下文工程约束正文，非空实质正文保存后原样交付，不做逐句审核打回或展示后改写；Jev 关闭。状态提取中的不确定事项留待后续澄清，不能冒充权威事实。

本文下方保留早期接口与 Jev 试验记录作为追溯，其中旧审核门禁和阶段验收结论不代表当前交付策略。

## 当前测试入口（2026-09-16）

功能测试和桌面夹具统一使用十万汉字长篇。《雨夜候车室》已停用，历史测试仅保留追溯，不能执行或计入当前通过数。清单自动扫描所有达标母本，并要求匹配官方运行包；当前唯一达标小说为《太虚遗录》第一部，102610 CJK、50 章。当前包已接通 7 个可玩身份入口；自然结局由故事包终点和场景完成结果自动登记，不需要玩家手动结束。

```sh
python3 -B -m test_support.run core
.venv-api/bin/python -B -m test_support.run api
cd web && npm ci && npm test && npm run build
```

首次准备前端环境或 `web/node_modules` 不存在时，必须先执行 `cd web && npm ci`；依赖版本由 `web/package-lock.json` 锁定。

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
STORY_PACKAGE_VERSION=0.1.3
STORY_LLM_API_KEY=...
STORY_LLM_ROUTE=direct
STORY_LLM_MODEL=deepseek-v4-flash
STORY_LLM_BASE_URL=https://你的中转站地址/v1
STORY_LLM_DIRECT_BASE_URL=https://api.deepseek.com
STORY_LLM_DIRECT_MODEL=deepseek-v4-flash
STORY_LLM_DIRECT_API_KEY=...
# 底层 Planner 可使用 SSE 接收模型增量；玩家续写 API 会在提交成功后再回放正文。
STORY_LLM_STREAM=true
# 正文 SSE 首段等待上限，直连 Flash 当前使用 15 秒；结构化 JSON 请求不使用 SSE。
STORY_LLM_FIRST_DELTA_TIMEOUT_SECONDS=15
# 保持自动 SSE/JSON 回退；若端点 SSE 不可靠，可与 STREAM=false 配合关闭。
STORY_LLM_TRANSPORT_FALLBACK=true
# 单次模型请求的超时上限，当前直连实测环境使用 30 秒。
STORY_LLM_TIMEOUT_SECONDS=30
# 可选：结构化审核 JSON 的 token 上限；默认跟随正文预算，需先做真实 A/B。
# STORY_LLM_JSON_MAX_TOKENS=8192
# 单次模型响应的结构化审核 token 预算上限，默认 8192。
STORY_LLM_MAX_TOKENS=8192
# 玩家续写正文的独立输出预算，当前直连 Flash 实测采用 2048；互动正文上限为 1,500 个汉字。
STORY_LLM_TEXT_MAX_TOKENS=2048
# 关闭隐藏推理预算，避免结构化规划只返回 reasoning_content 而没有 JSON 正文。
STORY_LLM_REASONING_EFFORT=none
# 上下文投影闸门；官方包声明全部完成前保持兼容审计模式。
STORY_CONTEXT_PROJECTION_STATE_VISIBILITY_MODE=audit_fallback
# 正文生成后的独立审核请求并行执行；供应商限制单请求时可设为 false。
STORY_LLM_PARALLEL_REVIEWS=true
# 审核失败时保留未确认草稿并返回可重试失败，不新增正式分支。
# 旧 STORY_LLM_CONSERVATIVE_FALLBACK 已停用，即使设为 true 也不会放行。
# 可选：正文通过确定性校验后做审阅；审阅意见仅写入 audit，不会自动重写本回合。
STORY_LLM_QUALITY_REVIEW=false
```

正文生成的 `STORY_LLM_ROUTE` 可设为 `relay` 或 `direct`：`relay` 读取 `STORY_LLM_BASE_URL`、`STORY_LLM_API_KEY`、`STORY_LLM_MODEL`，`direct` 读取 `STORY_LLM_DIRECT_BASE_URL`、`STORY_LLM_DIRECT_API_KEY`、`STORY_LLM_DIRECT_MODEL`。当前直连实测环境使用 `STORY_PLANNER=openai`、`STORY_LLM_ROUTE=direct`、30 秒总请求上限、15 秒正文首段等待、2048 正文预算和 8192 结构化审核预算。两条正文路由均使用 Flash 模型；不要把 DeepSeek 模型配置到 OpenRouter。Jev 剧情审核使用独立的 `JEV_OPENROUTER_BASE_URL`、`JEV_MODEL` 和 `JEV_API_KEY`，走 Decisions API，不与正文生成网关混用。整回合实验开关 `JEV_RUNTIME_REVIEW_MODE` 默认是 `off`，可临时设为 `shadow` 记录提交前审核，只有经过独立验收后才考虑 `block`。

### 真实长篇多动作试验（2026-09-23）

在《太虚遗录》第一部 `taixu-relics-part1@0.1.3`（102610 CJK）上，直连 `deepseek-v4-flash` 使用临时数据库执行了三种不同自由行动。一次抓取中三种动作均写入成功；为保存候选正文而进行的第二次抓取中，前两种动作写入成功，首字节分别为 16.8 秒和 14.6 秒，完整提交分别为 66.1 秒和 47.7 秒，调用均无未报告失败；第三种静默观察动作在生成阶段失败，返回 `generation_failed`，没有写入分支，也没有送入 Jev。详见 [多动作直连证据](docs/evidence/jev-narrative-review-2026-09-22/live-direct-varied-actions-2026-09-23.json)。

前两条已提交正文随后以独立 shadow 请求送入 Jev Decisions API，均返回 `allow_with_patch`，审核延迟为 5782 ms 和 1100 ms，L0 均通过，L3 最低观察分分别为 67 和 82。Jev 调用发生在提交之后，没有阻断或修改分支；这组结果只能证明审核器已连通并能对真实长篇候选给出结构化判断，不能替代人工逐段阅读，也不能宣称已完成集成运行时验收。详见 [多动作 Jev 复核](docs/evidence/jev-narrative-review-2026-09-22/live-direct-varied-actions-jev-review-2026-09-23.json)。

随后复用了 `jev-narrative-review-expanded.json` 的 34 个边界案例，覆盖 `allow`、`allow_with_patch`、`rewrite` 和 `reject`。Jev 与现有工程预期标签一致 34/34；31 个请求进入 provider，p50 为 1015 ms、p95 为 2276 ms、最大值为 6069 ms，另外 3 个因证据不足、权威冲突或缺少结果契约被本地前置检查拒绝。该准确率是与工程标签的一致性指标，不是人工质量真值；扩展回放仍属于 shadow 测试，不改变故事状态。详见 [Jev 扩展边界回放](docs/evidence/jev-narrative-review-2026-09-22/shadow-replay-expanded-2026-09-23.json)。

为验证时序，又在临时数据库中对一条真实自由行动做了同回合提交前 shadow：writer 先生成，Jev 审核完成后才调用故事提交，最终分支成功写入。该回合总耗时 34611 ms，Jev 耗时 5262 ms，提交耗时 221 ms，Jev 返回 `allow_with_patch`，L3 最低观察分 80。此处使用一次性 monkeypatch 验证时序，尚未改动生产提交路径，也没有让 Jev 阻断写入；证据见 [提交前 Jev shadow](docs/evidence/jev-narrative-review-2026-09-22/live-precommit-shadow-2026-09-23.json)。

三动作批量提交前 shadow 进一步显示：1 条动作完成 writer→Jev→commit，Jev 耗时 1151 ms、提交耗时 220 ms；另外 2 条在 writer 阶段返回 `generation_failed`，没有候选正文、Jev 请求或分支写入。该批次的失败属于正文链路稳定性问题，不能归因于 Jev；在扩大样本并定位 writer 失败原因前，不应启用阻断式审核。详见 [提交前批量 shadow](docs/evidence/jev-narrative-review-2026-09-22/live-precommit-shadow-batch-2026-09-23.json)。

此前一次诊断确认，失败动作的 provider 请求均返回 HTTP 200 且内容非空；本地安全校验拒绝了模型把已有知识前提 `K2` 改分类以规避证据要求，错误码为 `model_output_rejected`，随后重试仍未形成可提交场景计划。该规则属于 writer 的事实与知识边界保护，不能通过放宽校验修复；类型化摘要见 [K2 校验失败摘要](docs/evidence/jev-narrative-review-2026-09-22/live-writer-failure-k2-summary-2026-09-23.json)。

强化规划提示后又进行了两次同类动作复测：一条因 provider JSON 响应连续两次超时而失败，另一条成功提交；这说明提示修正尚不足以证明 writer 稳定性，provider 超时和本地校验拒绝需要分别统计与处理。完整类型化记录见 [提示强化后诊断](docs/evidence/jev-narrative-review-2026-09-22/live-writer-failure-diagnostics-postprompt-2026-09-23.json)。

为缩短串行等待，正文生成后的 `action_review`、`scene_grounding`、`scope_review` 以及终局分支的两个独立复核现在会在真实 OpenAI-compatible 网关上并行执行；审核项目、提示内容和本地硬校验均未减少，测试假网关仍保持串行以维护确定性。带请求起止时间的直连探针确认三类复核实际重叠；开启并行的单动作首轮成功写入约 41.8 秒，关闭并行的同类探针因连续修订和超时在 105.9 秒失败。该对照受 provider 波动和生成内容差异影响，不能作为固定耗时承诺；后续仍需扩大样本并分别记录正文、复核和 Jev 的 P50/P95。详见 [并行复核探针](docs/evidence/jev-narrative-review-2026-09-22/live-writer-parallel-probe-2026-09-23.json) 与 [串行复核探针](docs/evidence/jev-narrative-review-2026-09-22/live-writer-serial-probe-2026-09-23.json)。

结构化 JSON 上限也做过临时 `4096` token 对照，但该回合在三轮复核中因背景证据语义问题被拒绝，调用数升至 19，不能据此降低默认预算；代码保留 `STORY_LLM_JSON_MAX_TOKENS` 作为供应商专属 A/B 开关，当前默认仍跟随 8192。详见 [JSON 预算对照](docs/evidence/jev-narrative-review-2026-09-22/live-writer-parallel-json4096-2026-09-23.json)。

扩大到 5 个不同自由行动的最新批次后，1 条成功提交（25.2 秒），4 条在 writer 阶段失败（14.7–56.9 秒）；失败均未写入分支，主要是 DeepSeek 多轮重试、事实边界拒绝或传输回退，不能归因于 Jev。一次提交前 Jev shadow 的总耗时为 50.7 秒，其中 Jev 5.8 秒、提交 0.2 秒，返回 `allow_with_patch` 并成功写入。该证据确认并行复核已经生效，但 writer 稳定性仍是整体长尾的主因；Jev 仍未进入正式阻断路径。详见 [5 动作延迟批次](docs/evidence/jev-narrative-review-2026-09-22/live-writer-latency-batch-parallel-2026-09-23.json)、[并行提交前 Jev](docs/evidence/jev-narrative-review-2026-09-22/live-precommit-shadow-parallel-2026-09-23.json)。

随后将正文预算从 4096 调到 2048 做同类单回合 A/B：直连 `deepseek-v4-flash` 审核通过并写入，用户首字 23.384 秒；此前 4096 对照为 39.974 秒。结构化审核仍保持 8192，未提前展示未确认正文。该数字是单次对照，后续仍需扩展 P50/P95 样本；证据见 [2048 正文预算首字对照](docs/evidence/jev-narrative-review-2026-09-22/live-first-visible-2026-09-23-text-budget-2048.json)。

动态篇幅极端实测中，短动作规划区间为 `[80,160]`，实际正文 208 CJK，审核提交后用户首字 23.499 秒；长动作在 2048 正文预算下于生成或二次审核失败，用户首字为 0。仅进程内提高到 4096 仍二次审核失败且耗时 57.054 秒，因此正式直连环境收紧为 30 秒总超时、15 秒正文首段等待，仍保持 2048 正文预算和 8192 结构化审核预算。长篇幅上界的真实成功样本待 writer 稳定后补测，不能把内部生成首段当作用户首字。详见 [动态篇幅极端实测](docs/evidence/jev-narrative-review-2026-09-22/live-length-extremes-2026-09-23.json)。

此前在不修改用户 `.env` 的前提下，临时开启 `STORY_LLM_TRANSPORT_FALLBACK=true`、总超时 30 秒、首段等待 15 秒做对照：同类 writer 动作连续 2/2 成功，单回合约 29.4–29.5 秒；随后一条提交前 Jev shadow 也完成 writer→Jev→commit，总耗时 36505 ms，Jev 5900 ms，提交 215 ms，分支成功写入。当前 `.env` 已启用 `STORY_LLM_TRANSPORT_FALLBACK=true`，用于在 SSE 无正文时只做一次同期限 JSON 回退；两种传输均失败时不再重复整轮重试。详见 [回退开启 writer 诊断](docs/evidence/jev-narrative-review-2026-09-22/live-writer-fallback-enabled-2026-09-23.json) 和 [回退开启提交前 Jev](docs/evidence/jev-narrative-review-2026-09-22/live-precommit-shadow-fallback-enabled-2026-09-23.json)。

续写流的安全时序已固定：首稿和一次有界修复都只在服务端缓冲；候选完成审核并成功写入分支后，才通过 `replay_approved` 回放正文。首轮通过直接回放，首轮失败最多修复一次，二次仍失败则不展示未确认候选、保留原分支并返回可重试错误（`retryable=true`、`candidateShown=false`、`previousBranchUnchanged=true`）。这保证用户不会先读到随后被撤回的正文；后续延迟优化必须保持这一提交前不可见边界。

`STORY_PACKAGE_ID` 与 `STORY_PACKAGE_VERSION` 固定本次运行加载的内容包，当前默认分别为 `taixu-relics-part1` 与 `0.1.3`；新规则必须新建包版本。运行目录仅保存用户实际导入或构建的故事，回归测试独立读取测试专用样本。CLI 试玩前须先构建故事包，或指定已导入包的 ID 与版本。`STORY_LLM_BASE_URL` 必须是 OpenAI-compatible API 前缀，不要包含 `/chat/completions`。正文 Planner 可使用 SSE 接收模型增量，首段等待上限由 `STORY_LLM_FIRST_DELTA_TIMEOUT_SECONDS` 控制，传输失败时可按剩余时间回退到 JSON；玩家续写 API 会在审核和权威提交完成后才回放正文。每次请求默认 30 秒超时，玩家正文预算当前为 2048，结构化审核预算仍为 8192。互动场景规划目标为 80 至 1,500 个中文字符，短而完整的行动不以填充文字满足最低长度；空正文、超长正文、状态事实冲突或未登记命名因果都会被拒绝且不会写入分支。可用 `llm-audits` 查看模型调用和拒绝记录。

以下 `evaluate-live` 是旧短篇评估命令，已停用，不再执行。历史配置记录：显式设置 `STORY_LIVE_EVALUATION=1` 后运行 `python3 -m open_story_engine evaluate-live --output /private/tmp/open-story-engine-python-live.json`，它会以受限调用次数在隔离内存会话中运行六个场景；真实模型场景固定使用 JSON、60 秒超时且不发生 JSON/SSE 传输降级。详见[真实 LLM 评估 v0.1](docs/live-llm-evaluation-v0.1.md)。
