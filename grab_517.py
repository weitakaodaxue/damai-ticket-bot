# -*- coding: UTF-8 -*-
"""大麦抢票执行器 v3 —— 不依赖无障碍轮询（UiAutomator2 会被系统杀掉）

原理：
  1) 轮询用 `adb exec-out screencap` 原始像素，只对底部购买栏区域算指纹（~0.2s/次）
  2) 指纹变化 + activity 仍是详情页 → 说明按钮从「已预约」变成「立即购买」→ 点击
  3) 进入选票页后，用一次 Appium 抓取拿坐标（拿不到就用固定坐标兜底）
  4) 订单页勾观演人（姓名脱敏为「*永秋」，按后两字匹配）→ 提交 → 截图留证
  5) 任何不确定：停手 + 截图 + 日志

用法:
  python grab_517.py watch            # 只读：观察购买栏是否变化（现在测这个）
  python grab_517.py run --at 17:16:50 # 实跑
"""
import argparse
import hashlib
import shutil
import json
import os
import struct
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta

ADB = r"adb"
SERIAL = "YOUR_DEVICE_SERIAL"
ROOT = os.path.dirname(os.path.abspath(__file__))
LOGDIR = os.path.join(ROOT, "logs")
SERR = "http://127.0.0.1:4723"
FFMPEG = (r"C:\Users\18179\AppData\Local\Microsoft\WinGet\Packages"
          r"\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
          r"\ffmpeg-9.0.2-full_build\bin\ffmpeg.exe")
# 视频流监听：只裁底部 120 行，灰度，约 58fps
CROP_Y, CROP_H = 2280, 120
FRAME_BYTES = 1080 * CROP_H

# 底部购买栏（详情页 CTA）区域，按 1080x2400 实测
# 详情页主按钮实测边界 x 320-1040 / y 2306-2366（截图核对），中心取 (680, 2336)
STRIP_TOP = 2180
STRIP_H = 200
CTA_TAP = (680, 2336)
CONFIRM_TAP = (840, 2320)      # 选票弹层底部按钮：宽按钮(243-1017)与右侧按钮(666-1014)都命中
SUBMIT_TAP = (822, 2311)       # 订单页「立即提交」实测坐标

CAPS = {
    "platformName": "Android",
    "appium:automationName": "UiAutomator2",
    "appium:udid": SERIAL,
    "appium:noReset": True,
    "appium:autoLaunch": False,
    "appium:newCommandTimeout": 3600,
    "appium:skipServerInstallation": True,
    "appium:skipDeviceInitialization": True,
    "appium:disableWindowAnimation": True,
    "appium:ignoreHiddenApiPolicyError": True,
}


def log(path):
    f = open(path, "a", encoding="utf-8")

    def w(msg, level="INFO"):
        line = "[%s] %-5s %s" % (datetime.now().strftime("%H:%M:%S.%f")[:-3], level, msg)
        print(line, flush=True)
        f.write(line + "\n")
        f.flush()
    return w


def adb(*args, timeout=25):
    return subprocess.run([ADB, "-s", SERIAL] + list(args),
                          capture_output=True, timeout=timeout).stdout


def adb_text(*args):
    return adb(*args).decode("utf-8", "replace").strip()


def activity():
    out = adb_text("shell", "dumpsys", "activity", "activities")
    for line in out.splitlines():
        if "topResumedActivity" in line:
            return line.split("u0 ")[-1].split(" t")[0].strip()
    return ""


def frame():
    """返回 (raw_bytes, w, h)；失败返回 (None,0,0)"""
    try:
        raw = adb("exec-out", "screencap", timeout=20)
    except Exception:
        return None, 0, 0
    if len(raw) < 16:
        return None, 0, 0
    try:
        w, h, _f = struct.unpack("<III", raw[:12])
    except Exception:
        return None, 0, 0
    if w <= 0 or h <= 0 or len(raw) < 12 + w * h * 4:
        return None, 0, 0
    return raw, w, h


def strip_bytes(stride=1080 * 4, rows=STRIP_H):
    """只取屏幕底部条带的原始像素。

    注意：screencap 原始流 = 12字节头 + W*H*4 + 4字节 footer，
    用 tail 截取时必须多取 4 字节再把 footer 去掉，否则整幅图错位一个像素。
    """
    need = stride * rows
    try:
        out = subprocess.run([ADB, "-s", SERIAL, "exec-out", "sh", "-c", "screencap | tail -c %d" % (need + 4)],
                             capture_output=True, timeout=20).stdout
    except Exception:
        return None
    if len(out) < need + 4:
        return None
    return out[:need]


def strip_sig(data, w=1080):
    """把条带压成 16x4 分块平均色，抗闪烁/抗细微动画。"""
    if not data:
        return []
    stride = w * 4
    cols = list(range(0, w, 8))
    h = len(data) // stride
    rows = list(range(0, max(1, h - 4), 4))
    bx, by = 16, 4
    blocks = [[[0, 0, 0, 0] for _ in range(bx)] for _ in range(by)]
    cw, ch = max(1, len(cols)), max(1, len(rows))
    for ri, y in enumerate(rows):
        base = y * stride
        gy = min(by - 1, ri * by // ch)
        for ci, x in enumerate(cols):
            i = base + x * 4
            if i + 3 >= len(data):
                continue
            b = blocks[gy][min(bx - 1, ci * bx // cw)]
            b[0] += data[i]
            b[1] += data[i + 1]
            b[2] += data[i + 2]
            b[3] += 1
    out = []
    for row in blocks:
        for b in row:
            n = max(1, b[3])
            out.append((b[0] // n, b[1] // n, b[2] // n))
    return out


def vec_diff(a, b, thr=26):
    """颜色变化超过阈值的分块比例。"""
    if not a or not b or len(a) != len(b):
        return 1.0
    changed = 0
    for (r1, g1, b1), (r2, g2, b2) in zip(a, b):
        if abs(r1 - r2) + abs(g1 - g2) + abs(b1 - b2) > thr * 3:
            changed += 1
    return changed / len(a)


# ---------------------------------------------------------------- 视频流监听
def gray_sig(buf, w=1080, h=CROP_H, bx=16, by=4):
    """灰度条带 → 16x4 分块均值。"""
    out = []
    for gy in range(by):
        y0, y1 = gy * h // by, (gy + 1) * h // by
        for gx in range(bx):
            x0, x1 = gx * w // bx, (gx + 1) * w // bx
            s = c = 0
            for y in range(y0, max(y0 + 1, y1), 4):
                base = y * w
                for x in range(x0, max(x0 + 1, x1), 8):
                    s += buf[base + x]
                    c += 1
            out.append(s // max(1, c))
    return out


def gray_diff(a, b, thr=22):
    if not a or not b or len(a) != len(b):
        return 1.0
    return sum(1 for x, y in zip(a, b) if abs(x - y) > thr) / len(a)


def read_exact(stream, n):
    buf = b""
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def start_stream():
    """返回 (adb进程, ffmpeg进程)；失败返回 (None, None)。"""
    try:
        adb_p = subprocess.Popen(
            [ADB, "-s", SERIAL, "exec-out", "screenrecord",
             "--time-limit", "180", "--output-format=h264", "-"],
            stdout=subprocess.PIPE)
        ff_p = subprocess.Popen(
                [ffmpeg_bin(), "-loglevel", "error", "-f", "h264", "-i", "pipe:0",
             "-vf", "crop=1080:%d:0:%d" % (CROP_H, CROP_Y),
             "-pix_fmt", "gray", "-f", "rawvideo", "pipe:1"],
            stdin=adb_p.stdout, stdout=subprocess.PIPE)
        adb_p.stdout.close()
        return adb_p, ff_p
    except Exception:
        return None, None


def stream_watch(log_fn, live, max_frames=100000, seconds=None):
    """按视频流逐帧检测购买栏变化；返回 'tapped' / 'dry' / ''。"""
    t0 = time.time()
    base = None
    streak = 0
    n = 0
    warned = False
    restarts = 0
    while restarts < 40:
        adb_p, ff_p = start_stream()
        if not ff_p:
            log_fn("视频流启动失败", "FAIL")
            return ""
        log_fn("视频流已启动（crop y=%d h=%d）" % (CROP_Y, CROP_H), "OK")
        while True:
            buf = read_exact(ff_p.stdout, FRAME_BYTES)
            if buf is None:
                break
            n += 1
            sig = gray_sig(buf)
            if base is None:
                base = sig
                log_fn("基线已建立（第 %d 帧）" % n, "OK")
                continue
            d = gray_diff(base, sig)
            if d >= 0.16:
                streak += 1
            else:
                streak = 0
            if streak >= 2:
                log_fn("第 %d 帧检测到变化 %.0f%%（%.2fs）" % (n, d * 100, time.time() - t0), "HIT")
                for p in (ff_p, adb_p):
                    try:
                        p.kill()
                    except Exception:
                        pass
                if not live:
                    shot("stream-changed")
                    log_fn("只读模式，未点击", "OK")
                    return "dry"
                # 先点再校验：省掉点击前 135ms 的页面查询，点击后由 do_sku 校验
                tap(*CTA_TAP)
                log_fn("已点击购买按钮 %s" % (CTA_TAP,), "OK")
                return "tapped"
            if n % 120 == 0:
                log_fn("第 %d 帧：无变化（%.0fs, %.0ffps）"
                       % (n, time.time() - t0, n / max(0.1, time.time() - t0)))
            # 开抢时刻后 3 秒仍无变化 → 大声提示人工接管（不点，避免误触取消预约）
            if not warned and time.time() - t0 > 13:
                warned = True
                log_fn("已过开抢时刻仍未见按钮变化！请立即手动点击「立即购买」", "STOP")
                shot("no-change-after-sale")
            if n >= max_frames or (seconds and time.time() - t0 > seconds):
                for p in (ff_p, adb_p):
                    try:
                        p.kill()
                    except Exception:
                        pass
                log_fn("达到测试上限，退出", "OK")
                return ""
        for p in (ff_p, adb_p):
            try:
                p.kill()
            except Exception:
                pass
        restarts += 1
        log_fn("视频流结束，重启第 %d 次" % restarts, "WARN")
        time.sleep(0.3)
    return ""


def shot(tag, path_dir=None):
    d = path_dir or LOGDIR
    p = os.path.join(d, "shot-%s-%s.png" % (tag, datetime.now().strftime("%H%M%S")))
    try:
        raw = adb("exec-out", "screencap", "-p", timeout=20)
        if raw and len(raw) > 5000:
            with open(p, "wb") as f:
                f.write(raw)
            return p
    except Exception:
        pass
    return None


def tap(x, y):
    adb("shell", "input", "tap", str(x), str(y))


# ---------------------------------------------------------------- Appium 一次性
def http(method, path, payload=None, timeout=8):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(SERR + path, data=data, method=method,
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def appium_find_all(sid, xpath, timeout=6):
    try:
        r = http("POST", "/session/%s/elements" % sid, {"using": "xpath", "value": xpath}, timeout=timeout)
        return r.get("value") or []
    except Exception:
        return []


def appium_rect(sid, eid):
    try:
        r = http("GET", "/session/%s/element/%s/rect" % (sid, eid), timeout=5)["value"]
        return int(r["x"] + r["width"] / 2), int(r["y"] + r["height"] / 2)
    except Exception:
        return None


def appium_source(sid, timeout=8):
    try:
        return http("GET", "/session/%s/source" % sid, timeout=timeout)["value"]
    except Exception:
        return ""


def ffmpeg_bin():
    if os.path.exists(FFMPEG):
        return FFMPEG
    return shutil.which("ffmpeg") or ""

def open_appium(log_fn):
    try:
        r = http("POST", "/session", {"capabilities": {"alwaysMatch": CAPS}}, timeout=60)
        sid = r["value"]["sessionId"]
        try:
            http("POST", "/session/%s/appium/settings" % sid,
                 {"settings": {"waitForIdleTimeout": 0}}, timeout=8)
        except Exception:
            pass
        log_fn("Appium 会话 %s" % sid[:8], "OK")
        return sid
    except Exception as e:
        log_fn("Appium 连不上: %s" % e, "WARN")
        return None


def close_appium(sid):
    if not sid:
        return
    try:
        http("DELETE", "/session/%s" % sid, timeout=8)
    except Exception:
        pass


# ---------------------------------------------------------------------- 主逻辑
def wait_sale(log_fn, cfg, live, at=None, max_n=30000):
    """轮询底部购买栏指纹，变化即认为是开卖。"""
    if at:
        h, m, s = (int(x) for x in at.split(":"))
        now = datetime.now()
        target = now.replace(hour=h, minute=m, second=s, microsecond=0)
        while True:
            left = (target - datetime.now()).total_seconds()
            if left <= 0:
                break
            if left > 60:
                log_fn("距目标 %d 分 %d 秒" % (int(left // 60), int(left % 60)))
                time.sleep(min(30, left - 55))
            else:
                time.sleep(0.5)
        log_fn("到达目标时刻，进入轮询", "OK")

    data = strip_bytes()
    if not data:
        log_fn("条带截取失败，无法开始", "FAIL")
        return False
    base = strip_sig(data)
    log_fn("购买栏基线已建立（activity=%s，数据 %d 字节）" % (activity(), len(data)), "OK")

    t0 = time.time()
    n = 0
    streak = 0
    last_d = 0.0
    while True:
        n += 1
        data = strip_bytes()
        if not data:
            time.sleep(0.1)
            continue
        d = vec_diff(base, strip_sig(data))
        last_d = d
        # 兜底：每 5 帧查一次 activity，你手动点开选票页/订单页时也能接管
        if n % 5 == 0:
            a = activity()
            if "NcovSku" in a:
                log_fn("检测到已进入选票页（人工点击），接管", "HIT")
                return "sku"
            if "order" in a.lower() and "Confirm" in a:
                log_fn("检测到已进入订单页（人工点击），接管", "HIT")
                return "order"
        if d >= 0.22:
            streak += 1
            log_fn("第 %d 次: 购买栏变化 %.0f%%（连续 %d 帧）" % (n, d * 100, streak), "HIT")
            if streak >= 2:
                act = activity()
                log_fn("确认变化 activity=%s (%.1fs)" % (act, time.time() - t0), "HIT")
                if "ProjectDetailActivity" in act:
                    if not live:
                        shot("changed")
                        log_fn("只读模式：检测到变化但未点击（截图已存）", "OK")
                        return "dry"
                    tap(*CTA_TAP)
                    log_fn("已点击购买按钮 %s" % (CTA_TAP,), "OK")
                    return "tapped"
                log_fn("活动页不是详情页（%s），不点击" % act, "WARN")
                return ""
        else:
            streak = 0
        if n % 40 == 0:
            log_fn("第 %d 次轮询：无变化 %.0f%%（已 %.0fs）" % (n, last_d * 100, time.time() - t0))
        if n > max_n:
            log_fn("轮询超时", "FAIL")
            return ""
        time.sleep(0.05)


def do_sku(log_fn, cfg, sid=None):
    """选票弹层：数量 → 确定。Appium 拿坐标，失败用固定坐标。"""
    time.sleep(0.5)
    act = activity()
    if "DmOrder" in act:
        log_fn("已在订单页，跳过选票", "OK")
        return True
    log_fn("当前 activity=%s" % act, "INFO")
    if "NcovSku" not in act and "Sku" not in act:
        log_fn("没进到选票页，停手", "FAIL")
        shot("no-sku")
        return False

    need = int(cfg.get("quantity", 1))
    if not sid:
        sid = open_appium(log_fn)
    # 预约里已经锁了 2 张，这里只读校验，绝不主动加号（否则会买成 3 张）
    if sid:
        src = appium_source(sid, timeout=8)
        nums = re.findall(r'"?(\d+)\s*张', src) or re.findall(r'×\s*(\d+)', src)
        log_fn("SKU 页面张数线索: %s（目标 %d 张，不自动加号）"
               % (nums[:5] or "未读到", need), "OK")
    else:
        log_fn("Appium 不可用，无法校验张数（按预约锁定的 2 张走）", "WARN")

    # 底部按钮：优先进程内坐标，其次固定坐标；点完校验是否离开选票页
    candidates = []
    if sid:
        btn = appium_find_all(sid, "//*[@resource-id='cn.damai:id/btn_buy_view']")
        if btn:
            pt = appium_rect(sid, btn[0]["ELEMENT"])
            if pt:
                candidates.append(pt)
    candidates += [CONFIRM_TAP, (630, 2320), (900, 2320)]
    for i, pt in enumerate(candidates):
        tap(*pt)
        log_fn("点确定 #%d %s" % (i + 1, pt), "OK")
        time.sleep(0.9)
        a2 = activity()
        if "NcovSku" not in a2 and "Sku" not in a2:
            log_fn("已离开选票页 -> %s" % a2.split(".")[-1], "OK")
            return True
    log_fn("三次点击后仍停在选票页，请手动点确定", "STOP")
    shot("sku-stuck")
    return False


def do_order(log_fn, cfg, auto_submit, sid=None):
    """订单页：勾观演人 → 提交。"""
    time.sleep(0.8)
    names = [x for x in (cfg.get("attendees") or []) if x]
    if not names:
        log_fn("配置无观演人姓名，拒绝提交", "FAIL")
        shot("order-noconf")
        return False
    if not sid:
        sid = open_appium(log_fn)
    if not sid:
        log_fn("Appium 不可用，订单页无法自动勾人，请手动完成", "STOP")
        shot("order-noappium")
        return False
    try:
        def attr(eid, key):
            try:
                return http("GET", "/session/%s/element/%s/attribute/%s"
                            % (sid, eid, key), timeout=5)["value"]
            except Exception:
                return None

        def find_for(name):
            for cand in (name, name[-2:], name[-1:]):
                for xp in ("//*[@text='%s']" % cand, "//*[contains(@text,'%s')]" % cand):
                    els = appium_find_all(sid, xp)
                    if els:
                        return cand, els[0]["ELEMENT"]
            return None, None

        def is_checked(eid):
            for k in ("checked", "selected"):
                v = attr(eid, k)
                if str(v).lower() == "true":
                    return True
            return False

        done = []
        for name in names:
            cand, eid = find_for(name)
            if not eid:
                log_fn("找不到观演人 %r" % name, "FAIL")
                shot("order-miss")
                return False
            if is_checked(eid):
                log_fn("%s 已经是勾选状态，跳过（避免反选）" % name, "OK")
                done.append(name)
                continue
            pt = appium_rect(sid, eid)
            if pt:
                tap(*pt)
                time.sleep(0.3)
            if is_checked(eid):
                log_fn("已勾选 %s（匹配词 %r）" % (name, cand), "OK")
            else:
                # 有些实现是 ImageView 包一层，点父节点再试
                pel = appium_find_all(sid, "//*[@text='%s']/.." % cand)
                if pel:
                    p2 = appium_rect(sid, pel[0]["ELEMENT"])
                    if p2:
                        tap(*p2)
                        time.sleep(0.3)
                log_fn("已点 %s（匹配词 %r，状态未确认）" % (name, cand), "WARN")
            done.append(name)

        # 复核：页面上有多少个 checked=true
        checked = appium_find_all(sid, "//*[@checked='true']")
        log_fn("勾选完成 %d/%d 人，页面 checked 节点 %d 个"
               % (len(done), len(names), len(checked)), "OK")

        if not auto_submit:
            log_fn("停在提交前，请你手动点提交", "OK")
            shot("before-submit")
            return True

        plans = []
        for label in ("立即提交", "提交订单", "确认提交", "提交"):
            els = appium_find_all(sid, "//*[@text='%s']" % label)
            if els:
                pt = appium_rect(sid, els[0]["ELEMENT"])
                if pt:
                    plans.append(("文案 %r" % label, pt))
        plans.append(("实测坐标", SUBMIT_TAP))
        plans.append(("次选坐标", (840, 2320)))
        for name, pt in plans:
            tap(*pt)
            log_fn("点提交（%s）%s" % (name, pt), "OK")
            time.sleep(1.3)
            act = activity()
            if "DmOrderActivity" not in act:
                log_fn("已离开订单页 -> %s" % act.split(".")[-1], "OK")
                shot("after-submit")
                return True
            log_fn("仍在订单页，换下一个方案", "WARN")
        log_fn("提交失败，请手动点「立即提交」", "STOP")
        shot("submit-failed")
        return False
    finally:
        pass


def wait_clock(log_fn, at):
    h, m, s = (int(x) for x in at.split(":"))
    target = datetime.now().replace(hour=h, minute=m, second=s, microsecond=0)
    while True:
        left = (target - datetime.now()).total_seconds()
        if left <= 0:
            log_fn("到达 %s，开始监听" % at, "OK")
            return
        if left > 60:
            log_fn("距 %s 还有 %d 分 %d 秒" % (at, int(left // 60), int(left % 60)))
            time.sleep(min(30, left - 55))
        else:
            time.sleep(0.3)


def preflight(log_fn, sid):
    """开抢前自检：屏幕/前台/常亮/ffmpeg/Appium，逐项报告，返回是否全部通过。"""
    ok = True
    pw = adb_text("shell", "dumpsys", "power")
    awake = "mWakefulness=Awake" in pw
    log_fn("屏幕: %s" % ("亮着" if awake else "已熄灭"), "OK" if awake else "FAIL")
    ok &= awake

    act = activity()
    log_fn("前台: %s" % act)
    if "ProjectDetailActivity" in act:
        log_fn("已在详情页", "OK")
    elif "NcovSku" in act:
        adb("shell", "input", "keyevent", "4")
        time.sleep(1.0)
        act = activity()
        log_fn("从选票页退回: %s" % ("成功" if "ProjectDetail" in act else "失败"), "WARN")
        ok &= "ProjectDetail" in act
    else:
        log_fn("不在详情页！请把大麦切回演出详情页", "FAIL")
        ok = False

    stay = adb_text("shell", "settings", "get", "global", "stay_on_while_plugged_in")
    log_fn("充电保持亮屏: %s" % (stay or "空"), "OK" if stay not in ("", "0") else "WARN")

    bat = adb_text("shell", "dumpsys", "battery")
    for line in bat.splitlines():
        if "level:" in line:
            log_fn("电量: %s" % line.strip(), "OK")
            break

    ff = ffmpeg_bin()
    log_fn("ffmpeg: %s" % (ff or "缺失，将退回截图轮询"), "OK" if ff else "WARN")
    log_fn("Appium 会话: %s" % ("就绪" if sid else "未建立，订单页只能靠固定坐标"), "OK" if sid else "WARN")

    data = strip_bytes()
    log_fn("截图条带: %s" % ("%d 字节" % len(data) if data else "失败"), "OK" if data else "FAIL")
    ok &= bool(data)
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["watch", "run"])
    ap.add_argument("--at", default=None)
    ap.add_argument("--no-submit", action="store_true")
    ap.add_argument("--max", type=int, default=30000)
    ap.add_argument("--stream", action="store_true", help="用视频流监听（更快）")
    a = ap.parse_args()
    os.makedirs(LOGDIR, exist_ok=True)
    log_fn = log(os.path.join(LOGDIR, "g517-%s.log" % datetime.now().strftime("%H%M%S")))
    cfg = json.load(open(os.path.join(ROOT, "bot-config.json"), encoding="utf-8"))

    log_fn("activity=%s 屏幕=%s" % (activity(), "亮" if "Awake" in adb_text("shell", "dumpsys", "power") else "灭"))
    sid = open_appium(log_fn) if a.mode == "run" else None
    if a.mode == "run":
        if not preflight(log_fn, sid):
            log_fn("自检未全通过，仍继续（可能被人工修复）", "WARN")

    if a.stream:
        if a.at:
            wait_clock(log_fn, a.at)
        res = stream_watch(log_fn, live=(a.mode == "run"), max_frames=a.max)
        if not res and a.mode == "run":
            log_fn("视频流未触发，退回截图轮询", "WARN")
            res = wait_sale(log_fn, cfg, live=True, at=None, max_n=600)
    else:
        res = wait_sale(log_fn, cfg, live=(a.mode == "run"), at=a.at, max_n=a.max)

    if not res:
        log_fn("未触发，结束", "FAIL")
        return 1
    if a.mode != "run":
        return 0
    # 分支修正：预约已锁票档/张数时，大麦会跳过选票页直接进订单页
    act_now = activity()
    if "DmOrder" in act_now:
        log_fn("预约直通订单页，跳过选票处理", "OK")
    elif res in ("tapped", "sku"):
        if not do_sku(log_fn, cfg, sid):
            log_fn("选票页处理失败，请手动接管", "STOP")
            return 1
    log_fn("进入订单页处理", "INFO")
    do_order(log_fn, cfg, auto_submit=not a.no_submit, sid=sid)
    close_appium(sid)
    return 0


if __name__ == "__main__":
    sys.exit(main())