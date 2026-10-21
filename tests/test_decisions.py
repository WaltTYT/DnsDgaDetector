"""改判逻辑测试（组长主笔，组员B 协助规则部分，D9）。

覆盖 plan.md 2.4 改判表：
- R=0 不请求 LLM；B=1 无论熵是否命中均保留告警
- E=1,B=0 且 verdict=normal 才改判；DGA/uncertain 保留告警
- API 失败/格式校验失败/缓存缺失 → 按 uncertain 保留告警并单列原因
- 缓存键变化后旧缓存失效
"""
