"""GLM-4-Flash 语义复核：调用、校验、缓存与改判。

负责人：组长（D7–D9）
参考：plan.md 2.4 改判规则、2.5 调用预算与缓存

待实现要点：
- 只送 E=1 且 B=0 的样本；输入为规范化域名+注册域标签，不含真值/生成来源
- 输出校验：{"verdict": "DGA|normal|uncertain", "reason": "..."} 枚举与字段校验
- hybrid = B OR (E AND verdict != normal)；失败/超时/缓存缺失按 uncertain 处理并单列原因
- 串行调用、有上限指数退避最多 3 次；temperature=0 并记录生成参数
- 密钥解析：优先环境变量 ZHIPU_API_KEY，缺省自动回退根目录 api.txt（已被 .gitignore 忽略）
- 缓存键含输入+模型+模板哈希+参数；原始响应入 results/llm_cache.jsonl；Key 不得写入缓存
- 正式调用前统计可复核样本数与去重请求数（约 800 内），先测 20 个样本估时
- 离线模式绝不请求 API，报告缓存覆盖率
"""

import os
from pathlib import Path


def get_api_key() -> str:
    """优先环境变量，其次根目录 api.txt；两者都没有时报错，不静默返回空。"""
    key = os.environ.get("ZHIPU_API_KEY", "").strip()
    if not key:
        env_file = Path(__file__).resolve().parent.parent / "api.txt"
        if env_file.exists():
            key = env_file.read_text(encoding="utf-8").strip()
    if not key:
        raise RuntimeError("未找到 API Key：请设置环境变量 ZHIPU_API_KEY，或将密钥写入根目录 api.txt")
    return key
