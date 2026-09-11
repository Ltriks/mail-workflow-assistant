#!/usr/bin/env python3
"""Run inside the uploaded service directory; never copies auth credentials."""
import json
import os
from pathlib import Path
import plistlib
import subprocess

root = Path(__file__).resolve().parents[1]
base = Path.home()
state = root / "state"
state.mkdir(mode=0o700, exist_ok=True)
(state / "feishu").mkdir(mode=0o700, exist_ok=True)
(root / "logs").mkdir(mode=0o700, exist_ok=True)
old_root = "/Users/mcq/Repos/cowork/agentmail"
for file in state.rglob("*.json"):
    value = json.loads(file.read_text())
    # Migrate only filesystem metadata. Approved recipient/body/hash stay exact.
    if isinstance(value, dict):
        changed = False
        for key in ("policy_path", "source"):
            if isinstance(value.get(key), str) and value[key].startswith(old_root + "/"):
                value[key] = str(root) + value[key][len(old_root):]
                changed = True
        if changed:
            file.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        file.chmod(0o600)
cfg_path = root / "service-config.json"
if not cfg_path.exists():
    cfg_path.write_text(json.dumps({"app_id": "cli_aafff69826381cd5",
        "reviewer": "ou_9883864148e1680efa5720a7fb91da69", "mailbox": "lqtrikst@agent.qq.com",
        "allow_real_mail": False}, indent=2) + "\n")
    cfg_path.chmod(0o600)
service_path = str(root / "bin") + ":" + str(base / ".local/bin") + ":" + str(base / ".local/node/bin") + ":/usr/bin:/bin:/usr/sbin:/sbin"
wrapper = root / "bin/agentmail-service"
wrapper.write_text('#!/bin/sh\nexport PATH="' + service_path + '"\ncd "' + str(root) + '"\nexec /usr/bin/python3 skills/agentmail-assistant/scripts/feishu_service.py "$@"\n')
wrapper.chmod(0o700)
plist_path = base / "Library/LaunchAgents/com.qcu.agentmail-callback.plist"
plist_path.parent.mkdir(parents=True, exist_ok=True)
plist = {"Label": "com.qcu.agentmail-callback", "ProgramArguments": [str(wrapper), "serve"],
         "WorkingDirectory": str(root), "RunAtLoad": True, "KeepAlive": True,
         "ThrottleInterval": 30, "ExitTimeOut": 20,
         "EnvironmentVariables": {"PATH": service_path, "PYTHONUNBUFFERED": "1",
             "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1", "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1"},
         "StandardOutPath": str(root / "logs/service.stdout.log"),
         "StandardErrorPath": str(root / "logs/service.stderr.log")}
plist_path.write_bytes(plistlib.dumps(plist))
plist_path.chmod(0o600)
skill_dir = base / ".qclaw/workspace/skills/agentmail-callback"
skill_dir.mkdir(parents=True, exist_ok=True)
skill = (root / "deploy/qclaw-SKILL.md").read_text().replace("__SERVICE_ROOT__", str(root))
(skill_dir / "SKILL.md").write_text(skill)
print(json.dumps({"root": str(root), "launch_agent": str(plist_path), "qclaw_skill": str(skill_dir),
                  "auth_copied": False, "allow_real_mail": json.loads(cfg_path.read_text())["allow_real_mail"]}, ensure_ascii=False))
