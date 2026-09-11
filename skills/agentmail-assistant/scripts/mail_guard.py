#!/usr/bin/env python3
"""Bounded mail sessions. No scheduler or autonomous model; all sends require user authority."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone


class GuardError(Exception):
    pass


def now():
    return datetime.now(timezone.utc)


def stamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise GuardError("时间戳缺少时区")
    return result


def read_json(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    path = Path(path)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def cli(*args):
    try:
        result = subprocess.run(["rtk", "proxy", "agently-cli", *args],
                                capture_output=True, text=True, timeout=45)
    except (subprocess.TimeoutExpired, OSError) as error:
        raise GuardError(f"CLI 未取得明确结果：{type(error).__name__}") from error
    try:
        data = json.loads(result.stdout)
    except ValueError as error:
        raise GuardError("CLI 没有返回可验证的 JSON；请核查后再处理") from error
    if result.returncode or data.get("ok") is not True:
        raise GuardError(f"CLI 错误（退出码 {result.returncode}）：{data.get('error', '未确认成功')}")
    return data


def account(policy):
    aliases = cli("+me").get("data", {}).get("aliases", [])
    # The CLI automatically chooses an alias; fail closed when selection is ambiguous.
    emails = [a.get("email", "").lower() for a in aliases]
    if emails != [policy["mailbox"].lower()]:
        raise GuardError(f"邮箱不匹配或存在多个别名：{emails}；要求 {policy['mailbox']}")


def load_policy(path):
    path = Path(path).resolve()
    p = read_json(path)
    if not p.get("allowed_senders") or not all(
        isinstance(x, str) and "@" in x and "*" not in x for x in p["allowed_senders"]
    ):
        raise GuardError("先填写明确的 allowed_senders 邮箱列表（不支持通配符）")
    if not p.get("mailbox") or not p.get("subject_prefix", "").strip():
        raise GuardError("需要 mailbox 和非空 subject_prefix")
    if not 1 <= p["duration_minutes"] <= 60 or not 1 <= p["max_replies"] <= 20:
        raise GuardError("单次时长需为 1—60 分钟，回复上限需为 1—20")
    if not isinstance(p.get("templates"), dict) or any(
        not isinstance(k, str) or not isinstance(v, str) or not v.strip()
        for k, v in p["templates"].items()
    ):
        raise GuardError("templates 需要非空正文的键值映射，可为空对象")
    knowledge = path.parent / p["knowledge_file"]
    material = path.read_bytes() + b"\0" + knowledge.read_bytes()
    return p, hashlib.sha256(material).hexdigest()


def active(state, require_time=True):
    if not state or state.get("closed"):
        raise GuardError("没有活动会话，请先 start")
    if require_time and now() >= stamp(state["expires_at"]):
        raise GuardError("会话已到期，不再收件或发送；可 status / finish")
    p, digest = load_policy(state["policy_path"])
    if digest != state["policy_digest"]:
        raise GuardError("运行期间规则或知识库发生变化，请结束本次会话并重新确认范围")
    return p


def key(policy, message_id):
    return policy["mailbox"].lower() + ":" + message_id


def in_scope(p, state, message):
    sender = message.get("from", {}).get("email", "").lower()
    if sender == p["mailbox"].lower() or sender not in [s.lower() for s in p["allowed_senders"]]:
        raise GuardError("发件人不在允许范围或为自己")
    if not message.get("subject", "").startswith(p["subject_prefix"]):
        raise GuardError("主题不符合前缀")
    try:
        created = stamp(message["created_at"])
    except (KeyError, ValueError, TypeError) as error:
        raise GuardError("无法验证邮件接收时间") from error
    if not stamp(state.get("receive_after", state["started_at"])) <= created <= now():
        raise GuardError("邮件不属于本次运行时间范围")


def load_message(p, state, message_id):
    account(p)
    message = cli("message", "+read", "--id", message_id)["data"]
    directory = message.get("dir", "")
    if isinstance(directory, dict):
        directory = directory.get("dir_name", "")
    if message.get("message_id") != message_id or not isinstance(directory, str) or directory.lower() != "inbox":
        raise GuardError("邮件 ID 或收件箱方向无法验证")
    in_scope(p, state, message)
    targets = [x.get("email", "").lower() for x in message.get("to", [])]
    if p["mailbox"].lower() not in targets:
        raise GuardError("当前邮箱不在原邮件直接收件人中")
    return message


def execute(a, root):
    session_path = root / "session.json"
    ledger_path = root / "ledger.json"
    state = read_json(session_path) if session_path.exists() else {}
    ledger = read_json(ledger_path) if ledger_path.exists() else {}
    if a.command == "start":
        if state and not state.get("closed") and now() < stamp(state["expires_at"]):
            raise GuardError("已有活动会话，请先 finish")
        p, digest = load_policy(a.policy)
        if a.mode == "auto" and (not a.authorized or not p.get("auto_enabled")):
            raise GuardError("自动模式需要已启用的规则和用户明确授权（--authorized 仅记录该事实）")
        account(p)
        started = now()
        receive_after = stamp(a.since) if a.since else started
        if receive_after > started:
            raise GuardError("--since 不能晚于当前时间")
        if state:
            history = root / "history"
            history.mkdir(exist_ok=True, mode=0o700)
            save(history / (state["run_id"] + ".json"), state)
        state = dict(run_id=uuid.uuid4().hex[:12], policy_path=str(Path(a.policy).resolve()),
                     policy_digest=digest, started_at=started.isoformat(),
                     receive_after=receive_after.isoformat(),
                     expires_at=(started + timedelta(minutes=p["duration_minutes"])).isoformat(),
                     mode=a.mode, closed=False, attempts=0, drafts={})
        save(session_path, state)
        return state
    if a.command == "status":
        # Includes previous uncertain attempts, so operators can reconcile across restarts.
        return {"session": state, "ledger": ledger}
    if a.command == "finish":
        if state:
            state["closed"] = True
            save(session_path, state)
        return {"closed": True, "attempts": state.get("attempts", 0)}
    p = active(state)
    if a.command == "scan":
        account(p)
        items, cursor, seen_cursors = [], None, set()
        for _ in range(20):
            active(state)
            args = ["message", "+list", "--dir", "inbox", "--after", state.get("receive_after", state["started_at"]), "--limit", "50"]
            if cursor:
                args += ["--cursor", cursor]
            data = cli(*args)["data"]
            pending = {d["message_id"] for d in state["drafts"].values()}
            for m in data["data"]:
                try:
                    in_scope(p, state, m)
                except GuardError:
                    continue
                if key(p, m["message_id"]) not in ledger and m["message_id"] not in pending:
                    items.append(m)
            page = data.get("pagination", {})
            if not page.get("has_more"):
                return {"messages": items}
            cursor = page.get("next_cursor")
            if not cursor or cursor in seen_cursors:
                raise GuardError("分页游标异常，请人工检查；未发送任何邮件")
            seen_cursors.add(cursor)
        raise GuardError("扫描超过 1000 封，请缩小范围后重启")
    message = load_message(p, state, a.id)
    message_key = key(p, a.id)
    if message_key in ledger:
        raise GuardError(f"邮件已有处理记录，禁止重复操作：{ledger[message_key]['status']}")
    if a.command == "skip":
        ledger[message_key] = dict(status="skipped", reason=a.reason, run_id=state["run_id"])
        save(ledger_path, ledger)
        return ledger[message_key]
    if a.command == "draft":
        body = Path(a.body_file).read_text().strip()
        if not body or len(body.encode()) > 100000:
            raise GuardError("草稿为空或过长")
        draft_id = uuid.uuid4().hex[:12]
        draft = dict(message_id=a.id, to=message["from"]["email"], subject=message["subject"],
                     body=body, reason=a.reason, created_at=now().isoformat())
        state["drafts"][draft_id] = draft
        save(session_path, state)
        return {"draft_id": draft_id, **draft, "sent": False}
    if a.command != "reply":
        raise GuardError("未知操作")
    if state["attempts"] >= p["max_replies"]:
        raise GuardError("本次发送尝试已达到上限（结果不明确的尝试也计数）")
    if a.template:
        if state["mode"] != "auto":
            raise GuardError("preview 模式不能自动发信；请先生成草稿")
        if any(d["message_id"] == a.id for d in state["drafts"].values()):
            raise GuardError("该邮件已进入待确认，不能改走自动模板")
        body = p["templates"].get(a.template)
        if not body:
            raise GuardError("模板不在本次授权规则内")
        basis = "template:" + a.template
    else:
        draft = state["drafts"].get(a.draft)
        if not a.approved or not draft or draft["message_id"] != a.id:
            raise GuardError("需要匹配草稿和用户对该完整草稿的明确批准")
        if draft["to"].lower() != message["from"]["email"].lower():
            raise GuardError("草稿收件人与原发件人不一致")
        body, basis = draft["body"], "approved-draft:" + a.draft
    active(state)  # Reads may take time; check deadline again just before reservation.
    state["attempts"] += 1
    entry = dict(status="uncertain", run_id=state["run_id"], time=now().isoformat(),
                 to=message["from"]["email"], basis=basis,
                 body_sha256=hashlib.sha256(body.encode()).hexdigest())
    # Reserve durably BEFORE invoking any external write. A crash must never cause automatic resend.
    ledger[message_key] = entry
    save(ledger_path, ledger)
    save(session_path, state)
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", dir=root,
                                         encoding="utf-8") as draft_file:
            draft_file.write(body)
            draft_file.flush()
            relative = os.path.relpath(draft_file.name, Path.cwd())
            result = cli("message", "+reply", "--id", a.id, "--body-file", relative,
                         "--body-format", "plain", "--confirmed")
        payload = result.get("data", {})
        if payload.get("confirmation_required") or not (
            result.get("queued") is True or payload.get("queued") is True
        ):
            raise GuardError("未获得 queued=true；发送状态待核查，禁止自动重发")
        entry["status"] = "submitted"
    except GuardError as error:
        entry["error"] = str(error)
        save(ledger_path, ledger)
        raise
    save(ledger_path, ledger)
    return entry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", default="state")
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start")
    start.add_argument("--policy", required=True)
    start.add_argument("--mode", choices=["preview", "auto"], default="preview")
    start.add_argument("--authorized", action="store_true")
    start.add_argument("--since", help="仅当用户明确指定处理历史邮件时使用，含时区 ISO 时间")
    for name in ("status", "finish", "scan"):
        sub.add_parser(name)
    draft = sub.add_parser("draft")
    draft.add_argument("--id", required=True)
    draft.add_argument("--body-file", required=True)
    draft.add_argument("--reason", required=True)
    skip = sub.add_parser("skip")
    skip.add_argument("--id", required=True)
    skip.add_argument("--reason", required=True)
    reply = sub.add_parser("reply")
    reply.add_argument("--id", required=True)
    group = reply.add_mutually_exclusive_group(required=True)
    group.add_argument("--template")
    group.add_argument("--draft")
    reply.add_argument("--approved", action="store_true")
    a = parser.parse_args()
    root = Path(a.state_dir).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"ok": False, "error": "另一个处理命令正在运行"}, ensure_ascii=False))
            return 1
        try:
            result = execute(a, root)
            print(json.dumps({"ok": True, "data": result}, ensure_ascii=False, indent=2))
            return 0
        except (GuardError, ValueError, KeyError, TypeError, OSError) as error:
            print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
            return 1


if __name__ == "__main__":
    sys.exit(main())
