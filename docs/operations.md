# 运行与发布说明

## 启动与配置

Windows／macOS 安装命令见 [README](../README.md)。推荐入口为 `python -m geo_workbench --open`；它在服务就绪后打开默认浏览器，默认监听 127.0.0.1:8765，可通过 `--port` 指定端口。

`start.cmd` 和 `start.command` 使用项目自己的虚拟环境，不修改系统 PATH。首次配置需要手动安装依赖和编辑 `.env`。`.env` 在导入存储模块之前加载，使用已有系统环境变量优先，不覆盖它们。

| 变量 | 用途 |
|---|---|
| DEEPSEEK_API_KEY | 文本、研究工具与视觉请求凭证 |
| DEEPSEEK_BASE_URL | 当前官方适配器地址，保持 https://api.deepseek.com |
| DEEPSEEK_MODEL | 各模型角色使用的 DeepSeek 模型，默认 deepseek-flash；更换时需确认视觉／工具能力 |
| SEEDREAM_API_KEY | 图片生成凭证；兼容既有 Seedream_api_key／ARK_API_KEY 写法 |
| GEO_DATA_DIR | 数据目录，默认项目根目录的 .local-data |
| GEO_LABEL_FONT | 可选中文 TTF/TTC，覆盖系统字体选择 |
| HTTPS_PROXY / HTTP_PROXY | 可选环境 HTTP 代理 |
| NO_PROXY | 建议包含 127.0.0.1,localhost，防止本地请求进入代理 |

`GEO_WORKER_DISABLED=1` 仅用于测试／只读排查，不是正常运行方式。已有本地验收任务需要继续使用原来的数据目录；启动新目录不会自动导入历史任务。

## API 联网

应用按正常 Python 程序发起外网请求，不依赖 Codex 的授权弹窗。原来搜索与图片客户端中的 `trust_env=False` 已改为 True，统一继承环境代理；Writer 等角色仍使用冻结证据，不额外增加搜索职责。

若必须使用本地代理，在 `.env` 中取消对应代理项的注释，将端口改为自己的代理 HTTP 端口，重启服务。直接联网可保持这些项注释。不要将代理地址改填为模型 API 基址。

可以运行 `python scripts/check_network.py` 做只读模型列表连接检查。它不生成文本／图片、不打印凭证或完整异常地址。HTTP 401／403 表示已到达服务但认证／权限不通过，429 需检查账号配额，连接异常应检查代理、DNS 和证书。网络可达不证明计费生成请求必然成功。

HTTPX 也会继承相关证书环境配置。不通过关闭 TLS 验证解决证书问题。只监听本机、同源校验、网页公开地址校验是不同层的保护，不是外网 API 的联网限制。

## 数据与异常恢复

SQLite 保存任务、队列、版本和审批；产物文件、checkpoint 与数据库共同构成完整数据目录。备份应先正常停止服务，再复制整个 `GEO_DATA_DIR`，不要只拷一张数据库表。

一个数据目录只运行一个服务进程。正常停止使用 Ctrl+C；不使用 Uvicorn 多 worker 或自动 reload 处理付费任务。异常退出后，执行中的外部调用标为未知／中断，不自动重复计费；核查已有产物再显式重试。

模型连接错误优先查看节点和错误类型；不需要重新上传 JSON 才能排查。修改输入需通过现有候选／确认流程；不要手动修改 SQLite 中的批准、摘要或资产归属。

## 中文图片标记

Windows 优先微软雅黑／黑体，macOS 优先 PingFang／STHeiti／Arial Unicode。若系统字体路径不同，在 `.env` 设置 `GEO_LABEL_FONT` 指向可用的中文字体。找不到字体时明确报错，不将乱码方块伪装为成功标记。Linux 可自行安装 Noto CJK 后指定路径，但本项目未做 Linux 完整验收。

## 发布到 GitHub

源码发布保留代码、应用 Skills、当前文档、示例输入、测试、启动脚本和空凭证 `.env.example`。以下只保留本地：

- `.env` 与其他真实环境文件。
- `.venv/`、数据库、checkpoint、生成结果与用户文件。
- `.local-archive/` 中的旧开发日志、教程摘录、探针和迁移样本。
- `.release/` 内的打包文件、日志和 Python 缓存。

运行 `python scripts/prepare_release.py --check` 检查文档链接、示例凭证和源码中的本地凭证泄漏；去掉 `--check` 后生成 `.release/geo-aigc-source.zip`。打包采用明确发布清单，独立于当前目录是否已经初始化 Git；检查是辅助措施，不代表能检测所有未知秘密。

源码准备与远程发布分别执行。提交前检查暂存文件及发布范围，保留仓库原有 LICENSE；公开仓库不会自动赋予访客写入权限。默认分支采用所有者控制的 PR 贡献流程，见 [贡献说明](../CONTRIBUTING.md)。开发归档保留本地，正式文档只描述当前契约。
