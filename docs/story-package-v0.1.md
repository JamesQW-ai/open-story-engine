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
  "story": {},
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
- `monotonicEnums`：只能按声明顺序推进的状态序列，例如“失联 -> 已定位 -> 获救”。
- `immutableFields`：分支方向不得改写的会话范围或身份字段。
- `transitionRules`：在满足 `when` 时必须按声明方式变化的计数或阶段字段。
- `invariants`：条件成立时必须同时满足的跨字段事实；`equals`、`oneOf` 与 `sameAs` 分别表示相等、属于候选值和与另一状态字段相等。
- `narrativeAssertions`：条件成立时禁止出现在正文中的正则模式，以及相应的提示词约束和拒绝信息。
- `derivativeEntry`：满足前置状态后进入衍生篇时的首个受控节点与状态补丁。
- `mockFollowups`：仅供离线 `MockPlanner` 验收使用的状态到后续方向模板；真实 Planner 不读取它，不能承载母本事实或生产剧情。

例如，某部小说可以声明“角色 A 未脱困时的位置必须是地点 X”；另一部小说可以声明“角色 B 获救后与焦点人物同行”。二者都只是包数据，运行时不需要知道角色和地点的显示名称。模型只能提交候选补丁，最终仍由 `stateModel` 校验。

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

### `directions` 与 `defaultDirectionId`

`StoryDirection` 是玩家在一局故事开始时选择或声明的高层叙事意图，例如“优先救出失联的朋友”或“先查清这场事故的真相”。它决定系统在开局和后续叙事中优先强调的目标、冲突与回收方式；它不是一回合内的具体操作。

每个方向至少包含稳定 `id`、面向玩家的标题与简介、`primaryGoal`、可优先加载的节点或上下文，以及可达结局的范围。`defaultDirectionId` 指向包的默认方向。

MVP 只从 `defaultDirectionId` 创建会话，不实现方向选择器，也不接受用户自定义方向。这样故事包先验证“方向约束下的自由行动”是否成立。后续版本可提供预置方向选择；再之后才允许用户用自然语言声明自定义方向，并将其校验、固化为该会话的故事契约，而不是直接修改原始包。

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
- `currentNodeId`、`currentLocationId` 与 `directionId`，其中节点必须等于 `story.startNodeId`，方向必须等于 `defaultDirectionId`。
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

## 后续演进

未来支持用户导入小说时，导入器只能输出待审核的候选包，并保留来源、解析置信度与待确认项。通过人工或用户确认、满足本契约后，候选包才可成为引擎输入。

共创模式将另行扩展 `story.mode` 和故事契约字段；在此之前，不为未知需求提前加入可选字段或通用脚本语言。
