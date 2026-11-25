# 测试数据说明

本目录是可复用的虚构商品测试输入，不是运行结果。`test_scenarios` 用于解释用例，不是商品事实或写作指令。

| 用例 | 商品数据 | 指令及预期 |
|---|---|---|
| LUMA 正常 | `luma-normal.json` | `test-cases.json` 中 `luma-normal` |
| LUMA 诱导虚构 | `luma-unsupported-claim.json` | 同一商品资料，单独传入要求免烫、防紫外线及免费退货的 `user_query` |
| LUMA 缺失字段 | `luma-missing-data.json` | 删除 `/product/facts/price` 与 `/product/facts/care` |
| ORBIT 跨品类 | `orbit-normal.json` | 同一工作流处理水瓶，不写商品专属代码 |

`test-cases.json` 是测试清单：`product_file` 和 `user_query` 是输入；`expected` 是测试执行器的判定预期，不传给写作 Agent。当诱导请求触发暂停时，测试执行器模拟选择“移除无依据要求并继续”，再检查最终成品；这与真实用户的交互确认分开。

LUMA 正常与诱导测试的商品 JSON 内容相同，以隔离用户指令变化。缺失数据用例仅删除上述两个字段。目标字数以各商品 JSON 的 `content_requests` 为准。确定性测试见 tests；这些输入已有真实服务小样例，但不代表质量统计或真实商品验证。
