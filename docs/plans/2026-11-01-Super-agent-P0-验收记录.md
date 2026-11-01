# Super-agent V1 P0 验收记录

- 日期：2026-11-01
- 范围：Thread / Task Node / Event / Artifact / Approval 最小契约、SQLite 持久化、假 Worker、恢复与幂等边界
- 状态：P0 已完成；P1 Adapter / Router 尚未开始

## 已实现

- SQLite schema v29：`super_threads`、`super_nodes`、`super_events`、`super_artifacts`、`super_approvals`
- v29 迁移逐条 DDL + 版本号同事务提交，失败回滚后可重试
- Thread owner 隔离与显式状态机
- Node 依赖、attempt fencing、`stopping` / `interrupted` / `needs_recovery`
- Execution snapshot 创建时校验并冻结，运行期间禁止修改
- Event 稳定幂等键 / source event id，事件入库与节点应用原子化
- 暂停期间完成事件保留未处理，继续时确定性重放
- 取消路径：`running → stopping → cancelled`，晚到事件不能写回
- Approval 创建和解决的事务边界、重复解析幂等、reject/cancel 保护
- Artifact / Event / Approval 的 owner、Thread、Node 关系校验
- App 启动调用 Super-agent recovery；恢复幂等
- FakeWorker 创建、推进、审批、暂停、继续、取消、重试和恢复验证

## 测试结果

- P0 主路径测试：6/6 通过
- P0 边界测试：6/6 通过
- P0 + 存储迁移 + Run/Job 状态回归：110/110 通过
- `compileall`：通过
- `git diff --check`：通过

## 明确未做

- 未实现 ChatRunAdapter、JobAdapter、WriterAdapter、NovelAdapter
- 未实现 Fast Path Router / Slow Path Router
- 未新增 Super-agent HTTP API
- 未修改 `public/` 或五个工作台页面
- 未接入真实供应商、真实付费 Worker 或真实任务总管 UI
- 未提交 commit、未推送

## 交付判定

P0 可以进入代码审阅和后续 P1 设计；在用户明确继续前，不自动开始 P1 Adapter/Router。
