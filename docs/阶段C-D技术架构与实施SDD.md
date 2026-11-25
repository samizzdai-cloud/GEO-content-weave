# 技术架构与实施 SDD

## 模块与执行

| 模块 | 职责 |
|---|---|
| [app.py](../geo_workbench/app.py) | FastAPI、同源保护、用户动作和文件下载 |
| [batches.py](../geo_workbench/batches.py) | 输入规范化、来源范围、按商品展开批次 |
| [workflow.py](../geo_workbench/workflow.py) | 入口→并行受众／联网研究→规划→人工确认的 LangGraph |
| [intake_dialog.py](../geo_workbench/intake_dialog.py) | 读取资料、候选差异、多轮对话和版本化确认 |
| [research.py](../geo_workbench/research.py) | 模型／Skill 适配、模拟访谈、搜索读取、证据与 Planner |
| [content.py](../geo_workbench/content.py) | Writer、核验、Editor、回归、渲染与交付判定 |
| [composition.py](../geo_workbench/composition.py) | 章节预算与可见字数契约 |
| [images.py](../geo_workbench/images.py) | 图片会话、Seedream、读图、候选与图文确认 |
| [store.py](../geo_workbench/store.py) | SQLite、不可变产物、摘要、事件与决策 |
| [queue.py](../geo_workbench/queue.py) | 单执行槽、事务领取、排序、中断恢复 |
| [ownership.py](../geo_workbench/ownership.py) | 数据目录的单服务进程锁 |
| [静态 Web](../geo_workbench/static) | 原生界面、独立草稿、轮询、同页图片操作 |

当前主队列顺序执行；研究内部受众与证据分支可并行，画像访谈最多三份并行。Blog 双候选是两个会话，不代表两个新增 Agent 或已启用图片并行。

## 输入、版本与来源

每次上传形成独立来源快照，按 product 建立 run。品牌同名只影响展示分组。配置继承和 SKU 值都有来源指针；变体信息不会提升为通用事实。

研究产物绑定输入与意图版本，规划确认绑定修订和摘要。内容尝试必须验证批准的依赖；图片进一步绑定 `attempt_id / text_revision / product_digest / plan_revision / plan_digest`。历史产物不能作为当前版本的静默替代。

`store.put` 将产物写为文件并登记摘要；读取时验证路径归属和内容摘要。事件保存节点、错误类型、模型用量及 Skill hash。真实凭证不写入 Prompt、产物或普通错误信息。

## 工作流与工具权限

研究 LangGraph 的人工输入处置和规划门禁通过 SQLite checkpoint 持久化；恢复前检查确认点是否已落盘。用户等待不占后台工作槽。

入口模型可读事实与预览候选，不直接改 JSON。研究模型提出查询和摘录，程序执行搜索／读取、去重及摘录定位。Writer／Editor 使用字数与候选工具，冻结事实／证据；其他角色不新增网页检索。详细原理见 [Agent 说明](agents.md)。

文字是另一个受控 LangGraph：写作→初检→编辑→回归／必要返工→渲染／结构检查。输出统一 ContentIR，HTML、Markdown 和 JSON-LD 从同一对象派生，不分别手写。

## 核验与编辑契约

程序为当前正文单元分配 ID，核验只返回单元 ID、判断、理由与来源，不允许用模型转述替代原文。单元覆盖、当前版本、未知／重复 ID、来源范围和必要分类字段由程序校验。界面从当前 IR 取原文。

分类区分商品主张、品类知识、普通背景、非事实建议和输入缺失。输入路径分 provided／empty／missing／invalid／unavailable；false、0 不自动算缺失。无法定位的路径不能支持“未提供”。检查器协议异常优先有限修复核验输出；实际未支持、冲突或未知主张才进入内容处理。

Editor 操作包含准确的单元、旧值、新值及原因。跨轮候选必须是工具认可且可重建的基线；过期基线或错误旧值被拒绝。事实回归使用当前编辑后的版本，不复制之前的判定。字数／引用可解析只说明确定性契约有效，不说明语义成立。

一次文字尝试上限：总调用 14、480 秒、已观测 240000 tokens；Writer 4，Editor 首次 4＋最多剩余 3。调用前同时检查角色／总预算并预留下游核验，不通过重建 provider 重置累计用量。

## 图片与候选

一个 ImageAgent 定义使用 DeepSeek Flash 的 enabled／low 模式组织 Prompt 并读取实际 PNG，使用 Seedream `doubao-seedream-5-5-261028` 生图。两页结构不同，字段及含义见 [Agent 说明](agents.md)。程序将字段串为供应商文本 prompt，不将营销分析或内部推理发给 Seedream。

上下文来自当前商品、品牌／商品风格、对应正文和编辑策略。Blog 额外高亮主题、受众及显式目标场景，保留来源指针；缺失时在同次调用内理解正文，不伪造来源字段。共享风格由程序组装，不另加风格规划调用。

`image_sessions` 绑定 run／page／candidate／正文版本，保存配置、自动次数和本卡选定版本。`with_user` 与 `without_user` 的消息、参考和预算隔离；旧单图迁移为 legacy，保留费用和选择。

`image_jobs` 事务登记幂等请求，`work_items` 持久排队。每次外发生成前递增计数；失败也保守计入。每候选最多三次，Blog 双候选最多六次，旧 legacy 费用不被重置。当前请求及首次要求持续作为任务上下文；供应商缓存只按实际 usage 观察，本地完整历史不依赖缓存。

自动生成后检查真实 PNG、尺寸比例及 alpha，再发实际图片给 DeepSeek。生成结果在视觉调用前保存，读图失败仍保留付费图片并显示 unknown。自动返工以本会话上一张原图为参考。人工修改只传本候选选定原图，单次生成后程序检查，状态 `not_run_user_edit`，无后置读图或循环。

新请求 `watermark=false`，展示版由 Pillow 在右下角叠加中文磨砂标记；原图和展示版分开保存，避免标签反复进入参考。字体支持 Windows／macOS 系统 CJK 字体及 `GEO_LABEL_FONT` 覆盖。

## 选图、批准与导出

`image_page_choices` 将页级选图与候选最新版本分开。自动返回只填补未选图页面，不能覆盖现有选择；用户 select／confirm 才更新选择。旧单图选择迁移时保留。

`page_release` 绑定正文依赖、确切资产、HTML 摘要与 actor；同版本确认幂等，过期依赖拒绝。确认某候选只生成该页 release，不自动确认另一页。单页已确认 HTML 可直接下载；ZIP 要求两页当前组合均已确认，保留相对图片／页面引用和全部当前候选历史。

## 网络与恢复

`.env` 在包初始化时加载，早于数据目录与客户端构建；系统已有环境变量优先。DeepSeek 模型设置各角色一致，默认官方 API 和 deepseek-flash。HTTPX 客户端使用 `trust_env=True`，模型 SDK 使用正常环境代理，不植入 Codex 网络权限流程。

保留公开网页 URL 校验、任务归属、供应商图片下载地址校验、本地 Web 同源／令牌保护；这些不阻止正常外网模型请求。代理和证书配置详见 [运行说明](operations.md)。

数据目录同一时刻只允许一个服务进程，队列领取使用事务。运行中断／外部结果未知不自动重放，已排队未外发的请求可恢复。图片失败不改变已完成的文字结果。后续若需多用户或并行 worker，应先重新设计认证、并发领取、计费和恢复策略。
