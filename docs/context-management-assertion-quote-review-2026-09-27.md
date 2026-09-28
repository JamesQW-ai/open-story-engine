本轮接续[逐字引用定位比较](context-management-assertion-locator-review-2026-09-27.md)，将定位预览补为独立的候选契约，再执行有界模型实验。当前范围为断言提取诊断，不是完整上下文工程或正文质量验收。

## 实验前冻结与审核

- 新契约：[context_assertion_quote.py](../test_support/context_assertion_quote.py)，协议 `assertion-quote/0.1`。完整原文只注入一次，实体锚点、核心与线索使用单元内逐字引用；完整条件前提仍使用单元 ID。
- [提示](../test_support/prompts/context_assertion_quote.md)为 1,533 字符，未超过前一版 1,537 字符；全部 48 条请求预检最大为 2,168 字符，单次预算 4,000 字符。
- [冻结参考](../test_support/fixtures/context-assertion-quote-2026-09-27.json)包含原 44 条诊断及 4 条设计控制。新增控制验证同主体双来源、重复字词、嵌套转述和未知说话人；使用同一官方长篇登记实体，单独统计，不冒充未见场景。
- 直述候选的 core 必须包含当前人物和边界锚点，origin=null。转述必须分别引用 speaker、cue、scope、subjectMention；core 可含代词，但绑定仅是模型提出的假设，不能证明代词指向。全部转述保留待复核状态。
- 未知说话人、嵌套来源在任务约定中要求 unresolved；程序仅检查其结构及参考一致性，尚不能独立识别所有嵌套转述或错误来源。合法引文、宽范围含多人、错误的 origin=null 都可能结构合法而语义错误。
- 所有结果均为 semanticStatus=unverified、acceptance=false、productionEnablement=false，不输出 decodedProposal，不接生产提交。

参考比较分别统计决策、内容、来源、限定与引用范围，保留重复候选数量，检测交换限定与范围的错误。仅允许外围空白和结尾一个中文句号的范围差异，不忽略引号、否定和核心内部文字。参考相等不等于语义真值；与旧五分类的子类成绩不可直接比较。旧参考不能表达的自述、转述、位置与字词共存已在实测前明确写入新参考，旧报告保持不变。

## 验证与预算

实验前 38/38 项相关测试通过（39.784 秒），包含新契约 10 项及定位、来源、评分相关回归。测试覆盖故意错绑、条件丢失、非法引用、完整依赖划分、数组预算、有限范围等价、标签不注入、固定调用上限、篡改检测、失败即停、检查点失败和排他写入。

```sh
python3 -B -m unittest tests_py.test_context_assertion_quote tests_py.test_context_assertion_locator tests_py.test_context_assertion_scoring tests_py.test_context_assertion_scope
python3 -B -m test_support.context_assertion_quote_eval
python3 -B -m test_support.context_assertion_quote_eval --audit
```

实验工具为 [context_assertion_quote_eval.py](../test_support/context_assertion_quote_eval.py)。每条一次、总计最多 48 次请求，结构化输出上限 1,024 token，不进行传输重试、格式修复或评分后补答。传输或执行器异常停止后续请求；逐条保留原请求、响应和检查点。结果文件采用首次排他创建，旧实验与源文件哈希绑定不改写。

不修改生产代码、官方小说、StoryPackage、环境文件或 relay。未重复全量核心、API 或前端回归；已有 consequences.plan 历史提示基线失败未改动。进入正文提交、未见场景推广或 Jev 之前仍须后续独立验收。

## 实测与复审

首轮 [原始实验](evidence/context-management-2026-09-22/context-assertion-quote-eval-2026-09-27.json)完成 48 次 direct 请求，请求模型 deepseek-v4-flash，响应模型 deepseek-flash；全部 HTTP 200、finish_reason=stop。[独立审计](evidence/context-management-2026-09-22/context-assertion-quote-eval-audit-2026-09-27.json)重建全部输入和评分，通过哈希绑定核对。

| 组别 | 严格参考匹配 | 引用范围差异 | 其他参考不符 | 结构无效 | 范围外 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 原 44 条 | 24 | 4 | 7 | 7 | 2 |
| 新增 4 条 | 2 | 0 | 1 | 1 | 0 |

范围差异四条为 two_people:first_operator、two_people:second_operator、hall:source_position、hall:two_people。前三条只少了句末逗号，最后一条去掉连接词“而”；逐字复核未发现位置或限定改变，但冻结评分仍保持 reference_mismatch。未把它们混作语义错误，也未修改旧参考追认得分。

实际问题包括：5 条将明确位置判为 absent（其中 prefix:mention 与 hall:word_mention 原文相同，不算独立场景）；4 条直述 core 漏人物；1 条加入任务之外的相邻单元；将否定断定能力误作否定位置；将他人位置复制给说话人；念出字词误作本人断言或选错边界。新增嵌套转述未保持 unresolved，未知说话人输出了非法 speaker=null。这些不能用评分范围过严解释。

原 44 条输入 token 为 37,803（旧归属实验 41,105），输出为 5,351（旧 4,635），合计 43,154（旧 45,740）；48 条全部合计 47,311 token。同 44 条 HTTP 完整响应耗时中位数为 944.5 ms，旧为 835 ms。输入有所下降、输出增加、耗时没有改善；未控制服务负载，协议及评分也不同，不能归因或声称质量/时延优化成功。

## 第二轮修正

保留首轮提示、代码、金标和原始响应。第二轮仅替换任务组织提示为 [context_assertion_quote_v2.md](../test_support/prompts/context_assertion_quote_v2.md)，1,532 字符；同一输入、契约、评分、48 次预算和 1,024 token 上限，使用独立文件记录。没有追加历史或失败答案，没有自动回填主体、改位置或补引用。

修正重点为先提取本人位置，再判断来源；分别说明定语位置、做动作时的位置、独立字词与位置共存、否定作用范围、未知/嵌套来源，以及仅划分实际 contextUnits。该提示利用已知失败类别设计，成绩属于开发集诊断，不能当留出泛化结果。

第二轮新增 11 项检查通过（18.948 秒），随后 [48 次真实实验](evidence/context-management-2026-09-22/context-assertion-quote-v2-eval-2026-09-27.json)及[独立审计](evidence/context-management-2026-09-22/context-assertion-quote-v2-eval-audit-2026-09-27.json)完成。所有响应 HTTP 200、finish_reason=stop，原 44 条为 29 严格匹配、5 仅范围不同、5 其他参考不符、3 结构无效、2 范围外；新增 4 条全部匹配。

第二轮范围差异为 in_core:mention、two_people:first_operator、hall:two_people、frame:mixed_ownership、frame:self_report。模型分别取较短的位置主句、包含念字动作的完整单元、省去连接词、包含第二个人物的完整单元，以及“说”而非“低声说”。Codex 阅读认为这些引用差异没有改变相应位置和来源判断，但不属于已冻结评分允许的范围，机器分数不改。

真实失败仍有 8 条：denial:operator 与 hall:denied_certainty 将“没有人敢断定”截为“敢断定”（两条同原文）；control:object_position 将木盒的位置分给主人；frame:other_report 将被谈人物的位置分给说话人，并把被谈人物的转述错作直述；frame:position_and_quote 多报字词位置；其余 3 条缺依赖分区、引用主体在核心外或选错边界。新提示修好了一些问题，也使首轮正确的物件归属和混合人物案例退步，因此不能只报告提高后的总分。

原 44 条 token 为输入 37,891、输出 5,683、合计 43,574，HTTP 完整响应中位数 911.5 ms；全部 48 条为 47,533 token。两轮都是同一开发集，未做未见场景验收。相关回归在合并加载两版工具及能力探测工具后为 51/51（68.159 秒）。

## 推理配置能力探测

不再继续堆提示，冻结第二轮输入、提示、契约和评分，选择 12 条已声明用例：7 条既有错误、2 条正例及 3 条设计控制。只在隔离网关对象上请求 reasoning_effort=low；项目 .env、route、model、URL 均不改变。此组用于检查能力和成本，不能代表总体成功率或公平的无推理 A/B。

2,048 token 的 [首次探测](evidence/context-management-2026-09-22/context-assertion-quote-reasoning-eval-2026-09-27.json)在第 1 条停止：原始响应 finish_reason=length，只有 reasoning_content，最终 content 为空，后 11 条 not_run。不得把它计为语义失败或“网络不通”；[审计](evidence/context-management-2026-09-22/context-assertion-quote-reasoning-eval-audit-2026-09-27.json)确认失败即停和原始证据一致。

因此执行一次预算容量修正，仍使用相同 12 条和同一提示，改用既有配置允许的 8,192 token 上限。该修正有截断证据支持，不更改生产预算、不重跑挑选旧成功结果，也不继续自动上调预算。[容量探测](evidence/context-management-2026-09-22/context-assertion-quote-reasoning-capacity-eval-2026-09-27.json)全部返回完整响应；[审计](evidence/context-management-2026-09-22/context-assertion-quote-reasoning-capacity-eval-audit-2026-09-27.json)通过。

12 条中 8 条严格匹配，3 条仅范围不同（two_people:second_operator、denial:operator、frame:self_report）；设计控制 design:nested_pending 返回外层人物 absent、内层人物 unresolved，与冻结的“全部 unresolved”策略不同。该结果没有把嵌套话语升级为直接位置，不能夸大为事实放行错误，也不能静默修改旧分数。物件位置、字词与断言、混合人物和他人转述等旧错误在这组中消失，仍不代表稳定性已获证实。

12 条总输入 10,829 token，输出 29,817 token（其中推理 28,208），总计 40,646 token；完整响应中位数 11,562 ms，最大 28,905 ms。当前能力改善有明显成本，不能当延迟优化或直接改默认配置。

随后保持全部配置不变，补测原 48 条中尚未在该配置测过的 36 条；与已测 12 条互不重叠，后者原记录直接保留。三套能力探测工具共 7 项回归通过（37.211 秒），覆盖固定子集、请求等价、预算、失败即停、证据漂移和两组无重复。

另增加 [绑定检查](../test_support/context_assertion_binding_audit.py)：只检查 subjectMention 精确对应唯一登记人物时，是否与任务人物相同。明确他人姓名错绑直接报告 binding_conflict；代词、未知/歧义称呼仍为 unverified，不自动映射、不改原响应、不授予生产权限。它不解决所有格、位置肯否、转述来源或限定缺失，不能替代语义审核。

绑定检查 3 项回归通过（9.259 秒）。[离线重放](evidence/context-management-2026-09-22/context-assertion-binding-replay-2026-09-27.json)及[独立复算](evidence/context-management-2026-09-22/context-assertion-binding-replay-audit-2026-09-27.json)不调用模型：首轮 1 条、第二轮 1 条明确姓名错绑均被报告；容量探测 12 条没有此项矛盾，但全部仍为 unverified。两条错绑来自同一个原诊断，不计作两种独立风险的通过证明。

## 最终复审与暂停点

[剩余用例补测](evidence/context-management-2026-09-22/context-assertion-quote-reasoning-remainder-eval-2026-09-27.json)前 9 条严格匹配，第 10 条 two_people:first_operator 在 30,239 ms 后报“JSON 响应超时”，只有响应头时间 237 ms，没有完整原始响应或 usage，后 26 条全部 not_run。[失败审计](evidence/context-management-2026-09-22/context-assertion-quote-reasoning-remainder-eval-audit-2026-09-27.json)通过；未再次请求该例，未延长超时或扩大预算。不能认定它是语义错误，也不能推测服务最终生成了什么。

合并互不重叠的 12 条容量探测与 36 条补测记录，同一 8,192 token 配置只实际尝试 **22/48 条**：17 严格匹配、3 仅范围不同、1 嵌套策略不符、1 超时、26 未运行。21 条完整响应的中位数为 8,805 ms，最大 28,905 ms；可见 usage 共 62,598 token，另 1 次超时的 token 消耗 Unknown。它改善了部分已知错误，但没有形成完整覆盖，更没有稳定性或延迟优势证据。

本轮五批实验共 **119 次真实调用**：117 次 finish_reason=stop，1 次 length，1 次超时；已报告 usage 合计 160,492 token，超时调用用量 Unknown。这是各次实验的调用账目，包含重复诊断输入，不能合并成准确率或当作 119 个独立场景。逐批指标、提示未变核对和子集无重复证明见[汇总](evidence/context-management-2026-09-22/context-assertion-iteration-summary-2026-09-27.json)及[汇总复算](evidence/context-management-2026-09-22/context-assertion-iteration-summary-audit-2026-09-27.json)。

最终相关回归 **59/59 通过（106.538 秒）**，git diff --check 通过。测试覆盖首轮、第二轮、三组能力探测、绑定检查以及来源/定位/评分依赖；所有真实实验和绑定重放均有独立审计。未重复无关 API/前端和全量核心回归，未改 consequences.plan 历史基线。正式会话、权威状态、官方长篇、生产配置和 Jev 均未由本轮修改。

**本轮按失败即停规则暂停在线推进。** 无推理路径仍有语义误判；推理路径的完整覆盖被约 30 秒超时阻断，且成本明显上升。不能用延长超时、追加提示、重跑挑成功或放宽评分把这两类问题掩盖掉。该判断针对当前已测试路径，不表示其他模型或更好的任务拆分不可能解决问题。

## 恢复开发的清单与进入条件

1. **先选定新的能力验证条件。** 优先明确一个已可用的候选模型/服务版本，沿用当前固定输入做有界对照；若仍使用当前 low 配置，需要先决定可接受的单次时延和 token 上限，再建立新实验，不静默延长现有 30 秒窗口。当前没有已验证的替代配置。
2. **修正评分表达，保持历史原分。** 对完整/最小位置核心、终止标点、说话谓语与声音修饰的合法替代引用，冻结新的引用范围规则；分别记录证据合法性、命题一致性与来源策略一致性。明确嵌套中外层人物 absent、内层 unresolved 是否满足覆盖政策，不能通过改旧金标制造提升。
3. **闭合固定诊断集合。** 根据新实验条件声明全部待测项与重用边界；当前配置若不变，26 条未运行及超时条目仍是缺证。测试必须保留物件/主人、说话人/被谈者、条件/推测、字词/断言、重复边界与嵌套来源反例；新增本地绑定检查只作为额外拒绝依据。
4. **再做未见官方入口。** 在另一个达标官方长篇入口冻结实体来源、位置/来源对照与标签；不能拿同场景改词或新增设计控制代替留出验收。
5. **随后验证来源授权与完整正文链路。** 完成断言到公开依据的核对，再接入缓冲正文、有限 repair、独立复核、权威状态和幂等提交，在临时数据库中验证。仅凭本轮位置提取结果不得宣布上下文工程完成。
6. **质量和时延共同达标后再评估默认启用。** 20 条配对、长期连续性和玩家正文验收仍未完成；Jev 继续后置。
