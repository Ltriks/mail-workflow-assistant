# 仓库维护说明

## 仓库用途

本仓库保存邮件事务助理的源码、规则、演示资料、测试和部署说明。当前是具体用户的内部试点项目，文档包含实际测试邮箱和部署环境信息，建议使用私有远程仓库。公开发布前再制作通用示例、移除环境标识并明确许可证。

现有项目目录可以直接初始化为本地 Git 仓库，无需另建文件夹。远程仓库用于备份和协作，可在 GitHub、GitLab 或其他代码平台创建；本地提交不等于已经推送。

## 目录

| 路径 | 内容 |
| --- | --- |
| `README.md` | 使用入口与文档索引 |
| `demo/` | 虚构业务知识、限定收件范围、模板与离线样例 |
| `live/` | 真实业务待配置模板，默认禁止自动发送 |
| `docs/` | 产品定位、来信人指南、审批规则、测试和运维说明 |
| `skills/agentmail-assistant/` | Agent 操作规则及三个 Python 程序 |
| `tests/` | 使用模拟邮件/飞书接口的离线测试 |
| `deploy/` | 当前 Mac mini 安装脚本与 QClaw 技能模板 |
| `templates/` | 本地台账和报告模板 |

## 不进入 Git 的内容

`.gitignore` 排除 `state/`、`logs/`、运行时 `service-config.json`、`bin/`、环境变量文件、密钥文件、Python 缓存和本地 `.ai/` 工作记录。不要强制添加这些路径。

`state/` 保存草稿、审批和跨会话去重记录，不是可随意删除的缓存。代码仓库不能替代它的受控备份；迁移时需单独保留记录，禁止通过清空台账重试不确定的发送。OAuth 授权在运行机器上完成，不随源码传递。

`.gitignore` 只能排除文件，不能保证源码里没有凭据。每次提交仍检查暂存差异，不把临时授权 URL、令牌、密码或私钥写进文档。

## 开发验证

在项目根目录运行；本项目约定 shell 命令使用 `rtk` 前缀。

```bash
rtk proxy python3 -m unittest discover -s tests -v
rtk proxy git diff --check
rtk proxy git diff --cached --check
rtk proxy git status --short
```

测试使用 Python 标准库和模拟外部接口，不发送真实邮件。实际运行另需 rtk、已授权的 agently-cli，以及审批功能使用的 lark-cli。CLI 版本和目标机器路径见[Mac mini 文档](macmini-service.md)。

部署脚本针对当前 Mac mini 环境，包含现有应用和审核人标识，假定 `bin/` 已准备好工具；它不是通用的一键安装器。新环境需检查路径、配置和授权，并按[测试状态](test-status.md)完成验收。

## 连接远程仓库

建议创建一个空的私有仓库，例如 `mail-workflow-assistant`，不要初始化 README 或许可证，以便直接推送已有历史。创建完成后，在项目根目录把下面的占位文字替换为真实仓库地址：

```bash
rtk proxy git remote add origin <远程仓库地址>
rtk proxy git push -u origin main
```

如果已有合适的远程仓库，可以直接使用其地址；若远程已有提交，先检查历史再合并，不强推覆盖。当前没有实施自动部署，Git 提交或推送不会自动更新 Mac mini 服务。
