# 大麦抢票机器人（实战版）

一套**不依赖 Appium 轮询**的大麦 App 抢票实现：用 `screenrecord` 视频流 + 像素差分检测开售瞬间，用 `adb` 注入点击，Appium 只用于取元素坐标。

> 本仓库是一次真实抢票的**代码 + 完整复盘**。目标：薛之谦昆明站 2026-12-31 21:00 / 看台 ¥517 / 2 张。
> 结果：**检测与点击成功（273ms 内完成），提交环节失败**。
> 失败原因、时间线、根因分析见 **[REPORT.md](REPORT.md)**。

## 为什么值得一看

- 实测端到端 **160–280 ms**（人手 350–500ms，接口级工具 50–150ms）
- 视频流 13–57 fps，**600 帧零误报**
- 记录并修复了 7 个致命 bug，包括：
  - `adb shell "screencap | tail"` 的 PTY 污染（字节数 869625 vs 864000）
  - screencap 原始流 4 字节 footer 导致的像素错位
  - 无障碍服务（UiAutomator2 instrumentation）被系统杀掉的绕行方案

## 文件

| 文件 | 说明 |
|---|---|
| `grab_517.py` | 主程序：视频流监听 → 检测 → 点击 → 选票/订单处理 |
| `hammer.py` | 订单页强推：识别「继续尝试」弹窗并连点 |
| `final_check.py` | 开抢前 16 项验收自检 |
| `bot-config.sample.json` | 参数模板（场次/票档/张数/观演人姓名） |
| `log-excerpt.txt` | 实战关键 10 秒的原始日志 |

## 运行

```bash
pip install -r requirements.txt      # 仅需标准库 + appium（用于取坐标）
adb devices                          # 确认设备

python final_check.py                # 开抢前自检
python grab_517.py run --stream --at 17:16:50   # 到点自动监听并下单
```

依赖：`adb`、`ffmpeg`（放在 PATH 或改 `FFMPEG` 常量）、`appium` + `uiautomator2` driver（可选，仅取坐标用）。

`grab_517.py` 顶部常量需按你的设备调整：

```python
SERIAL   = "YOUR_DEVICE_SERIAL"
CTA_TAP  = (680, 2336)   # 详情页底部购买按钮中心（按你的分辨率实测）
SUBMIT_TAP = (822, 2311) # 订单页「立即提交」中心
```

## 核心思路

```
每帧: adb exec-out screenrecord(h264) → ffmpeg crop 底部120行 → 灰度
      → 16x4 分块均值 → 与基线比较
      → 连续 2 帧 >16% 变化 → adb tap 购买按钮
```

不读文字、不依赖控件树：因为大麦 9.0.39 的**场次/票档/购买按钮都没有 text 节点**，
只有**像素变化**是可靠的信号。

## 免责声明

仅供学习与研究。大麦用户协议禁止自动化抢票，使用可能导致账号风控、订单取消。
本项目未用于转售。仓库内所有个人信息均已脱敏。