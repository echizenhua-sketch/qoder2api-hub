"""qoder_scheduler.py —— 后台定时调度器 (Scheduler)

负责常驻后台自动执行：
1. 每日签到 (Daily Checkin)：每日 09:00 / 21:00 为所有账号自动签到领积分。
2. 福利包巡检：签到巡检时顺带刷新额度与套餐快照。
3. Token 保活 (Keepalive)：每日 22:00 集中刷新；另对剩余寿命不足 4 小时的
   账号在任意巡检中提前刷新（drt- / jrt- 按 token 前缀路由，PAT 兜底）。
4. 状态持久化与看板展示：暴露状态、执行记录、支持手动立即触发与开关切换。

状态落盘（本轮修复）：enabled / last_run_time / next_run_time / logs 以及
「启动补签当日已做过」标记写入 <账号目录>/scheduler/state.json。
  · 放在账号目录的**子目录**里：AccountPool.load() 只扫顶层 *.json
    （qoder_accounts.py:1441-1460），子目录不会被误当成账号；
  · accounts/* 已被 .gitignore 忽略，无需新增忽略规则；
  · 目的：进程重启不再产生重复领取——启动巡检每天最多补签一次
    （mark-before-act：先打标记落盘再动作，崩溃重启也不会重放）。
"""
import json
import os
import sys
import threading
import time

import qoder_tasks
from qoder_tasks import set_logger, run_batch_checkin, run_keepalive

# 状态文件相对账号目录的子路径（避免被 AccountPool 读取为账号 JSON）
STATE_SUBDIR = "scheduler"
STATE_FILE_NAME = "state.json"

# 巡回来由：启动自动 / 整点排程 / 手动触发
CYCLE_STARTUP = "startup"
CYCLE_HOUR = "hour"
CYCLE_MANUAL = "manual"


def _slot_to_tuple(value):
    """'HH:MM' -> (时, 分)；非法值返回 None。"""
    try:
        text = str(value or "").strip()
        hour, _, minute = text.partition(":")
        hour, minute = int(hour), int(minute)
    except (TypeError, ValueError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return (hour, minute)


def qoder_settings_default_slots():
    """代码内建的默认签到槽位（qoder_settings 不可用时的兜底）。"""
    return ["09:00", "10:01", "10:05", "10:15",
            "11:00", "12:00", "13:00", "14:00", "15:00",
            "16:00", "17:00", "18:00", "19:00", "20:00", "21:00"]


class Scheduler(object):
    def __init__(self, pool, state_dir=None):
        self.pool = pool
        # 对齐社区默认排程 (本地时区 24 小时制)
        #
        # 签到槽位：分钟粒度 (时, 分)。**09:00 已从"主槽位"移出**——官方每日
        # 活动批次在 10:00（UTC+8）刷新（campaign 的 start_at 实测精确为
        # 10:00:00，end_at 为次日 09:59:00）。09:00 去签拿到的是**上一批**
        # （状态已 CLAIMED），既领不到当天的分，还会因「上一批已领」的错误盖章
        # 把当天 10:00 之后的补签机会一并堵死。详见 qoder_accounts 的
        # current_checkin_window_start() 说明。
        #
        # 09:00 仍作为**普通补签槽位**保留：它是上一批关窗（09:59）前的最后
        # 补领机会，批次感知后它不再污染本轮盖章。
        #
        # 槽位可由面板「配置 → 每日签到时间」维护，落盘在 accounts/settings.json
        # （qoder_settings.CHECKIN_SLOTS_KEY），保存后由 reload_slots() 热更新，
        # 无需重启容器。
        self.keepalive_hours = [22]      # 每日 22:00 集中 Token 保活
        self._slots_lock = threading.RLock()
        self.checkin_slots = []
        self.checkin_primary_slots = []
        self.all_hours = list(self.keepalive_hours)
        self._load_slots()
        self._stop_event = threading.Event()
        self._thread = None
        self._run_lock = threading.Lock()
        # 状态目录：优先显式传入（测试）→ 账号池目录 → 环境变量 → 脚本同级 accounts/
        self.state_dir = (state_dir or getattr(pool, "dir", None)
                          or os.environ.get("ACCOUNTS_DIR")
                          or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          "accounts"))
        self._state_lock = threading.Lock()
        st = self._load_state()
        self.enabled = bool(st.get("enabled", True))   # 看板开关重启后仍生效
        self.last_run_time = st.get("last_run_time") or None
        self.next_run_time = None
        self.logs = [str(x) for x in (st.get("logs") or [])][-60:]
        # 「启动补签」当日标记：进程重启 N 次，同一天最多补签 1 次
        self.startup_claim_date = str(st.get("startup_claim_date") or "")
        self.last_cycle_date = str(st.get("last_cycle_date") or "")
        # 本轮已触发的槽位（防止同一分钟内重复触发）。跨日后要清空，
        # 否则 (9,0) 这类槽位第二天就再也命中不了。
        self._fired_slots = set()
        self._fired_slots_date = time.strftime("%Y-%m-%d")
        self._calc_next_fire()
        # 把任务层失败（死端点、上游结构变化）也打进看板日志。
        set_logger(self.log)
        if st:
            self.log("调度器状态已从 %s 恢复（enabled=%s，上次运行 %s）"
                     % (self._state_path(), self.enabled,
                        self.last_run_time or "-"))

    # -- 排程槽位（可由面板热更新） ----------------------------------------
    def _accounts_dir(self):
        """账号目录（槽位配置与账号池同处）。"""
        return (getattr(self.pool, "dir", None) or self.state_dir
                or os.environ.get("ACCOUNTS_DIR"))

    def _load_slots(self):
        """从 settings.json 读签到槽位；读不到就用代码内建默认值。"""
        slots = None
        primary = None
        accounts_dir = getattr(self.pool, "dir", None)
        if accounts_dir:
            try:
                import qoder_settings
                slots = qoder_settings.checkin_slots(accounts_dir)
                primary = qoder_settings.checkin_primary_slots(accounts_dir)
            except Exception:
                slots = None
        if not slots:
            slots = list(qoder_settings_default_slots())
        self.checkin_slots = [_slot_to_tuple(s) for s in slots]
        self.checkin_primary_slots = [_slot_to_tuple(s) for s in (primary or [])]
        # 主槽位必须是签到槽位的子集（配置层已裁剪，这里再兜一次）
        valid = set(self.checkin_slots)
        self.checkin_primary_slots = [s for s in self.checkin_primary_slots
                                      if s in valid]
        self.all_hours = sorted(set([h for h, _ in self.checkin_slots]
                                    + list(self.keepalive_hours)))

    def reload_slots(self):
        """重新从配置加载槽位（面板保存后调用；热更新，无需重启）。

        返回 {"checkin_slots": [...], "checkin_primary_slots": [...],
               "next_run_time": "..."}，供面板回显。
        """
        with self._slots_lock:
            self._load_slots()
            self._calc_next_fire()
            # 已触发过的槽位若被移除，允许重新触发；新增槽位自然可触发。
            # 注意 _fired_slots 混存两种形态：签到槽位 (时, 分) 与保活
            # ("ka", 时) —— 后者不参与裁剪。
            valid_slots = set(self.checkin_slots)
            self._fired_slots = {
                s for s in getattr(self, "_fired_slots", set())
                if (isinstance(s, tuple) and len(s) == 2 and s[0] == "ka")
                or s in valid_slots
            }
            self.log("排程槽位已更新：签到 %s（主槽位 %s），下次 %s"
                     % (self.slots_text(), self.primary_slots_text(),
                        self.next_run_time))
        return self.slots_view()

    def slots_text(self):
        return "、".join("%02d:%02d" % s for s in self.checkin_slots)

    def primary_slots_text(self):
        return "、".join("%02d:%02d" % s for s in self.checkin_primary_slots)

    def slots_view(self):
        """面板可读的槽位视图（字符串形态，便于回显与编辑）。"""
        with self._slots_lock:
            return {
                "checkin_slots": ["%02d:%02d" % s for s in self.checkin_slots],
                "checkin_primary_slots": ["%02d:%02d" % s
                                          for s in self.checkin_primary_slots],
                "keepalive_hours": list(self.keepalive_hours),
                "next_run_time": self.next_run_time,
            }

    # -- 状态持久化 ---------------------------------------------------------
    def _state_path(self):
        return os.path.join(self.state_dir, STATE_SUBDIR, STATE_FILE_NAME)

    def _load_state(self):
        """读状态文件；不存在/损坏都返回 {}（按全新进程处理）。"""
        try:
            with open(self._state_path(), encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_state(self):
        """原子落盘（tmp + os.replace，同账号文件的写法）。失败只写 stderr。

        注意：本函数**不得**调用 self.log()，否则与 log→_save_state 形成递归。
        """
        with self._state_lock:
            try:
                path = self._state_path()
                os.makedirs(os.path.dirname(path), exist_ok=True)
                tmp = path + ".tmp"
                data = {
                    "enabled": bool(self.enabled),
                    "last_run_time": self.last_run_time,
                    "next_run_time": self.next_run_time,
                    "startup_claim_date": self.startup_claim_date,
                    "last_cycle_date": self.last_cycle_date,
                    "logs": self.logs[-60:],
                    "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, ensure_ascii=False, indent=2)
                os.replace(tmp, path)
            except Exception as exc:
                try:
                    sys.stderr.write("[scheduler] 状态落盘失败: %s\n" % exc)
                    sys.stderr.flush()
                except Exception:
                    pass

    def log(self, msg):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        entry = "[%s] %s" % (ts, msg)
        self.logs.append(entry)
        if len(self.logs) > 60:
            self.logs = self.logs[-60:]
        # 日志即状态：顺带落盘，看板开关/上次运行时间在重启后仍然可见
        self._save_state()
        try:
            import qoder_proxy
            qoder_proxy.add_log_entry("[调度器] %s" % msg, tag="scheduler")
        except Exception:
            pass

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self.log("后台定时调度器已启动")

    def stop(self):
        self._stop_event.set()
        self.log("后台定时调度器已暂停")

    def _run_loop(self):
        # 启动后先休眠 10 秒等待主服务就绪，然后执行初次检查
        time.sleep(10)
        try:
            self._execute_cycle("启动初次初始化巡检", CYCLE_STARTUP)
        except Exception as exc:
            self.log("初次巡检异常: %s" % exc)

        while not self._stop_event.is_set():
            self._calc_next_fire()
            now = time.localtime()
            cur_hour = now.tm_hour
            cur_min = now.tm_min
            if self.enabled:
                # 跨日：清空已触发槽位，否则 (9,0) 这类槽位次日不再命中。
                today = time.strftime("%Y-%m-%d")
                if today != self._fired_slots_date:
                    self._fired_slots.clear()
                    self._fired_slots_date = today
                # 签到槽位：分钟粒度（10:01 / 10:05 / 10:15 / 整点…）。
                # 用「(时,分) 精确命中 + 记录已触发槽位」避免同一分钟内重复触发。
                # 读槽位时持锁：面板热更新（reload_slots）会整体替换列表。
                with self._slots_lock:
                    slots = list(self.checkin_slots)
                    primary_slots = set(self.checkin_primary_slots)
                slot = (cur_hour, cur_min)
                if slot in slots and slot not in self._fired_slots:
                    self._fired_slots.add(slot)
                    reason = "签到排程命中 (%d:%02d)" % (cur_hour, cur_min)
                    primary = slot in primary_slots
                    try:
                        self._execute_cycle(reason, CYCLE_HOUR, checkin_primary=primary)
                    except Exception as exc:
                        self.log("排程执行异常: %s" % exc)
                    time.sleep(65)   # 避开当前这一分钟重复触发
                elif (cur_hour in self.keepalive_hours and cur_min == 0
                      and ("ka", cur_hour) not in self._fired_slots):
                    self._fired_slots.add(("ka", cur_hour))
                    try:
                        self._execute_cycle("保活排程命中 (%d:00)" % cur_hour,
                                            CYCLE_HOUR)
                    except Exception as exc:
                        self.log("排程执行异常: %s" % exc)
                    time.sleep(65)
            self._stop_event.wait(20)

    def _calc_next_fire(self):
        """下一个触发时刻（支持分钟粒度槽位）。"""
        now = time.localtime()
        with self._slots_lock:
            slots = list(self.checkin_slots)
            keepalive = list(self.keepalive_hours)
        candidates = []
        for h, m in slots:
            candidates.append((h, m, "签到"))
        for h in keepalive:
            candidates.append((h, 0, "保活"))
        candidates.sort()
        for h, m, _kind in candidates:
            if h > now.tm_hour or (h == now.tm_hour and m > now.tm_min):
                t_struct = time.struct_time(
                    (now.tm_year, now.tm_mon, now.tm_mday, h, m, 0, 0, 0, -1))
                self.next_run_time = time.strftime("%Y-%m-%d %H:%M:%S", t_struct)
                return
        # 今天的槽位都过了 -> 明天第一个
        t_tomorrow = time.time() + 86400
        now_tom = time.localtime(t_tomorrow)
        h, m, _kind = candidates[0]
        t_struct = time.struct_time(
            (now_tom.tm_year, now_tom.tm_mon, now_tom.tm_mday, h, m, 0, 0, 0, -1))
        self.next_run_time = time.strftime("%Y-%m-%d %H:%M:%S", t_struct)

    def trigger_now(self):
        """手动立即触发一次调度检查（不受"启动补签当日一次"闸门限制）。"""
        if self._run_lock.locked():
            return {"ok": False, "msg": "已有巡检正在执行，请稍候再试"}
        threading.Thread(target=self._execute_cycle,
                         args=("手动立即触发", CYCLE_MANUAL),
                         daemon=True).start()
        return {"ok": True, "msg": "已触发后台调度执行"}

    def _execute_cycle(self, trigger_reason="周期巡检", cycle_kind=CYCLE_HOUR,
                       checkin_primary=None):
        if not self._run_lock.acquire(blocking=False):
            self.log("跳过本次巡检 (%s)：上一轮仍在执行" % trigger_reason)
            return
        try:
            self._run_cycle(trigger_reason, cycle_kind, checkin_primary)
        finally:
            self._run_lock.release()

    def _allow_complement_checkin(self, cycle_kind):
        """启动巡检的补签闸门：**同一天最多补签一次**（其余巡回来由不受限）。

        为什么需要：启动巡检会对 can_checkin() 为真的账号发真实领取请求，
        进程反复重启就会反复领取。这里做 mark-before-act——先把当日标记落盘，
        再去做领取；即使随后进程崩溃/被杀，重启后也不再重放。
        can_checkin() 依赖的 per-account lastCheckin（落盘在账号 JSON）是第二道闸。
        """
        if cycle_kind != CYCLE_STARTUP:
            return True
        today = time.strftime("%Y-%m-%d")
        if self.startup_claim_date == today:
            return False
        self.startup_claim_date = today
        self._save_state()
        return True

    def _run_cycle(self, trigger_reason="周期巡检", cycle_kind=CYCLE_HOUR,
                   checkin_primary=None):
        self.last_run_time = time.strftime("%Y-%m-%d %H:%M:%S")
        self.last_cycle_date = time.strftime("%Y-%m-%d")
        self.log("开始执行任务 (%s)..." % trigger_reason)
        if not self.pool or not self.pool.accounts:
            self.log("暂无可用的活跃账号，跳过本次巡检")
            self._save_state()
            return

        cur_hour = time.localtime().tm_hour
        keepalive_due = cur_hour in self.keepalive_hours
        # checkin_due 由 checkin_primary 参数决定（见下方「2. 每日签到」）。
        # 保留 checkin_hours 仅作对外兼容/状态展示，实际槽位看 checkin_slots。
        allow_complement = self._allow_complement_checkin(cycle_kind)

        # 1. Token 保活：整点 22:00 全量刷新；其余巡检只刷新临近过期的
        force = keepalive_due
        ka = run_keepalive(self.pool, force=force)
        self.log("Token 保活：刷新 %d 个，失败 %d 个%s"
                 % (ka["refreshed"], ka["failed"],
                    "（22:00 集中保活）" if force else "（临近过期）"))
        for line in ka["logs"]:
            if line.startswith("!"):
                self.log(line)

        # #25 a：本轮失效凭证的账号**整体排除**在后续步骤之外。
        # 理由：跳过只是"不再打上游"，但一轮里若已用失效 token 拿到过退化响应，
        # 那次响应会被写进账本（把健康余额覆盖成 0）；所以这里在**源头**把这些
        # 账号从签到与额度快照里去掉 —— 它们的余额等下一次成功刷新再恢复。
        # 两类来源：① 刚才保活明确失败的 uid；② token 已过期（含没被保活覆盖的）。
        stale_uids = set(ka.get("failed_uids") or [])
        now_ts = time.time()
        for a in (self.pool.accounts if self.pool else []):
            exp = getattr(a, "expires_at", 0) or 0
            if a.access_token and exp and exp <= now_ts:
                stale_uids.add(a.uid)
        if stale_uids:
            self.log("跳过 %d 个失效凭证账号（本轮不签到、不刷额度）：%s"
                     % (len(stale_uids),
                        "、".join(sorted(str(u)[:8] for u in stale_uids))))

        # 2. 每日签到
        #    · 主槽位（10:01 / 21:00）: 对全部账号发起尝试（不看 can_checkin），
        #      因为上游批次可能有延迟，主槽位值得无条件再撞一次；
        #    · 其它槽位（10:05 / 10:15 / 逐小时重试）: 只对**本轮尚未领到**的
        #      账号发起 —— can_checkin() 已是批次感知，领到后自然为 False，
        #      没领到就继续重试，直到当天批次真正可领。
        #    · 非签到槽位（保活 22:00 等）: 走补签语义，受启动闸门约束。
        targets = [a for a in self.pool.accounts
                   if a.enabled and a.access_token and a.uid not in stale_uids]
        if checkin_primary is None:
            # 非签到槽位：按是否处于签到小时粗判（保活轮不签到）
            checkin_due = False
        else:
            checkin_due = True
        if checkin_due:
            if checkin_primary:
                pending = targets          # 主槽位：全量尝试
            else:
                # 补签槽位：只打本轮还没领的
                pending = [a for a in targets if a.can_checkin()]
        elif allow_complement:
            pending = [a for a in targets if a.can_checkin()]
        else:
            pending = []               # 当日已补签：不发起任何领取
        checkin_count = 0
        earned = 0
        if pending:
            self.log("检测到 %d 个账号需要签到，执行自动签到..." % len(pending))
            res = run_batch_checkin(pending, gap=1.0, inter_gap=1.0)
            checkin_count = res["accounts_count"]
            earned = res.get("credit_added") or 0
            for line in res["logs"]:
                if line.startswith("✓") or line.startswith("!") \
                        or line.startswith("·"):
                    self.log(line)
        elif checkin_due:
            self.log("所有账号本轮已签到")

        # 3. 刷新额度快照（看板积分卡片依赖）
        for acc in targets:
            try:
                acc.fetch_credits()
            except Exception:
                pass
            time.sleep(0.5)

        self.log("巡检完成：Token 保活 %d 个，签到 %d 个，本次新增积分 +%d"
                 % (ka["refreshed"], checkin_count, earned))
        self._save_state()

    def status(self):
        return {
            "enabled": self.enabled,
            "mode": "批次感知排程 (10:01/10:05/10:15 签到 · 逐小时重试至 20:00 · 21:00 兜底 · 22:00 Token 保活)",
            "mode_cn": "批次感知排程 (10:01/10:05/10:15 每日签到 · 逐小时重试至 20:00 · 21:00 兜底 · 22:00 Token 保活)",
            "mode_intl": "批次感知排程 (10:01/10:05/10:15 每日签到 · 逐小时重试至 20:00 · 21:00 兜底 · 22:00 Token 保活)",
            "last_run_time": self.last_run_time or "尚未运行",
            "next_run_time": self.next_run_time or "待调度",
            "startup_claim_date": self.startup_claim_date or "",
            "last_cycle_date": self.last_cycle_date or "",
            "state_file": self._state_path(),
            "logs": self.logs[-20:],
        }
