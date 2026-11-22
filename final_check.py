# -*- coding: UTF-8 -*-
"""开抢前最终验收：逐项 PASS/FAIL，不改动任何状态。"""
import json
import os
import re
import subprocess
import time

import grab_517 as g

ROOT = os.path.dirname(os.path.abspath(__file__))
fails = []
oks = []


def chk(name, ok, detail=""):
    (oks if ok else fails).append(name)
    print("  [%s] %-38s %s" % ("PASS" if ok else "FAIL", name, detail))


def main():
    print("=== 1. 设备与屏幕 ===")
    pw = g.adb_text("shell", "dumpsys", "power")
    chk("屏幕亮着", "mWakefulness=Awake" in pw, "mWakefulness=Awake" if "Awake" in pw else pw[:40])
    stay = g.adb_text("shell", "settings", "get", "global", "stay_on_while_plugged_in")
    chk("充电保持亮屏", stay not in ("", "0"), "stay_on=%s" % (stay or "空"))
    bat = g.adb_text("shell", "dumpsys", "battery")
    lvl = re.search(r"level:\s*(\d+)", bat)
    chk("电量 > 30%", bool(lvl) and int(lvl.group(1)) > 30, "%s%%" % (lvl.group(1) if lvl else "?"))

    print("=== 2. 应用状态 ===")
    act = g.activity()
    chk("大麦在详情页", "ProjectDetailActivity" in act, act.split(".")[-1])

    print("=== 3. 依赖 ===")
    ff = g.ffmpeg_bin()
    chk("ffmpeg 可用", bool(ff), (ff or "缺失")[-40:])
    adb_ok = os.path.exists(g.ADB)
    chk("adb 可执行", adb_ok, g.ADB[-30:] if adb_ok else "缺失")
    try:
        st = g.http("GET", "/status", timeout=5)
        chk("Appium 服务在线", True, "ready" if (st.get("value") or {}).get("ready") else "up")
    except Exception as e:
        chk("Appium 服务在线", False, str(e)[:40])

    print("=== 4. 配置 ===")
    cfg = json.load(open(os.path.join(ROOT, "bot-config.json"), encoding="utf-8"))
    chk("观演人 2 位", len([x for x in cfg.get("attendees") or [] if x]) == 2, str(cfg.get("attendees")))
    chk("数量 = 2", int(cfg.get("quantity", 0)) == 2, "quantity=%s" % cfg.get("quantity"))
    chk("场次正确", "2026-12-31" in (cfg.get("session_date") or ""), cfg.get("session_date"))
    chk("票档正确", "517" in (cfg.get("ticket_price") or ""), cfg.get("ticket_price"))

    print("=== 5. 关键坐标合理性 ===")
    x, y = g.CTA_TAP
    chk("CTA 坐标在底部栏", 2200 < y < 2399 and 200 < x < 1040, str(g.CTA_TAP))
    x2, y2 = g.SUBMIT_TAP
    chk("提交坐标在底部栏", 2200 < y2 < 2399 and 600 < x2 < 1040, str(g.SUBMIT_TAP))

    print("=== 6. 视频流实时测试（18 秒） ===")
    t0 = time.time()
    adb_p, ff_p = g.start_stream()
    chk("视频流启动", bool(ff_p), "ok" if ff_p else "失败")
    base = None
    n = 0
    maxd = 0.0
    while ff_p and time.time() - t0 < 18:
        buf = g.read_exact(ff_p.stdout, g.FRAME_BYTES)
        if buf is None:
            break
        n += 1
        sig = g.gray_sig(buf)
        if base is None:
            base = sig
            continue
        maxd = max(maxd, g.gray_diff(base, sig))
    fps = n / max(0.1, time.time() - t0)
    for p in (ff_p, adb_p):
        try:
            p.kill()
        except Exception:
            pass
    chk("帧率 >= 10fps", fps >= 10, "%.1f fps, %d 帧" % (fps, n))
    chk("无假阳性（最大变化 < 16%%）", maxd < 0.16, "max_diff=%.1f%%" % (maxd * 100))

    print("\n=== 汇总 ===")
    print("PASS %d 项 / FAIL %d 项" % (len(oks), len(fails)))
    if fails:
        print("未通过: " + ", ".join(fails))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())