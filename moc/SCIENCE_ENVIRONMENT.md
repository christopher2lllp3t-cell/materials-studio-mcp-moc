# Scientific analysis environment

The MCP server environment and the scientific analysis environment are intentionally separate.

- MCP/MOC core: use `E:\ms_mcp\ms_mcp_runtime\materials_studio_2023\.venv` and `requirements.lock`.
- First-party G02/G04/G06 analysis and gate tests: use 64-bit CPython 3.12 with `science-requirements.lock`.
- Verified local scientific runtime: CPython 3.12.10 and NumPy 2.5.1.

Create or update a dedicated scientific environment with:

```powershell
python -m pip install -r D:\分子动力学模拟\07_mcp_materials_studio\science-requirements.lock
```

NumPy is not added to the MCP core package because the server and MOC evidence reader do not import it. It is required by the G04 coordinate-placement implementation and its scientific regression tests.

The model-specific analysis programs remain hash-bound by their gate analysis locks. Installing dependencies does not authorize changing a frozen analyzer or its thresholds.
