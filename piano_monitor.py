"""
Piano room monitor (新校区琴房) - auto tabs, auto setup, staggered checks, auto time shift.

What it does:
  * Paste the booking link -> it opens 5 tabs for you.
  * On every tab it sets, in this order: 预计使用时段 = 2 hours long
    (e.g. 10:00-11:00 -> 10:00-12:00), then 预约时长 = 2小时, then 预约琴型 = 公用琴房.
  * Checks are staggered: a new check starts on the next tab every
    (check time / number of tabs) seconds.
  * Each check: click 自动分配查询 -> read 剩余可用数
      - remaining == TARGET (exactly): click 开始自动分配 and stop everything
      - otherwise: click 关闭
  * If the page says "预计开始时间不能小于当前日期", that tab opens the time
    picker and moves start and end time 5 minutes forward, then carries on.

Run (Mac):
  python3 piano_monitor.py
It opens its own Chrome window (close your normal Chrome first). The first time,
log in to the school site in that window; the login is remembered next time.
"""

import asyncio
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta

from playwright.async_api import async_playwright

# ====== texts on the page (change only if the wording changes) ======
FIND_BUTTON = "text=自动分配查询"
CLOSE_BUTTON = "text=关闭"
BOOK_BUTTON = "text=开始自动分配"
REMAINING_LABEL = "剩余可用数"
EXPIRED_TEXT = "预计开始时间不能小于当前日期"
TIME_FIELD = "text=预计使用时段"
PICKER_TITLE = "请选择预计使用时段"
PICKER_OK = "text=确定"
PICKER_CANCEL = "text=取消"
LABEL_TIME = "预计使用时段"
LABEL_DURATION = "预约时长"
LABEL_TYPE = "预约琴型"
LATEST_END = "22:00"         # a booking must end by this time
DURATION_OPTIONS = [2, 1.5, 1, 0.5]   # hours, longest first (the options of 预约时长)
SETUP_BUFFER_SECONDS = 60    # setting up takes a while, so start slots a little in the future
WANT_TYPE = "公用琴房"
SHIFT_MINUTES = 5
SHIFT_LEAD_SECONDS = 10      # start moving a tab's time this many seconds BEFORE its start time
DEFAULT_TABS = 5
PROFILE_DIR = os.path.expanduser("~/piano-chrome-profile")             # Google Chrome: keeps your login
PROFILE_DIR_CHROMIUM = os.path.expanduser("~/piano-chromium-profile")  # built-in browser: keeps your login
FIRST_GUESS_CYCLE = 5.0      # seconds; only used until the real time is measured
MIN_STEP = 0.8               # never start checks faster than this
# =====================================================================

TIME_PATTERN = r"^(\d{2}|:)$"
DURATION_PATTERN = r"^([\d.]+\s*(小时)?|小时)$"
TYPE_PATTERN = r".+"

MODES = ["click", "locator", "scrolltop", "jsevents", "mouse", "touch", "wheel"]   # ways of moving a picker wheel
WORKING_MODE = None                            # remembered once one works
DEBUG_PICKER = False                           # prints what the picker code sees (setup only)
PICKER_LOCK = None                             # created in main()

# Lists the options of the open picker (position, text, orange = currently selected).
GATHER_JS = r"""
([title, pattern, visibleOnly]) => {
  const re = new RegExp(pattern);
  document.querySelectorAll('[data-pm]').forEach(x => x.removeAttribute('data-pm'));
  const leaves = [...document.querySelectorAll('body *')].filter(e =>
    e.children.length === 0 && (e.textContent || '').trim() !== '');
  const titles = leaves.filter(e => (e.textContent || '').trim().includes(title));
  if (!titles.length) return null;
  const titleEl = titles[titles.length - 1];
  let root = titleEl;
  while (root && root !== document.body) {
    const t = root.textContent || '';
    if (t.includes('取消') && t.includes('确定') && root.getBoundingClientRect().height > 200) break;
    root = root.parentElement;
  }
  if (!root) root = document.body;
  const ty = titleEl.getBoundingClientRect().bottom;
  const out = [];
  for (const e of leaves) {
    if (!root.contains(e)) continue;
    const text = (e.textContent || '').trim();
    if (text === '取消' || text === '确定') continue;
    if (!re.test(text)) continue;
    const r = e.getBoundingClientRect();
    if (r.width === 0 || r.height === 0 || r.top <= ty) continue;
    if (visibleOnly) {
      const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
      if (!top || !(e.contains(top) || top.contains(e))) continue;
    }
    const c = (getComputedStyle(e).color.match(/\d+/g) || [0, 0, 0]).map(Number);
    const orange = c[0] > 170 && c[1] > 60 && c[1] < 175 && c[2] < 115 && (c[0] - c[2]) > 90;
    const id = out.length;
    e.setAttribute('data-pm', String(id));
    out.push({id: id, t: text, x: r.left + r.width / 2, y: r.top + r.height / 2, h: r.height, orange: orange});
  }
  return out;
}
"""


# Scrolls the wheel's scrollable container so option `id` sits on the highlighted row (if it scrolls).
SCROLL_JS = r"""
(arg) => {
  const el = document.querySelector('[data-pm="' + arg.id + '"]');
  if (!el) return 'no-element';
  let c = el.parentElement;
  while (c && c !== document.body) {
    const oy = getComputedStyle(c).overflowY;
    if (c.scrollHeight > c.clientHeight + 4 && (oy === 'auto' || oy === 'scroll' || oy === 'hidden')) break;
    c = c.parentElement;
  }
  if (!c || c === document.body) return 'no-scroll-container';
  const r = el.getBoundingClientRect();
  const delta = (r.top + r.height / 2) - arg.ys;
  c.scrollTop += delta;
  c.dispatchEvent(new Event('scroll', {bubbles: true}));
  return 'scrolled ' + Math.round(delta);
}
"""

# Sends a full mouse/pointer click sequence to option `id` (works even if it is off screen).
EVENT_JS = r"""
(id) => {
  const el = document.querySelector('[data-pm="' + id + '"]');
  if (!el) return 'no-element';
  const r = el.getBoundingClientRect();
  const o = {bubbles: true, cancelable: true, view: window,
             clientX: r.left + r.width / 2, clientY: r.top + r.height / 2};
  for (const t of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
    const E = t.startsWith('pointer') && window.PointerEvent ? PointerEvent : MouseEvent;
    el.dispatchEvent(new E(t, o));
  }
  return 'sent';
}
"""


def alert():
    if os.system("afplay /System/Library/Sounds/Glass.aiff >/dev/null 2>&1") != 0:
        print("\a\a\a")


def add_minutes(hhmm, minutes):
    h, m = map(int, hhmm.split(":"))
    total = (h * 60 + m + minutes) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def median(values):
    v = sorted(values)
    return v[len(v) // 2]


def to_min(hhmm):
    h, m = map(int, hhmm.split(":"))
    return h * 60 + m


def from_min(total):
    total %= 24 * 60
    return f"{total // 60:02d}:{total % 60:02d}"


LATEST_END_MIN = to_min(LATEST_END)
LAST_START_MIN = LATEST_END_MIN - int(round(min(DURATION_OPTIONS) * 60))     # 21:30


def next_slot_start(dt):
    """The next 5-minute mark strictly after `dt`  (20:00 -> 20:05, 20:32 -> 20:35)."""
    return from_min((dt.hour * 60 + dt.minute) // 5 * 5 + 5)


def plan_for_start(start):
    """(start, end, duration text) with the LONGEST duration that still ends by 22:00,
    e.g. 20:05 -> 21:35 (1.5小时), 20:35 -> 21:35 (1小时), 21:05 -> 21:35 (0.5小时).
    None if not even the shortest duration fits."""
    s0 = to_min(start)
    for d in DURATION_OPTIONS:
        minutes = int(round(d * 60))
        if s0 + minutes <= LATEST_END_MIN:
            return start, from_min(s0 + minutes), f"{d:g}"
    return None


def as_number(text):
    m = re.match(r"\d+(\.\d+)?", text.strip())
    return float(m.group()) if m else None


async def dump_html(page, tab_no):
    name = f"debug_tab{tab_no}.html"
    with open(name, "w", encoding="utf-8") as f:
        f.write(await page.content())
    print(f"tab {tab_no}: saved {name} (page HTML, for debugging)")


async def close_picker(page):
    try:
        await page.locator(PICKER_CANCEL).last.click(timeout=1500)
    except Exception:
        pass


# ---------------------------------------------------------------- wheel pickers

def build_columns(items):
    """Group picker options into columns by x position; keep columns with 2+ different options."""
    items = sorted(items, key=lambda i: i["x"])
    cols = []
    for it in items:
        if cols and abs(it["x"] - cols[-1][-1]["x"]) < 40:
            cols[-1].append(it)
        else:
            cols.append([it])
    return [sorted(c, key=lambda i: i["y"]) for c in cols if len({i["t"] for i in c}) >= 2]


def row_height(col):
    ys = [i["y"] for i in col]
    diffs = [b - a for a, b in zip(ys, ys[1:]) if b - a > 5]
    return median(diffs) if diffs else 44.0


def selection_y(items, col, cols):
    """y of the highlighted row: the orange option, or a lone item like ':' / '小时'."""
    orange = [i["y"] for i in col if i["orange"]] or [i["y"] for i in items if i["orange"]]
    if orange:
        return median(orange)
    in_cols = set(id(i) for c in cols for i in c)
    singles = [i["y"] for i in items if id(i) not in in_cols]
    return median(singles) if singles else None


def matches(text, target):
    return re.fullmatch(rf"{re.escape(target)}\s*(小时)?", text) is not None


async def drag(page, mode, x, y0, y1):
    steps = 12
    if mode == "mouse":
        await page.mouse.move(x, y0)
        await page.mouse.down()
        for i in range(1, steps + 1):
            await page.mouse.move(x, y0 + (y1 - y0) * i / steps)
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.25)          # hold still so there is no flick momentum
        await page.mouse.up()
    elif mode == "touch":
        cdp = await page.context.new_cdp_session(page)
        try:
            await cdp.send("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 1})
            await cdp.send("Input.dispatchTouchEvent",
                           {"type": "touchStart", "touchPoints": [{"x": x, "y": y0}]})
            for i in range(1, steps + 1):
                await cdp.send("Input.dispatchTouchEvent",
                               {"type": "touchMove",
                                "touchPoints": [{"x": x, "y": y0 + (y1 - y0) * i / steps}]})
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.25)
            await cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        finally:
            try:
                await cdp.send("Emulation.setTouchEmulationEnabled", {"enabled": False})
                await cdp.detach()
            except Exception:
                pass
    elif mode == "wheel":
        await page.mouse.move(x, y0)
        await page.mouse.wheel(0, -(y1 - y0))


async def locator_click(page, tgt, sel, ys, rows):
    """Let Playwright scroll the option into view and click it - only if it then really
    sits on the wheel (never click somewhere else by accident)."""
    loc = page.locator(f'[data-pm="{tgt["id"]}"]')
    try:
        await loc.scroll_into_view_if_needed(timeout=2000)
        box = await loc.bounding_box()
        if not box:
            return False
        cy = box["y"] + box["height"] / 2
        ref = ys
        if sel is not None:
            sbox = await page.locator(f'[data-pm="{sel["id"]}"]').bounding_box()
            if sbox:
                ref = sbox["y"] + sbox["height"] / 2
        if abs(cy - ref) > 2.5 * rows:
            return False
        await page.mouse.click(box["x"] + box["width"] / 2, cy)
        return True
    except Exception:
        return False


async def wheel_select(page, title, pattern, which, target, visible_only=False, hint=None,
                       expect_cols=1):
    """Move one picker column until `target` sits on the highlighted row.
    hint = 'top' / 'bottom': if the option is not in the list yet (long lists may only
    load the options near the highlighted row), first move toward that end of the list."""
    global WORKING_MODE
    modes = list(MODES)
    if WORKING_MODE in modes:
        modes.remove(WORKING_MODE)
        modes.insert(0, WORKING_MODE)

    for mode in modes:
        prev = None                      # (kind, value) seen before the last move
        waited = 0
        for _ in range(40):
            items = await page.evaluate(GATHER_JS, [title, pattern, visible_only])
            if not items and visible_only:
                items = await page.evaluate(GATHER_JS, [title, pattern, False])
            if not items:
                raise RuntimeError(f"picker '{title}' not found")
            cols = build_columns(items)
            if len(cols) < expect_cols and waited < 12:
                waited += 1                      # e.g. the minute wheel is being rebuilt: wait
                await asyncio.sleep(0.4)
                continue
            if not cols:
                raise RuntimeError("no picker columns found")
            if which == "any":           # the column that contains the wanted option
                col = next((c for c in cols if any(matches(i["t"], target) for i in c)), None)
                if col is None:
                    col = max(cols, key=len)
            else:
                col = cols[0] if which == "first" else cols[-1]
            tgt = next((i for i in col if matches(i["t"], target)), None)
            ys = selection_y(items, col, cols)
            if ys is None:
                raise RuntimeError("could not find the highlighted row")
            rows = row_height(col)
            x = sum(i["x"] for i in col) / len(col)
            sel = next((i for i in col if i["orange"]), None)
            selected = tuple(i["t"] for i in col if i["orange"])

            if tgt is None:
                direction = hint
                target_n = as_number(target)
                if direction not in ("top", "bottom") and target_n is not None:
                    nums = [v for v in (as_number(t) for t in selected) if v is not None]
                    if nums:
                        direction = "top" if target_n < nums[0] else "bottom"
                if direction not in ("top", "bottom"):
                    raise RuntimeError(f"option '{target}' not found in the picker")
                kind, value = "hint", selected
                sign = 1 if direction == "bottom" else -1   # +1: look at options further down
                dy = -sign * 3 * rows
            else:
                dy = ys - tgt["y"]
                kind, value = "tgt", tgt["y"]
                if abs(dy) < rows * 0.35:                 # already on the highlighted row
                    if prev is not None and WORKING_MODE != mode:
                        print(f"  (picker wheel works with mode '{mode}')")
                        WORKING_MODE = mode
                    return

            if DEBUG_PICKER:
                print(f"  [{title}] mode={mode} target={target} found={tgt is not None} "
                      f"selected={list(selected)} dy={dy:.0f} row={rows:.0f}")

            if tgt is None and mode in ("locator", "scrolltop", "jsevents"):
                break                                     # these modes need the option to exist
            if prev is not None and prev[0] == kind:
                if (kind == "tgt" and abs(value - prev[1]) < 4) or (kind == "hint" and value == prev[1]):
                    break                                 # nothing moved -> try next mode
            prev = (kind, value)

            if mode == "click":
                if tgt is None:
                    side = [i for i in col if 0.35 * rows < (i["y"] - ys) * sign <= 2.5 * rows]
                    if not side:
                        break
                    step = max(side, key=lambda i: (i["y"] - ys) * sign)
                else:
                    # click the visible option closest to the target; repeat until we are there
                    near = [i for i in col if 0.35 * rows < abs(i["y"] - ys) <= 2.5 * rows]
                    if not near:
                        break
                    step = min(near, key=lambda i: abs(i["y"] - tgt["y"]))
                    if abs(step["y"] - tgt["y"]) >= abs(dy):
                        break                             # clicking cannot get closer
                await page.mouse.click(x, step["y"])
            elif mode == "locator":
                if not await locator_click(page, tgt, sel, ys, rows):
                    break
            elif mode == "scrolltop":
                res = await page.evaluate(SCROLL_JS, {"id": tgt["id"], "ys": ys})
                if not str(res).startswith("scrolled"):
                    break
            elif mode == "jsevents":
                await page.evaluate(EVENT_JS, tgt["id"])
            else:
                chunk = max(-3 * rows, min(3 * rows, dy))
                await drag(page, mode, x, ys, ys + chunk)
            await asyncio.sleep(0.6)
    raise RuntimeError(f"could not move the picker wheel to '{target}'")


# ---------------------------------------------------------------- time helpers

async def read_range(page):
    """Last 'HH:MM 至 HH:MM' on the page = the one in the open picker."""
    body = re.sub(r"\s+", " ", await page.inner_text("body"))
    found = re.findall(r"(\d{2}:\d{2})\s*至\s*(\d{2}:\d{2})", body)
    return found[-1] if found else None


async def read_main_range(page):
    body = re.sub(r"\s+", " ", await page.inner_text("body"))
    m = re.search(r"(\d{2}:\d{2})\s*至\s*(\d{2}:\d{2})", body)
    return (m.group(1), m.group(2)) if m else None


async def settle(page):
    """Changing the hour refreshes the minute wheel (it jumps back to the old minute).
    Wait until the minute wheel stops changing before touching it."""
    last = None
    for _ in range(12):
        items = await page.evaluate(GATHER_JS, [PICKER_TITLE, TIME_PATTERN, False]) or []
        cols = build_columns(items)
        cur = tuple((i["t"], round(i["y"])) for i in cols[-1] if i["orange"]) if len(cols) >= 2 else None
        if cur is not None and cur == last:
            return
        last = cur
        await asyncio.sleep(0.4)


async def pick_time(page, old_time, new_time):
    """Switch to the start or end time (by its current text), then pick the new time."""
    try:
        await page.get_by_text(old_time, exact=True).last.click(timeout=2000)
    except Exception:
        pass
    await asyncio.sleep(0.4)
    hh, mm = new_time.split(":")
    await wheel_select(page, PICKER_TITLE, TIME_PATTERN, "first", hh, expect_cols=2)
    await asyncio.sleep(0.7)                     # the minute wheel refreshes and rolls after the hour changes
    for _ in range(3):
        await settle(page)
        await wheel_select(page, PICKER_TITLE, TIME_PATTERN, "last", mm, expect_cols=2)
        await asyncio.sleep(1.0)                 # let any late refresh of the minute wheel finish
        rng = await read_range(page)
        if DEBUG_PICKER:
            print(f"  [time] wanted {new_time}, header now shows {rng}")
        if rng and new_time in rng:
            return


# ---------------------------------------------------------------- automatic setup

async def click_center(page, label):
    """Mouse-click the centre of the first visible element showing `label`."""
    pos = await page.evaluate("""(label) => {
        const els = [...document.querySelectorAll('body *')].filter(e =>
            e.children.length === 0 && (e.textContent || '').includes(label));
        for (const e of els) {
            const r = e.getBoundingClientRect();
            if (r.width > 0 && r.height > 0) return [r.left + r.width / 2, r.top + r.height / 2];
        }
        return null;
    }""", label)
    if not pos:
        raise RuntimeError(f"'{label}' is not visible on the page")
    await page.mouse.click(pos[0], pos[1])


async def open_picker(page, label):
    """Click the field called `label` and wait until its picker is open. Some labels do not
    accept a normal click (the page passes the click to the element around them), so
    try a normal click, a forced click, then a plain mouse click on the label."""
    loc = page.locator(f"text={label} >> visible=true").first

    async def is_open():
        return await page.locator(f"{PICKER_OK} >> visible=true").count() > 0

    attempts = [
        lambda: loc.click(timeout=2500),
        lambda: loc.click(timeout=2500, force=True),
        lambda: click_center(page, label),
    ]
    for attempt in attempts:
        try:
            await attempt()
        except Exception:
            pass
        await asyncio.sleep(0.8)
        if await is_open():
            return
    raise RuntimeError(f"could not open the '{label}' picker (clicking the label does nothing)")


async def page_text(page):
    return re.sub(r"\s+", " ", await page.inner_text("body"))


async def check_duration(page, dur_text):
    return re.search(rf"{LABEL_DURATION}\s*{re.escape(dur_text)}\s*小时", await page_text(page)) is not None


async def read_duration(page):
    m = re.search(rf"{LABEL_DURATION}\s*([\d.]+)\s*小时", await page_text(page))
    return float(m.group(1)) if m else None


async def check_type(page):
    return re.search(rf"{LABEL_TYPE}\s*{WANT_TYPE}", await page_text(page)) is not None


async def set_choice(page, label, pattern, target, which="first", visible_only=False, check=None,
                     hint=None):
    """Open the picker behind `label`, choose `target`, confirm. Verifies the page really
    shows the new value; retries up to 3 times, then tries clicking the option text directly."""
    last_err = None
    for attempt in range(1, 4):
        try:
            await open_picker(page, label)
            await asyncio.sleep(0.5)
            await wheel_select(page, label, pattern, which, target, visible_only, hint)
            await page.locator(PICKER_OK).last.click(timeout=5000)
            await asyncio.sleep(0.9)
            if check is None or await check(page):
                return
            last_err = RuntimeError(f"page still does not show {target}")
        except Exception as e:
            last_err = e
            await close_picker(page)
            await asyncio.sleep(0.6)
        if DEBUG_PICKER:
            print(f"  [{label}] attempt {attempt} failed: {str(last_err)[:110]}")

    # last resort: click the option's text directly
    try:
        await open_picker(page, label)
        await asyncio.sleep(0.5)
        await page.get_by_text(target, exact=True).last.click(timeout=3000)
        await asyncio.sleep(0.6)
        await page.locator(PICKER_OK).last.click(timeout=5000)
        await asyncio.sleep(0.9)
        if check is None or await check(page):
            return
    except Exception:
        await close_picker(page)
    raise last_err


async def set_range_exact(tab_no, page, new_s, new_e):
    """Open 预计使用时段 and make it exactly new_s - new_e (start first, then the end if needed)."""
    if await read_main_range(page) == (new_s, new_e):
        return
    await open_picker(page, LABEL_TIME)
    await page.wait_for_selector(f"text={PICKER_TITLE}", timeout=5000)
    await asyncio.sleep(0.8)
    current = await read_range(page)
    if not current:
        raise RuntimeError("could not read the time range in the picker")
    old_s, old_e = current
    if old_s != new_s:
        await pick_time(page, old_s, new_s)
    rng = await read_range(page)
    if rng and rng[1] != new_e:              # the end time did not follow the start time
        await pick_time(page, rng[1], new_e)
        rng = await read_range(page)
    if rng != (new_s, new_e):
        raise RuntimeError(f"time picker ended at {rng}, wanted {(new_s, new_e)}")
    await page.locator(PICKER_OK).last.click(timeout=5000)
    await asyncio.sleep(0.7)


async def apply_plan(tab_no, page, new_s, new_e, dur_text):
    """Set time range + duration. The site refuses numbers when the range is SHORTER than the
    duration, so: a longer duration -> range first; a shorter duration -> duration first."""
    new_dur = float(dur_text)
    cur_dur = await read_duration(page)
    same_dur = cur_dur is not None and abs(cur_dur - new_dur) < 1e-9

    async def do_duration():
        if same_dur:
            return
        await set_choice(page, LABEL_DURATION, DURATION_PATTERN, dur_text,
                         check=lambda p: check_duration(p, dur_text))

    if cur_dur is None or new_dur >= cur_dur:
        await set_range_exact(tab_no, page, new_s, new_e)
        await do_duration()
    else:
        await do_duration()
        await set_range_exact(tab_no, page, new_s, new_e)


async def plan_applied(page, new_s, new_e, dur_text):
    dur = await read_duration(page)
    return (await read_main_range(page) == (new_s, new_e)
            and dur is not None and abs(dur - float(dur_text)) < 1e-9)


async def setup_tab(tab_no, page, plan):
    global DEBUG_PICKER
    new_s, new_e, dur_text = plan
    async with PICKER_LOCK:
        await page.bring_to_front()
        DEBUG_PICKER = True
        failed = []

        async def step(name, coro):
            try:
                await coro
            except Exception as e:
                failed.append(name)
                print(f"tab {tab_no}: could not set {name}: {str(e)[:150]}")
                await close_picker(page)
                await asyncio.sleep(0.5)

        await step("time range / duration", apply_plan(tab_no, page, new_s, new_e, dur_text))
        # 公用琴房 is the 9th of 11 options, i.e. third from the bottom -> hint="bottom"
        await step("room type", set_choice(page, LABEL_TYPE, TYPE_PATTERN, WANT_TYPE, "any", False,
                                           check=check_type, hint="bottom"))

        body = await page_text(page)
        rng = await read_main_range(page)
        dur = await read_duration(page)
        ok_rng = rng == (new_s, new_e)
        ok_dur = dur is not None and abs(dur - float(dur_text)) < 1e-9
        ok_type = re.search(rf"{LABEL_TYPE}\s*{WANT_TYPE}", body) is not None
        shown_type = re.search(rf"{LABEL_TYPE}\s*(\S+)", body)
        rng_txt = f"{rng[0]}-{rng[1]}" if rng else "?"
        dur_txt = f"{dur:g}小时" if dur is not None else "?"
        print(f"tab {tab_no}: time {rng_txt} {'OK' if ok_rng else 'CHECK (wanted ' + new_s + '-' + new_e + ')'}"
              f" | duration {dur_txt} {'OK' if ok_dur else 'CHECK (wanted ' + dur_text + '小时)'}"
              f" | room type {'OK' if ok_type else 'CHECK'} (page shows: "
              f"{shown_type.group(1) if shown_type else '?'})")
        DEBUG_PICKER = False
        if failed:
            await dump_html(page, tab_no)


# ---------------------------------------------------------------- monitoring

async def wait_state(page):
    """After clicking find: returns 'ok' (popup with numbers) or 'expired'."""
    ok = page.locator(f"text={REMAINING_LABEL}")
    expired = page.locator(f"text={EXPIRED_TEXT}")
    await ok.or_(expired).first.wait_for(timeout=10000)
    if await expired.count() > 0 and await expired.first.is_visible():
        return "expired"
    return "ok"


async def read_remaining(page):
    await asyncio.sleep(0.2)
    body = re.sub(r"\s+", " ", await page.inner_text("body"))
    m = re.search(rf"(\d+)\s*个\s*{REMAINING_LABEL}", body)
    return int(m.group(1)) if m else None


HELP = "Commands (type, then Enter):  p = pause | r = resume | t 60 = change target number | q = quit"


def start_input_thread(loop, queue):
    """Reads what you type in Terminal without blocking the checks."""
    def reader():
        while True:
            line = sys.stdin.readline()
            if line == "":
                break
            loop.call_soon_threadsafe(queue.put_nowait, line.strip())
    threading.Thread(target=reader, daemon=True).start()


async def command_listener(queue, control, stop, done):
    while True:
        line = await queue.get()
        if control["booked"]:            # after booking, any Enter finishes the program
            done.set()
            return
        words = line.lower().split()
        if not words:
            continue
        cmd = words[0]
        if cmd == "p":
            control["paused"] = True
            print("Pausing... (checks already in progress finish first)")
        elif cmd == "r":
            control["paused"] = False
            control["reread"] = True        # you may have changed times by hand: read them again
            print("Resumed.")
        elif cmd in ("t", "target") and len(words) > 1 and words[1].isdigit():
            control["target"] = int(words[1])
            print(f"Target changed: will book when remaining == {control['target']}")
        elif cmd.isdigit():
            control["target"] = int(cmd)
            print(f"Target changed: will book when remaining == {control['target']}")
        elif cmd == "q":
            print("Quitting...")
            stop.set()
            done.set()
            return
        else:
            print(HELP)


def slot_start_dt(hhmm, now):
    """Today's date + a 'HH:MM' start time (handles the midnight wrap)."""
    h, m = map(int, hhmm.split(":"))
    dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if dt - now < -timedelta(hours=12):
        dt += timedelta(days=1)
    elif dt - now > timedelta(hours=12):
        dt -= timedelta(days=1)
    return dt


def is_due(control, tab_no, seconds_early=0):
    """True when the system clock has reached (or is about to reach) this tab's start time.
    The website still shows numbers at that moment but refuses the booking, so the tab's
    time must be moved forward by the system clock, not only when the site complains."""
    slot = control["slots"].get(tab_no)
    if not slot:
        return False
    now = datetime.now()
    return now >= slot_start_dt(slot[0], now) - timedelta(seconds=seconds_early)


async def refresh_slots(chosen, control):
    """Read every tab's current start/end time from the page."""
    for n, pg in chosen:
        if n in control["dead"]:
            continue
        try:
            rng = await read_main_range(pg)
        except Exception:
            rng = None
        if rng:
            control["slots"][n] = rng
        else:
            control["slots"].pop(n, None)


def mark_finished(control, tab_no):
    control["dead"].add(tab_no)
    control["slots"].pop(tab_no, None)
    print(f"tab {tab_no}: no booking slot is left before {LATEST_END} - this tab is finished")


async def do_shift(tab_no, page, control, why):
    """Move this tab to its next time slot (one step later, keeping the group's rhythm) and set
    the longest 预约时长 that still ends by 22:00. If the tab is late it jumps several steps."""
    global DEBUG_PICKER
    now = datetime.now()
    step = control["shift_by"][tab_no]
    lead = timedelta(seconds=SHIFT_LEAD_SECONDS + 30)
    last_dt = now.replace(hour=LAST_START_MIN // 60, minute=LAST_START_MIN % 60, second=0, microsecond=0)

    slot = control["slots"].get(tab_no)
    if slot:
        start_dt = slot_start_dt(slot[0], now)
        cand = start_dt + timedelta(minutes=step)
        while cand <= now + lead:
            cand += timedelta(minutes=step)
    else:                                   # unknown slot: use the next free 5-minute mark
        start_dt = None
        cand = slot_start_dt(next_slot_start(now + timedelta(seconds=SETUP_BUFFER_SECONDS)), now)

    if cand > last_dt:                      # too late for a normal step: use the last possible slot
        cand = last_dt if (last_dt > now + lead and (start_dt is None or start_dt < last_dt)) else None
    plan = plan_for_start(cand.strftime("%H:%M")) if cand else None
    if plan is None:
        mark_finished(control, tab_no)
        return
    new_s, new_e, dur_text = plan

    async with PICKER_LOCK:
        await page.bring_to_front()
        DEBUG_PICKER = True
        try:
            ok = False
            for _ in range(2):
                await apply_plan(tab_no, page, new_s, new_e, dur_text)
                ok = await plan_applied(page, new_s, new_e, dur_text)
                if ok:
                    break
            if not ok:
                raise RuntimeError(f"the page does not show {new_s}-{new_e} / {dur_text}小时")
            control["slots"][tab_no] = (new_s, new_e)
            print(f"{time.strftime('%H:%M:%S')}  tab {tab_no}: {why}; now {new_s}-{new_e} ({dur_text}小时)")
        except Exception as e:
            await dump_html(page, tab_no)
            await close_picker(page)
            control["paused"] = True
            print(f"\n!!! Tab {tab_no} could not move to {new_s}-{new_e} ({str(e)[:100]}).")
            print("    PAUSED. Fix the time on that tab by hand, then type r + Enter to resume.\n")
            alert()
        finally:
            DEBUG_PICKER = False


async def check_once(tab_no, page, control, stop, stats):
    t0 = time.monotonic()
    try:
        await page.click(FIND_BUTTON, timeout=5000)
        state = await wait_state(page)

        if state == "expired":
            await do_shift(tab_no, page, control, "website says the start time expired")
            return

        remaining = await read_remaining(page)
        target = control["target"]

        if remaining == target and not stop.is_set() and is_due(control, tab_no, 1):
            # the system clock already reached this slot's start time: the site would refuse the booking
            print(f"tab {tab_no}: remaining = {remaining}, but this tab's start time has arrived - "
                  f"moving its time instead of booking")
            await page.locator(CLOSE_BUTTON).last.click(timeout=5000)
            await do_shift(tab_no, page, control, "system time reached the start time")
            return

        if remaining == target and not stop.is_set():
            stop.set()
            control["booked"] = True
            await page.locator(BOOK_BUTTON).last.click(timeout=5000)
            print(f"\n>>> Tab {tab_no}: remaining = {target}, clicked 开始自动分配! "
                  f"Check the page to confirm.\n")
            alert()
            return

        await page.locator(CLOSE_BUTTON).last.click(timeout=5000)

        cycle = time.monotonic() - t0
        stats["avg"] = 0.7 * stats["avg"] + 0.3 * cycle
        print(f"{time.strftime('%H:%M:%S')}  tab {tab_no}: remaining = {remaining}  "
              f"(check took {cycle:.1f}s)")
    except Exception as e:
        print(f"tab {tab_no}: error (will retry): {str(e)[:120]}")
        try:
            await page.locator(CLOSE_BUTTON).last.click(timeout=1500)
        except Exception:
            pass


async def main():
    global PICKER_LOCK
    PICKER_LOCK = asyncio.Lock()

    async with async_playwright() as p:
        # Opens its own browser window (no debug port needed). Uses Google Chrome if it is
        # installed, otherwise the Chromium that the launcher downloaded. The profile folder
        # keeps your login between runs.
        ctx = None
        last_error = None
        for channel, folder in (("chrome", PROFILE_DIR), (None, PROFILE_DIR_CHROMIUM)):
            try:
                options = dict(headless=False, no_viewport=True,
                               ignore_default_args=["--enable-automation"])
                if channel:
                    options["channel"] = channel
                ctx = await p.chromium.launch_persistent_context(folder, **options)
                print("Browser:", "Google Chrome" if channel else "built-in Chromium")
                break
            except Exception as e:
                last_error = e
        if ctx is None:
            print("Could not open a browser:", str(last_error)[:200])
            print("Close any other window of this tool and try again. If it keeps failing, "
                  "delete the folder ~/.piano-monitor and start again.")
            return

        url = ""
        while not url.startswith("http"):
            url = input("Paste the booking website link: ").strip()
        n_in = input(f"How many tabs? [{DEFAULT_TABS}]: ").strip()
        n_tabs = int(n_in) if n_in.isdigit() else DEFAULT_TABS

        async def open_tab(first_page=None):
            pg = first_page or await ctx.new_page()
            await pg.goto(url)
            return pg

        blank = ctx.pages[0] if ctx.pages else None     # reuse the empty tab Chrome starts with
        jobs = [open_tab(blank)] + [open_tab() for _ in range(n_tabs - 1)]
        pages = await asyncio.gather(*jobs)
        chosen = list(enumerate(pages, 1))
        input(f"\n{n_tabs} tabs opened. If you need to log in, log in on one tab and refresh the "
              f"others.\nPress Enter when the pages are ready...")

        if not chosen:
            print("No tabs to monitor.")
            return

        shift_by = {n: SHIFT_MINUTES for n, _ in chosen}
        auto = input(f"Set the time range, 预约时长 and 预约琴型 automatically? A booking must end by "
                     f"{LATEST_END}, so the longest allowed 预约时长 is used. [Y/n]: ").strip().lower()
        if auto != "n":
            # Staggering: two groups of tabs whose start times are 5 minutes apart. When a tab's
            # time arrives it jumps 10 minutes, so the groups always change 5 minutes apart and
            # while one group changes its time the other group keeps checking.
            group_b = set()
            if len(chosen) >= 2:
                ans = input("Stagger the tabs in 2 groups, 5 minutes apart, so only half of them "
                            "change time at once? [Y/n]: ").strip().lower()
                if ans != "n":
                    group_b = {n for k, (n, _) in enumerate(chosen) if k % 2 == 1}
                    for n, _ in chosen:
                        shift_by[n] = 2 * SHIFT_MINUTES

            base = next_slot_start(datetime.now() + timedelta(seconds=SETUP_BUFFER_SECONDS))
            plans = {n: plan_for_start(add_minutes(base, SHIFT_MINUTES if n in group_b else 0))
                     for n, _ in chosen}
            skipped = [n for n, _ in chosen if plans[n] is None]
            chosen = [(n, pg) for n, pg in chosen if plans[n] is not None]
            if skipped:
                print(f"No slot fits before {LATEST_END} for tab(s) {skipped} - they will not be used.")
            if not chosen:
                print(f"Too late: a booking must end by {LATEST_END} and the shortest one is "
                      f"{min(DURATION_OPTIONS):g}小时. Nothing to monitor.")
                return
            print("\nPlan (obeying the " + LATEST_END + " limit):")
            for n, _ in chosen:
                s0, e0, d0 = plans[n]
                print(f"  tab {n}: {s0}-{e0}  预约时长 {d0}小时" + ("   (group B)" if n in group_b else ""))
            print("\nSetting up the tabs one by one (don't touch the mouse/keyboard)...")
            for n, pg in chosen:
                await setup_tab(n, pg, plans[n])
        else:
            print("Automatic setup skipped: set the tabs yourself. (No staggering in this mode.)")

        target = int(input("\nBook when remaining rooms is EXACTLY: ").strip())

        loop = asyncio.get_running_loop()
        queue = asyncio.Queue()
        start_input_thread(loop, queue)

        stop = asyncio.Event()
        done = asyncio.Event()
        control = {"target": target, "paused": False, "booked": False, "shift_by": shift_by,
                   "slots": {}, "reread": False, "dead": set()}
        stats = {"avg": FIRST_GUESS_CYCLE}
        busy = {n: False for n, _ in chosen}
        tasks = []

        await refresh_slots(chosen, control)
        print("\nStart time of each tab: " + ", ".join(
            f"tab {n} {control['slots'][n][0]}" if n in control["slots"] else f"tab {n} ?" for n, _ in chosen))
        print("Each tab's time is moved forward by the SYSTEM clock "
              f"({SHIFT_LEAD_SECONDS}s before its start time).")
        print(f"\nMonitoring {len(chosen)} tab(s), staggered. "
              f"Will book when remaining == {target}.")
        print(HELP + "\n")
        listener = asyncio.create_task(command_listener(queue, control, stop, done))

        async def run(n, pg):
            busy[n] = True
            try:
                await check_once(n, pg, control, stop, stats)
            finally:
                busy[n] = False

        async def run_shift(n, pg):          # busy[n] was already set by the scheduler
            try:
                await do_shift(n, pg, control, "system time reached the start time")
            finally:
                busy[n] = False

        i = 0
        pause_announced = False
        while not stop.is_set():
            if all(n in control["dead"] for n, _ in chosen):
                print(f"\nNo booking slot is left before {LATEST_END}. Stopping.")
                stop.set()
                break
            if control["paused"]:
                if not pause_announced and not any(busy.values()):
                    print(f"\n== PAUSED ==  All checks stopped (target is {control['target']}).")
                    print("   Fix the time or anything else on the tabs by hand.")
                    print("   Then type r + Enter to resume, or t 60 + Enter to change the target.\n")
                    pause_announced = True
                await asyncio.sleep(0.3)
                continue
            pause_announced = False

            if control.get("reread"):
                control["reread"] = False
                await refresh_slots(chosen, control)

            # a tab whose start time has arrived (system clock) changes its time first
            due = next(((n, pg) for n, pg in chosen
                        if not busy[n] and n not in control["dead"]
                        and is_due(control, n, SHIFT_LEAD_SECONDS)), None)
            if due:
                busy[due[0]] = True
                tasks.append(asyncio.create_task(run_shift(*due)))
                await asyncio.sleep(0.2)
                continue

            started = False
            for _ in range(len(chosen)):
                n, pg = chosen[i % len(chosen)]
                i += 1
                if not busy[n] and n not in control["dead"]:
                    tasks.append(asyncio.create_task(run(n, pg)))
                    started = True
                    break
            if not started:
                await asyncio.sleep(0.2)
                continue
            await asyncio.sleep(max(MIN_STEP, stats["avg"] / len(chosen)))

        await asyncio.gather(*tasks)

        if control["booked"]:
            # keep Chrome open so you can confirm the booking on the page
            print("Check the page and finish the booking in Chrome.")
            print("Press Enter here when you are done (this closes Chrome).")
            await done.wait()
        listener.cancel()


if __name__ == "__main__":
    asyncio.run(main())
