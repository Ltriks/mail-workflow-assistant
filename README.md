# 邮件事务助理：Agent Mail 接入与飞书人工审核

已准备：教师培训演示资料、6 封测试邮件、邮件助理技能、本地台账、发送检查程序和真实业务模板。
当前配置：`lqtrikst@agent.qq.com` 为助手邮箱；`lqtriks@agent.qq.com` 为自测来信邮箱；`xiaofan1990@agent.qq.com` 为同事来信邮箱。
安装技能不等于启动自动收发。本项目包含限时邮件处理工具和常驻飞书审批回调服务；尚未实现全天收件、模型判级与业务调度。Mac mini 当前仅完成回调服务部署，真实邮件执行仍关闭。部署与测试状态记录截至 2026-09-11，不代表实时健康状态。

## 文档入口

| 读者或用途 | 文档 |
| --- | --- |
| 向老师、领导介绍产品价值 | [产品定位与普通邮箱接入对比](docs/product-overview.md) |
| 邀请来信人试用 | [来信人使用说明](docs/sender-guide.md) |
| 与同事、同事的 Agent 联调 | [邮件联调说明](docs/colleague-test-guide.md) |
| 确认自动化和人工介入边界 | [001—005 规则](docs/review-levels.md) |
| 使用飞书审批 | [飞书卡片说明](docs/feishu-review.md) |
| 维护 Mac mini 服务 | [部署与运维](docs/macmini-service.md) |
| 查验收结果与剩余工作 | [测试状态](docs/test-status.md) |
| 开发、提交与远程仓库管理 | [仓库维护说明](docs/repository-guide.md) |

## 同事配合与等级规则

- 转给同事及其 Agent：[邮件联调说明](docs/colleague-test-guide.md)，文件内有准备指令和五个等级的完整测试邮件。
- 领导与接收方使用：[001—005 人工介入规则](docs/review-levels.md)。001整理、002自动回复、003审稿、004业务决策、005阻断接管。
- 同事地址 `xiaofan1990@agent.qq.com` 已加入 `demo/policy.json` 的 `allowed_senders`，原自测地址仍保留；不使用通配符。
- 在控制会话中说：“使用 $agentmail-assistant，按 demo 规则和001—005规范，处理白名单中的 lqtriks@agent.qq.com 与 xiaofan1990@agent.qq.com 发给 lqtrikst@agent.qq.com、主题以 [演示] 开头的本轮新邮件，运行20分钟，002允许模板自动回复、最多5封；003先审稿、004先问我作决定、005阻断并通知我。收到就绪信号后同事才发邮件。”
- 同事说明是准备材料，复制或转发文件本身不启动助理、不授权发信。003 本机限时飞书卡片审批已通过真实本人批准、邮件提交和卡片更新验证：[飞书审批卡使用说明](docs/feishu-review.md)。修改、保存、重新确认、拒绝和自动到期已通过不发邮件的实际测试；Mac mini 真人按钮回调迁移验证仍待完成。004 的业务决定仍在 Codex 确认。

## 下周演示：按这个顺序使用

### 1. 打开项目，先离线预演

在 Codex 中打开 `/Users/mcq/Repos/cowork/agentmail`。技能安装后新开本项目会话；如果技能列表尚未出现，可直接要求读取本项目 `skills/agentmail-assistant/SKILL.md`。

复制给 Codex：

> 使用 $agentmail-assistant，按 demo/test-emails.md 离线预演六封邮件。展示分类、采用的资料、拟回复或待确认问题，以及本地事务台账。不要联网收发邮件。

现成预演结果见 `demo/rehearsal.md`。该文件是离线示例，不是真实邮件处理记录。

### 2. 正式演示时启动助理

先查看 [演示规则](demo/policy.json) 和 [虚构业务资料](demo/knowledge.md)，然后复制：

> 使用 $agentmail-assistant，按 demo/policy.json 和 demo/knowledge.md 启动 20 分钟邮件助理演示。我授权在这 20 分钟内，仅处理 lqtriks@agent.qq.com 发给 lqtrikst@agent.qq.com、主题以 [演示] 开头的新邮件；允许直接发送规则内的固定模板回复，不必逐封确认，最多发送 5 封。其他情况先展示完整草稿和原因，再问我。同步登记本地待办，结束后生成报告。

Codex 应先核对账号再报告运行时间窗。如果需要平台命令执行权限，按实际提示批准；业务授权不能绕过平台权限。首次正式演示前建议先跑一遍此步骤。

### 3. 用另一个邮箱发测试邮件

在网页端登录 `lqtriks@agent.qq.com`，确认发件地址正确，然后按 [测试邮件](demo/test-emails.md) 顺序复制主题、正文发送给 `lqtrikst@agent.qq.com`。
不要在同一 CLI 中重新登录测试邮箱，以免切换助理正在使用的身份。若网页端无法使用该地址发送，先解决该账号的登录/发信能力，再演示。

前三封即可演示核心流程：自动回答地点 → 登记报名并补信息 → 改期和费用交你确认。最后在网页查看收到的真实回复。

### 4. 回答待确认事项或停止

批准时指明草稿，如：“同意发送草稿 abc123，收件人和正文按刚才展示的内容。”
修改时直接写希望发送的准确正文并明确说“发送”。待确认不会自动视为同意。

停止口令：

> 停止邮件助理，生成本次处理报告，列出待确认和未完成事项。

记录在 `state/`：`session.json` 当前会话及草稿、`ledger.json` 跨会话发送/跳过记录、`history/` 历史会话、`tasks.md` 事务台账、`report-*.md` 运行报告。不要清空 ledger 来重试结果不明确的发送。

## 改成真实办公业务

1. 填 [业务资料](live/knowledge.md)：真实安排、有效日期、必需材料、常见答案、例外处理人。
2. 在 `live/policy.json` 填写明确的发件邮箱白名单和主题前缀；保留 `auto_enabled: false`。空白名单会拒绝启动，不会处理全邮箱。
3. 先使用草稿模式：

> 使用 $agentmail-assistant，检查 live/ 资料是否完整。按 live/policy.json 启动 20 分钟 preview，只处理启动后的合规新来信，整理本地事务并生成草稿，未经我明确批准不得发送。

4. 验证数封真实样本后，再要求 Codex 整理可自动回答的固定模板、使用条件与配置差异。你审阅并明确批准后，才启用真实业务 auto。
5. 处理历史邮件需明确说出起始时间；未指定则只处理启动后的邮件。日历、飞书、附件下载、对外发文件不在默认范围内，可按具体业务另行加入。

发件人白名单只筛选地址，不证明身份；涉及费用、审批、敏感信息的请求仍须人工判断。自动语义分类由 Codex 完成；脚本只能强制检查具体字段和发送限制。

## 程序与验证

无需额外 Python 依赖。macOS/Linux 使用 Python 3、rtk 和已登录的 agently-cli。

```bash
rtk proxy python3 skills/agentmail-assistant/scripts/mail_guard.py --help
rtk proxy python3 -m unittest discover -s tests -v
rtk proxy python3 skills/agentmail-assistant/scripts/mail_guard.py status
```

`start` / `scan` / `draft` 会连接邮件服务；`reply` 会真实发信。`--authorized`、`--approved` 只应记录已经取得的用户授权，不能当作无需批准的开关。
离线测试用模拟 CLI，不发送真实邮件；脚本没有定时调度或模型推理。运行演示期间保持电脑唤醒和 Codex 任务活跃。需要全天自动服务时，还需部署调度、模型运行环境、人工审批入口和监控。

官方参考：[Agent Mail 安装文档](https://agent.qq.com/doc/cli-setup.md)、[Codex AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md)。
