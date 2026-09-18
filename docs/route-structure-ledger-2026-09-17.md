本轮补齐 P6 路线结构账本的第一层映射：路线大纲从当前分支的 `sourceProgress` 查找运行包中已冻结的 Beat，并按 Arc 的 `availableWhen` 条件绑定当前 Arc。

当前结构返回 `volume`、Arc ID、Beat ID 和 `source_progress`。运行包没有可验证卷标时，`volume` 保持 `null`；缺少或无法确认 `sourceProgress` 时，Arc 和 Beat 都保持 `null`，不根据回合数、正文长度或缺席推断位置。Arc 和 Beat 只返回冻结运行包中的 ID，保持现有只读 HTTP 结构兼容。

验证覆盖《太虚遗录》第一部的三个身份开局：各自按真实 `sourceProgress` 映射对应 Beat 和 Arc。另有缺失进度回归，确认未知结构不会伪造位置。路线专项 15 项、全 API 435 项、核心 25 项通过；未调用真实模型、未修改正式存档或冻结母本。
