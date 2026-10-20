# -*- coding: utf-8 -*-
"""针对「09:00 自动签到拿不到积分」修复的定向回归测试。

覆盖四条关键不变量：
  T1 批次感知：领到「上一批」时**不得**盖章 -> 本轮仍可领。
  T2 本轮已领时**必须**盖章 -> 不会重复领取。
  T3 批次键取自活动 start_at（10:00 整点未翻页时不能误判）+ 老数据升级兼容。
  T4 调度器槽位覆盖 10:01/10:05/10:15 且分钟粒度生效。
"""
import contextlib
import datetime
import os
import sys
import time as _time_mod
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qoder_accounts as qa

UTC8 = qa._UTC8


def ts(y, m, d, h, mi=0, s=0):
    """构造 UTC+8 本地时刻对应的 epoch 秒。"""
    return int(datetime.datetime(y, m, d, h, mi, s, tzinfo=UTC8).timestamp())


@contextlib.contextmanager
def frozen_time(epoch):
    """临时冻结 time.time() 与 time.localtime()（真实还原，不污染全局）。

    调度器用 time.localtime()（本地时区）判定槽位，因此两者都要冻。
    localtime 内部走 C 层，直接替换即可。
    """
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


def campaigns_stub(items):
    return lambda force=False: {
        "ok": True, "available": True, "show_campaign": True,
        "claimable": True, "campaign_url": "", "campaigns": items,
        "identity": "runtime-info", "machine_headers": "native", "hint": ""}


def campaign(cid, key, start, end, amount=100, status="CLAIMED"):
    return {"campaign_id": cid, "campaign_key": key, "claim_status": status,
            "action_type": "CLAIM_BENEFIT",
            "benefit": {"kind": "CREDITS", "amount": amount},
            "start_at": start, "end_at": end, "unavailable_reason": "",
            "achievement_completed": True, "required_achievement_key": ""}


class T1_StaleBatchNotStamped(unittest.TestCase):
    """领到上一批 -> 不盖章 -> 本轮仍可领。"""

    def _acct(self, **extra):
        data = {"uid": "u1", "realm": "cn", "accessToken": "t",
                "refreshToken": "r"}
        data.update(extra)
        return qa.Account(data, None)

    def test_stale_already_does_not_stamp(self):
        a = self._acct()
        # 上一批：start = 前一天 10:00，end = 当天 09:59
        stale = campaign("c1", "act-20251019-894",
                         ts(2025, 10, 19, 10), ts(2025, 10, 20, 9, 59))
        with frozen_time(ts(2025, 10, 20, 9, 0)):
            a.campaigns = campaigns_stub([stale])
            res = a.campaign_checkin(gap=0, only_kinds=("", "CREDITS"))
            self.assertEqual(res["earned"], 0, "上一批不该产生新增")
            self.assertIsNone(a.last_checkin, "上一批已领不得盖章")
            self.assertIsNone(a.last_checkin_batch, "上一批已领不得盖批次")
            self.assertTrue(a.can_checkin(), "本轮必须仍可领取")
            self.assertIn("上一轮", res["message"])

    def test_current_batch_stamps(self):
        a = self._acct()
        cur = campaign("c2", "act-20251020-894",
                       ts(2025, 10, 20, 10), ts(2025, 10, 21, 9, 59))
        with frozen_time(ts(2025, 10, 20, 11, 0)):
            a.campaigns = campaigns_stub([cur])
            res = a.campaign_checkin(gap=0, only_kinds=("", "CREDITS"))
            self.assertIsNotNone(a.last_checkin, "本轮已领必须盖章")
            self.assertEqual(a.last_checkin_batch, ts(2025, 10, 20, 10),
                             "盖章必须记录活动自己的批次起点")
            self.assertFalse(a.can_checkin(), "本轮已领后不应再领")
            self.assertNotIn("上一轮", res["message"])

    def test_mixed_batches_stamps_current(self):
        """上一批 + 本轮同时在列 -> 盖本轮的章。"""
        a = self._acct()
        stale = campaign("c1", "act-20251019-894",
                         ts(2025, 10, 19, 10), ts(2025, 10, 20, 9, 59))
        cur = campaign("c2", "act-20251020-894",
                       ts(2025, 10, 20, 10), ts(2025, 10, 21, 9, 59))
        with frozen_time(ts(2025, 10, 20, 11, 0)):
            a.campaigns = campaigns_stub([stale, cur])
            a.campaign_checkin(gap=0, only_kinds=("", "CREDITS"))
            self.assertEqual(a.last_checkin_batch, ts(2025, 10, 20, 10))


class T2_BatchHelpers(unittest.TestCase):

    def test_current_window_start(self):
        """基准 = **今天 10:00** 那一批（不早于 now 的最近一个 10:00 之前
        的"今天"）。这是判定"今天这一轮领了没"的基准。"""
        # 09:00 -> 基准是今天 10:00（今天那批还没开启）
        self.assertEqual(qa.current_checkin_window_start(ts(2025, 10, 20, 9, 0)),
                         ts(2025, 10, 20, 10))
        # 10:00 -> 今天 10:00
        self.assertEqual(qa.current_checkin_window_start(ts(2025, 10, 20, 10, 0)),
                         ts(2025, 10, 20, 10))
        # 23:00 -> 今天 10:00
        self.assertEqual(qa.current_checkin_window_start(ts(2025, 10, 20, 23, 0)),
                         ts(2025, 10, 20, 10))
        # 次日 00:30 -> 次日 10:00（今天那一轮尚未开启）
        self.assertEqual(qa.current_checkin_window_start(ts(2025, 10, 21, 0, 30)),
                         ts(2025, 10, 21, 10))

    def test_active_window_start(self):
        """正在生效的批次 = 不晚于 now 的最近一个 10:00。"""
        # 09:00 -> 昨天 10:00 那批仍生效（到今天 09:59）
        self.assertEqual(qa.active_checkin_window_start(ts(2025, 10, 20, 9, 0)),
                         ts(2025, 10, 19, 10))
        # 11:00 -> 今天 10:00
        self.assertEqual(qa.active_checkin_window_start(ts(2025, 10, 20, 11, 0)),
                         ts(2025, 10, 20, 10))
        # 次日 09:59 -> 仍是今天 10:00 那批（最后一刻）
        self.assertEqual(qa.active_checkin_window_start(ts(2025, 10, 21, 9, 59)),
                         ts(2025, 10, 20, 10))

    def test_batch_key_from_start_at(self):
        c = {"start_at": ts(2025, 10, 20, 10), "campaign_key": "act-20251020-894"}
        self.assertEqual(qa.campaign_batch_key(c), ts(2025, 10, 20, 10))

    def test_batch_key_fallback_to_key_date(self):
        c = {"start_at": 0, "campaign_key": "act-20251020-894"}
        self.assertEqual(qa.campaign_batch_key(c), ts(2025, 10, 20, 10))

    def test_batch_key_fallback_to_window(self):
        c = {"start_at": 0, "campaign_key": "no-date-here"}
        self.assertEqual(qa.campaign_batch_key(c, ts(2025, 10, 20, 11)),
                         ts(2025, 10, 20, 10))

    def test_infer_batch_from_legacy_stamp(self):
        # 旧数据：09:00 盖章 -> 应属前一天批次
        self.assertEqual(
            qa.Account._infer_checkin_batch("2025-10-20 09:00:10"),
            ts(2025, 10, 19, 10))
        # 旧数据：11:00 盖章 -> 属当日批次
        self.assertEqual(
            qa.Account._infer_checkin_batch("2025-10-20 11:33:12"),
            ts(2025, 10, 20, 10))
        # 旧数据：次日 01:00 盖章 -> 仍属前一日批次
        self.assertEqual(
            qa.Account._infer_checkin_batch("2025-10-21 01:00:00"),
            ts(2025, 10, 20, 10))
        # 坏数据
        self.assertEqual(qa.Account._infer_checkin_batch(""), 0)
        self.assertEqual(qa.Account._infer_checkin_batch("garbage"), 0)


class T3_LegacyUpgrade(unittest.TestCase):
    """老账号 JSON（只有 lastCheckin）升级后行为正确。"""

    def test_legacy_09_stamp_allows_retry(self):
        a = qa.Account({"uid": "u2", "realm": "cn", "accessToken": "t",
                        "lastCheckin": "2025-10-20 09:00:10"}, None)
        with frozen_time(ts(2025, 10, 20, 11, 0)):
            self.assertTrue(a.can_checkin(),
                            "老数据 09:00 盖章不得把本轮机会堵死")

    def test_legacy_11_stamp_blocks(self):
        a = qa.Account({"uid": "u3", "realm": "cn", "accessToken": "t",
                        "lastCheckin": "2025-10-20 11:33:12"}, None)
        with frozen_time(ts(2025, 10, 20, 12, 0)):
            self.assertFalse(a.can_checkin(),
                             "本轮 11:00 领过就应拦截重复领取")

    def test_legacy_next_day_stamp_blocks(self):
        """次日 01:00 盖章（属 10-20 那一批）。

        按"今天这一轮"的语义：次日 10:00 那一批尚未开启 -> 判「未领」，
        允许再撞一次（上游幂等挡掉重复发放，无害）；10:00 之后今天那批
        若已领，则判「已领」不再重复。
        """
        a = qa.Account({"uid": "u5", "realm": "cn", "accessToken": "t",
                        "lastCheckin": "2025-10-21 01:00:00"}, None)
        # 次日 02:00：10-21 那批还没开启 -> 允许再撞一次
        with frozen_time(ts(2025, 10, 21, 2, 0)):
            self.assertTrue(a.can_checkin(),
                            "10-21 批次尚未开启，应允许领取尝试")
        # 次日 11:00：10-21 那批已开启；本账号尚未领 10-21 这批 -> 仍应允许
        with frozen_time(ts(2025, 10, 21, 11, 0)):
            self.assertTrue(a.can_checkin(),
                            "10-21 批次已开启但本号没领过，应允许领取")

    def test_same_day_after_claim_blocks(self):
        """同一天领过之后必须拦截重复领取。"""
        a = qa.Account({"uid": "u6", "realm": "cn", "accessToken": "t",
                        "lastCheckin": "2025-10-20 11:00:00",
                        "lastCheckinBatch": ts(2025, 10, 20, 10)}, None)
        with frozen_time(ts(2025, 10, 20, 12, 0)):
            self.assertFalse(a.can_checkin(), "同批次内不应重复领取")
        with frozen_time(ts(2025, 10, 20, 23, 0)):
            self.assertFalse(a.can_checkin(), "同批次内不应重复领取")
        # 次日 11:00：新批次 -> 允许
        with frozen_time(ts(2025, 10, 21, 11, 0)):
            self.assertTrue(a.can_checkin(), "新批次应允许领取")

    def test_batch_persisted(self):
        a = qa.Account({"uid": "u4", "realm": "cn", "accessToken": "t",
                        "lastCheckin": "2025-10-20 11:00:00",
                        "lastCheckinBatch": ts(2025, 10, 20, 10)}, None)
        d = a.to_dict()
        self.assertEqual(d["lastCheckinBatch"], ts(2025, 10, 20, 10))


class T4_SchedulerSlots(unittest.TestCase):

    def test_slots_cover_window(self):
        import qoder_scheduler as qs
        s = qs.Scheduler.__new__(qs.Scheduler)
        s.keepalive_hours = [22]
        s.checkin_slots = [(9, 0), (10, 1), (10, 5), (10, 15), (11, 0),
                           (21, 0)]
        s.checkin_primary_slots = [(10, 1), (21, 0)]
        s.all_hours = sorted(set([h for h, _ in s.checkin_slots] + [22]))
        s.next_run_time = None
        for slot in [(10, 1), (10, 5), (10, 15), (21, 0)]:
            self.assertIn(slot, s.checkin_slots)
        self.assertIn((10, 1), s.checkin_primary_slots)
        self.assertIn((21, 0), s.checkin_primary_slots)

    def test_next_fire_minute_granularity(self):
        import qoder_scheduler as qs
        s = qs.Scheduler.__new__(qs.Scheduler)
        s.keepalive_hours = [22]
        s.checkin_slots = [(9, 0), (10, 1), (10, 5), (21, 0)]
        s.checkin_primary_slots = [(10, 1), (21, 0)]
        s.all_hours = sorted(set([h for h, _ in s.checkin_slots] + [22]))
        s.next_run_time = None
        # 09:30 时，下一个触发应是 10:01（分钟粒度），不是 10:00
        with frozen_time(ts(2025, 10, 20, 9, 30)):
            s._calc_next_fire()
            self.assertEqual(s.next_run_time, "2025-10-20 10:01:00")

    def test_real_scheduler_defaults(self):
        """真实 Scheduler 默认槽位必须含 10:01/10:05/10:15，且 09:00 仍在。"""
        import qoder_scheduler as qs
        import inspect
        src = inspect.getsource(qs.Scheduler.__init__)
        self.assertIn("(10, 1)", src)
        self.assertIn("(10, 5)", src)
        self.assertIn("(10, 15)", src)
        self.assertIn("(21, 0)", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
