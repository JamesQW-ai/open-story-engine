# StoryPackage v0.1

## 决策状态

本文定义 MVP 的 `StoryPackage` 内容契约。它是游戏引擎创建新存档时读取的、版本化的初始内容输入；首版仅支持一个人工制作或已获授权的预置包。

本契约服务于一条约 30 分钟、包含 3 至 5 个关键节点与 2 至 3 个结局的主线试玩。它不是“任意小说解析”的通用中间格式，也不要求在此版本覆盖共创模式。

## 边界

```text
StoryPackage（固定、版本化内容）
  + 新存档创建
  -> initialState（运行期快照）
  -> 玩家行动 / RuleEngine
  -> game_events（运行期历史）
  -> Narrator（仅依据已确认结果写作）
```

`StoryPackage` 是权威内容来源，但不是运行期唯一输入。它不包含或修改下列数据：

- 某位玩家的当前属性、背包、关系、旗标和当前场景。
- 玩家的自由文本输入、检定结果、状态差异和事件历史。
- LLM 请求、原始响应、提示词版本或调用错误。
- 未经整理的原作全文、用户上传的原始文件或模型自行推断的事实。

这些数据分别属于 `GameState`、`game_events` 和 `llm_audits`，由引擎在运行期创建、校验和持久化。

## 顶层结构

```json
{
  "schemaVersion": "1.0",
  "id": "stable-package-id",
  "version": "semantic-content-version",
  "metadata": {},
  "world": {},
  "characters": [],
  "locations": [],
  "items": [],
  "timeline": [],
  "story": {"entryModel": {}},
  "directions": [],
  "defaultDirectionId": "direction-id",
  "rules": {},
  "initialState": {},
  "stateModel": {}
}
```

所有实体 `id` 在包内必须唯一且稳定。运行中的会话只保存这些 ID 的引用，避免通过显示名称匹配内容。版本更新不得无提示地改变已发布实体的含义；若影响既有存档，需新增包版本并声明迁移策略。

### `stateModel`

`stateModel` 将母本中可随剧情推进的事实声明为运行时可执行的规则。引擎不认识任何特定的人名、地点名或状态字段；它只读取此结构。因此导入新的小说时，解析器与人工审核应产出新的实体目录和状态声明，而不是修改 Python 代码。

- `locationReferenceFields`：哪些状态字段必须引用已登记地点，例如焦点人物和同行角色的位置。
- `characterLocationIds`：脚本生成包的会话级角色到地点映射。仅本地方向解析器能因玩家明确点名的同行或带路行动更新它；它不属于母本固定内容，也不由正文模型输出。
- `monotonicEnums`：只能按声明顺序推进的状态序列，例如“失联 -> 已定位 -> 获救”。
- `immutableFields`：分支方向不得改写的会话范围或身份字段。
- `transitionRules`：在满足 `when` 时必须按声明方式变化的计数或阶段字段。
- `invariants`：条件成立时必须同时满足的跨字段事实；`equals`、`oneOf` 与 `sameAs` 分别表示相等、属于候选值和与另一状态字段相等。
- `narrativeAssertions`：条件成立时禁止出现在正文中的正则模式，以及相应的提示词约束和拒绝信息。
- `derivativeEntry`：满足前置状态后进入衍生篇时的首个受控节点与状态补丁。
- `mockFollowups`：仅供离线 `MockPlanner` 验收使用的状态到后续方向模板；真实 Planner 不读取它，不能承载母本事实或生产剧情。

例如，某部小说可以声明“角色 A 未脱困时的位置必须是地点 X”；另一部小说可以声明“角色 B 获救后与焦点人物同行”。二者都只是包数据，运行时不需要知道角色和地点的显示名称。模型只能提交候选补丁，最终仍由 `stateModel` 校验。

### 分支状态账本

母本 StoryPackage 始终只读。每个共创分支的 `branchState.branchLedger` 由运行时受控追加，使用 `branch-state-ledger/0.1` 记录角色、地点、物品、关系、线索、事件的新增或变化，以及每条事实的前后状态和来源（方向、玩家输入、母本或规划器）。`derived*` 实体目录只保存分支私有实体，账本才是跨回合因果的权威记录；节点 `summary` 和正文片段不能作为因果状态的来源。创建衍生故事时，运行时将账本快照写入 `session_derived_story_packages.branchLedger`，其后只允许追加条目和修订记录，不能改写母本引用或既有账本。

## 字段定义

### `metadata`

用于识别内容来源和展示基本信息。

| 字段 | 要求 |
| --- | --- |
| `title` | 面向玩家的故事标题。 |
| `summary` | 不剧透或仅轻度剧透的开局简介。 |
| `authoringSource` | `original` 或明确授权的来源说明，不存放原文。 |
| `contentRating` | MVP 的内容分级或警示。 |
| `language` | 叙事文本的默认语言，例如 `zh-CN`。 |

### `world`

世界是所有场景共享的不可违背约束。

- `premise`：一句至一段的世界前提。
- `immutableFacts`：无法被玩家或叙事器改写的事实，例如力量体系、历史结果、已确认人物生死。
- `narrativeGuidelines`：叙事视角、语气、时态、单回合篇幅和避免事项。
- `globalConstraints`：适用于所有场景的限制，例如不存在瞬移、资源消耗有后果。

这里不存放可随剧情改变的事实；它们应由 `GameState` 的旗标、关系或地点状态表示。

### `characters`、`locations` 与 `items`

三个集合共同构成引擎可引用的实体目录。每个实体至少应包含：

- 稳定 `id`、显示名称与可供叙事器调用的简短描述。
- 初始可见性和关联地点或持有者。
- 规则层需要读取的标签或能力值。

角色额外包含公开动机、隐藏信息、关系初值和行为边界。隐藏信息必须有明确的解锁条件，不能仅以自然语言备注期待模型自行保密。

地点额外包含可用出口、初始状态和可发现线索。物品额外包含用途、是否可消耗及对状态或行动的确定性影响。

### `timeline`

`timeline` 只保存开局前已发生、且会影响因果关系的事件。每个事件应记录：

- `id`、发生顺序或时间点，以及简述。
- 已知范围：玩家开局已知、需通过线索得知、永久保密。
- 其导致的当前事实或关联实体。

运行后的新事件绝不回写此集合，而写入会话的 `game_events`。

### `story`

`story` 是主线可读性和收束的来源，而非固定分支树。

| 字段 | 要求 |
| --- | --- |
| `mode` | MVP 固定为 `mainline`。 |
| `premise` | 玩家进入这段故事时的处境。 |
| `longTermGoal` | 故事完成时要回答的核心问题或解决的冲突。 |
| `startNodeId` | 新会话的起始节点。 |
| `nodes` | 3 至 5 个关键节点，每个节点有近目标、张力、入口条件和退出条件。 |
| `endings` | 2 至 3 个结局，需有明确达成条件和结局文本锚点。 |
| `narrativeGraph` | 规则节点之间可审阅的叙事锚点、条件边与结局锚点映射。 |

### `story.entryModel`

共创入口由故事包数据声明，而不是由 Python 针对某部小说的角色、地点或章节写条件。它将“选择身份 -> 选择剧情节点”固定为可审计的会话输入：

- `sourceCharacterIds`：可作为既有身份进入的主要角色 ID。仅有姓名的龙套仍留在实体目录中，但不必出现在这里。
- `newCharacter.enabled`：是否允许新建会话角色。由编包脚本生成的 `profileFields` 固定包含姓名、性别、年龄、职业、与原著角色或势力的关系、个人背景；档案只写入该会话的 `SessionStoryContract`，不回写母本故事包。
- `entryPoints`：每项包含稳定 `id`、展示用 `title`、`summary`、`chapterTitle`，以及一致的 `nodeId` 与 `beatId`。脚本生成包还会声明 `sourceChapterId`，供同版本目录中的 `reader.json` 在本地找到完整章节；该字段不进入 Planner 上下文。`sourceCharacterIds` 决定既有角色能看到哪些节点，`availableToNewCharacter` 决定新角色可选择的主要节点。
- `timelineRefs`：该入口之前应提供给运行时的压缩时间线摘要 ID。Planner 只能取得这些摘要、当前状态和已确认分支摘要，不读取 TXT、不检索章节原文，也不接收母本全文。
- 允许新建角色的入口必须有 `newCharacterNarrative`。这是该身份的包内开场锚点，避免把另一个原著角色的原文片段误当成新角色已经经历的事实。

`defaultEntryPointId` 只用于无界面调用的兼容默认值。正式界面应先列出包声明的主要身份，再按身份过滤剧情节点；每个入口对应的章节内容可由阅读界面展示，但其原文不作为 Planner 上下文。`entryModel` 是来源分析和审核后的编包产物，不得由运行时根据当前示例小说推断或补写。

### `directions` 与 `defaultDirectionId`

`StoryDirection` 是玩家在一局故事开始时选择或声明的高层叙事意图，例如“优先救出失联的朋友”或“先查清这场事故的真相”。它决定系统在开局和后续叙事中优先强调的目标、冲突与回收方式；它不是一回合内的具体操作。

每个方向至少包含稳定 `id`、面向玩家的标题与简介、`primaryGoal`、可优先加载的节点或上下文，以及可达结局的范围。`defaultDirectionId` 指向包的默认方向。

`defaultDirectionId` 是规则层的兼容默认方向，不替代共创的角色或进入节点选择。会话建立后，运行时先根据 `story.entryModel` 固化身份与节点，再公布由 `arcModel` 声明的大方向。用户可用自然语言表达当前意图，但只能锚定到已公布的合法方向，不能直接改写原始包。

`StoryDirection` 与每回合 `ActionIntent` 的职责必须分开：前者回答“这局故事想走向哪里”，后者回答“此刻玩家尝试做什么”。方向不应写成“检查某个物品”或“询问某个角色”。

### `story.arcModel`

共创模式可在 `story.arcModel` 声明可验证的两层剧情推进。`entryArcIds` 是当前进入节点可选择的大方向；每个 `arcs[]` 包含稳定 `id`、面向玩家的 `title` 与 `summary`、`completionWhen`、归属的 `phaseDirectionIds`，以及可选的状态门槛 `availableWhen` 和完成后的 `nextArcIds`。

大方向不含 `statePatch`，它只确立跨章节目标。`phaseDirectionIds` 指向 `narrativeGraph` 中已声明的、带非空 `statePatch` 的小方向；一次小方向选择生成并结算一章。运行时在该章结束后检查 `completionWhen`：未满足则继续公布同一大方向内当前状态可执行的小方向，满足则只公布 `nextArcIds` 中满足 `availableWhen` 的新大方向。一个小方向只能属于一个大方向，避免同一状态变化被两个阶段重复结算。

每个 `StoryNode` 至少定义：

- `objective`：玩家当前清楚可感知的近目标。
- `sceneSetup`：进入该节点时必定成立的场景事实。
- `requiredProgress`：推进该节点所需的已确认条件，而非指定唯一行动。
- `pressure`：若玩家拖延、失败或偏离时增加的风险、代价或悬念。
- `transitions`：满足条件后可转入的节点；允许多个行动路径汇聚到同一转折。
- `contextRefs`：本节点给行动解析和叙事生成加载的实体、世界事实和近期线索引用。

节点不应以“玩家必须选择 A 或 B”描述。玩家可以自由表达意图；`RuleEngine` 判断其效果是否满足节点条件，`Narrator` 将结果表现为自然叙事。

`narrativeGraph` 不替代 `nodes` 或 `rules`：节点负责规则推进，图负责在已确认的状态下选择能够承接上一段正文的剧情锚点。每个 `NarrativeBeat` 必须有 `narrativeAnchor`，它是运行时承接和模型上下文使用的简短文本，不得冒充原文。若该节点来自原著，则可附 `sourceExcerpt`，其中的 `text` 必须逐字来自母本，`lineRange` 必须能定位母本行号；偏离原著的分支不得伪造 `sourceExcerpt`。每条图边都必须有稳定 ID、来源与目标锚点、条件、规范性标记和仅属于该转移的承接文本。完整规则见[连贯叙事推进 v0.1](narrative-progression-v0.1.md)。

共创场景窗口由 `story.sceneRoutes` 补充声明。每条路线固定 `fromNodeId`、`fromLocationId`、`toLocationId` 与 `toNodeId`，并且必须对应可达地点与两个节点的地点上下文。动态方向只可通过 `statePatch.playerLocationId` 请求移动；服务层以路线表解析目标场景，Planner 不得输出场景节点 ID。这样地点变化、状态校验和模型上下文会在同一受控决策中切换。

`story.rejoinTargets` 声明从已偏离分支回到一个兼容叙事锚点的检查条件。每项包含来源场景 `fromNodeId`、目标锚点 `targetBeatId` 与必须仍未关闭的 `requiredOpenThreads`。目标锚点自身的完整 `branchState` 是隐含的状态契约：服务会在方向补丁生效后进行精确比较。

方向可选 `rejoinTargetId`，但只有服务在玩家选中方向时验证所有条件才会写入 `canonicalRelation: rejoined`。它不授权直接复用目标锚点的 `sourceExcerpt`。

偏离规范线却仍有固定后续菜单时，方向可声明 `followupBeatId`。该字段指向同一包内的叙事锚点，运行时据此读取该锚点的 `nextDirections`；不得在 Python 服务层按具体方向 ID 编写映射。

### `rules`

`rules` 只描述代码能够权威执行的部分，并和后续 `RuleEngine` 一一对应。

- `actionTypes`：MVP 固定三种行动，例如 `investigate`、`negotiate`、`risk`。
- `checks`：属性、难度、随机范围和成功/部分成功/失败的规则。
- `effects`：合法的状态变化模板，如增加线索旗标、消耗物品、改变关系或切换节点。
- `guards`：不可违反的前置条件，例如地点不可达、物品不在背包、角色不在场。

任何无法由代码确定执行的“结果”不得写入 `rules`。它可以作为叙事建议存在于场景描述中，但不能作为修改状态的依据。

### `initialState`

`initialState` 是创建 `GameState` 的模板，而不是可变存档。它应包含：

- `player`：玩家角色的初始属性和身份。
- `inventory`、`relationships`、`flags` 与 `knownFacts` 的初值。
- `currentNodeId`、`currentLocationId` 与 `directionId`，其中默认模板的节点等于 `story.startNodeId`，方向等于 `defaultDirectionId`。共创会话若选择了 `entryModel.entryPoints` 中的节点，运行时从该节点对应的 `NarrativeBeat.branchState` 复制独立快照，不修改模板或原故事包。
- 已解锁的实体和开局可感知的冲突，不提供强制的具体行动菜单。

同一包创建的不同会话从模板复制，各自独立演化。模板内容在游戏中不可变。

## 首版验收条件

一个 `StoryPackage v0.1` 在进入代码实现前，应能通过以下内容审查：

1. 可在不调用 LLM 的条件下，从 `initialState` 推演到每一个结局。
2. 每个节点都有近目标、有效变化和未解决的张力。
3. 每个结局的条件都可由明确的状态或事件判定，不依赖模型主观判断。
4. 所有 `id` 引用存在且无循环死路；任一允许状态至少有一种可能行动。
5. 不可违背事实、运行期状态和叙事建议没有混在同一字段中。
6. 取当前节点及其 `contextRefs` 可组成一份有限、相关的模型上下文，不需要加载整个包。
7. 默认剧情方向以高层目标描述，且不要求玩家在任一节点执行某个指定动作。

## 脚本生成的模块目录

`build-story-package` 会为每个固定 StoryPackage 创建 `content/packages/<package-id>/<version>/` 版本目录。目录的固定入口是 `package.json`，同级的 `analysis.json`、`audit.json`、本地 `reader.json` 与 `modules/` 都属于同一次脚本构建结果。`modules/` 不是人工编辑的第二份故事包，而是同一来源、同一版本的可审计投影：`package-index.json` 保存源哈希、模块路径和每个模块的 SHA-256；`runtime-index.json` 只声明世界、状态、全局图谱、场景节点索引、宏观方向索引、身份入口索引、剧情拍点索引和实体索引这些运行时入口；`chapter-index.json`、`beat-index.json`、`entry-index.json`、`arc-index.json` 是根总清单，只保存分段定位所需的稳定 ID 与选择键，详细索引写入 `indexes/` 下的分段文件：章节按 `sourceProgress` 定位，拍点按当前章节定位，身份入口按所选角色定位，宏观方向按当前剧情节点定位。启动只读根总清单；只有实际选择或进入对应上下文时才读取分段。`node-index.json`、`arc-model.json`、`entry-model.json` 和各实体索引保留各自运行时的轻量入口；`main-story-graph.json` 保存全局骨架；`world.json` 和 `state-schema.json` 保存全局规则；场景节点、宏观方向、身份入口、剧情拍点、章节、角色、地点、物品和关系按稳定 ID 分文件。所有这些文件均为脚本生成物，不能作为人工编辑入口。

剧情节点模块只保留与按需上下文选择有关的压缩拍点、时间线、事实和 `contextRefs`。运行时可读取当前拍点及最多两个已发生拍点的摘要；不得读取后续拍点，也不得把当前章节中尚未到达拍点的摘要、事实或角色证据放入 Planner 上下文。完整母本章节只能写入同名 `reader/` 模块，供本地阅读器显示，不能作为 Planner 上下文。`audit-story-package-modules` 必须验证所有索引路径、覆盖范围和内容哈希；任何人工改写或文件遗漏均会导致审计失败。

`package.json` 是构建与审计时的完整、版本化快照；脚本会将 `package-index.json` 的 SHA-256 写入 `package.moduleIndexSha256`。具有 `runtime-index.json` 的脚本生成包在共创启动时只读取核心模块和索引，场景节点和宏观方向通过 `id`、身份入口在用户选中后、剧情拍点通过 `id` 与 `sourceProgress` 在实际访问时才从其模块加载；身份选择菜单只读取入口索引卡，不读取开场叙事和时间线明细。每个已读取模块均验证版本绑定与 SHA-256，并排除所有 `NarrativeBeat.sourceExcerpt`。会话会保存同一模块索引哈希，续局时若内容投影变化则明确拒绝混用。正文 Planner 再从该投影按需选择当前拍点、最多两个已发生拍点、当前地点、已登场角色和相关物品；角色卡片仅能使用当前拍点行范围及此前的证据，绝不读取 `reader/` 原著章节。缺少模块目录或运行时索引的历史包保留单文件兼容路径。

## 后续演进

未来支持用户导入小说时，导入器只能输出待审核的候选包，并保留来源、解析置信度与待确认项。通过人工或用户确认、满足本契约后，候选包才可成为引擎输入。

共创模式将另行扩展 `story.mode` 和故事契约字段；在此之前，不为未知需求提前加入可选字段或通用脚本语言。
