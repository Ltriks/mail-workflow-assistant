#!/usr/bin/env python3
"""Persistent Feishu callback service and local approval queue. No inbox polling."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import threading
import time

import feishu_review as review

guard = review.guard
CONFIG = review.ROOT / "service-config.json"
HEALTH = review.STATE / "service-health.json"
STOP = threading.Event()


def config():
    return guard.read_json(CONFIG)


def health(status, **fields):
    guard.save(HEALTH, dict(status=status, updated_at=guard.now().isoformat(), pid=os.getpid(), **fields))


def tickets():
    for path in review.REVIEW.glob("*.json"):
        ticket = guard.read_json(path)
        if ticket.get("ticket_id"):
            yield ticket


def enqueue(source=None, draft=None, minutes=20, interaction_test=False):
    cfg = config()
    if interaction_test:
        t = review.prepare_interaction_test(cfg["reviewer"], minutes)
    else:
        if not cfg.get("allow_real_mail"):
            raise guard.GuardError("本机尚未启用真实邮件执行；先完成 Agent Mail 授权及核验")
        if not source or not draft:
            raise guard.GuardError("真实审批需要原会话文件和草稿 ID")
        t = review.prepare(source, draft, cfg["reviewer"], minutes)
        # prepare uses the policy's original bound; explicit queue TTL may shorten it.
    with (review.REVIEW / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if any(old.get("email_id") == t["email_id"] and old.get("status") in
               {"queued", "pending", "editing", "sending", "uncertain", "publish_uncertain", "paused"}
               for old in tickets()):
            raise guard.GuardError("该邮件已有待处理或结果未知的审批，禁止重复排队")
        t["status"] = "queued"
        review.persist(t)
    return {"ticket_id": t["ticket_id"], "status": "queued", "expires_at": t["expires_at"],
            "interaction_test": interaction_test}


def verify_app():
    proc = subprocess.run(["rtk", "proxy", "lark-cli", "config", "show"],
                          capture_output=True, text=True, timeout=30, cwd=review.ROOT)
    try:
        data, _ = json.JSONDecoder().raw_decode(proc.stdout.lstrip())
    except ValueError as exc:
        raise guard.GuardError("飞书尚未完成本机配置") from exc
    if proc.returncode or data.get("appId") != config()["app_id"]:
        raise guard.GuardError("飞书应用未配置或与指定审批应用不一致")


def expire(t):
    t["status"] = "expired"
    review.persist(t)
    if t.get("message_id"):
        try:
            review.sync_card_state(t)
        except Exception as exc:
            t["card_update_error"] = str(exc)
            review.persist(t)


def tick(connected):
    cfg = config()
    for t in list(tickets()):
        if t["status"] not in {"queued", "pending", "editing"}:
            continue
        if guard.now() >= guard.stamp(t["expires_at"]):
            expire(t)
            continue
        if not connected or t["status"] != "queued":
            continue
        if t.get("reviewer") != cfg["reviewer"]:
            t.update(status="blocked", error="审核人与服务配置不一致")
            review.persist(t)
            continue
        t["status"] = "publish_uncertain"
        review.persist(t)
        rendered = dict(t, status="pending")
        try:
            result = review.lark("im", "+messages-send", "--user-id", t["reviewer"], "--as", "bot",
                                 "--msg-type", "interactive", "--content", json.dumps(review.card(rendered), ensure_ascii=False),
                                 "--idempotency-key", "mail-review-" + t["ticket_id"])
            if not result.get("message_id") or not result.get("chat_id"):
                raise guard.GuardError("发卡结果缺少标识，需人工核查")
            t.update(status="pending", message_id=result["message_id"], chat_id=result["chat_id"])
            review.persist(t)
            review.emit(card_sent=True, ticket_id=t["ticket_id"])
        except Exception as exc:
            t["error"] = str(exc)
            review.persist(t)  # No automatic resend of ambiguous writes.


def process(event):
    if not isinstance(event, dict):
        return
    matches = [t for t in tickets() if t.get("message_id") and t["message_id"] == event.get("message_id")]
    if len(matches) != 1:
        return
    t = matches[0]
    cfg = config()
    if t.get("reviewer") != cfg["reviewer"]:
        raise guard.GuardError("审批单审核人与服务配置不匹配")
    if not t.get("interaction_test") and not cfg.get("allow_real_mail"):
        raise guard.GuardError("本机尚未启用真实邮件执行")
    review.handle(t, event)


def recover():
    for t in tickets():
        if t["status"] == "sending":
            t.update(status="uncertain", error="进程在执行期间中断，禁止自动重发，请核查邮件台账")
            review.persist(t)


def consume():
    """Renew a bounded transport session; ticket deadlines remain independent."""
    verify_app()
    messages = queue.Queue()
    proc = subprocess.Popen(["rtk", "proxy", "lark-cli", "event", "consume", "card.action.trigger",
                             "--as", "bot", "--timeout", "1h"], cwd=review.ROOT,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    def read(pipe, channel):
        for line in pipe:
            messages.put((channel, line))
    for pipe, channel in [(proc.stdout, "event"), (proc.stderr, "diagnostic")]:
        threading.Thread(target=read, args=(pipe, channel), daemon=True).start()
    connected = False
    next_health = 0
    ready_deadline = time.monotonic() + 45
    try:
        while not STOP.is_set() and proc.poll() is None:
            try:
                channel, line = messages.get(timeout=1)
            except queue.Empty:
                channel, line = None, ""
            if channel == "diagnostic" and "[event] ready event_key=card.action.trigger" in line:
                connected = True
                review.emit(service="ready")
            elif channel == "event":
                try:
                    process(json.loads(line))
                except (ValueError, KeyError, TypeError, guard.GuardError) as exc:
                    review.emit(callback="ignored", reason=str(exc))
            if not connected and time.monotonic() > ready_deadline:
                raise guard.GuardError("监听未能在45秒内就绪，请检查配置、权限及其他占用连接")
            with (review.REVIEW / ".lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                tick(connected)
            if time.monotonic() >= next_health:
                health("ready" if connected else "connecting", allow_real_mail=config().get("allow_real_mail", False))
                next_health = time.monotonic() + 10
        if not STOP.is_set() and proc.poll() not in (None, 0):
            raise guard.GuardError("飞书消费进程退出；检查飞书认证、回调订阅或连接占用")
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                raise guard.GuardError("旧监听仍未退出，交由进程托管恢复")


def serve():
    with (review.REVIEW / ".service.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        recover()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: STOP.set())
        while not STOP.is_set():
            try:
                health("connecting")
                consume()
            except Exception as exc:
                health("needs_attention", error=str(exc))
                review.emit(service="needs_attention", reason=str(exc))
            if not STOP.is_set():
                health_data = guard.read_json(HEALTH)
                if health_data["status"] != "needs_attention":
                    health("reconnecting")
                STOP.wait(30)
        health("stopped")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["serve", "enqueue", "status", "enable-mail"])
    parser.add_argument("--source")
    parser.add_argument("--draft")
    parser.add_argument("--interaction-test", action="store_true")
    parser.add_argument("--minutes", type=int, default=20)
    a = parser.parse_args()
    review.REVIEW.mkdir(parents=True, exist_ok=True, mode=0o700)
    if a.command == "serve":
        serve()
    elif a.command == "enqueue":
        review.emit(**enqueue(a.source, a.draft, a.minutes, a.interaction_test))
    elif a.command == "enable-mail":
        cfg = config()
        guard.account({"mailbox": cfg["mailbox"]})
        cfg["allow_real_mail"] = True
        guard.save(CONFIG, cfg)
        review.emit(allow_real_mail=True, mailbox=cfg["mailbox"])
    else:
        review.emit(health=guard.read_json(HEALTH) if HEALTH.exists() else {"status": "not_started"},
                    queue=[{k:t.get(k) for k in ("ticket_id", "status", "version", "expires_at", "interaction_test")}
                           for t in tickets()])


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        review.emit(ok=False, error=str(exc))
        raise SystemExit(1)
