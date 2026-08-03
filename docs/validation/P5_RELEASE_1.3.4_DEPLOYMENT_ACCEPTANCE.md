# P5 发布与切换验收（1.3.4）

日期：2026-08-03（Asia/Shanghai）

## 结论

1.3.4 已作为非生产科学发布完成候选构建、候选安装、双次独立验收、真实 current 指针切换和 MCP stdio 端到端检查。当前：

- E:\ms_mcp\deployments\current 为 Junction，目标 E:\ms_mcp\deployments\1.3.4；
- 已部署包版本为 1.3.4，公共工具数为 50；
- 固定 profile 预检仍为只读，execution_allowed=false；
- 通用 castep.calculation 与 results.castep_parsing 仍为 unverified；
- production_science_released=false，本阶段没有启动真实 CASTEP、MPI 或许可证 checkout。

这不是任意材料或任意参数的 CASTEP 公开执行发布；公开范围仍严格限于 P4-C 的固定、哈希绑定、只读预检。

## 发布卫生修复

候选制作过程中发现并关闭了三个发布机制问题：

1. 1.3.1 的递归打包携带了 122 个 __pycache__/egg-info 生成物。构建器现逐文件复制并拒绝生成物；1.3.4 bundle 的生成物计数为 0。
2. 1.3.2 的 P5-A 验收器错误地将完整离线 wheelhouse 要求在部署目录内。部署设计只保留项目 wheel，故验收器改为全量审计 release bundle、再审计部署关键文件。
3. 1.3.3 已能候选安装但不能在后续显式激活。1.3.4 增加 scripts/switch_current_release_v1.ps1，并将回退脚本改为显式确认的安全包装器。

1.3.1、1.3.2、1.3.3 均保留为未激活的历史候选；没有作为 current 运行。

## 1.3.4 候选验收

发布包：

- 目录：E:\ms_mcp\releases\materials-studio-mcp-moc-1.3.4
- 文件数：174
- 全部文件 SHA-256 匹配
- 项目 wheel：恰好 1 个
- 生成 Python 产物：0
- production_science_released=false

候选部署：

- 目录：E:\ms_mcp\deployments\1.3.4
- 安装回执 activated=false
- pip check 通过
- verify-deployment 通过
- 两次 P5-A 验收回执字节完全一致：
  9723435A603F6A89F9AF6FD7E4E5BFB2FB036FDB52992663BAFC91678F719E05
- 固定 profile 请求哈希：
  C85E2C2642487C6FE8859179A56B75E39AE4DDA430691030B1994E41496A7DD9
- MS 内置 Perl locale 诊断 stderr：0 字节
- 验收前后无 CASTEP/MPI/Materials Studio 受限运行进程，且预检未写入部署目录。

回执：

- docs/validation/receipts/p5a-1.3.4-candidate-verification-first.json
- docs/validation/receipts/p5a-1.3.4-candidate-verification-second.json

## 指针切换与回退

新切换脚本在真实指针变更前执行目标部署的依赖与哈希验证，并要求：

- 明确 -ConfirmSwitch；
- 当前指针与 -ExpectedCurrentTarget 精确一致；
- 临时 Junction 指向已验证目标；
- 切换中失败时恢复旧 current；
- 成功后写入部署目录外的切换回执。

隔离演练已完成 1.3.0 → 1.3.3 → 1.3.0，并产生两份回执；真实 current 在演练期间未变。真实切换结果：

- 前一目标：E:\ms_mcp\deployments\1.3.0
- 当前目标：E:\ms_mcp\deployments\1.3.4
- 切换回执：
  E:\ms_mcp\deployment-activation-receipts\switch-1.3.4-20260803T021851316Z.json
- 回执 SHA-256：
  059C3D3FF4871C9944D0CE3B38A92697E561715BCA79A8338C2B3B36973248EE
- 无残留 .current-* 临时 Junction。

如需回退，使用已验证的显式命令：

    .\scripts\rollback_release_v1.ps1 -TargetVersion 1.3.0 -ExpectedCurrentTarget E:\ms_mcp\deployments\1.3.4 -ConfirmRollback

## 运行后检查

切换后，旧 MCP 服务进程已停止；宿主自动创建新服务进程。以 current 启动的新 MCP stdio 会话已完成：

1. MCP 初始化；
2. tools/list 发现 50 个工具；
3. 发现并调用 ms_castep_fixed_profile_preflight；
4. 返回 fixed_profile_preflight_pass 与 execution_allowed=false；
5. 无 CASTEP/MPI/许可证相关进程。

切换后 pip check 和 verify-deployment 也均通过。

## 既有资格链路复核

发布过程中已重跑且通过：

- P3-C 固定 α-石英 SinglePoint 真实资格证据的完整性维护检查（原始真实结果不重跑）；
- P4-C 公共固定 profile 预检与 Windows Perl locale 防护检查；
- 源码全量回归：328/328。

P3-C 的真实运行仍只证明固定 profile，不构成一般 CASTEP 执行资格；计划已退役。
