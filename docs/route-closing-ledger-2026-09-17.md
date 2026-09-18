P6 收束阶段账本封口

## 本轮变更

收束意图进入 `closing` 后，结果契约验证拒绝三类会重新拉长路线的变化：

- `goalUpdates` 中的 `new` 目标；
- `goalUpdates` 中的 `transformed` 后继目标；
- `threadUpdates` 中的 `new-1` 至 `new-8` 新问题。

已有目标和问题仍可依据本回合正文完成、放弃或解决。`preparing`、无意图和账本未知阶段不受此限制，方便先补齐有证据的路线记录。收束阶段由当前分支账本实时计算，不由意图字段单独授予。

后果审查版本更新为 `reader-consequences/4`，旧版本结果不能绕过新的契约校验。

## 验证

- `tests_api.test_reader_consequences`、`tests_api.test_reader_threads`：20 项通过；
- `tests_api.test_longform_route_closure`：23 项通过；
- 使用当前唯一达标官方长篇《太虚遗录》第一部，覆盖账本未清、账本清空及新目标／新问题封口。

## 边界

本轮没有自动结束路线，没有判断自然结局，也没有修改正文生成长度或输出质量策略。终局正文仍须经过独立审查和显式提交。
