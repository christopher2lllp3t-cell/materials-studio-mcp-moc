# Materials Studio MOC interface

MOC 是本机 Materials Studio 的受控桌面与脚本层；MCP 是带类型、项目、哈希、确认令牌和审计记录的编排层。两者互补，但不等价。

## 主命令

```powershell
python D:\分子动力学模拟\tools\ms_moc.py status --json
```

## MOC 到 MCP

MOC 每次都建立新的 stdio MCP 会话，不复用隐藏状态：

```powershell
python D:\分子动力学模拟\tools\ms_moc.py mcp-tools --json
python D:\分子动力学模拟\tools\ms_moc.py mcp-status --json
python D:\分子动力学模拟\tools\ms_moc.py assess-nanopore <contract.json> --json
```

当前握手必须看到 43 个公开工具，并确认 `md_architecture_compliance_audit`、`md_task_submit`、`ms_forcite_calculation_checked`、`ms_geology_assess_nanopore_contract`、`ms_moc_get_status` 和 `ms_moc_open_document` 均存在。

## v1 自检与验收

```powershell
python D:\分子动力学模拟\tools\ms_moc.py doctor --json
python D:\分子动力学模拟\tools\ms_moc.py acceptance --g01-report D:\分子动力学模拟\07_mcp_materials_studio\mcp_projects\g01_v1_reproduction_20260715_r2\reports\G01_V1_REPRODUCTION_REPORT.json --json
python D:\分子动力学模拟\tools\ms_moc.py science-status --json
```

离线部署副本会优先发现本机受控工作区；迁移到其他工作站时，应设置 `MS_MOC_SCIENCE_ROOT` 或向 `science-status` 传入 `--science-root`，明确指定科学证据目录。

`doctor` 验证本机依赖、真实 MCP 握手、43 个公开工具、版本探针和发布清单哈希。`acceptance` 还会运行完整回归、`pip check`，并对 G01 项目中的全部登记产物重新验哈希。G01 仍是三原子非周期 PCFF 校准夹具，始终保持 `production_science_released=false`。

`science-status` 独立汇总 G02、G04 和 G06 的哈希证据、检查点进度和阻断原因。`acceptance=pass` 只代表平台与 G01 校准验收通过；只有 `science-status.production_science_released=true` 才代表这些模型的生产科学门全部通过。

## MCP 到 MOC

- `ms_moc_get_status`：只读返回 MOC、MatStudio、RunMatScript、桥接器、允许根目录和失败模型保护状态。
- `ms_moc_open_document`：只允许打开绑定项目目录内的 `.xsd/.xtd/.stp/.car/.mdf/.cif` 文件。

打开文档时必须提供项目目录、文档绝对路径、精确 SHA-256 和新的幂等键。先以 `dry_run=true` 预演；真实打开时以完全相同的最终参数调用 `md_prepare_production_confirmation`，再提交一次性令牌。MOC 会隔离 MCP 标准流，并返回请求进程、稳定桌面 PID 和启动模式。

## MaterialsScript 验证

```powershell
python D:\分子动力学模拟\tools\ms_moc.py verify-xsd <model.xsd> --json
```

输入会复制到 ASCII 临时目录并记录 SHA-256。只有运行器退出码为 0，且 `<project>_Files\MatStudioLog.htm` 同时包含 `Completion status: (OK)`、不包含 `(FAIL)` 时，结果才是成功。

`export-xsd` 还要求 project-mode `Documents` 目录中的 CAR 和 MDF 同时存在，并分别记录大小与 SHA-256；缺少任一文件都返回失败。

## MCP checked 转换

- `md_export_xsd_to_car_mdf_checked`：项目内 XSD + 输入哈希 + 输出槽 + 幂等键 + 一次性确认。
- `md_convert_to_lammps_checked`：项目内 CAR/MDF 各自哈希 + `.frc` 哈希 + 显式 Class + 输出槽 + 幂等键 + 一次性确认。

checked 转换验证源/目标原子数一致和 LAMMPS data preflight，但始终返回 `production_released=false`；模型专属能量等价和科学门仍须单独通过。

## 其他受控操作

```powershell
python D:\分子动力学模拟\tools\ms_moc.py list-scripts --json
python D:\分子动力学模拟\tools\ms_moc.py launch <model.xsd> --dry-run --json
python D:\分子动力学模拟\tools\ms_moc.py export-xsd <model.xsd> --json
python D:\分子动力学模拟\tools\ms_moc.py run-wrapper run_build_stage3_water_only.ps1 --json
```

`run-wrapper` 只接受 `ms_scripts` 内 basename-only 的 `run_*.ps1`。文档和包装脚本的路径穿越、链接逃逸、未知后缀及超时均会失败关闭。

## 禁用模型

以下失败的自动清理输出不得作为建模基础：

- `stage5a_oil_water_restrained_min.data`
- `stage5b_rigid_oil_water_damped.data`
- `stage5d_ms_rigid_cleanup_1p20.data`
- `stage5d_ms_trial_translate_cleanup.data`
