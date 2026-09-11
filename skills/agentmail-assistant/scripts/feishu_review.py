#!/usr/bin/env python3
"""One bounded Feishu review ticket. No inbox polling or unapproved mail sends."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import threading
import time
from types import SimpleNamespace
import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation
import math

import mail_guard as guard

ROOT = Path(__file__).resolve().parents[3]
STATE = ROOT / "state"
REVIEW = STATE / "feishu"
ACTIVE = {"pending", "editing"}


def digest(body):
    return hashlib.sha256(body.encode()).hexdigest()


def event_seconds(value):
    """Normalize epoch precision, then let validate enforce the actual ticket window.

    CLI schema declares milliseconds, but callback producers can preserve a
    different epoch precision. Never remove the freshness check or accept NaN.
    """
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise guard.GuardError("回调时间戳格式无效") from exc
    if not number.is_finite() or number <= 0 or number >= Decimal("1e20"):
        raise guard.GuardError("回调时间戳无效")
    scale = next(s for upper, s in [("1e11", 1), ("1e14", 1000),
                                    ("1e17", 1000000), ("1e20", 1000000000)]
                 if number < Decimal(upper))
    return float(number / scale)


def emit(**data):
    print(json.dumps(data, ensure_ascii=False), flush=True)


def persist(ticket):
    guard.save(REVIEW / (ticket["ticket_id"] + ".json"), ticket)


def lark(*args):
    result = subprocess.run(["rtk", "proxy", "lark-cli", *args],
                            capture_output=True, text=True, timeout=45, cwd=ROOT)
    try:
        data = json.loads(result.stdout or result.stderr)
    except ValueError as exc:
        raise guard.GuardError("飞书未返回明确 JSON 结果；请核查，勿重复发送") from exc
    if result.returncode or data.get("ok") is not True:
        error = data.get("error", {})
        raise guard.GuardError("飞书调用失败：" + str(error.get("message", "未知结果")))
    return data.get("data", {})


def prepare(source, draft_id, reviewer, minutes):
    source = Path(source).resolve()
    if not source.is_relative_to(STATE):
        raise guard.GuardError("草稿必须来自本项目 state")
    history = guard.read_json(source)
    draft = history["drafts"][draft_id]
    if not draft["reason"].startswith("003："):
        raise guard.GuardError("此入口只接受 003 审稿；004 先在控制会话记录业务决定，005 禁止普通批准")
    policy, policy_digest = guard.load_policy(history["policy_path"])
    if policy_digest != history["policy_digest"]:
        raise guard.GuardError("规则已变化，先重新核对草稿")
    if not reviewer.startswith("ou_") or not 1 <= minutes <= 20:
        raise guard.GuardError("需要审核人 open_id，时长为 1—20 分钟")
    if not 0 < len(draft["body"]) <= 1000:
        raise guard.GuardError("此卡片支持 1—1000 字正文")
    ledger = guard.read_json(STATE / "ledger.json")
    if guard.key(policy, draft["message_id"]) in ledger:
        raise guard.GuardError("邮件已处理，不再发审批卡")
    now = guard.now()
    return dict(ticket_id=uuid.uuid4().hex[:12], source=str(source), source_draft=draft_id,
                email_id=draft["message_id"], to=draft["to"], subject=draft["subject"],
                body=draft["body"], body_sha256=digest(draft["body"]), reason=draft["reason"],
                mailbox=policy["mailbox"], policy_path=history["policy_path"],
                policy_digest=policy_digest, receive_after=history["receive_after"],
                reviewer=reviewer, version=1, nonce=uuid.uuid4().hex,
                created_at=now.isoformat(), expires_at=(now + timedelta(minutes=minutes)).isoformat(),
                status="prepared", events=[], revisions=[], message_id=None, chat_id=None)


def plain(text):
    return {"tag": "plain_text", "content": text}


def prepare_interaction_test(reviewer, minutes):
    if not reviewer or not reviewer.startswith("ou_") or not 1 <= minutes <= 20:
        raise guard.GuardError("需要已核验审核人 open_id，测试时长为 1—20 分钟")
    now = guard.now()
    ticket_id = uuid.uuid4().hex[:12]
    body = "您好，培训安排为 2026 年 9 月 18 日下午。请携带笔记本电脑。\n\n以上仅用于卡片交互测试，不会发送邮件。"
    return dict(ticket_id=ticket_id, interaction_test=True, email_id="ui-test:" + ticket_id,
                to="无实际收件人", mailbox="无实际发信邮箱", subject="修改正文交互测试（不发邮件）",
                body=body, body_sha256=digest(body), reason="003：仅验证卡片编辑、保存和重新审核",
                reviewer=reviewer, version=1, nonce=uuid.uuid4().hex,
                created_at=now.isoformat(), expires_at=(now + timedelta(minutes=minutes)).isoformat(),
                status="prepared", events=[], revisions=[], message_id=None, chat_id=None)


def div(text, size="normal", color=None):
    node = {"tag": "div", "text": {**plain(text), "text_size": size}}
    if color:
        node["text"]["text_color"] = color
    return node


def binding(t, action):
    return dict(ticket_id=t["ticket_id"], version=t["version"], nonce=t["nonce"],
                body_sha256=t["body_sha256"], action=action)


def edit_name(t):
    return "save_" + t["ticket_id"] + "_" + str(t["version"]) + "_" + t["nonce"]


def card(t):
    statuses = {"prepared": "待审核", "pending": "待审核", "editing": "修改正文",
                "sending": "已批准，正在提交", "submitted": "已提交邮件服务",
                "rejected": "已拒绝，未发邮件", "expired": "审批已结束，未发邮件",
                "paused": "审批监听已暂停", "test_approved": "交互测试完成，未发邮件",
                "uncertain": "执行结果需人工核查", "publish_uncertain": "卡片发送结果待核查"}
    label = statuses[t["status"]]
    deadline = guard.stamp(t["expires_at"]).astimezone().strftime("%m-%d %H:%M")
    metadata = div(f"发件邮箱：{t['mailbox']}\n回复给：{t['to']}\n主题：{t['subject']}\n"
                   f"审核等级：003 · 版本 {t['version']} · 截止 {deadline}\n无抄送、无附件", "notation", "grey")
    body = {"tag": "column_set", "flex_mode": "none", "columns": [
        {"tag": "column", "width": "weighted", "weight": 1, "padding": "12px",
         "background_style": "grey-50", "vertical_spacing": "8px",
         "elements": [div("待发送的完整回复", "heading-3"), div(t["body"])]}]}
    elements = [metadata, body]
    if t["status"] in {"pending", "prepared"}:
        buttons = []
        approve_label = "完成测试（不发邮件）" if t.get("interaction_test") else "批准发送"
        for action, text, kind in [("approve", approve_label, "primary_filled"),
                                   ("edit", "修改正文", "default"), ("reject", "拒绝", "danger")]:
            buttons.append({"tag": "column", "width": "weighted", "weight": 1, "elements": [
                {"tag": "button", "text": plain(text), "type": kind, "width": "fill",
                 "behaviors": [{"type": "callback", "value": binding(t, action)}]}]})
        elements.extend([{"tag": "column_set", "flex_mode": "none", "horizontal_spacing": "8px", "columns": buttons},
                         div("请先修改正文并保存，再完成测试。所有操作均不发送邮件。" if t.get("interaction_test") else
                             "批准后仅发送上方正文一次。修改后需重新批准；到期未确认不发送。", "notation", "grey")])
    elif t["status"] == "editing":
        elements.append({"tag": "form", "name": "edit_form", "elements": [
            {"tag": "input", "name": "revised_body", "label": plain("修改完整回复正文"),
             "default_value": t["body"], "input_type": "multiline_text", "rows": 5,
             "max_length": 1000, "required": True, "width": "fill"},
            {"tag": "button", "name": edit_name(t), "form_action_type": "submit",
             "text": plain("保存并重新审核"), "type": "primary_filled", "width": "fill"}]})
    else:
        elements.append(div(label + ("；服务接受任务不等于送达。" if t["status"] == "submitted" else "。")))
        if t["status"] in {"expired", "paused"}:
            elements.append(div("本轮监听已结束。请回到 Codex 重新启动联调，使用新一轮有效卡片。", "notation", "grey"))
    return {"schema": "2.0", "config": {"update_multi": True, "width_mode": "default",
            "enable_forward": False, "summary": {"content": "邮件审批 · " + label}},
            "header": {"title": plain(("交互测试（不发邮件） · " if t.get("interaction_test") else "邮件回复审批 · ") + label), "subtitle": plain("003 · 请核对完整正文"),
                       "template": "blue", "icon": {"tag": "standard_icon", "token": "approval_colorful"}},
            "body": {"direction": "vertical", "padding": "12px", "vertical_spacing": "12px", "elements": elements}}


def validate(t, event):
    now = guard.now()
    if t["status"] not in ACTIVE or now >= guard.stamp(t["expires_at"]):
        raise guard.GuardError("审批已结束或过期")
    for field, expected in [("type", "card.action.trigger"), ("operator_id", t["reviewer"]),
                            ("message_id", t["message_id"]), ("chat_id", t["chat_id"]),
                            ("action_tag", "button"), ("host", "im_message")]:
        if not expected or event.get(field) != expected:
            raise guard.GuardError("回调身份、卡片或操作不匹配")
    if not event.get("card_content") or not event.get("token"):
        raise guard.GuardError("缺少原卡片或回调令牌")
    if not event.get("event_id") or event["event_id"] in t["events"]:
        raise guard.GuardError("缺少事件 ID 或重复事件")
    event_time = event_seconds(event["timestamp"])
    earliest = guard.stamp(t.get("accept_after", t["created_at"])).timestamp()
    if event_time < earliest - (0 if t.get("accept_after") else 5) or event_time > now.timestamp() + 60:
        raise guard.GuardError("回调时间不在本次审批范围")
    if digest(t["body"]) != t["body_sha256"]:
        raise guard.GuardError("正文校验失败")
    if t["status"] == "editing":
        if event.get("action_name") != edit_name(t):
            raise guard.GuardError("修改表单版本不匹配")
        form = json.loads(event.get("form_value", "{}"))
        body = form.get("revised_body")
        if not isinstance(body, str) or not 0 < len(body.strip()) <= 1000:
            raise guard.GuardError("新正文为空或超过 1000 字")
        return "save", body.strip()
    value = json.loads(event.get("action_value", "{}"))
    action = value.get("action")
    if action not in {"approve", "edit", "reject"} or value != binding(t, action):
        raise guard.GuardError("动作或草稿版本不匹配")
    return action, None


def send_approved(t):
    if t.get("interaction_test") or t.get("email_id", "").startswith("ui-test:"):
        raise guard.GuardError("交互测试禁止调用邮件发送流程")
    if t["status"] != "sending" or not t.get("approval"):
        raise guard.GuardError("没有有效审批，禁止发送")
    p, policy_digest = guard.load_policy(t["policy_path"])
    if policy_digest != t["policy_digest"] or digest(t["body"]) != t["approval"]["body_sha256"]:
        raise guard.GuardError("规则或已批准正文发生变化")
    with (STATE / ".lock").open("a") as lock:
        guard.fcntl.flock(lock, guard.fcntl.LOCK_EX)
        def run(command, **kw):
            return guard.execute(SimpleNamespace(command=command, **kw), STATE)
        run("start", policy=t["policy_path"], mode="preview", authorized=False, since=t["receive_after"])
        try:
            body_file = REVIEW / (t["ticket_id"] + ".txt")
            body_file.write_text(t["body"])
            body_file.chmod(0o600)
            draft = run("draft", id=t["email_id"], body_file=str(body_file),
                        reason="003：飞书本人批准；审批单 " + t["ticket_id"])
            if (draft["to"].lower() != t["to"].lower() or draft["subject"] != t["subject"]
                    or draft["body"] != t["body"] or p["mailbox"] != t["mailbox"]):
                raise guard.GuardError("原邮件或执行草稿与批准内容不一致")
            if guard.now() >= guard.stamp(t["expires_at"]):
                raise guard.GuardError("审批已到期，未发送")
            t["execution_draft"] = draft["draft_id"]
            persist(t)
            return run("reply", id=t["email_id"], draft=draft["draft_id"], approved=True, template=None)
        finally:
            run("finish")


def update(t, event):
    # Original card is fetched by the authenticated event consumer. The same renderer
    # preserves its structure, replacing only our own ticket's state and controls.
    if not event.get("card_content"):
        raise guard.GuardError("无法读取原卡片")
    emit(ticket_id=t["ticket_id"], card_update=t["status"])
    lark("api", "POST", "/open-apis/interactive/v1/card/update", "--as", "bot", "--data",
         json.dumps({"token": event["token"], "card": card(t)}, ensure_ascii=False))


def sync_card_state(t):
    """Update our shared card without needing a user callback token.

    Official contract: /document/server-docs/im-v1/message-card/patch.md.
    This changes only the card display; never grants approval or sends email.
    """
    if not t.get("message_id"):
        return
    result = lark("api", "PATCH", "/open-apis/im/v1/messages/" + t["message_id"], "--as", "bot",
                  "--data", json.dumps({"content": json.dumps(card(t), ensure_ascii=False)}, ensure_ascii=False))
    if isinstance(result, dict) and result.get("code", 0) != 0:
        raise guard.GuardError("卡片状态更新失败：" + str(result.get("msg", "未知结果")))
    t["display_status"] = t["status"]
    t.pop("card_update_error", None)
    persist(t)
    emit(ticket_id=t["ticket_id"], card_display=t["status"])


def handle(t, event):
    action, body = validate(t, event)
    t["events"].append(event["event_id"])
    t["last_callback"] = dict(timestamp=event["timestamp"], normalized_epoch=event_seconds(event["timestamp"]),
                              received_at=guard.now().isoformat(), action=action)
    if action == "save":
        t["revisions"].append(dict(version=t["version"], body=t["body"], body_sha256=t["body_sha256"]))
        t.update(body=body, body_sha256=digest(body), version=t["version"] + 1,
                 nonce=uuid.uuid4().hex, status="pending")
    elif action == "edit":
        t["status"] = "editing"
    elif action == "reject":
        t["status"] = "rejected"
    else:
        t["status"] = "sending"
        t["approval"] = dict(operator_id=event["operator_id"], event_id=event["event_id"],
                             approved_at=guard.now().isoformat(), version=t["version"],
                             body_sha256=t["body_sha256"])
    persist(t)  # Durable reservation before ANY mail write; never auto-retry a crash.
    if action == "approve":
        try:
            t["execution"] = ({"status": "test_approved", "email_sent": False,
                               "body_sha256": t["body_sha256"], "version": t["version"]}
                              if t.get("interaction_test") else send_approved(t))
            t["status"] = t["execution"]["status"]
        except Exception as exc:
            t["status"] = "uncertain"
            t["error"] = str(exc)
        persist(t)
    try:
        update(t, event)
        t.pop("card_update_error", None)
    except Exception as exc:
        t["card_update_error"] = str(exc)
    persist(t)
    emit(ticket_id=t["ticket_id"], status=t["status"], version=t["version"],
         card_updated="card_update_error" not in t)


def run(t, minutes, resume=False):
    events = queue.Queue()
    proc = subprocess.Popen(["rtk", "proxy", "lark-cli", "event", "consume", "card.action.trigger",
                             "--as", "bot", "--timeout", str(minutes) + "m"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=ROOT,
                            start_new_session=True)
    diagnostics = []
    def reader(pipe, channel):
        for line in pipe:
            events.put((channel, line))
    for pipe, channel in [(proc.stdout, "event"), (proc.stderr, "diagnostic")]:
        threading.Thread(target=reader, args=(pipe, channel), daemon=True).start()
    try:
        ready_by = time.monotonic() + 45
        while time.monotonic() < ready_by:
            if proc.poll() is not None:
                # Startup errors can be on either pipe and can span multiple lines.
                while not events.empty():
                    diagnostics.append(events.get_nowait()[1])
                try:
                    failure = json.loads("".join(diagnostics)).get("error", {})
                except ValueError:
                    failure = {}
                message = failure.get("message", "飞书监听提前退出，请检查 event status / 认证")
                if failure.get("hint"):
                    message += "；" + failure["hint"]
                t["startup_error"] = message
                persist(t)
                raise guard.GuardError(message)
            try:
                channel, line = events.get(timeout=1)
            except queue.Empty:
                continue
            if channel == "diagnostic" and "[event] ready event_key=card.action.trigger" in line:
                break
            diagnostics.append(line)
        else:
            raise guard.GuardError("未取得监听 ready 信号，未发送审批卡")
        emit(listener="ready", ticket_id=t["ticket_id"])
        if resume:
            sync_card_state(t)
            emit(card_resumed=True, ticket_id=t["ticket_id"], expires_at=t["expires_at"], message_id=t["message_id"])
        else:
            t["status"] = "publish_uncertain"
            persist(t)
            preview = copy.deepcopy(t)
            preview["status"] = "pending"
            result = lark("im", "+messages-send", "--user-id", t["reviewer"], "--as", "bot",
                          "--msg-type", "interactive", "--content", json.dumps(card(preview), ensure_ascii=False),
                          "--idempotency-key", "mail-review-" + t["ticket_id"])
            if not result.get("message_id") or not result.get("chat_id"):
                raise guard.GuardError("卡片回执缺少 message_id/chat_id，请核查，勿重发")
            t.update(status="pending", message_id=result["message_id"], chat_id=result["chat_id"])
            persist(t)
            emit(card_sent=True, ticket_id=t["ticket_id"], expires_at=t["expires_at"], message_id=t["message_id"])
        while proc.poll() is None and guard.now() < guard.stamp(t["expires_at"]):
            try:
                channel, line = events.get(timeout=1)
            except queue.Empty:
                continue
            if channel != "event":
                continue
            event = None
            try:
                event = json.loads(line)
                handle(t, event)
            except (ValueError, KeyError, TypeError, guard.GuardError) as exc:
                if isinstance(event, dict) and event.get("message_id") == t["message_id"]:
                    diagnostic = dict(reason=str(exc), received_at=guard.now().isoformat())
                    # Store only timing metadata, never the callback token or raw payload.
                    raw = str(event.get("timestamp", ""))
                    if raw.isdigit() and len(raw) <= 20:
                        diagnostic["timestamp"] = raw
                    t.setdefault("ignored_callbacks", []).append(diagnostic)
                    t["ignored_callbacks"] = t["ignored_callbacks"][-20:]
                    persist(t)
                emit(callback="ignored", reason=str(exc))
            if t["status"] not in ACTIVE:
                break
    finally:
        if t["status"] in ACTIVE:
            t["paused_status"] = t["status"]
            t["status"] = "expired" if guard.now() >= guard.stamp(t["expires_at"]) else "paused"
            persist(t)
            try:
                sync_card_state(t)
            except Exception as exc:
                t["card_update_error"] = str(exc)
                persist(t)
                emit(ticket_id=t["ticket_id"], card_update_failed=True, reason=str(exc))
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                emit(listener="termination_pending")
        emit(listener="stopped", ticket_id=t["ticket_id"], status=t["status"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["preview", "run", "resume"])
    parser.add_argument("--source")
    parser.add_argument("--draft")
    parser.add_argument("--reviewer")
    parser.add_argument("--ticket")
    parser.add_argument("--interaction-test", action="store_true", help="真实飞书交互，禁止调用邮件发送")
    parser.add_argument("--minutes", type=int, default=20)
    a = parser.parse_args()
    REVIEW.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (REVIEW / ".lock").open("a") as lock:
        try:
            guard.fcntl.flock(lock, guard.fcntl.LOCK_EX | guard.fcntl.LOCK_NB)
        except BlockingIOError:
            raise guard.GuardError("已有卡片审核进程，请先处理现有审批")
        signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        if a.command == "resume":
            if not a.ticket or len(a.ticket) != 12 or any(c not in "0123456789abcdef" for c in a.ticket):
                raise guard.GuardError("需要有效审批单 ID")
            t = guard.read_json(REVIEW / (a.ticket + ".json"))
            if t["status"] not in ACTIVE | {"paused", "expired"} or t.get("approval"):
                raise guard.GuardError("仅能恢复尚未批准的审批，禁止重试发送")
            if guard.now() >= guard.stamp(t["expires_at"]) or not t.get("message_id") or not t.get("chat_id"):
                raise guard.GuardError("审批已到期或未发卡，不能恢复")
            p, policy_digest = guard.load_policy(t["policy_path"])
            if policy_digest != t["policy_digest"] or digest(t["body"]) != t["body_sha256"]:
                raise guard.GuardError("规则或正文已变化，不能恢复")
            if guard.key(p, t["email_id"]) in guard.read_json(STATE / "ledger.json"):
                raise guard.GuardError("邮件已处理，不能恢复")
            t["status"] = t.get("paused_status", "pending") if t["status"] not in ACTIVE else t["status"]
            t["accept_after"] = guard.now().isoformat()
            persist(t)
            remaining = math.ceil((guard.stamp(t["expires_at"]) - guard.now()).total_seconds() / 60)
            run(t, remaining, resume=True)
            return
        if not a.interaction_test and not all([a.source, a.draft, a.reviewer]):
            raise guard.GuardError("新建审批需要 --source、--draft 和 --reviewer")
        t = (prepare_interaction_test(a.reviewer, a.minutes) if a.interaction_test else
             prepare(a.source, a.draft, a.reviewer, a.minutes))
        if a.command == "preview":
            guard.save(REVIEW / "preview-card.json", card(t))
            emit(preview=str(REVIEW / "preview-card.json"), to=t["to"], body=t["body"], sent=False)
            return
        for file in REVIEW.glob("*.json"):
            old = guard.read_json(file)
            if old.get("email_id") == t["email_id"] and old.get("status") in (ACTIVE | {"paused", "sending", "uncertain", "publish_uncertain"}):
                raise guard.GuardError("该邮件已有待处理或结果不明的审批，请先核查旧审批单")
            if (old.get("email_id") == t["email_id"] and old.get("status") == "expired"
                    and old.get("message_id") and old.get("display_status") != "expired"):
                sync_card_state(old)
        persist(t)
        signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        run(t, a.minutes)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        emit(stopped=True)
    except Exception as error:
        emit(ok=False, error=str(error))
        raise SystemExit(1)
