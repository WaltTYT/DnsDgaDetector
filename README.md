# DNS DGA Detector

DNS 异常查询检测的规则 + LLM 混合方案，实施范围见 [plan.md](plan.md)。

## 开发环境

- Conda 环境：`D:\study\miniconda\envs\dns-dga`（命名环境 `dns-dga`）
- Python：3.11.17
- 核心依赖：dnspython、requests、tldextract、NumPy、pandas、Matplotlib、pytest

项目环境统一存放在 Miniconda 默认环境目录。`D:\study\miniconda\envs` 对普通用户不可写，创建/删除/重建环境需在管理员终端执行；激活与日常使用不受影响。

环境已创建并通过依赖检查，以及 DNS 报文处理、离线后缀解析、数据计算和图表生成验证。直接依赖版本见 `requirements.txt`。请在自己的终端执行下面的激活命令。

```powershell
conda activate dns-dga
Set-Location D:\study\AgentWorkPlace\CodexWorkPlace\DnsDgaDetector
python -c "import sys; print(sys.executable)"
python -m pip check
```

也可直接使用解释器，无需激活：

```powershell
& "D:\study\miniconda\envs\dns-dga\python.exe" --version
```

## 从配置重建环境

以下命令用于目标环境尚不存在时，在项目目录中执行。缓存路径设置仅影响当前 PowerShell 会话。缓存目录 `D:\study\miniconda\.cache` 已授予当前用户写权限，普通终端即可执行；`conda env create` 需写入 `envs` 目录，仍建议用**管理员终端**。

```powershell
Set-Location D:\study\AgentWorkPlace\CodexWorkPlace\DnsDgaDetector
$env:CONDA_PKGS_DIRS = 'D:\study\miniconda\.cache\pkgs'
$env:PIP_CACHE_DIR = 'D:\study\miniconda\.cache\pip'
conda env create -n dns-dga --file environment.yml
```

`environment.yml` 固定 Python 版本，并通过 `requirements.txt` 安装直接依赖。间接依赖由 pip 解析；复建后运行 `python -m pip check` 检查兼容性。

## LLM 接口

实际调用前，在当前 PowerShell 会话中设置 `ZHIPU_API_KEY` 环境变量。不要将密钥写入配置、源码、截图或提交记录。环境安装和本地依赖验证不需要 API Key；模型可用性与免费政策留待 D1 的接口冒烟测试确认。

本地便捷方式：将密钥单独粘贴到根目录 `api.txt`（仅一行 Key，该文件已被 `.gitignore` 忽略，切勿提交）。`src/llm_judge.py` 的 `get_api_key()` 会优先读环境变量 `ZHIPU_API_KEY`，未设置时自动回退读取 `api.txt`，无需手动开终端设置。
