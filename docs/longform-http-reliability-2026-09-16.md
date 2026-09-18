## 本轮交付

在叙事质量专项 NQ-001 暂缓期间，补齐当前长篇的 HTTP 写入可靠性回归。新增 `tests_api/test_longform_play.py`，从历史模块迁移 16 项仍适用的保障，再增加 3 项并发／提交故障检查。本轮没有修改业务逻辑，没有把测试迁移表述为修复了新的产品缺陷。

每次运行调用 `test_support.longform.longform_cases()`，对所有符合原始十万 CJK 门槛且匹配官方运行包的小说建立独立测试套件；不固定小说 ID 或版本。当前清单只有《太虚遗录》第一部，原始 102610 CJK、50 章、`taixu-relics-part1@0.1.2`。本套件逐书选首个官方身份及其绑定开局；三身份路线结束覆盖继续由 `test_longform_routes.py` 承担。

所有故事包复制到临时目录，数据库与预生成草稿库也在临时目录；使用 MockPlanner，不调用真实模型，不导入或运行停用短篇测试，不触及实际玩家存档。测试退出通过应用 lifespan 关闭任务服务；生成中删除测试额外等待所有后台工作线程退出，再核对正式库与草稿库。

## 迁移去向

以下 16 项从 `tests_api/test_play_api.py` 迁移，方法名保持一致，场景改为官方身份开局与“留在原地观察周围”。原模块继续整体停用。

| 保障 | 已迁移的方法 |
| --- | --- |
| 删除全部依赖、保留另一存档和故事包 | `test_delete_save_removes_all_dependents_and_preserves_other_save` |
| 不存在数据库时不创建文件 | `test_delete_missing_database_does_not_create_it` |
| 只读模式不能删除 | `test_read_mode_cannot_delete_existing_save` |
| 删除事务完整回滚 | `test_delete_transaction_rolls_back_all_records_on_failure` |
| 故事包丢失后仍可删除存档 | `test_delete_available_with_unavailable_package` |
| 跨域允许删除 | `test_play_cors_allows_delete` |
| 正文先输出、完成后可读取、重复请求去重 | `test_stream_sends_prose_then_committed_result_and_deduplicates` |
| 流重置、失败不写分支、相同请求可重试 | `test_stream_reset_and_failure_do_not_publish_a_branch` |
| 不存在的存档返回终止错误 | `test_stream_unknown_save_returns_terminal_error` |
| 只读模式没有流式写入路由 | `test_read_mode_has_no_streaming_write_route` |
| 写入能力报告 | `test_health_reports_play_phase` |
| 非预设身份拒绝 | `test_create_session_rejects_foreign_character` |
| 预设方向重复请求去重 | `test_continue_direction_is_idempotent` |
| 相同请求换行动拒绝 | `test_continue_direction_conflict_rejected` |
| 自定义行动写入分支 | `test_free_text_writes_custom_direction` |
| 无效方向不写入 | `test_invalid_direction_rejected_without_write` |

原模块剩余 14 项没有计为本轮迁移通过，其中包含不再适用的新建人物、自由选介入节点等旧流程；其余开局、人物卡、手记及路线检查仍需与当前专项测试逐项核对。`test_player_routes.py` 与两个历史核心模块也未在本轮恢复执行。

## 新增故障边界

- **提交失败后复用已生成正文**：在写入请求回执时用 SQLite 触发器制造失败，此时分支与审查结果已进入同一事务。比较前后完整数据库 dump，确认正文、状态、评估和回执全部回滚；SSE 只有错误终止，没有 `done`，不暴露底层错误。去除故障后禁用模型规划，重试仍能提交同一草稿，最终只有一个子分支、一条评估和一条回执。
- **并发点击去重**：用事件同步，确认两个真实 HTTP 请求同时等待同一草稿后才允许生成完成；两个请求返回同一分支，仅一个标记为首次写入，正式库只多一条分支与一条回执。
- **生成中删除**：规划正在等待时删除存档，再放行生成；流以错误终止。等待后台线程全部退出后，确认内存任务、正式会话、正式分支和持久化草稿均没有恢复。

上述检查验证当前进程的并发、事务和 HTTP 事件契约；没有模拟网络断线或进程强杀，也不证明真实模型的叙事质量或耗时。

## 验证与后续

- 专项：`.venv-api/bin/python -B -m unittest tests_api.test_longform_play -v`，19 项通过。
- 当前 API：`.venv-api/bin/python -B -m test_support.run api`，211 项通过。
- 当前核心：`python3 -B -m test_support.run core`，16 项通过。
- `git diff --check` 通过。前端未改，本轮未重复构建或桌面试玩。

下一批优先迁移仍适用的核心事实校验与局部修复回归：保留问题类型、修复前后正文、失败原因，核对拒绝错误事实与失败不落盘的边界。测试应继续从长篇包取得真实人物、地点与事实依据。NQ-001 和冻结母本重复问题 SRC-001 仍未解决；没有开始完整 P4、大规模配图或修改冻结母本。
