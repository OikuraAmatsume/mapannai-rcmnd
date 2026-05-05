# 🗺️ AI 推荐生成 API

基于 AWS Lambda 的异步推荐生成服务，使用 Google Places API 和 Gemini AI 为用户生成周边景点、美食、活动推荐。

## ✨ 功能特点

- **三种推荐类型**：美食 / 名胜古迹和旅游景点 / 跳蚤市场或活动
- **AI 驱动**：使用 Gemini AI 生成个性化中文推荐概述
- **异步架构**：支持长时间运行任务，避免 API Gateway 超时
- **无数据库设计**：使用 S3 存储临时结果，降低成本

## 🏗️ 架构

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│  Lambda A   │────▶│  Lambda B   │────▶│     S3      │
│  (启动器)    │     │  (执行器)    │     │  (结果存储)  │
└─────────────┘     └─────────────┘     └─────────────┘
       │                                       │
       │  轮询查询    ┌─────────────┐           │
       └────────────▶│  Lambda C   │◀──────────┘
                     │  (查询器)    │
                     └─────────────┘
```

## 🧠 开发 Agent Harness

上面的 Lambda A / B / C 是**线上业务执行链**，不等同于后续开发时要使用的 Agent Harness。

仓库内新增了一套 `agent_harness/`，用于把“未来所有代码改动”固定为下面这条开发链路：

```
用户意图
  │
  ▼
Agent1 (独立 context, 只拆意图和出 prompt)
  │  输出：可执行 prompt / 更优方案问题 / 验收标准
  ▼
Agent2 (独立 context, 只按 prompt 改代码)
  │  输出：代码改动
  ▼
git commit
  │
  ▼
Agent3 (独立 context, 先产出 review 要点，再审查 Agent2 结果)
  │  检查：
  │  1. 继续坚持无数据库设计
  │  2. 所有新增持久化和中间状态继续落在当前指定 S3
  ▼
不通过 → 自动返工 → git commit → Agent3 复检
```

### 目录说明

```text
agent_harness/
├── config.json                  # 项目级硬约束、模型和 git 约定
├── runner.py                    # Harness 编排入口
├── README.md                    # 使用说明
├── schemas/                     # Agent1 / Agent3 结构化输出约束
├── templates/                   # 三个 Agent 的提示词模板
└── runs/                        # 每次执行的本地工件目录（默认不纳入 git）
```

### 独立 context 的实现方式

- 每个 Agent 都通过独立的 `codex exec --ephemeral` 进程启动
- Agent 之间只通过 `agent_harness/runs/<run_id>/` 下的工件文件传递信息
- Agent2 和 Agent3 不会继承 Agent1 的对话上下文，只消费显式输入

### 推荐使用方式

1. 先生成 Prompt，不直接改代码：

```bash
python3 agent_harness/runner.py prepare --intent "你的改动目标"
```

2. 如果 Agent1 提出了更优方案问题，先回答它：

```bash
python3 agent_harness/runner.py answer --run-id <run_id> --text "你的补充说明"
```

3. 确认最终 Prompt 后，再执行 Agent2 + Agent3：

```bash
python3 agent_harness/runner.py execute --run-id <run_id>
```

4. 随时查看当前 run 状态：

```bash
python3 agent_harness/runner.py status --run-id <run_id>
```

## 📁 文件结构

```
├── agent_harness/               # 开发 Agent Harness（与业务 Lambda 链分离）
├── recommendation_generator.py  # 核心推荐逻辑
├── config.py                    # 配置管理
├── lambda_a_starter.py          # Lambda A - 启动器
├── lambda_b_executor.py         # Lambda B - 执行器
├── lambda_c_checker.py          # Lambda C - 查询器
├── lambda_function.py           # Lambda 入口
└── requirements.txt             # Python 依赖
```

## 🚀 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置环境变量

| 变量名 | 说明 |
|--------|------|
| `GOOGLE_PLACES_API_KEY` | Google Places API 密钥 |
| `GEMINI_API_KEY` | Google Gemini API 密钥 |
| `S3_BUCKET_NAME` | S3 存储桶名称 |
| `S3_REGION` | S3 区域 (如 ap-northeast-1) |
| `LAMBDA_B_FUNCTION_NAME` | Lambda B 函数名 (Lambda A 需要) |

### 3. 部署到 AWS Lambda

**Lambda A (启动器)**
- 运行时：Python 3.11
- 处理程序：`lambda_function.lambda_handler`
- 超时：10 秒

**Lambda B (执行器)**
- 运行时：Python 3.11
- 处理程序：`lambda_function.lambda_handler`
- 超时：5 分钟
- 内存：256 MB+

**Lambda C (查询器)**
- 运行时：Python 3.11
- 处理程序：`lambda_function.lambda_handler`
- 超时：10 秒

## 📡 API 使用

### 启动推荐任务

```bash
POST /recommendation
Content-Type: application/json

{
  "lat": 35.4437,
  "lng": 139.6380,
  "main_type": "美食",
  "sub_type": "拉面",
  "budget": 2000
}
```

**响应：**
```json
{
  "message": "Recommendation generation started",
  "jobId": "job_abc123",
  "statusCheckUrl": "/status/job_abc123"
}
```

### 查询结果

```bash
GET /status/{jobId}
```

**响应（处理中）：**
```json
{
  "status": "processing",
  "message": "任务正在处理中"
}
```

**响应（完成）：**
```json
{
  "status": "completed",
  "result": { ... }
}
```

## 🔧 支持的推荐类型

| 类型 | 子类型 | 说明 |
|------|--------|------|
| 美食 | 拉面、寿司、烧肉等 | 基于评论生成 100 字概述 |
| 名胜古迹和旅游景点 | - | Gemini 搜索 + 200 字历史概述 |
| 跳蚤市场或活动 | - | 未来 30 天内的活动，含时间和网站 |

## 🤖 AI 技术

- **Prompt Engineering**：角色设定、结构化输出、字数约束
- **类 RAG 模式**：检索评论数据 → Gemini 生成概述
- **Structured Output**：JSON Schema 约束输出格式
- **Agent Harness**：Agent1 拆意图，Agent2 改代码，Agent3 审查并兜底无数据库 / S3 约束
