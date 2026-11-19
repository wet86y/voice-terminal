# 远程后台轨迹与授权按钮映射（2026-11-19）

本轮在远程配对模式下把工作台“后台协作”面板的后台轨迹、待决审批和授权按钮接到服务端语音实时事件流。不实现完整 DSH Web UI 隧道，也不改动 SSH 公钥、`sshd` 配置或远程操作白名单。

## 实现

- `apps/frontend/src/product/useProductAdapter.ts`：远程模式的 `shadow` 计算属性优先使用语音实时通道中的 `shadowProduct`（来自 `runtime.snapshot` 和 `shadow.product`）；仅当语音实时通道未连接，或尚未收到带 `backend`、`available`、活动或时间线的权威投影时，才回退到打开页面时读取的产品快照。本机模式保持原有逻辑。
- `apps/frontend/src/product/ProductApp.test.ts`：新增“远程模式后台轨迹与授权按钮跟随服务端语音实时快照”，用一次性的旧产品快照和随后到达的实时审批活动对比，确认面板采用实时投影，并确认授权按钮仍提交原 `approval_id` 与 `run_seq`。
- 服务端与 Rust 侧无需新增操作：`/v1/voice` 已向已认证设备转发 `runtime.snapshot`（含 `shadow`）和 `shadow.product`；授权按钮经既有 `productApi.decideDshApproval` 映射到远程固定操作 `dsh.approval.decide`，再走 `/internal/application/dsh-approval`。该操作已在服务端操作表和客户端原生白名单中。

## 验证

- 定向测试：`npm run test --workspace @voice-terminal/frontend -- --run src/product/ProductApp.test.ts`，93 项通过。
- 前端全套：`npm run test --workspace @voice-terminal/frontend -- --run`，43 个测试文件、539 项通过。
- 类型检查：`npm run typecheck --workspace @voice-terminal/frontend`（`vue-tsc --noEmit`）通过。

## 边界

- 面板只展示当前运行序号的轨迹；已完成或失败条目保留供展开查看。远程实时通道断开时可能回退到打开页面时的旧产品快照，旧审批卡仍可能显示；服务端会按原审批编号和运行序号拒绝已失效决定。
- 授权决定仍由服务端 DSH 会话权威结算；前端不做乐观“已同意”标记。
- “代理控制台”入口仍保持禁用并显示“仅服务端本机可打开”；本轮只映射后台协作面板，不包含 DSH Web UI 隧道，也不改变双机验收状态。
- 本轮未在真实双机配对环境验收；远端真机上的轨迹刷新和授权按钮仍需现场确认。

## 交付

- 本机与分发安装器已同包更新为 product-593d771db3cd30ae，正式程序包摘要与分发交付回执一致，安装后本体校验通过；本机正式页面已重新挂载。交付记录见[当前交付](../releases/current-release.md)。
