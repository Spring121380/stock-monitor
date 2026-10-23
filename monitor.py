#!/usr/bin/env python3
"""
奢侈品补货监控（Python + Playwright）

判断逻辑（重点）：
  页面 HTML 里“有货按钮”和“没货按钮”常常同时存在，只是其中一个被 CSS 隐藏。
  所以本脚本不是搜文字，而是找出所有文字匹配的按钮，再逐个判断它“真的看得见”：
    - 自身或任一祖先 display:none
    - 自身 visibility:hidden
    - 自身或任一祖先 opacity:0
    - 宽或高为 0
    - 被移到页面可视范围之外（如 left:-9999px）
    - 被 overflow:hidden 的父容器裁掉（如折叠的容器）
  结果：
    有货按钮可见            -> 有货（即使没货按钮也可见，仍算有货）
    只有没货按钮可见        -> 没货
    两个都看不到            -> 保存截图，退出码 1

有货时同时做两件事（互不影响，一个失败另一个照常发）：
  1. Bark 推送 level=critical&call=1（静音/勿扰/睡眠专注模式下也会响，并重复响铃约 30 秒）
  2. Gmail 邮件（应用专用密码）
  每个渠道每次“补货”只通知一次，变成没货后会重置。

退出码：
  0  正常检测完成（有货或没货都是 0）
  1  两个按钮都不可见（已保存截图到 screenshots/）
  2  配置错误 / 网络错误 / 发邮件失败等其他异常

用法：
  python monitor.py --test-email      # 只发一封测试邮件，不访问商品页
  python monitor.py --test-bark       # 只发一条 Bark 紧急警报，不访问商品页
  python monitor.py                   # 检测一次
  python monitor.py --headed          # 本机调试：显示浏览器窗口
  python monitor.py --loop 300        # 本机常驻：每 300 秒检测一次

配置（环境变量，也可用命令行参数覆盖）：
  PRODUCT_URL         商品链接
  IN_STOCK_TEXT       有货按钮文字，如 ADD TO BAG（多个写法用 | 分隔）
  OUT_OF_STOCK_TEXT   没货按钮文字，如 ORDER BY PHONE
  GMAIL_ADDRESS       发件 Gmail 地址
  GMAIL_APP_PASSWORD  Gmail 应用专用密码（16 位）
  MAIL_TO             收件邮箱（不填则发给自己）
  BARK_KEY            Bark 的设备 key
  BARK_SERVER         可选，自建 Bark 服务器地址，默认 https://api.day.app
  LOCALE / TIMEZONE   可选，默认 en-US / Asia/Tokyo
"""

import argparse
import json
import os
import smtplib
import sys
import time
import urllib.request
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from playwright.sync_api import Error as PWError
from playwright.sync_api import sync_playwright

EXIT_OK, EXIT_NO_BUTTON, EXIT_ERROR = 0, 1, 2

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "state.json"
SHOT_DIR = BASE_DIR / "screenshots"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

# 在浏览器里执行：找出文字匹配的元素，并判断每个是否“真的可见”
JS_FIND_BUTTONS = r"""
(needles) => {
  const norm = s => (s || "").replace(/\s+/g, " ").trim().toUpperCase();
  const targets = needles.map(norm).filter(Boolean);
  const nodes = document.querySelectorAll(
    "button, a, input[type=button], input[type=submit], [role=button]"
  );
  const result = [];
  for (const el of nodes) {
    const raw = el.tagName === "INPUT" ? el.value : (el.innerText || el.textContent);
    const text = norm(raw);
    const label = norm(el.getAttribute("aria-label"));
    // 文字包含目标，且总长度不能太长（避免整张商品卡片被误匹配）
    const hit = targets.find(
      t => (text.includes(t) && text.length <= t.length + 40) || label.includes(t)
    );
    if (!hit) continue;

    let visible = true, reason = "";
    const rect = el.getBoundingClientRect();

    if (rect.width <= 0 || rect.height <= 0) {
      visible = false; reason = "宽或高为0";
    } else if (rect.right <= 0 || rect.bottom <= 0) {
      visible = false; reason = "在页面可视范围之外";
    }

    if (visible) {
      const own = getComputedStyle(el);
      if (own.visibility === "hidden" || own.visibility === "collapse") {
        visible = false; reason = "visibility:hidden";
      }
    }

    if (visible) {
      for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
        const cs = getComputedStyle(n);
        if (cs.display === "none") { visible = false; reason = "display:none"; break; }
        if (parseFloat(cs.opacity) === 0) { visible = false; reason = "opacity:0"; break; }
        if (n !== el && n !== document.body && n !== document.documentElement) {
          const clips = cs.overflowX !== "visible" || cs.overflowY !== "visible";
          if (clips) {
            const pr = n.getBoundingClientRect();
            if (pr.width <= 0 || pr.height <= 0 ||
                rect.right <= pr.left || rect.left >= pr.right ||
                rect.bottom <= pr.top || rect.top >= pr.bottom) {
              visible = false; reason = "被父容器裁剪"; break;
            }
          }
        }
      }
    }

    result.push({
      tag: el.tagName.toLowerCase(),
      text: (raw || "").replace(/\s+/g, " ").trim().slice(0, 80),
      visible: visible,
      reason: reason,
      disabled: !!el.disabled || el.getAttribute("aria-disabled") === "true",
    });
  }
  return result;
}
"""


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------- 邮件 ----
def send_mail(subject: str, body: str) -> None:
    user = (os.environ.get("GMAIL_ADDRESS") or "").strip()
    password = (os.environ.get("GMAIL_APP_PASSWORD") or "").replace(" ", "").strip()
    to_addr = (os.environ.get("MAIL_TO") or "").strip() or user
    if not user or not password:
        raise RuntimeError("缺少 GMAIL_ADDRESS 或 GMAIL_APP_PASSWORD")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_addr
    msg["X-Priority"] = "1"
    msg["Importance"] = "High"
    msg.set_content(body)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(user, password)
        smtp.send_message(msg)
    log(f"邮件已发送 -> {to_addr}")


# ---------------------------------------------------------------- Bark ----
def send_bark(title: str, body: str, url: str = "") -> None:
    """Bark 紧急警报：level=critical + call=1，静音/勿扰/睡眠模式下也会响。"""
    key = (os.environ.get("BARK_KEY") or "").strip()
    server = (os.environ.get("BARK_SERVER") or "").strip() or "https://api.day.app"
    if not key:
        raise RuntimeError("缺少 BARK_KEY")

    payload = {
        "device_key": key,
        "title": title,
        "body": body,
        "level": "critical",   # 重要警告：突破静音、勿扰、专注模式
        "call": "1",           # 重复响铃约 30 秒，像来电一样
        "volume": 10,          # 重要警告音量 0-10
        "group": "stock-monitor",
    }
    if url:
        payload["url"] = url   # 点通知直接打开商品页

    req = urllib.request.Request(
        server.rstrip("/") + "/push",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read().decode("utf-8") or "{}")
    if result.get("code") != 200:
        raise RuntimeError(f"Bark 返回异常：{result.get('message') or result}")
    log("Bark 警报已发送")


# ---------------------------------------------------------------- 状态 ----
def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(status: str, notified: dict = None) -> None:
    STATE_FILE.write_text(
        json.dumps(
            {
                "status": status,
                "notified": notified or {},
                "updated": datetime.now().isoformat(timespec="seconds"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------- 检测 ----
def scan(page, texts: list) -> list:
    return page.evaluate(JS_FIND_BUTTONS, texts)


def describe(name: str, matches: list) -> None:
    visible = [m for m in matches if m["visible"]]
    log(f"{name}：匹配 {len(matches)} 个，可见 {len(visible)} 个")
    for m in matches:
        state = "可见" if m["visible"] else f"隐藏({m['reason']})"
        extra = "，disabled" if m["disabled"] else ""
        log(f"    <{m['tag']}> “{m['text']}” -> {state}{extra}")


def check_once(args) -> str:
    """返回 'in_stock' / 'out_of_stock' / 'none'"""
    in_texts = [t for t in args.in_text.split("|") if t.strip()]
    out_texts = [t for t in args.out_text.split("|") if t.strip()]

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=not args.headed,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            locale=os.environ.get("LOCALE") or "en-US",
            timezone_id=os.environ.get("TIMEZONE") or "Asia/Tokyo",
            user_agent=USER_AGENT,
        )
        page = context.new_page()
        try:
            page.goto(args.url, wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except PWError:
                pass

            # 按钮可能由 JS 渲染，轮询等待
            deadline = time.time() + args.wait
            while True:
                in_m = scan(page, in_texts)
                out_m = scan(page, out_texts)
                in_visible = any(m["visible"] for m in in_m)
                out_visible = any(m["visible"] for m in out_m)
                if in_visible:
                    break
                if out_visible:
                    # 再等 2 秒复查一次，防止页面还在切换状态
                    time.sleep(2)
                    in_m = scan(page, in_texts)
                    out_m = scan(page, out_texts)
                    in_visible = any(m["visible"] for m in in_m)
                    out_visible = any(m["visible"] for m in out_m)
                    break
                if time.time() >= deadline:
                    break
                time.sleep(1)

            try:
                log(f"页面标题：{page.title()}")
            except PWError:
                pass
            describe("有货按钮", in_m)
            describe("没货按钮", out_m)

            if in_visible:
                return "in_stock"
            if out_visible:
                return "out_of_stock"

            SHOT_DIR.mkdir(exist_ok=True)
            shot = SHOT_DIR / f"neither-{datetime.now().strftime('%Y%m%d-%H%M%S')}.png"
            page.screenshot(path=str(shot), full_page=True)
            log(f"两个按钮都看不到，已保存截图：{shot}")
            return "none"
        except Exception:
            # 出错时也尽量留一张截图方便排查
            try:
                SHOT_DIR.mkdir(exist_ok=True)
                page.screenshot(
                    path=str(SHOT_DIR / f"error-{datetime.now().strftime('%Y%m%d-%H%M%S')}.png")
                )
            except Exception:
                pass
            raise
        finally:
            context.close()
            browser.close()


def run_cycle(args) -> int:
    try:
        result = check_once(args)
    except Exception as e:  # noqa: BLE001
        log(f"检测出错：{type(e).__name__}: {e}")
        return EXIT_ERROR

    if result == "none":
        return EXIT_NO_BUTTON

    if result == "out_of_stock":
        log("结果：没货")
        save_state("out_of_stock", {})
        return EXIT_OK

    # 有货：Bark 警报 + Gmail 邮件，各自独立，各自只通知一次
    log("结果：有货！")
    state = load_state()
    notified = dict(state.get("notified") or {}) if state.get("status") == "in_stock" else {}
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    failed = False

    if not notified.get("bark"):
        try:
            send_bark("有货了！快下单", f"按钮 “{args.in_text}” 已出现（{now}）", args.url)
            notified["bark"] = True
        except Exception as e:  # noqa: BLE001
            log(f"Bark 推送失败：{type(e).__name__}: {e}")
            failed = True

    if not notified.get("mail"):
        try:
            send_mail(
                "【有货提醒】商品补货了，快去下单！",
                f"检测到有货按钮 “{args.in_text}” 已出现。\n\n"
                f"商品链接：\n{args.url}\n\n检测时间：{now}",
            )
            notified["mail"] = True
        except Exception as e:  # noqa: BLE001
            log(f"发邮件失败：{type(e).__name__}: {e}")
            failed = True

    if notified.get("bark") and notified.get("mail") and not failed:
        log("两个渠道都已通知；之后有货期间不再重复发送")
    save_state("in_stock", notified)   # 失败的渠道下次继续重试，成功的不重复
    return EXIT_ERROR if failed else EXIT_OK


# ---------------------------------------------------------------- 入口 ----
def parse_args():
    ap = argparse.ArgumentParser(description="奢侈品补货监控")
    ap.add_argument("--url", default=os.environ.get("PRODUCT_URL", ""))
    ap.add_argument("--in-text", default=os.environ.get("IN_STOCK_TEXT", ""),
                    help="有货按钮文字，多个写法用 | 分隔")
    ap.add_argument("--out-text", default=os.environ.get("OUT_OF_STOCK_TEXT", ""),
                    help="没货按钮文字，多个写法用 | 分隔")
    ap.add_argument("--test-email", action="store_true", help="只发测试邮件然后退出")
    ap.add_argument("--test-bark", action="store_true", help="只发测试 Bark 警报然后退出")
    ap.add_argument("--headed", action="store_true", help="显示浏览器窗口（本机调试）")
    ap.add_argument("--wait", type=int, default=15, help="等待按钮渲染的最长秒数")
    ap.add_argument("--loop", type=int, default=0, help="本机常驻：每隔 N 秒检测一次")
    return ap.parse_args()


def main() -> None:
    args = parse_args()

    if args.test_email:
        try:
            send_mail(
                "【测试】补货监控邮件通道正常",
                f"如果你收到这封邮件，说明 Gmail 发信配置正确。\n"
                f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            )
        except Exception as e:  # noqa: BLE001
            log(f"测试邮件发送失败：{type(e).__name__}: {e}")
            sys.exit(EXIT_ERROR)
        sys.exit(EXIT_OK)

    if args.test_bark:
        try:
            send_bark("【测试】Bark 紧急警报", "如果静音/勿扰下手机也响了，说明配置成功。")
        except Exception as e:  # noqa: BLE001
            log(f"测试 Bark 发送失败：{type(e).__name__}: {e}")
            sys.exit(EXIT_ERROR)
        sys.exit(EXIT_OK)

    missing = [n for n, v in (("PRODUCT_URL", args.url), ("IN_STOCK_TEXT", args.in_text),
                              ("OUT_OF_STOCK_TEXT", args.out_text)) if not v.strip()]
    if missing:
        log(f"缺少配置：{', '.join(missing)}")
        sys.exit(EXIT_ERROR)

    if args.loop > 0:
        log(f"常驻模式：每 {args.loop} 秒检测一次，Ctrl+C 退出")
        try:
            while True:
                code = run_cycle(args)
                log(f"本轮退出码：{code}")
                time.sleep(args.loop)
        except KeyboardInterrupt:
            sys.exit(EXIT_OK)

    sys.exit(run_cycle(args))


if __name__ == "__main__":
    main()
