# Mac mini 回调服务

2026-09-11 安装到 `mcumacmini@192.168.1.19`，目录 `/Users/mcumacmini/Services/agentmail`。

## 已完成

- 安装 rtk 0.44.2、lark-cli 1.0.84、agently-cli 1.0.18 的已验证 macOS arm64 二进制。
- 回调常驻进程与本地审批队列已安装，33项离线测试在本机及Mac mini通过。
- LaunchAgent `com.qcu.agentmail-callback` 已加载；用户登录期间由launchd托管，退出会重新拉起。
- QClaw技能在 `~/.qclaw/workspace/skills/agentmail-callback/SKILL.md`。未修改原QClaw日报、模型或频道配置。
- 历史审批和邮件去重台账已复制，路径字段已迁移，正文/收件人/哈希保持不变。

## 当前待完成

2026-09-11已完成Mac mini飞书配置，复用审批应用 `cli_aafff69826381cd5`，健康状态为ready。已验证实际发卡、1分钟无人审批自动过期及按钮移除（28f59afdf77c），卡片结束后服务仍在线。已用SIGTERM正常结束主进程，launchd自动恢复，PID从33852变为34002并恢复ready。待补验证Mac mini收到真人按钮回调；断网、断电或系统重启尚未实测。

真实邮件执行默认关闭：`allow_real_mail=false`。Agent Mail在Mac mini完成授权且唯一邮箱核验为 `lqtrikst@agent.qq.com` 后，才执行 `bin/agentmail-service enable-mail`。此前只能做不发邮件的交互测试，不声称已经能够独立全天收发邮件。

## 在QClaw中使用

新开QClaw会话后，可说：

> 使用 agentmail-callback 技能，检查常驻回调服务是否在线。在线后发一张1分钟、不发邮件的测试卡；等待过期，确认卡片结束而服务仍在线。

手工入口（在Mac mini执行）：

```bash
/Users/mcumacmini/Services/agentmail/bin/agentmail-service status
/Users/mcumacmini/Services/agentmail/bin/agentmail-service enqueue --interaction-test --minutes 1
```

真实003草稿排队：

```bash
/Users/mcumacmini/Services/agentmail/bin/agentmail-service enqueue --source state/session.json --draft 草稿ID --minutes 20
```

只提交已按规则核对的完整草稿，排队不是批准。service会在取得ready后发卡；本人批准准确版本后才经mail_guard执行。004仍先作业务决定，005没有普通批准入口。每张卡片期限独立，过期只结束该卡片，不停止常驻服务。

## 运维

- 健康文件：`state/service-health.json`。status=ready且更新时间在30秒内，才代表最近监听在线。
- 审批记录：`state/feishu/*.json`；邮件去重：`state/ledger.json`。
- 运行日志：`logs/service.stdout.log`、`logs/service.stderr.log`。不记录原始回调令牌。
- 查看托管状态：`launchctl print gui/501/com.qcu.agentmail-callback`。
- 停止：`launchctl bootout gui/501 ~/Library/LaunchAgents/com.qcu.agentmail-callback.plist`。
- 启动：`launchctl bootstrap gui/501 ~/Library/LaunchAgents/com.qcu.agentmail-callback.plist`。
- 重启：`launchctl kickstart -k gui/501/com.qcu.agentmail-callback`。

后台每小时更新一次有界WebSocket订阅，断线或配置缺失时30秒后重新检查。发送结果未知不重发；重启时把执行中断的发送标为uncertain，需人工核查。

这是回调服务，不包含全天收件、模型判级和业务调度。电脑关机、休眠、断网或用户未登录时不能保证在线。现阶段不要在两台机器同时启动同一审批应用的回调消费者；后续审批优先走Mac mini。Mac mini真实发信启用后，以其state作为唯一执行台账，不并行使用旧本机快照发信。
