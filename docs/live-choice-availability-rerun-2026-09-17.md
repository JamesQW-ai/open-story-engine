P3 人物可互动性真实复跑

## 范围

对当前唯一达标官方长篇《太虚遗录》第一部的三个预设身份各执行一次首轮方向生成。使用 `deepseek-v4-pro:cloud`，最多 3 次调用；不创建正式分支、不修改母本或正文。

## 结果

三项均通过：

- `entry_lu_gate`：通过，1741 reported tokens；
- `entry_gu_trial`：通过，1880 reported tokens；
- `entry_ye_gallery`：通过，1921 reported tokens。

合计 3 次供应商调用，约 5542 reported tokens。人物位置、永久离队状态、互动对象和方向依赖的程序契约均通过。本次结果独立于此前叶观澜样本失败记录；此前失败响应继续保留，不用本次复跑覆盖历史。

## 边界

这是开局菜单的有限真实验证，不是连续试玩，也不代表正文质量或人物语义漏报已解决。END-001、NQ-001、SRC-001 仍暂缓。原始报告和三份响应见 `docs/evidence/live-choice-availability-2026-09-17-rerun/`。
