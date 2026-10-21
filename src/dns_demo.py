"""教材原理复现：真实 DNS 查询与报文解码演示。

负责人：组员B（D4）
参考：plan.md 阶段2 D4、报告第一部分素材

待实现要点：
- 用 dnspython 向配置的递归解析器查询少量已知正常域名，记录查询/响应
- 逐字段解码报文头与 Question/Answer（A/AAAA/CNAME/NS、TTL、NXDOMAIN）
- Wireshark 过滤 udp.port == 53 or tcp.port == 53，截图存入 report/evidence/
- 说明 stub→递归解析器 与 递归解析器→根/TLD/权威 的区别，
  不将普通 Resolver 调用当成逐级迭代演示，不声称看到解析器内部查询
"""
