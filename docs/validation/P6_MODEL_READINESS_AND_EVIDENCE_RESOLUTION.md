# P6 模型就绪度与证据补救能力

## 目的

P6 为 Materials Studio 2023 MCP 增加受控的建模入口：在结构、晶胞、组分、力场、部分电荷、状态条件或证据不完整时，先判断缺口性质，再给出可审计的补救路径。它不把“本地有文件”或“网上找到记录”误称为科学有效性。

## 新增接口

| 接口 | 风险 | 行为 |
| --- | --- | --- |
| `md_model_readiness_assess` | R0 | 只读校验部分模型规格，扫描用户指定的工作区结构文件和已配置的本地 LAMMPS 力场资源，输出 `ready`、`resolvable` 或 `blocked`。 |
| `md_model_gap_resolution_plan` | R0 | 根据同一规格生成按优先级排序的补救计划，区分机器可检查项、候选来源和必须人工决定的科学项。 |
| `md_search_public_model_evidence` | R1 | 仅在显式网络许可和单次确认后查询固定的 PubChem/Crossref JSON 元数据端点；默认 dry-run，不下载内容。 |

`ready` 只表示机器可发现的输入条件齐备，可继续走已有预检；它不是力场适用性认证、生产科学放行或执行许可。所有真实写入、Materials Studio/LAMMPS 计算仍沿用哈希绑定、dry-run、预检与确认流程。

## 不可自动越过的边界

- 不生成或拼接未知的力场参数、交叉项、部分电荷、晶胞或科学结论。
- 本地 `.frc` 文件只报告为候选，并对选中的文件计算 SHA-256；其存在不证明适配性。
- 公开检索只返回身份/文献元数据和来源链接；不下载 SDF/CIF、力场包、脚本或可执行程序。
- 网络查询固定为 HTTPS 的 `pubchem.ncbi.nlm.nih.gov` 或 `api.crossref.org`，拒绝重定向和超大响应；不接收任意 URL。
- 通用 CASTEP 执行和结果解析继续保持 `unverified`，P6 不改变 P3/P4 的固定 profile 边界。
- Windows Materials Studio Perl locale 防护仍是每周维护的独立最高优先级检查项。

## 验收范围

新增回归涵盖：缺项阻断、可解析 XSD 的三维周期性、力场/结构哈希漂移、局部候选扫描、带电/电荷模型缺口、通用 CASTEP 边界、未知字段拒绝、网络 dry-run 零外发、固定 provider、响应规范化、网络显式许可与单次确认、公开工具注册与能力登记。

候选发布使用 `scripts/verify_candidate_p6_model_readiness.ps1`：它要求 current 指针仍指向上一已部署版本，验证源与候选完整性、P6 回归、53 个公开工具、dry-run 网络零外发、CASTEP 边界、Windows Perl locale、部署目录只读性和受限运行进程为零。

## 真实公开网络验证的前提

当前本机 Browser Harness 未连接到 Chrome，且没有已认证的云浏览器。因此 P6 对公开 API 的真实浏览器端到端验证不能伪称已完成；代码层使用模拟响应完成了协议与安全边界验证。待 Chrome 开启远程调试并授权（或配置 Browser Use Cloud）后，应以一个无敏感信息的 PubChem/Crossref 查询完成一次只读浏览器验证，再把结果记录为补充运行证据。

## 1.3.6 发布结果

1.3.6 已在两个字节一致的候选验收回执后激活：

- 源回归：345 项通过；候选 P6 回归：17 项通过。
- 部署包、已安装 wheel 与运行时依赖完整性均通过；公开工具数为 53。
- 实际 stdio MCP 协议检查通过：可发现三个 P6 工具，缺结构规格保持 `blocked`，PubChem 工具保持 `network_access=not_requested`。
- Windows Materials Studio Perl locale 审计通过：固定 Perl 哈希匹配、子进程 locale 回退到 `C`、stderr 为 0、未启动 CASTEP/许可证。
- 通用 `castep.calculation` 与 `results.castep_parsing` 仍为 `unverified`；无受限运行进程。
- `current` 从 `1.3.4` 原子切换到 `1.3.6`；切换回执位于 `E:\ms_mcp\deployment-activation-receipts\switch-1.3.6-20260803T031146118Z.json`，可显式回退到 `1.3.4`。
