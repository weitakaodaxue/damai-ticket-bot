# -*- coding: UTF-8 -*-
"""订单页强推：精确识别『继续尝试』弹窗并点它，否则点『立即提交』。

用 Appium 定位元素而不是盲点坐标，避免误触分期/观演人选项。
"""
import sys
import time

import grab_517 as g


def log(m, l="INFO"):
    print("[%s] %s" % (l, m), flush=True)


def main(rounds=240):
    sid = g.open_appium(log)
    if not sid:
        log("Appium 连不上", "FAIL")
        return 1
    t0 = time.time()
    moved = False
    try:
        for i in range(rounds):
            act = g.activity()
            if "DmOrderActivity" not in act and "ContainerActivity" not in act:
                log("已离开订单页 -> %s" % act.split(".")[-1], "OK")
                moved = True
                break

            retry = g.appium_find_all(sid, "//*[@text='继续尝试']", timeout=3)
            if retry:
                pt = g.appium_rect(sid, retry[0]["ELEMENT"])
                if pt:
                    g.tap(*pt)
                    log("%.0fs 第%d次 点『继续尝试』%s" % (time.time() - t0, i + 1, pt), "OK")
                    time.sleep(0.2)
                    continue

            sub = g.appium_find_all(sid, "//*[@text='立即提交']", timeout=3)
            if sub:
                pt = g.appium_rect(sid, sub[0]["ELEMENT"])
                if pt:
                    g.tap(*pt)
                    if i % 5 == 0:
                        log("%.0fs 第%d次 点『立即提交』%s" % (time.time() - t0, i + 1, pt), "OK")
                    time.sleep(0.22)
                    continue

            # 安全校验容器里没有这些控件，等它自己回去
            time.sleep(0.35)
            if i % 40 == 0:
                log("%.0fs 无控件，activity=%s" % (time.time() - t0, act.split(".")[-1]), "WARN")
                g.shot("hammer-state")
    finally:
        g.close_appium(sid)
    if moved:
        g.shot("moved")
    log("结束，moved=%s，用时 %.0fs" % (moved, time.time() - t0), "OK" if moved else "FAIL")
    return 0 if moved else 1


if __name__ == "__main__":
    raise SystemExit(main())