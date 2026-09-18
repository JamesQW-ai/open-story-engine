## 工作边界

- 唯一产品方向（2026-09-15）：官方先完成并冻结 10 万字以上长篇小说，玩家以完全未知原著的视角选择预设身份并互动游玩。不提供玩家上传、已读原著节点介入或玩家从零创作整部小说；不再保留其他产品方向的排期。当前只开发桌面端。以 `docs/product-direction.md`、`docs/mvp-acceptance.md` 和 `docs/next-development-plan-2026-09-15.md` 为准，规划记录不等于完成验收。
- 默认中文沟通与文档；代码、字段、命令和文件名保留原语言。
- 测试只使用十万汉字及以上的官方长篇（2026-09-16 用户确认）；《雨夜候车室》已停用，功能夹具、桌面测试和回归也不得再使用。`test_support.longform` 自动盘点所有达标母本并核对当前运行包；历史短篇用例保留供追溯，不执行、不计入当前通过数。
- 保留已有未提交改动。CLI 核心优化与 API 可以并行；先确认本轮文件责任，避免改写另一条工作线正在维护的生成流程。
- API 已包含受控游玩写入；不得绕过状态、事实或故事包完整性校验。只读会话接口不得调用数据库初始化或迁移；自动测试使用临时数据库。
- API 与 CLI 直接复用 Python 业务核心，不通过 CLI 子进程及终端文字解析提供 HTTP 接口。
- 玩家端使用现有 `web/` 下的 React/Vite 桌面界面；API 与前端共同按当前阶段验收。仓库根部被忽略的 TypeScript 文件是历史后端，不修改或恢复它们。

## 安装与启动

- CLI 核心依赖标准库；API 使用独立 Python 3.12 环境。
- API：`python3.12 -m venv .venv-api`，再执行 `.venv-api/bin/python -m pip install -c requirements-api.lock -e '.[api,api-test]'`。
- 启动：`.venv-api/bin/python -m uvicorn open_story_engine.api:create_app --factory --host 127.0.0.1 --port 8000`，接口文档位于 `/docs`。

## 检查

- 核心：`python3 -B -m test_support.run core`。
- API：`.venv-api/bin/python -B -m test_support.run api`。
- 不再使用遍历历史测试的 `unittest discover -s tests_py/tests_api` 命令；当前入口明确列出退役模块，迁移后的用例和新用例自动纳入。前端仍执行 `cd web && npm test && npm run build`。
- 桌面夹具：先用 `python3 -B -m test_support.export_reader_fixture /tmp/longform-reader.json --package-id <ID>` 导出盘点清单中的长篇，再用 `node web/tests/fixtures/reader-server.mjs 8766 /tmp/longform-reader.json` 启动。多部达标小说逐部验证，不用短篇或重复模板正文替代。
- 差异：`git diff --check`。
- 素材清单只读检查：`python3 -B -m test_support.check_scene_library`，默认枚举全部达标官方长篇；可用 `--package-id <ID>` 限定。输出 JSON 字段定位与原因，退出码 `0` 为通过、`1` 为清单问题、`2` 为参数错误；不生成图片或修改素材。用法及边界见 `docs/scene-library-check-2026-09-17.md`。
- 真实模型测试不属于默认回归，按 `docs/live-llm-evaluation-v0.1.md` 单独执行并记录验收边界。

接口与配置见 `docs/api-read-service-v0.1.md`；阶段范围见 `docs/api-web-architecture-v0.1.md`。
