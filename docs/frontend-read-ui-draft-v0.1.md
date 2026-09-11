## 决策状态

- 草案日期：2026-09-10。
- 状态：第一版只读前端已实施（`web/`）；2026-09-10 同日扩展为可玩闭环，写作接口见[写作与开局 API](api-play-write-v0.1.md)。
- 依据：[API 与 Web 架构选型](api-web-architecture-v0.1.md)已确认 React + TypeScript + Vite + Ant Design；[读取 API 服务](api-read-service-v0.1.md)已提供本文件引用的全部接口契约。
- 范围：第一版前端是只读浏览与上下文预览工作台。会话创建、正文生成、草稿编辑与状态提交依赖后续草稿服务，不在本版范围。

## 项目定位回顾

引擎当前处于 CLI MVP 之后的 API 封装阶段：Python 业务核心已提供故事包加载、共创分支会话与上下文装配；FastAPI 第一批只读接口已交付。正式玩家端目标是小说阅读器式交互（见 [README](../README.md) 与[产品方向](product-direction.md)），不复用命令行界面。第一版前端对应这一过渡：先做出能验证“目录 → 入口 → 上下文 → 已确认分支阅读”的只读界面，为后续“生成 → 编辑 → 确认”写作闭环预留布局位置。

## 设计目标与边界

目标：

1. 浏览已有故事包目录、入口、拍点与角色/地点/物品卡片。
2. 浏览已有会话，按分页目录与父子关系重建分支树，按需读取分支正文。
3. 对可用故事包执行原著角色入口的上下文预览；对已确认分支执行续写上下文预览。
4. 以明确的能力提示和错误展示处理不可预览、绑定变化、历史分支缺状态等情况。

边界（遵循已确认的取舍，不新增能力）：

- 不创建会话、不生成草稿、不修改状态；界面不展示“生成”“确认”等写入按钮。
- 不做上传故事包的完整编辑工作流；`POST /api/v1/package/parse` 仅作为校验工具入口，可选实现。
- 不引入账号、鉴权与多人协作；面向本机使用。
- 正文阅读区第一版只读展示 `narrativeText`；`Input.TextArea` 编辑属于后续草稿服务阶段。
- 不在前端推断人物位置、物品归属或剧情结果；一切状态展示以服务端返回为准。
- 小说母本导入仍通过 CLI `inspect-source` / `analyze-source` / `build-story-package` 产出本地故事包后，在前端目录中浏览；第一版前端不提供母本上传通道。

## 技术基线

| 领域 | 选择 | 说明 |
| --- | --- | --- |
| 框架 | React + TypeScript + Vite | 独立单页应用，开发时以 Vite `/api` 代理连接 `127.0.0.1:8000`。 |
| 组件库 | Ant Design（`antd`） | 树、分栏、抽屉、描述与时间线组件覆盖本阶段需求；不引入 Ant Design Pro 模板，不混用第二套组件库。 |
| 样式 | antd 主题 + 少量自定义 CSS | 正文阅读区单独设置字号、行距与留白，与表单区区分。 |
| 状态 | React 自带状态能力 | 路由选择、分页游标与请求进度由页面本地管理；已确认故事状态以服务端响应为准。 |
| 目录 | 仓库内 `web/` 前端子项目 | 与 Python 包、测试目录并列；仓库根部被忽略的历史 TypeScript 后端文件不修改、不恢复。 |

## 信息架构与页面

```text
/                      故事包目录（默认页）
/packages/:id/:version 故事包详情：目录 + 入口预览
/sessions              会话列表
/sessions/:sessionId   会话阅读：分支树 + 正文 + 状态/上下文
```

### 1. 故事包目录页

- 数据源：`GET /api/v1/packages`。
- 列表项展示 `PackageSummary` 的 `title`、`package_id`、`version`、`beat_count`、`character_count` 与 `context_preview` 标记。
- `issues` 中的损坏包单独成区展示 `code` 与 `message`，不与有效包混排。

### 2. 故事包详情页

- 数据源：`GET /api/v1/packages/{package_id}/{version}`。
- 布局：左侧入口与拍点列表，右侧实体卡片与入口预览。
- 入口预览：选中入口后，以其 `source_character_ids` 提供单选身份；选择后发送 `POST /api/v1/context/build`（`entry_preview` 模式）。身份选择只是预览参数，不暗示人物到场。
- 预览结果区展示 `ContextData` 与 `state` 快照、`context_sha256`（标注为内容摘要）。

### 3. 会话列表页

- 数据源：`GET /api/v1/sessions`；`available: false` 时展示空态与原因提示。

### 4. 会话阅读页（核心页面）

三栏布局：左栏分支树（分页加载、按 `parent_id` 建树）、中栏正文阅读与 `nextDirections` / `openThreads`、右栏/抽屉为状态与账本及上下文预览入口。顶栏展示 `binding_status`；`changed` 时禁用上下文预览但保留历史阅读。

## 数据加载顺序（新页面默认路径）

```text
GET /api/v1/sessions/{id}?include_branches=false
  -> GET /api/v1/sessions/{id}/branches (分页, after_sequence)
  -> 用户选中 -> GET /api/v1/sessions/{id}/branches/{branch_id}
  -> 需要时   -> GET /api/v1/sessions/{id}/state?branch_id=...
  -> 需要时   -> POST /api/v1/context/build (branch_preview)
```

`branches: []` + `branches_included: false` 只表示未加载，界面不显示“无分支”。入口与分支两种预览模式互斥，前端不混填。

## 能力提示与错误映射

| 情形 | 界面处理 |
| --- | --- |
| `context_preview.available: false` | 禁用预览入口并展示 `message`；浏览功能不受影响。 |
| `binding_status: changed / unavailable` | 保留历史阅读，禁用上下文预览并标注。 |
| `409 branch_state_unavailable` | 状态区提示该历史分支未保存状态快照；正文阅读继续可用，不 fallback 到会话当前状态。 |
| `422 branch_required` | 引导先在分支树选择节点。 |
| `503`（存储不可读、`invalid_response`） | 页面级错误提示，不清空已加载内容。 |
| 分页游标 | `next_after_sequence: null` 只表示当前已到末页；提供手动加载与刷新，不自动轮询。 |

## 数据契约注意事项

- 外层字段为 `snake_case`，核心对象内 `parentId`、`narrativeText`、`branchState` 等保留 `camelCase`；TypeScript 类型按 `api_models.py` 与 OpenAPI 对齐，不做重命名适配层。
- `StoryState` 的具体字段由故事包定义；状态面板按键值动态渲染，不写死具体包的变量集合。
- 核心对象允许额外字段；未知扩展字段原样保留，不丢弃、不猜测语义。
- 缺失的可选字段不自动补默认值：无 `branchState` 的分支按缺省/错误提示处理。

## 后续阶段位置预留

中栏底部为后续方向选择、自由行动输入与草稿确认预留布局空间，本版不渲染这些控件。流式展示、状态库升级、富文本编辑器与部署形态均按[架构选型](api-web-architecture-v0.1.md)的重新评估条件处理。
