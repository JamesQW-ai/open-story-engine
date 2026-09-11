## 工作边界

- 产品优先级（2026-09-10）：方向 2“已完整或部分读过原著的用户，选择节点与角色介入并改写剧情”是最高优先级 MVP，开发与验收均以此为准。方向 1“首次接触原著，以图片等吸引游玩，可改写或全程沿原著阅读”和方向 3“从零构建新小说”保留为后续方向，不混入当前 MVP 完成门槛。具体范围见 `docs/product-direction.md` 与 `docs/mvp-acceptance.md`；方向 2 正在完成，不因优先级记录而视为已验收。
- 默认中文沟通与文档；代码、字段、命令和文件名保留原语言。
- 保留已有未提交改动。CLI 核心优化与 API 可以并行；先确认本轮文件责任，避免改写另一条工作线正在维护的生成流程。
- API 首批只读，不因封装而绕过状态、事实或故事包完整性校验。读取会话不得调用数据库初始化或迁移；测试使用临时数据库。
- API 与 CLI 直接复用 Python 业务核心，不通过 CLI 子进程及终端文字解析提供 HTTP 接口。
- 本轮仅实施后端 API，不编写前端页面。前端选型保留为后续规划；仓库根部被忽略的 TypeScript 文件是历史后端，不修改或恢复它们。

## 安装与启动

- CLI 核心依赖标准库；API 使用独立 Python 3.12 环境。
- API：`python3.12 -m venv .venv-api`，再执行 `.venv-api/bin/python -m pip install -c requirements-api.lock -e '.[api,api-test]'`。
- 启动：`.venv-api/bin/python -m uvicorn open_story_engine.api:create_app --factory --host 127.0.0.1 --port 8000`，接口文档位于 `/docs`。

## 检查

- 核心：`python3 -B -m unittest discover -s tests_py -q`。
- API：`.venv-api/bin/python -B -m unittest discover -s tests_api -v`。
- 差异：`git diff --check`。
- 真实模型测试不属于默认回归，按 `docs/live-llm-evaluation-v0.1.md` 单独执行并记录验收边界。

接口与配置见 `docs/api-read-service-v0.1.md`；阶段范围见 `docs/api-web-architecture-v0.1.md`。
