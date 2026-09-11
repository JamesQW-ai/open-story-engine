# 小说母本输入基线 v0.1

## 目的

本规范定义首个“小说母本 -> StoryPackage”流程的输入边界。当前以结构清晰的标准原创小说跑通脚本主导的机器可读故事包构建；不在这一阶段承诺自动修复格式损坏或从混乱文本推断剧情。

## 标准小说母本

- 使用 UTF-8 编码的 `.txt` 文件，正文的第一行是小说标题。
- 文件不得包含 YAML frontmatter、故事包 ID、状态旗标、提示词、实现说明或其他元数据。
- 正文按自然段组织，段落以空行分隔；章节可使用普通小说标题，例如“第一章”或“一、雨夜”。
- 叙事采用一致的文学视角。`rainy-waiting-room.v0.1.txt` 使用第三人称限知视角。
- 母本必须有可辨识的开局、关键转折、人物动机、因果链、高潮和规范结局；它是可阅读的小说，不是行动菜单或规则表。
- 原著正文与后续的语义候选、故事包、运行期存档彼此独立保存。运行时不直接消费整篇正文。

## 当前流程

```text
标准原创 .txt 母本
  -> `inspect-source` 建立 SHA-256、章节、段落和范围索引
  -> `analyze-source` 按受限章节片段提取带段落引用的语义候选
  -> `build-story-package` 由 Python 编译完整 StoryPackage
  -> `audit-story-package` 校验源哈希、章节覆盖、实体引用、原文锚点和 schema
  -> `build-story-package` 同步生成模块索引与离散模块
  -> `audit-story-package-modules` 校验索引、文件覆盖与内容哈希
  -> 可执行 StoryPackage
```

当前实现的第一步只读取标准 TXT，并生成 `source-novel-manifest/0.1`。清单保存源文件 SHA-256、标题、章节、段落以及字符和行范围；候选角色、地点、关系、时间线、剧情节点均为空，并且整体状态固定为 `needs_review`。它不是可运行的 `StoryPackage`，也不会把任何推断写回母本。

```zsh
python3 -m open_story_engine inspect-source \
  content/source/rainy-waiting-room.v0.1.txt \
  --output /private/tmp/rainy-waiting-room.source-manifest.json
```

`analyze-source` 完全由本地 Python 脚本执行，不调用模型，也不会把母本文字发送到任何端点。它按章节自然段切成上限为 `12,000` 个字符的独立片段，并只从明确的文本模式生成保守候选；输出的角色、地点、物品、关系、事实和剧情节点必须引用该片段内的段落 ID。无法由规则确认的语义保持为空或 `needs_review`，不能由脚本补写。

```zsh
python3 -m open_story_engine analyze-source \
  content/source/rainy-waiting-room.v0.1.txt \
  --output /private/tmp/rainy-waiting-room.semantic-analysis.json
```

`build-story-package` 不让模型写执行 JSON。Python 只接受带证据的候选，并统一生成稳定 ID、角色卡、关系、章节时间线、规范锚点、状态推进、大方向到小方向的菜单、原著角色清单和新角色的姓名、性别、年龄、职业、与原著角色或势力的关系、个人背景六项档案。每个构建版本目录包含 `package.json`、`analysis.json`、`audit.json`、`reader.json` 和 `modules/`：阅读器保存完整章节文本，与 StoryPackage ID、版本和母本 SHA-256 绑定；模块目录由 `package-index.json`、`runtime-index.json`、`chapter-index.json`、`node-index.json`、`arc-model.json`、`arc-index.json`、`entry-model.json`、`entry-index.json`、`beat-index.json`、`character-index.json`、`location-index.json`、`item-index.json`、`relationship-index.json`、`main-story-graph.json`、`world.json`、`state-schema.json` 及 `nodes/`、`arcs/`、`entries/`、`beats/`、`chapters/`、`reader/`、`characters/`、`locations/`、`items/`、`relationships/` 组成。`runtime-index.json` 只声明运行时核心模块和实体索引的稳定路径，不存原著文字或人工可编辑的状态。实体索引只含 ID、名称、核心标签和模块路径，详细资料仍留在对应离散模块。每个模块均由脚本生成，索引保存相对路径和 SHA-256。章节规划上下文只含当前拍点、最多两个已发生拍点的摘要、已到达行范围的实体证据和状态事实，绝不包含未来拍点或当前章节尚未到达拍点的内容；原著全文只位于 `reader/` 模块，不会传入 Planner。每个新包都是新文件，不会覆盖母本或既有包。

```zsh
python3 -m open_story_engine build-story-package \
  content/source/rainy-waiting-room.v0.1.txt \
  /private/tmp/rainy-waiting-room.semantic-analysis.json \
  --id rainy-waiting-room-source \
  --version 0.1.0 \
  --output /private/tmp/story-packages/rainy-waiting-room-source/0.1.0/package.json
```

可独立重跑 `audit-story-package` 和 `audit-story-package-modules`。任一审计失败即表示该包不能作为运行时输入。运行时正文模型只接收用户选择、当前 `BranchState` 和当前节点相关的压缩故事包上下文；它绝不读取母本 TXT，也不按章节检索或逐章总结原著。人物、地点、关系、时间线和关键剧情节点均在离线建包阶段压缩，再由运行时按当前节点装载；自由行动由本地脚本登记稳定方向 ID 和允许的状态变更，不强制映射到既有菜单。脚本生成包会将用户明确点名、且与玩家同行的已登记角色写入 `characterLocationIds`；正文模型不能自行修改该映射，也不能把未登记的工具、可取证痕迹或可进入地点写成跨回合线索。偏离结局均不得反向改写原著正文。

```zsh
python3 -m open_story_engine audit-story-package-modules \
  content/source/rainy-waiting-room.v0.1.txt \
  /private/tmp/rainy-waiting-room.semantic-analysis.json \
  /private/tmp/story-packages/rainy-waiting-room-source/0.1.0/package.json \
  /private/tmp/story-packages/rainy-waiting-room-source/0.1.0/modules
```

## 以生成包试玩

只有通过构建和审计的版本才放入 `content/packages/<package-id>/<version>/`；版本目录的固定入口为 `package.json`，并与 `analysis.json`、`audit.json`、`reader.json` 和 `modules/` 保持在同一级，再用包 ID 和版本启动共创。以下命令使用新的临时 SQLite 数据库和 `Mock Planner`，不读取母本 TXT，也不发起模型请求：

```zsh
export STORY_RUN_DIR="$(mktemp -d /private/tmp/open-story-engine-source-package.XXXXXX)"

STORY_PACKAGE_ID=rainy-waiting-room-source \
STORY_PACKAGE_VERSION=0.1.16 \
STORY_DATABASE_PATH="$STORY_RUN_DIR/session.sqlite" \
STORY_PLANNER=mock \
python3 -m open_story_engine co-create
```

结束后可核对隔离库确实只记录本次生成包：

```zsh
sqlite3 "$STORY_RUN_DIR/session.sqlite" \
  'SELECT story_package_id, story_package_version, count(*) FROM game_sessions GROUP BY story_package_id, story_package_version;'
```

## 已记录的后续边界

未来导入外部或用户提供的小说时，可能遇到编码错误、章节缺失、段落粘连、乱码、OCR 误识别、混入网页/广告内容、叙事视角不稳定和情节因果缺口。

这些情况不得由解析器静默修复或自行补写。后续解析器应：

1. 保留原始输入字节和来源信息。
2. 在规范化前检测编码、空白、章节和异常字符问题。
3. 输出带有证据位置与置信度的候选结构，而不是直接生成权威 `StoryPackage`。
4. 对无法确认的角色、事件、关系、时间或结局标记为 `Unknown` / `needs_review`。
5. 仅在结构审计通过后，将候选结构提升为可执行故事包；有歧义的语义结论仍须保留其来源段落以供人工复核。

格式异常处理、自动修复和无证据的事实补写均不属于当前“标准小说母本”流程的交付范围。文本语义抽取只支持上述有章节边界和段落边界的标准输入；母本不需要获得模型端点的外发授权，因为其内容不会离开本机。
