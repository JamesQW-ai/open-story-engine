修正契约字段错误并返回完整JSON，不改变用户要求：{{ error }}
requirements 对象的键必须与输入中的原始要求 ID 完全一致：只补齐缺少的原始 ID，删除所有多余 ID，绝不能把 NPC 回应、未来条件、模型推断或审查信息新增为 A8 等要求，也不能改写原始要求的含义。
若错误涉及 premiseChecks：scenePlan.knowledge 中 status 为 fact、reported 或 inference 的既有断言，premiseChecks 必须使用 kind=existing 并引用公开依据；只有 status=pending 且绑定本轮授权步骤时才可使用 kind=after_step。不能把已有事实改标为 after_step、unknown 或 restriction 来规避依据校验。
