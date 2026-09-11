# Codex Feishu Notifier

Codex 的生命周期观察器只记录通知，独立后台发送进程负责飞书网络请求。默认任务持续超过 10 秒才发送执行中卡片；短任务仅发送结果。运行过程中原位刷新耗时、Token、步骤数和当前步骤；本轮结束、需要处理或中断后，更新为对应终态。通知发送不需要调用额外模型。

## 可靠性优化（2026-09-12）

- 网络发送不占用全局状态锁；单个发送进程串行处理卡片，防止终态后又被旧进度覆盖。
- 持久化队列独立重试，默认 5 秒起指数退避、最长 15 分钟；一条失败不阻塞后面的可发送通知。
- 队列不再截断到 100 条；默认失败 12 次后保存至 `feishu_failed.json`，可查看原因并手动重新排队。
- 内核文件锁随进程退出自动释放；终态入队先持久化，再更新本地状态，重启可恢复。
- 标题、请求摘要、结果、进展和错误日志统一做常见凭据脱敏；可按项目关闭内容摘要。
- 修改配置仅更新显式参数，保留原来的关闭状态、发送目标和偏好。新配置默认关闭，需显式 `--enable`。
- Token 按本轮累计差值记录，分别保留输入、输出、缓存输入和总量；没有可靠基线时显示未记录。缓存输入是输入的子项，不应再次相加。
- 同一进程增量读取运行记录；每 15 秒发现文件，仅逐秒检查最近或活动文件。已结束状态默认 30 天后移入本地 `archive`，不删除记录。
- 提供只读 `status`，以及带时间、通知键、发送结果的轮转日志。日志不需要模型分析才能查看。
- 插件包用 `PLUGIN_ROOT` 定位脚本，并支持原生 `Interrupt` 钩子；旧全局钩子可继续指向稳定源码路径。参考 [Codex 官方钩子文档](https://learn.chatgpt.com/docs/hooks)。

## 通知样式

公司应用机器人直接发送完整飞书 Card 2.0 JSON，不再依赖卡片模板 ID：

- 蓝色卡片：Codex 已开始执行本轮任务，运行时持续原位刷新。
- 绿色卡片：Codex 本轮处理已结束，不代表整个项目永久完成。
- 红色卡片：任务需要确认、授权、补充资料，或执行受阻。
- 橙色卡片：用户停止了正在运行的任务，或本轮被取消。
- 字段：对话名、项目名、模型与推理等级、约计 Token、开始时间、实时用时、步骤数、当前步骤、最近进展、任务目标和最终一句话结果。
- 未完成卡片额外显示失败类型，例如等待确认、等待授权、缺少信息、权限受限或执行失败。
- 当前步骤优先来自 Codex 已公开的阶段更新；工具动作只显示“检查、构建、更新文件、查询资料”等安全分类，不发送终端命令、工具参数或凭据。
- 任务目标来自用户本轮指令，经本机清理并压缩为最多 160 字的一句话；不会额外调用模型。

新步骤最快每 12 秒刷新一次；没有新步骤时默认每 30 秒刷新运行时间。卡片编辑本身不会反复触发群提醒。

默认附带一条约 140 字的“一句话结果”。它直接从 Codex 已有的最终回复中清理 Markdown、合并有效内容并截短，不会再次调用模型，因此不产生额外模型 Token，也不会发送完整回复正文。模型、推理等级与 Token 约数直接读取 Codex 本地运行记录，不会新增模型调用。

通知名称优先使用 Codex 侧栏里的对话名；读取不到时才使用普通项目目录名。`g-p-...` 之类的内部目录代码不会显示在飞书通知中。

## 工作方式

- `SessionStart`：记录会话开始时间，作为兼容性回退。
- `UserPromptSubmit`：记录用户提交时间，排队发送可刷新的执行中卡片，默认延迟 10 秒。
- `Stop`：从最终回复判断完成/未完成状态，更新原卡片，再回复一条简短状态消息触发第二次普通提醒。
- 后台生命周期监听器：持续读取 Codex 本地运行记录里的 `task_started`、阶段更新、工具动作、`task_complete` 和 `turn_aborted`。即使某个新建任务没有加载生命周期钩子，也能补充发送开始、实时进展、完成或中断通知。
- 双通道去重：钩子和后台监听器可以同时工作；相同 session/turn 只会发送一次，不会产生重复卡片。
- 原卡片更新失败：自动降级为新发一张终态卡片，避免漏报。
- 发送失败：独立发送进程按退避时间自动重试，不需要下一次对话触发。
- 重复事件：按 session/turn 去重，避免同一通告重复发送。
- 后台建议：默认识别并忽略 Ambient Suggestions 的建议生成与安全检查会话，避免没有主动运行任务时收到通告。
- 自动审查：默认忽略模型为 `codex-auto-review` 的内部审批审查回合；它们不是用户主动创建的任务。
- 迁移时不会读取或发送旧微信队列，避免历史消息突然涌入飞书。

## 使用公司应用机器人

应用机器人已经通过飞书 CLI 配置并加入目标群时，使用 bot 身份发送：

```bash
python3 scripts/configure.py \
  --transport lark-cli \
  --lark-profile company-profile \
  --lark-chat-id oc_xxx \
  --enable \
  --test
```

插件只保存 CLI 路径、profile 名和群 ID；App Secret 与访问令牌继续由飞书 CLI 的安全配置保管。bot 身份不需要用户 OAuth 登录。

## 使用群 Webhook

显式指定环境文件，不自动读取其他项目的机器人配置：

```bash
python3 scripts/configure.py --transport webhook --from-env-file /path/to/.env --enable --test
```

也可以显式提供其他环境文件或 Webhook：

```bash
python3 scripts/configure.py --from-env-file /path/to/.env --test
python3 scripts/configure.py --webhook-url 'https://open.feishu.cn/open-apis/bot/v2/hook/...' --test
```

新安装的配置保存在 `~/.config/codex-feishu-notifier/config.json`；从旧名称升级时会继续读取原来的 `~/.config/codex-clawbot-notifier/config.json`，无需重新填写机器人凭据。配置权限为仅当前用户可读写，`show-config` 会隐藏 Webhook 密钥。

常用偏好：

- `--min-duration-seconds 300`：只通知运行至少 5 分钟的任务。
- `--include-last-message`：附带经过截断的最终回复摘要。
- `--no-result-summary`：关闭默认的一句话结果。
- `--project-name "显示名称"`：固定卡片里的项目名称。
- `--conversation-name "显示名称"`：固定覆盖 Codex 对话标题。
- `--no-cards`：关闭卡片并改用纯文字通知。
- `--no-start-notice`：关闭任务开始通知，恢复为只在结束时通知。
- `--no-finish-reply`：只刷新原卡片，不补发用于触发第二次提醒的简短回复。
- `--no-live-progress`：保留开始和结束卡片，但关闭运行中的持续刷新。
- `--progress-update-seconds 12`：设置新步骤触发更新的最短间隔，可选 5–60 秒。
- `--progress-heartbeat-seconds 30`：设置无新步骤时刷新耗时的间隔，可选 12–300 秒。
- `--prompt-summary-chars 160`：设置执行中卡片 `prompt` 变量的长度上限，可选 80–240。
- `--notify-ambient-suggestions`：如确实需要，可恢复 Codex 后台项目建议通知；默认关闭。
- `--enable` / `--disable`：明确打开或关闭通知；保存其他参数不会隐式打开。
- `--start-delay-seconds 10`：短任务仅发结果，超过延迟才显示运行中卡片。
- `--summary-excluded-project "项目名"`：指定不附带请求、进展或结果内容的项目，可重复传入。

## 手动检查

```bash
python3 scripts/notifier.py dry-run
python3 scripts/notifier.py send-test
python3 scripts/notifier.py flush
python3 scripts/notifier.py show-config
python3 scripts/notifier.py status
python3 scripts/notifier.py retry-failed
python3 scripts/interruption_watcher.py status
python3 scripts/interruption_watcher.py install
python3 scripts/interruption_watcher.py replay-latest --max-age-seconds 3600
python3 -m unittest discover -s tests -v
```

## Codex 语义

`Stop` 或 `task_complete` 表示本轮结束，不保证项目整体完成。明确的失败、中断、等待输入状态优先；只有缺少结构化状态时才根据最终回复做兼容判断，可能存在误判。原生 `Interrupt` 与本地 `turn_aborted` 都可补充中断通知。最短运行时间只过滤尚未展示执行中卡片的任务；已经展示或正在发送的卡片仍会收到终态更新。

## 边界与恢复

- 本版本通过 77 项自动化测试，并完成一次真实飞书链路验证：后台发送开始卡片、原位更新为本轮结束、发送完成提醒，并通过飞书回读确认更新。新部署仍需使用自己的机器人配置并验证收件。
- 不承诺网络层绝对只发一次。飞书接收后、客户端确认前断电等情况下依靠稳定幂等键重试，仍受服务端幂等期限影响。
- 运行记录和内部数据库不是稳定接口；新复制/恢复的记录默认不重播历史内容，因而极短任务若既漏掉钩子、又在首次发现前结束，仍可能无法补发。不能把“过滤历史”与“无条件补发全部历史”同时保证。
- 暂停期间不补发当时未入队的任务；关闭发送不会清空已经入队的通知。`retry-failed` 是显式重试，会归档原失败记录。
- 观察器用用户级 macOS 服务恢复运行；独立发送进程退出后由观察器每 10 秒检查恢复。电脑关机、休眠或离线期间无法实时通知。
- 脱敏是尽力防护，无法识别所有无标签秘密。敏感项目建议使用 `summary_excluded_projects`，不要把密钥写入聊天正文。

## 安全

- Webhook 只写入权限为 `0600` 的本机配置文件，不写进插件源码或日志。
- 公司应用机器人的 App Secret 和访问令牌不会写入插件配置或日志。
- 仅接受飞书或 Lark 官方 HTTPS Webhook 域名。
- 默认不转发 Codex 最终回复正文。
- 所有通知可见内容隐藏常见密钥和令牌；实时步骤额外移除 URL，不发送工具参数和终端命令。
- 配置、飞书重试队列和去重记录均以仅当前用户可读写的权限保存。
