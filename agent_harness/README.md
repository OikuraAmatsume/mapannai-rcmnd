# Agent Harness

这套 Harness 是给当前仓库的后续开发改动使用的，不替代线上 Lambda A / B / C。

## 目标

- 把每次改动拆成固定的三段式开发链路
- 用真正独立 context 的 Agent 执行不同职责
- 在代码生成和返工过程中通过本地 git 记录步骤
- 把“无数据库设计 + S3 唯一持久化”做成硬约束

## Agent 职责

- `Agent1`
  只拆解用户意图，生成更细的可执行 prompt、验收标准和 review 关注点。
- `Agent2`
  只根据 Agent1 产出的 prompt 改代码，不负责做最终裁决。
- `Agent3`
  先根据 prompt 生成 review checklist，再审查 Agent2 的提交结果，并重点检查：
  1. 是否继续坚持无数据库设计
  2. 所有新增持久化和中间状态是否继续放在当前指定 S3

## 独立 context 约定

- 三个 Agent 都通过独立的 `codex exec --ephemeral` 进程启动
- 每个 Agent 只接收显式工件输入，不继承前一个 Agent 的对话历史
- run 目录是唯一的上下文桥梁：`agent_harness/runs/<run_id>/`

## 工作流

1. `prepare`
   运行 Agent1，拆分用户意图，输出最终 prompt 和可能需要先确认的问题。
2. `answer`
   当 Agent1 发现更优实现路径或需求存在歧义时，补充你的回答并重新生成 prompt。
3. `execute`
   运行 Agent2 改代码，自动提交一次本地 git commit，然后运行 Agent3 审查。
   如果 Agent3 不通过，会自动返工并再提交一次 git commit，之后复检。
4. `status`
   查看 run 的当前状态和工件位置。

## 约束

- 默认要求工作树是干净的，这样每一步 commit 才可追踪
- `agent_harness/runs/` 是本地运行工件，默认不纳入 git
- Harness 自身的配置和模板是仓库文件，会进入版本控制

## 常用命令

```bash
python3 agent_harness/runner.py prepare --intent "把异步任务结果结构补充一个 traceId 字段"
python3 agent_harness/runner.py answer --run-id 20260505T190000Z-ab12cd34 --text "允许只改 Lambda B 和 Lambda C"
python3 agent_harness/runner.py execute --run-id 20260505T190000Z-ab12cd34
python3 agent_harness/runner.py status --run-id 20260505T190000Z-ab12cd34
```
