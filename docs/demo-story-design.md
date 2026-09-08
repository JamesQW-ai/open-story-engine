# 《雨夜候车室》故事包设计稿

## 定位与真源

《雨夜候车室》是首个用于验证引擎的原创现代悬疑故事。其小说母本位于 `content/source/rainy-waiting-room.v0.1.txt`，采用第三人称限知视角，以许川为焦点人物；母本没有面向机器的元数据或结构标记。

`content/annotations/rainy-waiting-room.v0.1.json` 保留从母本到结构化剧情锚点的溯源记录；`content/packages/rainy-waiting-room/0.1.1.json` 是引擎实际加载的候选 `StoryPackage`，`0.1.0.json` 保留为历史基线。稳定 ID、状态旗标、检定与结局条件均以候选故事包为准，不由叙事模型再次推断。

首个运行模式为 `mainline`。默认且唯一可选方向为 `direction_original_canon`，即沿原著的规范剧情推进至“救援和真相同时成立”的结局。后续的受控偏离、玩家新建角色和私有同人共创属于独立会话契约能力，不会修改本故事包的原著事实。

## 玩家承诺

许川在暴雨中赶到临潮站，收到唐栖求助语音后发现末班列车被扣在站台。零点前，隧道进水与列车放行会持续收紧选择空间。他需要找到唐栖、取得她留下的证据，并让陈砚无法继续掩盖维修事故。

玩家始终用自然语言表达行动。`investigate`、`negotiate` 与 `risk` 是引擎内部的行动分类，不是展示给玩家的固定选项。叙事以第三人称限知、现在时推进，不能替许川声明没有输入的行动，也不能描述 `RuleEngine` 未确认的事实或结果。

## 世界与实体

- 临潮站在暴雨夜通信受限，候车厅、站务室和信号维修隧道构成可达地点。
- 世界不存在超自然力量或凭空出现的援军；资源、通行与水位的后果必须由规则结算。
- 陈砚挪用维修款并伪造验收和门禁记录；录音笔与原始记录可作为证据。

| 类别 | 稳定 ID | 内容作用 |
| --- | --- | --- |
| 焦点角色 | `character_xu_chuan` | 原著主角、规范玩家角色。 |
| 证人 | `character_tang_qi` | 发现异常记录后被困在信号室。 |
| 对手 | `character_chen_yan` | 值班主管，急于放行列车并掩盖事故。 |
| 盟友 | `character_jiang_xu` | 夜班维修工，掌握隧道与验收问题。 |
| 安全见证者 | `character_train_driver` | 在救援与证据确认后拒绝放行列车。 |
| 地点 | `location_waiting_hall`、`location_station_office`、`location_signal_tunnel` | 分别承载开局调查、证据确认和高风险救援。 |

唐栖的语音、十七号柜铜牌、防水录音笔、维修图纸、维修通行证、应急灯、手动阀和信号室滑栓都在故事包中有稳定物品 ID 与确定用途。人物隐情只会在对应旗标已确认后对叙事器开放。

## 原著时间线与初始状态

| 顺序 | 时间线 ID | 已发生事件 | 开局是否已知 |
| --- | --- | --- | --- |
| 1 | `timeline_repairs_delayed` | 陈砚挪用维修款，并伪造姜序签名。 | 否 |
| 2 | `timeline_tang_records_evidence` | 唐栖录下谈话并将录音藏入十七号柜。 | 否 |
| 3 | `timeline_tang_message` | 22:55，唐栖向许川发送求助语音。 | 是 |
| 4 | `timeline_train_held` | 23:10，末班列车被扣留，陈砚准备零点前放行。 | 是 |

会话从 `node_arrival`、`location_waiting_hall` 开始。许川持有 `item_voice_message`，三项属性 `insight`、`empathy`、`nerve` 均为 `2`；`pressure_level` 与 `flood_level` 均为 `0`。初始状态的完整旗标、关系和已知事实由故事包 `initialState` 定义。

## 规范主线

原著的可运行主线是一条四节点链。实线含义是原著规范剧情，而不是要求玩家在每一处输入固定句子；每个节点内的自然语言行动仍须经过对应的权威规则。

```text
node_arrival
  -> node_records
  -> node_tunnel
  -> node_aftermath
  -> ending_truth_survives
```

当前候选包也验证一条受控偏离：在找到铜牌后，许川可说服姜序不等站务室记录、先进入隧道救援。它经过独立的叙事锚点与更高的无应急灯风险，成功后进入 `ending_rescue_without_proof`，不会被叙事器伪装为原著规范线。

| 节点 | 近目标 | 达成条件与转移 |
| --- | --- | --- |
| `node_arrival` | 找到进入站务室的依据，确认唐栖线索。 | 取得铜牌并进入站务室后转入 `node_records`；也可与姜序达成救援优先的受控偏离，直接转入 `node_tunnel`。 |
| `node_records` | 取得录音和记录，确认唐栖在信号室。 | 证据与唐栖下落已确认，并取得隧道访问权后，转入 `node_tunnel`。 |
| `node_tunnel` | 降低水位、打开信号室并救出唐栖。 | 唐栖获救后，转入 `node_aftermath`。 |
| `node_aftermath` | 向司机公开已确认的救援与证据事实。 | 列车放行被拦截后，满足规范结局。 |

`pressure_level >= 3` 或 `flood_level >= 3` 且唐栖未获救时，终局规则会设置 `flag_train_departed`。这个状态不可由叙事器撤销。

## 规则结算

每个 `resolution` 绑定当前节点、行动类型、目标、属性、难度、守卫条件和三组状态效果。引擎只在守卫成立时执行，以“属性值 + 骰点”作为分数，并使用 `successAt + difficulty - 1` 与 `partialSuccessAt + difficulty - 1` 作为阈值；随后应用该结果显式列出的 `StateDelta`。例如：

- 调查 `item_locker_token` 可获得铜牌，并设置 `flag_locker_token_found`。
- 冒险进入 `location_station_office` 需要已找到铜牌，成功后设置 `flag_office_access` 并移动地点。
- 与姜序交涉需要已有证据与唐栖下落，成功后取得维修通行证并进入隧道。
- 隧道内必须先处理手动阀，再借助应急灯处理信号室滑栓；失败会提升水位。

失败、部分成功和成功均由故事包声明具体后果。叙事器只接收已确认的结果，不拥有改写旗标、地点、库存、关系或计数器的权限。

## 结局边界

| 结局 ID | 内容身份 | 条件 | 当前用途 |
| --- | --- | --- | --- |
| `ending_truth_survives` | 原著规范结局 | 唐栖获救、证据保全、列车未放行且放行被拦截。 | 当前 MVP 唯一目标结局。 |
| `ending_rescue_without_proof` | 受控偏离 | 唐栖获救但证据未保全。 | 当前用于验证救援优先路径，不是原著补写。 |
| `ending_last_train` | 受控失败 | 列车离站且唐栖未获救。 | 当前由压力或水位终局规则触发，不是原著补写。 |

后二者不是小说母本的既有结尾，也不应在当前规范原著试玩中被表述为“原著剧情”。当前只以明确的偏离点和状态条件验证其连贯性；未来衍生共创会话会用独立的会话级世界契约扩展它们。

## 内容审查结果

- 小说母本、标注与候选包形成可追溯链路；原始 `.txt` 不被运行时直接解析为规则。
- `node_arrival -> node_records -> node_tunnel -> node_aftermath` 是唯一默认方向的完整规范路径。
- 关键人物、地点、物品、已知事实、旗标和结局均使用稳定 ID，能够被 schema 与规则测试验证。
- 当前实现范围只验证标准输入和原著规范链；格式错乱小说、复杂章节解析、角色代入和自定义世界线均已记录为后续能力。
