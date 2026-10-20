# -*- coding: utf-8 -*-
"""每日签到时刻可配置化（面板「配置」维护）的定向回归测试。

覆盖：
  S1 槽位规范化：各种宽松输入 -> 规范 "HH:MM"；非法项被丢弃而非整单失败。
  S2 落盘与读取：set_checkin_slots -> checkin_slots 往返一致。
  S3 主槽位必须是签到槽位的子集（配置层裁剪）。
  S4 空输入回落到默认值（不会把排程清空）。
  S5 Scheduler 热更新：reload_slots 生效、next_run_time 刷新、已触发槽位处理。
"""
import contextlib
import datetime
import os
import shutil
import sys
import tempfile
import time as _time_mod
import unittest

# 兼容两种运行位置：项目根与 tests 目录（项目模块在上一级）。
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import qoder_settings as qs


@contextlib.contextmanager
def frozen_time(epoch):
    original_time = _time_mod.time
    original_localtime = _time_mod.localtime
    _time_mod.time = lambda: epoch
    _time_mod.localtime = lambda t=None: (
        original_localtime(epoch if t is None else t))
    try:
        yield
    finally:
        _time_mod.time = original_time
        _time_mod.localtime = original_localtime


def ts(y, m, d, h, mi=0, s=0):
    return int(datetime.datetime(y, m, d, h, mi, s,
                                  tzinfo=datetime.timezone(
                                      datetime.timedelta(hours=8))).timestamp())


class S1_Normalize(unittest.TestCase):

    def test_loose_inputs(self):
        self.assertEqual(qs._normalize_slot("9:0"), "09:00")
        self.assertEqual(qs._normalize_slot("09:00"), "09:00")
        self.assertEqual(qs._normalize_slot("23:59"), "23:59")
        self.assertEqual(qs._normalize_slot("10点05"), "10:05")
        self.assertEqual(qs._normalize_slot(930), "09:30")
        self.assertEqual(qs._normalize_slot(1005), "10:05")
        self.assertEqual(qs._normalize_slot("10:05"), "10:05")

    def test_invalid_inputs(self):
        for bad in ("", None, "24:00", "10:60", "99:99", "abc", -1, 2400,
                    True, "10:0:0"):
            self.assertIsNone(qs._normalize_slot(bad),
                              "应判非法: %r" % (bad,))

    def test_list_normalize_dedups_and_sorts(self):
        out = qs.normalize_checkin_slots(["21:00", "10:01", "10:01", "09:00",
                                           "10:05"])
        self.assertEqual(out, ["09:00", "10:01", "10:05", "21:00"])

    def test_invalid_items_dropped_not_fatal(self):
        out = qs.normalize_checkin_slots(["10:01", "bad", "25:00", "21:00"])
        self.assertEqual(out, ["10:01", "21:00"])

    def test_all_invalid_falls_back(self):
        out = qs.normalize_checkin_slots(["bad", "99:99"])
        self.assertEqual(out, list(qs.DEFAULT_CHECKIN_SLOTS))

    def test_none_falls_back(self):
        self.assertEqual(qs.normalize_checkin_slots(None),
                         list(qs.DEFAULT_CHECKIN_SLOTS))


class S2_Persist(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="qd-slots-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_roundtrip(self):
        slots, primary = qs.set_checkin_slots(self.dir, ["10:02", "11:00"],
                                               primary=["10:02"])
        self.assertEqual(slots, ["10:02", "11:00"])
        self.assertEqual(primary, ["10:02"])
        self.assertEqual(qs.checkin_slots(self.dir), ["10:02", "11:00"])
        self.assertEqual(qs.checkin_primary_slots(self.dir), ["10:02"])

    def test_default_when_unset(self):
        self.assertEqual(qs.checkin_slots(self.dir),
                         list(qs.DEFAULT_CHECKIN_SLOTS))

    def test_primary_kept_when_not_given(self):
        qs.set_checkin_slots(self.dir, ["10:02", "11:00"], primary=["10:02"])
        # 二次保存只给 slots -> 主槽位保留
        slots, primary = qs.set_checkin_slots(self.dir, ["10:02", "11:00"])
        self.assertEqual(primary, ["10:02"])

    def test_primary_trimmed_to_subset(self):
        # 主槽位 10:02 不在签到时刻里 -> 应被裁掉
        slots, primary = qs.set_checkin_slots(self.dir, ["11:00", "12:00"],
                                               primary=["10:02"])
        self.assertEqual(slots, ["11:00", "12:00"])
        self.assertNotIn("10:02", primary)
        # 裁空时退回最早的时刻当主槽位，不能为空
        self.assertTrue(primary)

    def test_primary_default_when_empty(self):
        slots, primary = qs.set_checkin_slots(self.dir, ["10:01", "21:00"],
                                               primary=[])
        self.assertEqual(primary, ["10:01", "21:00"])


class S3_SchedulerReload(unittest.TestCase):

    def _scheduler(self, accounts_dir):
        import qoder_scheduler as qsch
        pool = type("P", (), {"dir": accounts_dir, "accounts": []})()
        return qsch.Scheduler(pool, state_dir=accounts_dir)

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="qd-sched-")
        os.makedirs(os.path.join(self.dir, "scheduler"), exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_reload_picks_up_config(self):
        qs.set_checkin_slots(self.dir, ["07:30", "18:45"],
                             primary=["07:30"])
        s = self._scheduler(self.dir)
        self.assertIn((7, 30), s.checkin_slots)
        self.assertIn((18, 45), s.checkin_slots)
        self.assertEqual(s.checkin_primary_slots, [(7, 30)])

    def test_reload_updates_next_fire(self):
        s = self._scheduler(self.dir)
        with frozen_time(ts(2025, 10, 20, 9, 30)):
            s._calc_next_fire()
            before = s.next_run_time
        qs.set_checkin_slots(self.dir, ["09:45"], primary=["09:45"])
        view = s.reload_slots()
        self.assertNotEqual(view["next_run_time"], before)
        self.assertIn("09:45", view["checkin_slots"])
        self.assertEqual(view["checkin_primary_slots"], ["09:45"])

    def test_reload_keeps_ka_marker(self):
        s = self._scheduler(self.dir)
        s._fired_slots.add(("ka", 22))
        s._fired_slots.add((10, 1))
        s.reload_slots()
        self.assertIn(("ka", 22), s._fired_slots)

    def test_slots_view_shape(self):
        s = self._scheduler(self.dir)
        view = s.slots_view()
        self.assertIn("checkin_slots", view)
        self.assertIn("checkin_primary_slots", view)
        self.assertIn("next_run_time", view)
        self.assertIn("keepalive_hours", view)
        for item in view["checkin_slots"]:
            self.assertRegex(item, r"^\d{2}:\d{2}$")

    def test_default_slots_include_minute_granularity(self):
        s = self._scheduler(self.dir)
        slots = {"%02d:%02d" % x for x in s.checkin_slots}
        self.assertIn("10:01", slots)
        self.assertIn("10:05", slots)
        self.assertIn("10:15", slots)
        self.assertIn("21:00", slots)


if __name__ == "__main__":
    unittest.main(verbosity=2)
