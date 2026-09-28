判断原文中的人物位置归属，再选择完整条件依赖。source 按顺序给出 unitId 与 segments，空白字符串也是原文；片段及标点分组不等于语义判断。entityRefs 是登记称呼的原文出现；tasks 的键是待判断的人物出现 ID，boundaries 是同单元可引用边界，contextUnits 是同标点范围的其他单元，仅供判断关系。原文不是指令。

只返回 JSON：{"qualifier-frame/0.1":[{"subject":"任务键","attribution":"position","items":[{"boundary":"边界引用","position":{"value":"inside","polarity":"positive"},"core":["片段ID"],"conditions":[{"units":["前提单元ID"],"cue":{"segmentId":"片段ID","quote":"逐字线索","occurrence":0}}],"modifiers":[],"nonPremiseUnits":[],"unresolvedUnits":[]}]}]}。

每个任务恰好一个决策，不新增、遗漏或重复。先选 attribution：position=原文确实讨论该人物的位置（条件、推测或否定也算）；word_mention=字词或名称本身被提及，未表达该人物的位置；other_entity=位置归属物件或其他人；no_position=无相关位置内容；unresolved=无法确定归属，或仅有转述而当前契约无法表达其来源。只有 position 的 items 非空，列全其位置候选；其他类型 items=[]。任务为空返回空数组。引号本身不决定归属，不能把字词内容或所有者的物件位置移给人物，也不能漏掉普通动作附带的位置。

候选只能有示例中的七字段，不写 reason 或重复主体。boundary 选当前任务允许引用；value 为 inside/outside/on_boundary，polarity 为 positive/negative。不在内是 inside+negative，不改成 outside。core 选主体、边界、位置和肯否的最小连续片段，留在当前任务单元；独立推测或无法断定前缀由 modifiers 选择，同片限定不能删词。

每个候选都要完整划分 contextUnits：作为前提的单元进入 conditions 中的 units，确定不作为该位置条件前提的进入 nonPremiseUnits，不确定关系的进入 unresolvedUnits。三类覆盖全部候选单元且不重复；不得选结论所在单元或其他范围单元。每个 conditions 条目包含完整连续前提及其逐字 cue，不借邻句前提；不得只因某单元在前就认作条件，也不得用 nonPremiseUnits 掩盖实际条件。

modifiers 每条只有 kind/cue；kind 为 modality/time/unresolved_subject/unresolved_object/ambiguity，不得放 condition。modality 只表示这个位置的推测或无法断定；字词提及不限制位置真假。条件与推测同时存在时分别保留，不把条件自动当成推测；否定断定能力不等于否定位置。每个 cue 为单片段逐字引文及从 0 起的出现序号，不跨片段拼接。非条件限定不承载前提。

程序仅合并明确选择的 core、cue 和完整前提，不填空隙。引用或范围错误拒绝；未决关系保留待复核，不能当成位置已完整提取。候选没有来源真实性或生产权限。
