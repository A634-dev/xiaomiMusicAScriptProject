# -*- coding: utf-8 -*-
"""
小米音乐（com.miui.player）自动看广告赚免费听时长
=====================================================

来源与致谢
----------
整体结构源于 wei6718189/qishuiAScriptProject（汽水音乐 iOS 版，作者 @wei6718189）：
  分阶段拆解、关键动作前判定前置条件、一次全屏 OCR 后在内存里按优先级匹配、
  所有点击前做危险词拦截 —— 这些都来自原项目。
小米音乐的具体交互为重新实现，原 iOS 代码未复制进本仓库。感谢原作者的开源工作。

小米音乐与汽水音乐的关键差异
----------------------------
  1. 入口是【发现页右上角的「免」徽标】，且有两种形态：
       无时长 → 单颗圆徽标，点完直接进广告
       有时长 → 绿圆环「免」+ 15:21 胶囊，点完跳到「免费听」页
  2. 广告多为互动广告：必须点「点击浏览10秒」跳淘宝，浏览满 10 秒才发奖励
  3. 跨应用跳转会弹「是否允许启动淘宝」，不点允许就跳不过去
  4. 播放页有「豪华绿钻已过期」续费弹窗，绝不能点「¥1.88立即续费」
  5. 成功判定读免费听页顶部的大倒计时，前后对比是否增加（不靠文案猜）

流程：
  第 1 轮   切「发现」→ 点「免」→（广告页 / 免费听页→加时长）→ 广告页
  广告页    点「点击浏览10秒」→ 浏览 12s → 拉起小米音乐 → 领奖 → 点 ✕ 关广告
  收尾      App 自动回到「免费听」页，读倒计时核对
  第 2 轮起 直接点「加时长」→ 广告 → 回到免费听页 → 循环

运行环境：Android + AScript 4.0.17+（无障碍模式），实测 1440x3200
"""

import re
import time
import traceback

# ============== 基础模块导入 ==============
from ascript.android import action, system  # type: ignore
from ascript.android.system import Device  # type: ignore
from ascript.android.screen import Ocr  # type: ignore


# ============== 全局配置 ==============

APP_NAME = "小米音乐"
APP_PACKAGE = "com.miui.player"

# 主循环
MAX_LOOP_COUNT = 20         # 最多 20 轮（约 10~12 分钟），额度跑完会提前退出
LOOP_INTERVAL = 2.5         # 每轮间隔秒数
MAX_IDLE_ROUNDS = 6         # 连续多少轮没收益就退出（当日额度用完）

# 无人值守
KEEP_SCREEN_ON = True       # 屏幕常亮，防息屏中断
AUTO_RESTART_APP = True     # 连续卡住时自动重开 App

# OCR：paddle=百度 PP-OCR V5（准） / mlkit=谷歌（快）
OCR_ENGINE = "paddle"

# 广告处理
AD_TIMEOUT = 80             # 单轮广告最长等待秒数
BROWSE_SECONDS = 12         # 点进第三方后停留秒数（广告要求浏览 10 秒，留 2 秒余量）
SKIP_AFTER_SEC = 12         # 屏幕上有「跳过」时，至少等这么久再点，避免把发奖掐断
AUTO_BROWSE_AD = True       # 是否自动点「点击浏览N秒」进第三方（必须进才给奖励）

# 体检模式：第一次真机调试务必用 True
#   True  = 只跑识别：dump 屏幕文字 + 逐个核对关键区域是否命中，不点广告、不循环
#   False = 正常跑广告循环
SELF_TEST = False
SELF_TEST_ENTER_AD = False  # 体检时是否顺便点一次入口看一眼广告页（不会点「点击浏览」）

# 底部导航「免费听」的兜底坐标：5 个 tab 等分，实测 1440x3200 上在 (719, 3067)
# 作用：页面刚跳转、OCR 还没识别出文字时，用固定位置点一次，避免卡死
NAV_FREE_RATIO = (0.50, 0.958)
NAV_FALLBACK_BY_RATIO = True

# 底部导航「发现」（第 1 个 tab），实测 1440x3200 上在 (142, 3066)
NAV_DISCOVER_RATIO = (0.0986, 0.958)

# 「免」字入口：发现在【发现页】右上角，是一个圆形徽标（灰边 + 免字）。
# 实测 1440x3200 上圆心在 (1320, 232) —— 由 864x1920 截图 (792,139) 等比换算而来。
# 第一次点 → 直接进广告；第二次点 → 跳到「免费听」页。脚本两种情况都处理。
MIAN_X_RATIO = 0.9167
MIAN_Y_RATIO = 0.0725
MIAN_BY_RATIO = True

# 广告页右上角关闭 ✕ 的位置（实测 1440x3200：(1343, 268)，由 864x1920 截图量取）
AD_CLOSE_RATIO = (0.9329, 0.0839)

# 是否在每轮结束后进「免费听」页读倒计时做核对（只读，不作为广告入口）
VERIFY_BY_FREE_TIME = True


# 运行时全局
SCREEN_W = 1080
SCREEN_H = 2400
LAST_FREE_TIME = -1         # 上一轮读到的免费听剩余秒数，用于前后对比


# ============== 关键词表 ==============

# 免费听页特征（"加时长"是最稳的锚点）
FREE_PAGE_STRONG = ["加时长", "赚更多时长"]
FREE_PAGE_WEAK = ["看广告", "限时任务"]

# 广告页特征 —— 不要用裸"广告"，免费听页也有"看广告 免费听"；
# 也不要用泛化的"点击浏览"：免费听页的任务卡里有"点击浏览1秒"，会误判。
AD_HINTS = [
    "点击广告浏览", "跳转详情页", "互动广告", "跳过",
    "开VIP免广告", "浏览10秒", "浏览5秒", "浏览3秒",
]

# 奖励到账提示
REWARD_HINTS = [
    "领取成功", "已到账", "恭喜获得", "恭喜已获得", "已获得奖励", "奖励已发放", "时长已到账",
]
REWARD_BTNS = ["领取奖励", "开心收下", "立即领取", "收下", "知道了", "确定"]

# 弹窗：强特征直接处理；弱特征要配合关闭按钮才处理（防止把常驻会员横幅当弹窗）
POPUP_STRONG = ["豪华绿钻", "已过期", "青少年模式", "版本更新", "升级提示", "会员已到期"]
POPUP_WEAK = ["续费", "开通会员", "会员权益", "首月"]
CLOSE_BTNS = ["关闭", "以后再说", "暂不", "取消", "我知道了", "跳过", "残忍拒绝", "暂不需要"]

# 危险词：任何命中这些词的文字都不允许自动点（续费/下载/跳转全靠它兜住）
DANGER_WORDS = [
    "续费", "开通", "支付", "购买", "充值", "下载", "安装",
    "¥", "￥", "元开通", "会员", "详情页", "去逛逛",
]


# ============== 工具函数 ==============

def log(msg: str):
    print("[{}] {}".format(time.strftime("%H:%M:%S"), msg))


def rect(x1, y1, x2, y2):
    """按比例生成屏幕区域 [l, t, r, b]"""
    return [int(SCREEN_W * x1), int(SCREEN_H * y1), int(SCREEN_W * x2), int(SCREEN_H * y2)]


def r_top_right():
    """右上角：跳过 / 广告关闭 X"""
    return rect(0.55, 0.04, 1.00, 0.13)


def r_ad_sheet():
    """互动广告底部白卡：「点击浏览10秒」按钮所在区域"""
    return rect(0.04, 0.70, 0.96, 0.98)


def r_popup_btn():
    """弹窗按钮区（中部到下部），用于点「领取奖励」等，避开顶部横幅"""
    return rect(0.12, 0.30, 0.88, 0.92)


def r_popup_close():
    """绿钻弹窗右上角 X 的粗略位置"""
    return rect(0.78, 0.45, 1.00, 0.78)


def r_free_time():
    """免费听页顶部大倒计时 00:15:35"""
    return rect(0.02, 0.09, 0.98, 0.26)


def r_nav():
    """底部导航栏"""
    return rect(0.00, 0.90, 1.00, 1.00)


def in_rect(x, y, r):
    return r[0] <= x <= r[2] and r[1] <= y <= r[3]


def is_danger(text: str) -> bool:
    """命中续费/下载/跳转类词，禁止自动点击"""
    return any(w in text for w in DANGER_WORDS)


def safe_click(x, y, desc=""):
    try:
        log("    点击 [{}] -> ({}, {})".format(desc, x, y))
        action.click(int(x), int(y))
        time.sleep(0.5)
        return True
    except Exception as e:
        log("    点击失败 [{}]: {}".format(desc, e))
        return False


def press_back(times=1, wait=1.2):
    for _ in range(times):
        try:
            action.Key.back()
        except Exception as e:
            log("    返回键异常: {}".format(e))
            return False
        time.sleep(wait)
    return True


def click_skip(x, y, desc="跳过"):
    """
    「跳过」永远贴在整行最右边，但 OCR 命中返回的是整行中心。
    实测 1440 宽屏命中 '开VIP免广告|4跳过' 中心 x=1147，而「跳过」实际在 1240~1330，
    直接点中心会落在「开VIP」上。这里把 x 往右收进 88%~96% 区间。
    """
    nx = int(min(max(int(x), SCREEN_W * 0.88), SCREEN_W * 0.96))
    return safe_click(nx, y, desc)


def scan():
    """
    一次全屏 OCR，返回 [(text, cx, cy), ...]。
    整个脚本只走这一个取文字入口 —— 每轮一次 OCR 比反复 Ocr.find 快，
    也避免"Ocr.find_all 返回 dict / paddleocr 返回 OcrText"两套类型踩坑。
    """
    try:
        items = Ocr.find_all()
    except Exception as e:
        log("  全屏 OCR 异常: {}".format(e))
        return []
    out = []
    for it in items or []:
        try:
            if isinstance(it, dict):
                t = it.get("text", "") or ""
                x = it.get("center_x", 0) or 0
                y = it.get("center_y", 0) or 0
            else:
                t = getattr(it, "text", "") or ""
                x = getattr(it, "center_x", 0) or 0
                y = getattr(it, "center_y", 0) or 0
            if t:
                out.append((str(t), int(x), int(y)))
        except Exception:
            continue
    return out


def joined_of(texts):
    return " | ".join(t for t, _, _ in texts)


def pick(texts, keywords, region=None, guard=True, bottommost=False):
    """
    按关键词优先级找目标，返回 (text, x, y) 或 None。
      keywords  : 列表，靠前的优先级高
      region    : 只在这个区域内找
      guard     : 过滤掉含危险词（续费/下载…）的文本
      bottommost: 取区域里最靠下的那个（用于底部导航）
    """
    best = None
    for kw in keywords:
        for t, x, y in texts:
            if kw not in t:
                continue
            if guard and is_danger(t):
                continue
            if region and not in_rect(x, y, region):
                continue
            if bottommost:
                if best is None or y > best[2]:
                    best = (t, x, y)
            else:
                return (t, x, y)
    return best


TIME_RE = re.compile(r"(\d{1,2})\s*[:：]\s*(\d{2})(?:\s*[:：]\s*(\d{2}))?")


def _parse_time(t, need_hms=False):
    """从一段文字里抠出时间并转成秒；need_hms=True 时只认 HH:MM:SS"""
    for m in TIME_RE.finditer(t):
        a, b, c = m.group(1), m.group(2), m.group(3)
        if need_hms and not c:
            continue
        try:
            if c:
                return int(a) * 3600 + int(b) * 60 + int(c)
            return int(a) * 60 + int(b)
        except Exception:
            continue
    return None


def read_free_time(texts=None):
    """
    读免费听页的大倒计时，返回总秒数；读不到返回 -1。
    这是本脚本唯一可靠的「成功判据」——比认文案靠谱。
    两遍扫：先扫顶部区域（正常情况），再全屏找 HH:MM:SS（页面改版/滚动时兜底）。
    只认三段式能排除歌曲时长（3:45 这种两段式）和广告倒计时（"10秒"）。
    """
    if texts is None:
        texts = scan()
    r = r_free_time()
    for t, x, y in texts:
        if not in_rect(x, y, r):
            continue
        s = _parse_time(t)
        if s is not None:
            return s
    for t, x, y in texts:
        s = _parse_time(t, need_hms=True)
        if s is not None:
            return s
    return -1


def fmt(sec):
    if sec is None or sec < 0:
        return "—"
    return "{}:{:02d}:{:02d}".format(sec // 3600, (sec % 3600) // 60, sec % 60)


def foreground():
    try:
        return str(system.get_foreground_app() or "")
    except Exception:
        return ""


def wait_app(timeout=10):
    """等小米音乐到前台。优先用 wait_for_package，没有就轮询前台包名"""
    try:
        r = system.wait_for_package(APP_PACKAGE, timeout)
        if r:
            return True
    except Exception:
        pass
    deadline = time.time() + timeout
    while time.time() < deadline:
        if APP_PACKAGE in foreground():
            return True
        time.sleep(0.5)
    return APP_PACKAGE in foreground()


def return_to_app(max_back=2):
    """
    从淘宝等第三方回到小米音乐。
    实测淘宝详情页连按 3 次返回都退不出来（前台一直是 com.taobao.taobao），
    所以改成「直接拉起 App」优先，返回键只做兜底。
    """
    if APP_PACKAGE in foreground():
        return True
    log("    当前前台 {}，直接拉起小米音乐".format(foreground() or "?"))
    try:
        system.open(APP_PACKAGE)
    except Exception as e:
        log("    拉起失败: {}".format(e))
    if wait_app(10):
        time.sleep(0.8)
        return True

    for i in range(max_back):
        log("    拉起没生效，按返回键兜底（{}/{}）".format(i + 1, max_back))
        press_back(1, 1.4)
        if APP_PACKAGE in foreground():
            return True
    log("    ⚠️ 仍不在小米音乐（当前 {}）".format(foreground() or "?"))
    return False


# ============== 阶段一：启动 / 开屏 / 弹窗 ==============

def init_screen():
    global SCREEN_W, SCREEN_H
    try:
        d = Device.display()
        w = int(getattr(d, "widthPixels", 0) or 0)
        h = int(getattr(d, "heightPixels", 0) or 0)
        if w > 0 and h > 0:
            SCREEN_W, SCREEN_H = w, h
    except Exception as e:
        log("  Device.display() 取值失败，沿用默认 {}x{}: {}".format(SCREEN_W, SCREEN_H, e))
    log("屏幕尺寸: {} x {}".format(SCREEN_W, SCREEN_H))


def launch_app():
    log("=" * 52)
    log("阶段一：启动 {}".format(APP_NAME))
    try:
        system.open(APP_PACKAGE)
        # 实测 system.open 之后 3 秒前台还停在 com.miui.home，必须等到包名真到前台
        if wait_app(12):
            log("  已在前台: {}".format(foreground()))
            time.sleep(1.0)
            return True
        log("  ⚠️ 12s 内没等到前台（当前 {}）".format(foreground() or "?"))
        return False
    except Exception as e:
        log("  启动异常: {}".format(e))
        return False


def skip_splash(timeout=8):
    """跳过开屏广告：右上角轮询「跳过」"""
    log("阶段二：跳过开屏广告")
    deadline = time.time() + timeout
    r = r_top_right()
    while time.time() < deadline:
        texts = scan()
        hit = pick(texts, ["跳过广告", "跳过"], region=r)
        if hit:
            log("  命中开屏跳过: '{}'".format(hit[0]))
            click_skip(hit[1], hit[2], "跳过开屏")
            time.sleep(2.5)          # 开屏关掉后首页要渲染，多留一点时间
            return True
        time.sleep(0.6)
    log("  未检测到开屏广告")
    return False


def dismiss_popups(rounds=3):
    """
    清掉续费/会员/更新类弹窗。判定分两档：
      强特征（豪华绿钻 / 已过期 / 青少年模式…）→ 直接当弹窗处理
      弱特征（续费 / 会员权益…）            → 必须同时出现关闭按钮才算弹窗
    原因：发现页常驻「VIP5续费」「￥1.88开通」这类会员入口，只按文字判会把首页当弹窗，
    然后找不到关闭按钮就按返回键，把 App 一路按退 —— 实测就是这么翻车的。
    """
    cleared = 0
    back_used = False
    for _ in range(rounds):
        texts = scan()
        if not texts:
            break
        j = joined_of(texts)
        strong = any(h in j for h in POPUP_STRONG)
        weak = any(h in j for h in POPUP_WEAK)

        hit = pick(texts, CLOSE_BTNS, guard=True)
        hit_x = pick(texts, ["×", "✕"], region=r_popup_close(), guard=False)
        # × 必须是个孤零零的符号，否则可能是列表里的删除图标
        if hit_x and len(hit_x[0].strip()) > 2:
            hit_x = None

        if not strong and not (weak and (hit or hit_x)):
            break
        log("  检测到弹窗（强特征={} 弱特征={}）".format(strong, weak))

        if hit:
            safe_click(hit[1], hit[2], "关闭弹窗'{}'".format(hit[0]))
            cleared += 1
            time.sleep(1.0)
            continue

        if hit_x:
            safe_click(hit_x[1], hit_x[2], "弹窗右上角 X")
            cleared += 1
            time.sleep(1.0)
            continue

        # 只有强特征（确定是弹窗）才允许退一步，且全局只按一次
        if strong and not back_used:
            log("  强弹窗但找不到关闭按钮，按一次返回键")
            press_back(1, 1.2)
            back_used = True
            cleared += 1
            continue
        break

    if cleared:
        log("  弹窗处理 {} 次".format(cleared))
    return cleared


# ============== 阶段三：进入「免费听」页 ==============

def is_free_page(texts=None):
    if texts is None:
        texts = scan()
    j = joined_of(texts)
    if any(k in j for k in FREE_PAGE_STRONG):
        return True
    # 顶部三段式倒计时（00:15:35）是免费听页最独特的特征，不依赖按钮文案
    return read_free_time(texts) >= 0


def ensure_free_page(max_try=4):
    """
    确保停在底部「免费听」页。两个实测坑：
      1) 点 nav 是幂等的 —— 已在免费听页再点一次也无害，所以「先点再判断」比死等识别稳
      2) 页面刚跳转时 OCR 常返回一堆无关文字甚至空，这时按返回键只会把 App 按退
         （实测三次盲目返回，最后停在「发现」tab）
    """
    log("阶段三：进入「免费听」页")
    ratio_used = False
    for i in range(max_try):
        texts = scan()
        if is_free_page(texts):
            log("  已在免费听页")
            return True

        # 有确定是弹窗的东西先清掉（dismiss_popups 只处理强特征）
        if any(h in joined_of(texts) for h in POPUP_STRONG):
            dismiss_popups(1)
            continue

        hit = pick(texts, ["免费听"], region=r_nav(), bottommost=True)
        if hit:
            log("  点击底部导航「免费听」@ ({}, {})".format(hit[1], hit[2]))
            safe_click(hit[1], hit[2], "导航-免费听")
            time.sleep(2.2)
            continue

        # 文本没识别到：导航栏位置固定，用比例兜底点一次
        if NAV_FALLBACK_BY_RATIO and not ratio_used:
            ratio_used = True
            x = int(SCREEN_W * NAV_FREE_RATIO[0])
            y = int(SCREEN_H * NAV_FREE_RATIO[1])
            log("  未识别到导航文字，按比例点一次「免费听」({}, {})".format(x, y))
            safe_click(x, y, "导航-免费听(比例兜底)")
            time.sleep(2.2)
            continue

        log("  页面还没就绪，等待重扫（{}/{}）".format(i + 1, max_try))
        time.sleep(1.5)
    return is_free_page()


# ============== 阶段三B：进「发现」页并点「免」字入口 ==============

def goto_discover(max_try=3):
    """停在「发现」tab。点导航是幂等的，已在该页再点一次也无害。"""
    log("阶段三B：切到「发现」页")
    for i in range(max_try):
        texts = scan()
        hit = pick(texts, ["发现"], region=r_nav(), bottommost=True)
        if hit:
            log("  点击底部导航「发现」@ ({}, {})".format(hit[1], hit[2]))
            safe_click(hit[1], hit[2], "导航-发现")
            time.sleep(1.8)
            return True
        if NAV_FALLBACK_BY_RATIO:
            x = int(SCREEN_W * NAV_DISCOVER_RATIO[0])
            y = int(SCREEN_H * NAV_DISCOVER_RATIO[1])
            log("  未识别到导航文字，按比例点「发现」({}, {})".format(x, y))
            safe_click(x, y, "导航-发现(比例兜底)")
            time.sleep(1.8)
            return True
        log("  等待重扫（{}/{}）".format(i + 1, max_try))
        time.sleep(1.2)
    return False


def find_mian_xy(texts=None):
    """
    定位「免」入口，返回 (x, y, 依据)。
    这个入口有两种形态，圆心位置不同：
      无时长：只有一颗灰边圆徽标，圆心 (1320, 232) = 0.9167W / 0.0725H
      有时长：变成「绿色免圆环 + 15:21」一颗胶囊，右边缘不动，圆心被挤到左边
              (1194, 233) = 0.829W，而且此时点进去是「免费听」页（不是直接进广告）
    所以优先按时间文字反推圆心，认不到再退回固定坐标。
    """
    if texts is None:
        texts = scan()
    r = r_top_right()

    # 1) 先找「免 XX:XX」里的时长，反推圆心
    for t, x, y in texts:
        if not in_rect(x, y, r):
            continue
        m = re.search(r"\d{1,2}:\d{2}(?::\d{2})?", t)
        if not m:
            continue
        n = len(m.group(0))
        # 实测：n=5（"15:21"）时圆心在时间文字中心左侧 98px = 0.068W
        off = SCREEN_W * (0.038 + 0.006 * n)
        bx = int(x - off)
        bx = min(max(bx, int(SCREEN_W * 0.55)), int(SCREEN_W * 0.96))
        return (bx, int(y), "有时长'{}'反推绿免".format(t))

    # 2) OCR 直接把「免」认出来了
    hit = pick(texts, ["免"], region=r, guard=True)
    if hit:
        return (hit[1], hit[2], "OCR 认出'免'")

    # 3) 都没有 → 无时长形态的固定坐标
    return (int(SCREEN_W * MIAN_X_RATIO), int(SCREEN_H * MIAN_Y_RATIO), "无时长默认坐标")


def click_mian():
    """点发现页右上角的「免」入口（自动适配有/无时长两种形态）"""
    texts = scan()
    x, y, how = find_mian_xy(texts)
    log("  点「免」入口 @ ({}, {}) [{}]".format(x, y, how))
    safe_click(x, y, "免入口")
    time.sleep(1.8)
    return True


def wait_entry_result(timeout=14):
    """
    点完「免」后判断落到哪儿：
      "free" 跳到了免费听页（有时长时的行为），那里要点「加时长」才进广告
      "ad"   直接进了广告页（无时长时的行为）
      ""     都不是
    必须先判免费听页：那个页面的任务卡里有「点击浏览1秒」，
    会被广告特征误命中，顺序反了就会把免费听页当广告页。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        texts = scan()
        j = joined_of(texts)
        if "允许" in j and allow_app_jump(texts):
            continue
        if is_free_page(texts):
            log("  → 跳到了「免费听」页（还需点「加时长」）")
            return "free"
        if any(k in j for k in AD_HINTS):
            log("  → 直接进入广告页")
            return "ad"
        # 可能弹了「看广告」二次确认
        hit = pick(texts, ["看广告"], region=r_popup_btn())
        if hit:
            log("  命中二次确认「{}」".format(hit[0]))
            safe_click(hit[1], hit[2], "确认看广告")
            time.sleep(1.5)
            continue
        time.sleep(0.6)
    return ""


# ============== 阶段四：点广告入口 ==============

def tap_entry():
    """
    入口优先级：
      1) 右上角绿色「加时长」（主入口）
      2) 任务卡「看视频 +20分钟」行尾的「观看」（回落）
    返回命中的入口名，未命中返回空串。
    """
    texts = scan()
    j = joined_of(texts)

    hit = pick(texts, ["加时长"])
    if hit:
        log("  点击主入口「加时长」@ ({}, {})".format(hit[1], hit[2]))
        safe_click(hit[1], hit[2], "加时长")
        time.sleep(1.2)
        return "加时长"

    if "看视频" in j:
        hit = pick(texts, ["观看"])
        if hit:
            log("  「加时长」未命中 → 回落任务卡「观看」@ ({}, {})".format(hit[1], hit[2]))
            safe_click(hit[1], hit[2], "任务-观看")
            time.sleep(1.2)
            return "观看"

    log("  ⚠️ 没找到任何广告入口")
    return ""


def wait_ad_page(timeout=12):
    """等广告页出现；期间点掉二次确认和系统跳转确认"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        texts = scan()
        j = joined_of(texts)
        if "允许" in j and allow_app_jump(texts):
            continue
        if any(k in j for k in AD_HINTS):
            log("  已进入广告页")
            return True
        hit = pick(texts, ["看广告"], region=r_popup_btn())
        if hit:
            log("  命中二次确认「{}」".format(hit[0]))
            safe_click(hit[1], hit[2], "确认看广告")
            time.sleep(1.5)
            continue
        time.sleep(0.6)
    log("  ⚠️ 未检测到广告页")
    return False


# ============== 阶段五：广告页处理 ==============

# 系统「是否允许启动 XX 应用」弹窗
# 只认「允许」这一族按钮：绝不能点「不允许」，也不能把「打开元宝」这类任务文案当按钮
ALLOW_BTNS = ["允许本次启动", "仅此一次", "始终允许", "允许"]
DENY_WORDS = ["不允许", "禁止", "拒绝"]


def find_allow_btn(texts):
    for kw in ALLOW_BTNS:
        for t, x, y in texts:
            s = t.strip()
            if kw not in s:
                continue
            if any(d in s for d in DENY_WORDS):
                continue
            return (t, x, y)
    return None


def allow_app_jump(texts=None, timeout=0):
    """
    处理「是否允许启动淘宝」这类系统跳转确认：点「允许本次启动」。
    只在屏幕上出现「允许」时才会动作 —— 避免把普通页面上的「打开xx」当按钮点。
    timeout > 0 时会轮询等待弹窗出现。
    """
    def _try(ts):
        if "允许" not in joined_of(ts):
            return False
        hit = find_allow_btn(ts)
        if not hit:
            return False
        log("  系统跳转确认 → 点「{}」".format(hit[0]))
        safe_click(hit[1], hit[2], "允许本次启动")
        time.sleep(1.2)
        return True

    ts = texts if texts is not None else scan()
    if _try(ts):
        return True
    if timeout <= 0:
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.5)
        if _try(scan()):
            return True
    return False


def claim_reward(texts=None):
    """点掉奖励弹窗的领取按钮（有就点，没有就跳过）"""
    if texts is None:
        texts = scan()
    hit = pick(texts, REWARD_BTNS, region=r_popup_btn(), guard=True)
    if hit:
        log("  领奖 → 点击「{}」@ ({}, {})".format(hit[0], hit[1], hit[2]))
        safe_click(hit[1], hit[2], "领取奖励")
        time.sleep(1.0)
        return True
    return False


def pick_close_glyph(texts):
    """
    广告右上角的关闭按钮，OCR 常认成 ) / x / X 这类单字符（实测 1440 屏上是 (1167,269) 的 ")"）。
    这里按「区域 + 长度 ≤2 + 不含跳过/危险词」兜底找它。
    """
    r = r_top_right()
    for t, x, y in texts:
        s = t.strip()
        if not in_rect(x, y, r):
            continue
        if len(s) > 2 or "跳过" in s or is_danger(s):
            continue
        return (t, x, y)
    return None


def is_ad_page(texts=None):
    if texts is None:
        texts = scan()
    j = joined_of(texts)
    return (any(k in j for k in AD_HINTS)
            or "开会员免广告" in j
            or "点击卡片" in j
            or "已获得奖励" in j)


def leave_ad():
    """
    关闭广告页。实测这一层右上角有个圆形 ✕（(1343,268)），点了才回到「免费听」页；
    位置固定，先按坐标点，点不掉再按返回。
    """
    if not is_ad_page():
        return False

    x = int(SCREEN_W * AD_CLOSE_RATIO[0])
    y = int(SCREEN_H * AD_CLOSE_RATIO[1])
    log("  关闭广告页 → 点右上角 ✕ ({}, {})".format(x, y))
    safe_click(x, y, "广告关闭✕")
    time.sleep(1.5)

    if not is_ad_page():
        log("  已离开广告页")
        return True

    log("  ✕ 没生效，按一次返回兜底")
    press_back(1, 1.6)
    time.sleep(1.0)
    return not is_ad_page()


def wait_reward(timeout=6):
    """浏览返回后等奖励弹窗，返回是否出现"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        texts = scan()
        if any(k in joined_of(texts) for k in REWARD_HINTS):
            log("  检测到奖励提示")
            return True
        if pick(texts, REWARD_BTNS, region=r_popup_btn(), guard=True):
            return True
        time.sleep(0.6)
    return False


def handle_ad():
    """
    广告页总调度。返回 True = 检测到奖励提示（不代表时长一定变化）。
      互动广告：点「点击浏览N秒」→ 跳淘宝 → 等 12s → 返回 → 等奖励 → 领奖 → 返回离开
      普通广告：等够 SKIP_AFTER_SEC 再点「跳过」
    收尾统一按一次返回，App 会自动落到「免费听」页，那里读倒计时核对。
    """
    log("阶段四：处理广告")
    start = time.time()
    deadline = start + AD_TIMEOUT
    browsed = False

    while time.time() < deadline:
        texts = scan()
        if not texts:
            time.sleep(0.5)
            continue
        j = joined_of(texts)

        # A0. 系统跳转确认（"是否允许启动淘宝"）—— 不点允许，跳转会被拦下
        if "允许" in j and allow_app_jump(texts):
            continue

        # A. 奖励已到账
        if any(k in j for k in REWARD_HINTS):
            log("  检测到奖励提示")
            claim_reward(texts)
            time.sleep(1.0)
            leave_ad()
            return True

        # B. 互动广告：必须点进广告详情页浏览满 10 秒
        if AUTO_BROWSE_AD and not browsed:
            hit = pick(texts, ["点击浏览", "浏览10秒", "浏览5秒", "浏览3秒"],
                       region=r_ad_sheet())
            if hit:
                log("  互动广告 → 点击「{}」@ ({}, {})".format(hit[0], hit[1], hit[2]))
                safe_click(hit[1], hit[2], "点击浏览")
                browsed = True
                # 点完可能弹「是否允许启动淘宝」→ 不点允许就跳不过去
                allow_app_jump(timeout=5)
                log("  浏览 {} 秒后返回…".format(BROWSE_SECONDS))
                time.sleep(BROWSE_SECONDS)
                return_to_app()
                time.sleep(1.5)
                got = wait_reward(6)
                claim_reward()
                time.sleep(0.8)
                leave_ad()
                return got

        elapsed = time.time() - start

        # C. 普通广告：等够时间再点「跳过」
        if elapsed >= SKIP_AFTER_SEC:
            hit = pick(texts, ["跳过"], region=r_top_right())
            if hit:
                log("  广告结束 → 点击「{}」".format(hit[0]))
                click_skip(hit[1], hit[2], "跳过广告")
                time.sleep(1.5)
                claim_reward()
                time.sleep(0.8)
                leave_ad()
                return True

        # D. 兜底：广告右上角关闭图标（常被 OCR 认成单字符）
        if elapsed >= SKIP_AFTER_SEC + 8:
            hit = pick_close_glyph(texts)
            if hit:
                log("  兜底点击广告关闭图标「{}」".format(hit[0]))
                safe_click(hit[1], hit[2], "关闭广告")
                time.sleep(1.5)
                claim_reward()
                time.sleep(0.8)
                leave_ad()
                return True

        time.sleep(0.6)

    log("  ⚠️ 广告处理超时（{}s），按返回离开".format(AD_TIMEOUT))
    leave_ad()
    return False


# ============== 无人值守 ==============

def setup_unattended():
    if not KEEP_SCREEN_ON:
        return
    try:
        Device.keep_screen_on()
        log("已开启屏幕常亮")
    except Exception as e:
        log("屏幕常亮设置失败: {}".format(e))
    try:
        Device.wake_up()
    except Exception:
        pass


def ensure_awake():
    try:
        Device.wake_up()
    except Exception:
        pass


def restart_app():
    log("  重启 {} 复位".format(APP_NAME))
    try:
        system.open(APP_PACKAGE)
        time.sleep(4.0)
        skip_splash(4)
        dismiss_popups(2)
        ensure_free_page(3)
    except Exception as e:
        log("  重启异常: {}".format(e))


# ============== 主循环 ==============

def enter_ad():
    """
    进入广告页。优先用当前页面，别绕路：
      已在「免费听」页 → 直接点「加时长」（广告结束返回后就是停在这页）
      不在该页         → 去「发现」页点右上角「免」
    返回 True = 已经进到广告页。
    """
    texts = scan()
    if is_free_page(texts):
        log("  已在免费听页 → 直接点「加时长」")
        entry = tap_entry()
        if entry and wait_ad_page(12):
            return True
        log("  免费听页没进到广告，改去发现页点「免」")

    if not goto_discover():
        log("  ⚠️ 切不到发现页")
        return False
    if not click_mian():
        log("  ⚠️ 没点上「免」")
        return False

    kind = wait_entry_result()
    if kind == "ad":
        return True
    if kind == "free":
        entry = tap_entry()
        if not entry:
            log("  ⚠️ 免费听页没有可用入口")
            return False
        return wait_ad_page(12)
    log("  ⚠️ 点「免」后既没进广告也没跳免费听页")
    return False


def run_loop():
    global LAST_FREE_TIME
    log("\n" + "#" * 60)
    log("进入自动赚时长循环（上限 {} 轮）".format(MAX_LOOP_COUNT))
    log("#" * 60)

    success = 0
    idle = 0

    for i in range(1, MAX_LOOP_COUNT + 1):
        log("\n---- 第 {} 轮 ----".format(i))
        ensure_awake()
        dismiss_popups(1)

        # 若此刻就停在免费听页，顺手把基准时长刷新成当前值
        before = LAST_FREE_TIME
        cur_texts = scan()
        if is_free_page(cur_texts):
            t = read_free_time(cur_texts)
            if t >= 0:
                before = t
                LAST_FREE_TIME = t

        if not enter_ad():
            idle += 1
            log("  ⚠️ 进不去广告页（连续 {}/{}）".format(idle, MAX_IDLE_ROUNDS))
            if AUTO_RESTART_APP and idle % 2 == 0:
                restart_app()
            if idle >= MAX_IDLE_ROUNDS:
                break
            time.sleep(LOOP_INTERVAL)
            continue

        got_reward = handle_ad()

        # 广告结束后 App 会自动回到「免费听」页（用户实测），这里直接读数字核对，
        # 不再手动点导航去那个页面。
        return_to_app()
        time.sleep(1.2)
        claim_reward()      # 有的奖励弹窗是回到免费听页之后才弹的
        time.sleep(0.8)
        if VERIFY_BY_FREE_TIME:
            after = read_free_time()
            gained = (after - before) if (before >= 0 and after >= 0) else None
            if after >= 0:
                LAST_FREE_TIME = after
            else:
                log("  没读到免费听倒计时（可能还没回到该页）")
        else:
            after = -1
            gained = None

        if gained is not None and gained > 0:
            success += 1
            idle = 0
            log("  ✅ 第 {} 轮成功：{} → {}（+{} 秒），累计 {} 次".format(
                i, fmt(before), fmt(after), gained, success))
        elif got_reward:
            success += 1
            idle = 0
            log("  ✅ 第 {} 轮有到账提示，但倒计时未变（可能按分钟结算），计成功".format(i))
        else:
            idle += 1
            log("  ⚠️ 第 {} 轮未确认到账：{} → {}（连续 {}/{}）".format(
                i, fmt(before), fmt(after), idle, MAX_IDLE_ROUNDS))

        if idle >= MAX_IDLE_ROUNDS:
            log("连续 {} 轮无收益，可能当日额度已用完，退出".format(MAX_IDLE_ROUNDS))
            break

        time.sleep(LOOP_INTERVAL)

    return success


# ============== 体检模式（第一次真机调试用） ==============

def dump_texts(texts, title="屏幕文字"):
    log("  {}：{} 条".format(title, len(texts)))
    for t, x, y in texts:
        log("      ({:4d},{:4d})  {}".format(x, y, t))


def self_test():
    """
    只做识别、不进循环。确认两件事：
      1) 能否切到「发现」页
      2) 点「免」之后落到哪儿（广告页 / 免费听页）
    免费听页不作为入口，广告结束按返回会自动回到那儿，所以这里不检测它。
    """
    log("🔍 体检模式（SELF_TEST=True）：只识别，不跑循环")
    init_screen()
    try:
        Ocr.set_engine(OCR_ENGINE)
        log("  OCR 引擎: {}".format(OCR_ENGINE))
    except Exception as e:
        log("  OCR 引擎设置失败（用默认）: {}".format(e))

    setup_unattended()
    launch_app()
    skip_splash(8)
    dismiss_popups(3)

    # ---- 1) 发现页 + 「免」字 ----
    goto_discover()
    d_texts = scan()
    dump_texts(d_texts, "发现页文字")
    mian = pick(d_texts, ["免"], region=r_top_right())
    mx = int(SCREEN_W * MIAN_X_RATIO)
    my = int(SCREEN_H * MIAN_Y_RATIO)
    nav_d = pick(d_texts, ["发现"], region=r_nav(), bottommost=True)

    log("-" * 46)
    log("  发现页导航   : {}".format(str(nav_d) if nav_d else "未命中 ❌"))
    log("  「免」OCR    : {}".format(
        str(mian) if mian else "认不出 → 退化为坐标 ({}, {})".format(mx, my)))

    # ---- 2) 试一次「免」入口，看落到哪个分支 ----
    if SELF_TEST_ENTER_AD:
        log("-" * 46)
        log("  试一次「免」入口（会真的进广告或跳免费听页）")
        goto_discover()
        if click_mian():
            kind = wait_entry_result(16)
            desc = {"ad": "直接进广告", "free": "跳到免费听页"}.get(kind, "都不是 ❌")
            log("  点「免」结果  : {}".format(desc))
            landed = scan()
            dump_texts(landed, "落地页文字")
            if kind == "ad":
                bh = pick(landed, ["点击浏览", "浏览10秒", "浏览5秒"], region=r_ad_sheet())
                log("  浏览按钮     : {}".format(str(bh) if bh else "未命中 ❌"))
            return_to_app()
        else:
            log("  ❌ 没点上「免」")

    log("-" * 46)
    log("体检结束。确认无误后把 SELF_TEST 改成 False 再跑真实循环")



# ============== 入口（AScript 约定：顶层直接执行） ==============

def main():
    global LAST_FREE_TIME
    log("🎵 {} 自动赚时长脚本启动".format(APP_NAME))
    log("   包名: {} | OCR: {} | 自动浏览广告: {}".format(
        APP_PACKAGE, OCR_ENGINE, AUTO_BROWSE_AD))
    try:
        init_screen()
        try:
            Ocr.set_engine(OCR_ENGINE)
            log("OCR 引擎已设置为 {}".format(OCR_ENGINE))
        except Exception as e:
            log("设置 OCR 引擎失败（用默认）: {}".format(e))

        setup_unattended()

        if not launch_app():
            log("❌ 启动失败，脚本终止")
            return

        skip_splash(8)
        dismiss_popups(3)

        # 不主动跳页：run_loop 每轮先看当前在哪。
        # 广告结束返回后 App 会停在「免费听」页，下一轮直接点「加时长」，不绕发现页。
        total = run_loop()

        log("=" * 60)
        log("✅ 脚本结束，共成功 {} 次".format(total))
    except Exception as e:
        log("❌ 脚本异常: {}".format(e))
        traceback.print_exc()
    finally:
        log("脚本退出")


if SELF_TEST:
    self_test()
else:
    main()
