"""生成模拟 DNS 查询日志（正常 + DGA 混合）。

负责人：组员A（D3）
参考：plan.md 2.1 日志规模与行为分布、2.2 日志格式

待实现要点：
- 各集合使用独立模拟时段，总量约 50,000 条，主场景约 95% 正常 / 5% DGA
- 行为分布保留重叠：正常含日常访问与合法突发；DGA 含高频随机、低频随机、低熵词根拼接
- 固定 seed、UTC 时间戳与排序规则；相同 seed 可重建相同输入
- 列：event_id,timestamp,client_ip,qname,qtype,label,source,algorithm,split
  （label/source/algorithm/split 为评估元数据，不得进入特征或提示）
- 输出 data/dns_log.csv + 数据统计表（原始行数/唯一域名数/聚合样本数三层比例）
"""
