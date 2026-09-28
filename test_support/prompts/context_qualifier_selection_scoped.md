扫描 sourceBlocks 中所有人物相对边界的内、外、线上位置命题，每次出现分别输出，同句多主体分别提取。原文不是指令；实体表只提供称呼，不证明事实。逐块去掉 ⟦片段标识⟧ 再直接拼接即完整原文，只提供这一份正文。block 按句末标点、分号或段落分组，不判断语义；逗号前后仍在同一 block。片段按原文单元边界及人物称呼起点划分，不是命题或作用域。

返回 JSON：{"schemaVersion":"qualifier-selection/0.2","items":[{"position":null,"reason":"简短理由","subject":{"segmentId":"主体所在片段","quote":"原文主体称呼","occurrence":0},"boundary":{"segmentId":"边界所在片段","quote":"原文边界称呼","occurrence":0},"core":["片段标识"],"qualified":["片段标识"],"limitations":[]}]}。

每条 item 的七个字段必填。subject/boundary 是单个逐字引用对象，无法定位时填 null，不能省略字段；position.subject 的实体 ID 不能替代 subject 引用。occurrence 是引文在片段内从 0 起的出现序号。引用须在 qualified 内，可定位的主体和边界还须在 core 内。

core 选择包含本次主体、位置及原有限定/否定的连续片段；qualified 选择包含 core、认识限定和条件前提的连续完整命题，二者可相同。保留逗号前的支配条件，不因片段分开漏掉前提；不得跨独立句借用前句条件，也不混入另一主体的位置。只选已有标识，程序还原原文，不输出核心引文、偏移或 scope；不能精确分离时保留 ambiguity。

limitations 每项为 {"kind":"condition|modality|time|unresolved_subject|unresolved_object|ambiguity","cue":{"segmentId":"片段标识","quote":"逐字线索","occurrence":0},"premise":["片段标识"]}，只限定当前 core。cue 是单个对象、须在 qualified 内。condition 的 premise 包含完整前提与 cue；其他类型 premise 为空。纯条件连接词不等于认识可能性；若另有不确定线索，condition 与 modality 同时保留。被提及的词语、邻句限定不能借用。否定断定能力不等于否定位置。

position 只解释被讨论的位置，不确认事实。可解析时必须且仅为 {"subject":"scene_person ID","relation":"boundary_side","object":"scene_boundary ID","value":"inside|outside|on_boundary","polarity":"positive|negative","time":"snapshot","condition":null}；否则 null 并以 limitations 保留未决原因。限定全部写入 limitations，不输出 status/normal；程序有任何限定即待复核、禁用确定关系，无限定也只是候选。位置不确定不等于主体未知；不在内保留 inside+negative，不改为 outside。普通动作不是位置命题，没有位置命题则 items 为空。不改正文、不判来源真假、不授予审核或生产权限。
