# Materials Studio 2023 MCP

这是一个面向 Windows 和 BIOVIA Materials Studio 2023/23.1 的 MCP 服务。它把 Materials Studio、Forcite、CASTEP 输入准备、地质模型构建和 LAMMPS/VMD 前处理封装成可审计的工具，同时保留严格的科学输入边界。

本项目的重点不是“自动猜参数”，而是让每一步都能回答三个问题：输入来自哪里、执行了什么、结果能否复现。

## 项目能做什么

- 检测本机 Materials Studio 2023 安装并读取已安装的本地帮助文档。
- 扫描和解析 `.xsd`、`.xtd`、`.stp` 等结构文件，检查原子、键、晶胞和拓扑信息。
- 生成受控的超胞、表面、替换、羟基化、反离子和周期性水/盐模型候选。
- 在固定的 COMPASSIII、PCFF、Dreiding/QEq、Universal/QEq 配置下准备 Forcite 力场输入，并记录类型、键级、电荷和自动项检查结果。
- 生成受哈希约束的 CASTEP 独立输入候选；通用 CASTEP 计算执行和通用结果解析仍然关闭。
- 对模型规格做只读就绪度评估，并给出缺口补齐计划。
- 对 PubChem 化合物身份元数据和 Crossref 文献元数据提供受限、默认 dry-run 的公开证据查询。
- 将 CAR/MDF、LAMMPS data、VMD 检查和科学质量门组织为可追溯的项目流程。
- 通过异步任务提交、查询、取消和重试管理受控操作。

## 重要安全边界

- 不接受任意 Perl、任意 MaterialsScript 或自然语言拼接脚本。
- 不自动编造晶体结构、晶胞、力场参数、交叉项、部分电荷或科学结论。
- 公开证据查询默认不联网；实时查询必须由用户明确授权并使用一次性确认。
- 不下载结构文件、力场文件、脚本或可执行文件作为“自动补齐”。
- 所有写入和计算工具都有 `dry_run`；真实的 R2/R3 操作需要精确、一次性的确认令牌。
- 通用 `castep.calculation` 和 `results.castep_parsing` 能力保持 `unverified`，不会因为计划或离线测试而开放。
- 资格计算只用于内部验证，不代表目标材料已经收敛，也不会自动获得生产许可。

## 可运行链路

典型的受控链路如下：

1. `md_model_readiness_assess` 读取模型规格，判断 `ready`、`resolvable` 或 `blocked`。
2. `md_model_gap_resolution_plan` 列出缺失结构、力场、晶胞、电荷和条件证据；缺失项由用户选择和审核。
3. `md_project_initialize`、`md_project_update_specification` 和 `md_project_register_artifact` 建立可复现项目记录。
4. `md_structure_preflight`、力场准备、CAR/MDF 导出和 LAMMPS 转换逐级检查哈希、计数、单位和质量门。
5. 只有在目标模型证据充分、人工授权和对应工具边界都满足时，才进入受控执行；候选和资格结果不会自动变成生产结果。

## 模型就绪度与公开证据

在构建或计算新体系前，先提供已经确认的模型信息：组分和数量、相态、目标引擎、结构来源、力场状态、电荷方法、晶胞和边界条件。

可使用以下工具：

- `md_model_readiness_assess`：只读检查已知字段和证据完整性。
- `md_model_gap_resolution_plan`：生成有顺序、有边界的补齐计划，不代替科学决策。
- `md_search_public_model_evidence`：只查固定的 PubChem/Crossref 元数据；默认 `network_access=not_requested`。

公开检索只产生“待人工复核的来源线索”，不会直接变成力场参数或结构输入。

## 安装

在 Windows PowerShell 中运行：

```powershell
.\install.ps1
```

安装脚本会：

- 创建或修复 `.venv`；
- 安装锁定版本的 Python 依赖并执行 `pip check`；
- 创建 ASCII 路径运行别名；
- 生成本机 MCP 客户端配置 `mcp-config.local.json`。

也可以手动安装：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e .
```

`config/software.local.json`、`config/research-environment.local.json` 和生成的 `mcp-config.local.json` 都是本机配置。里面的 Windows 路径只是示例，使用前必须根据本机安装位置修改；不要把个人凭据写入这些文件。

## 启动服务

推荐使用部署目录的 ASCII 路径：

```powershell
E:\ms_mcp\deployments\current\.venv\Scripts\python.exe -m materials_studio_mcp.server
```

本地开发环境也可以使用：

```powershell
.\.venv\Scripts\python.exe -m materials_studio_mcp.server
```

命令行入口为：

```powershell
.\.venv\Scripts\materials-studio-mcp.exe
```

如果虚拟环境仍指向已经移动的 Python，请重新运行 `.\install.ps1`，并优先使用 ASCII 运行别名。

## MCP 客户端配置

仓库中的 `mcp-config.example.json` 是模板。典型配置如下，请将路径替换为本机实际路径：

```json
{
  "mcpServers": {
    "materials-studio-2023": {
      "command": "E:\\ms_mcp\\deployments\\current\\.venv\\Scripts\\python.exe",
      "args": ["-m", "materials_studio_mcp.server"],
      "cwd": "E:\\ms_mcp\\deployments\\current",
      "env": {
        "MATERIALS_STUDIO_ROOT": "D:\\Program Files (x86)\\BIOVIA\\Materials Studio 23.1",
        "MATERIALS_STUDIO_MCP_ROOT": "E:\\ms_mcp\\deployments\\current",
        "MS_MOC_MCP_ROOT": "E:\\ms_mcp\\deployments\\current"
      }
    }
  }
}
```

## 工具分层

公开工具数量和精确签名以 `ms://catalog/public-tools` 与 `ms_task_catalog` 为准。主要分层如下：

- **R0 只读检查**：安装检测、本地帮助、工作区扫描、结构预检、项目读取、能力登记和模型就绪度。
- **R1 规划与证据**：工作流建议、CASTEP 输入规划、公开证据 dry-run 和生产确认准备。
- **R2 受控写入**：项目初始化、受哈希约束的结构构建、CAR/MDF 导出和 LAMMPS 转换。
- **R3 受控计算**：仅开放经过固定 profile、人工授权和证据审计的 Forcite/资格流程；不提供通用 CASTEP MCP 执行接口。

## 项目目录

```text
config/                      本机软件、策略和科学合同配置
docs/validation/             验收说明与哈希绑定回执
moc/                         Materials Studio MOC 接口和桥接文件
scripts/                     安装、发布、候选验收和回滚脚本
src/materials_studio_mcp/    MCP 服务实现
tests/                       单元测试、合同测试和安全测试
install.ps1                  本机安装入口
release-manifest.json        发布文件与哈希清单
requirements.lock            锁定依赖
```

## 验证状态

最近一次候选验收包含：

- 347 项源回归测试通过；
- P6 模型就绪度与公开证据专项测试通过；
- 53 个公开工具登记一致；
- 能力登记的声明证据和有效证据哈希一致；
- Windows Materials Studio Perl locale 子进程回退通过，stderr 为 0；
- 未启动 CASTEP、MPI 或 Materials Studio 受限计算进程。

版本以 `release-manifest.json` 为准。候选发布、切换和回滚脚本都会验证发布清单、依赖、哈希、locale 和 `current` 指针；候选验收不会自动切换生产部署。

## 开发与验证

运行完整回归：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
.\.venv\Scripts\python.exe -m pip check
```

验证发布清单：

```powershell
.\.venv\Scripts\python.exe -m materials_studio_mcp.release verify --manifest .\release-manifest.json
```

如需构建候选发布，请使用 `scripts/build_release_v1.ps1`；如需安装候选，请使用 `scripts/install_release_v1.ps1`，不要直接修改不可变部署目录或 `current` Junction。

## 公开说明

本仓库公开的是 MCP 服务源代码、测试、模板和审计回执。回执中的本机软件路径用于说明证据绑定，不是可供他人访问的文件共享路径；使用者应替换为自己的环境配置。

本项目不承诺自动完成任意材料体系的科学建模。它提供可复现的工具链、输入检查和证据管理，最终的结构、力场、晶胞、电荷和计算设置仍需材料研究者确认。
