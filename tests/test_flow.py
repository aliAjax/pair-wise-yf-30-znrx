import sys
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, PharmacovigilanceService, iso, parse_time, utcnow


class PharmacovigilanceFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = PharmacovigilanceService(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, dedupe="intake-1"):
        return self.svc.create_case(
            "reporter-a", "reporter", "CN",
            {"patient_ref": "P-1", "region": "CN", "product": "DrugA", "event_term": "肝损伤",
             "source": "email", "dedupe_key": dedupe, "received_at": iso(utcnow()), "serious": False},
        )["case"]

    def test_full_case_and_deduplication_flow(self):
        case = self.create()
        self.assertEqual(case["revision"], 1)
        followed = self.svc.add_followup(
            case["id"], "reporter-a", "reporter", "CN",
            {"content": "住院并出现死亡转归", "source": "phone", "expected_revision": 1,
             "received_at": iso(utcnow())},
        )
        self.assertEqual(followed["revision"], 2)
        reviewed = self.svc.medical_review(
            case["id"], "reviewer-1", "medical_reviewer",
            {"expected_revision": 2, "serious": True, "fatal": True, "causality": "possibly_related",
             "rationale": "住院记录和死亡证明已核验", "received_at": iso(utcnow())},
        )
        self.assertEqual(reviewed["case"]["revision"], 3)
        report = self.svc.create_report(case["id"], "lead-cn", "regional_lead", "CN", {"country": "CN"})
        submitted = self.svc.submit_report(report["id"], "lead-cn", "regional_lead", "CN", {})
        self.assertEqual(submitted["report"]["status"], "submitted")
        duplicate = self.svc.create_case(
            "reporter-b", "reporter", "CN",
            {"patient_ref": "P-1", "region": "CN", "product": "DrugA", "event_term": "肝损伤",
             "source": "fax", "dedupe_key": "intake-1", "received_at": iso(utcnow())},
        )
        self.assertTrue(duplicate["deduplicated"])
        detail = self.svc.get_case(case["id"], "global_admin", "")
        self.assertEqual(len(detail["intakes"]), 1)
        self.assertGreaterEqual(len(detail["audit"]), 5)
        self.assertEqual(submitted["report"]["late"], 0)

    def test_permissions_and_stale_revision(self):
        case = self.create("intake-2")
        with self.assertRaises(ApiError) as ctx:
            self.svc.get_case(case["id"], "reporter", "US")
        self.assertEqual(ctx.exception.status, 403)
        self.svc.add_followup(case["id"], "reporter-a", "reporter", "CN",
                              {"content": "第一次更新", "source": "email", "expected_revision": 1})
        with self.assertRaises(ApiError) as ctx:
            self.svc.add_followup(case["id"], "reporter-a", "reporter", "CN",
                                  {"content": "过期修改", "source": "email", "expected_revision": 1})
        self.assertEqual(ctx.exception.code, "revision_conflict")
        with self.assertRaises(ApiError) as ctx:
            self.svc.medical_review(case["id"], "lead-cn", "regional_lead",
                                    {"expected_revision": 2, "serious": True, "fatal": False,
                                     "causality": "related", "rationale": "x", "received_at": iso(utcnow())})
        self.assertEqual(ctx.exception.status, 403)


class FollowupTaskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = PharmacovigilanceService(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, dedupe, received=None, serious=False):
        return self.svc.create_case(
            "reporter-a", "reporter", "CN",
            {"patient_ref": "P-1", "region": "CN", "product": "DrugA", "event_term": "肝损伤",
             "source": "email", "dedupe_key": dedupe, "received_at": received or iso(utcnow()),
             "serious": serious},
        )["case"]

    def review(self, case, serious, fatal=False):
        return self.svc.medical_review(
            case["id"], "reviewer-1", "medical_reviewer",
            {"expected_revision": case["revision"], "serious": serious, "fatal": fatal,
             "causality": "related", "rationale": "医学裁定", "received_at": iso(utcnow())},
        )

    def open_tasks(self, case_id):
        detail = self.svc.get_case(case_id, "global_admin", "")
        return [t for t in detail["followup_tasks"] if t["status"] == "pending"]

    def test_review_schedules_followup_task_by_severity(self):
        received = iso(datetime(2026, 9, 1, tzinfo=timezone.utc))
        serious_case = self.create("fu-s", received)
        serious_task = self.review(serious_case, serious=True, fatal=True)["followup_task"]
        self.assertEqual(serious_task["status"], "pending")
        self.assertEqual(serious_task["due_at"], iso(parse_time(received) + timedelta(days=30)))
        mild_case = self.create("fu-n", received)
        mild_task = self.review(mild_case, serious=False)["followup_task"]
        self.assertEqual(mild_task["due_at"], iso(parse_time(received) + timedelta(days=90)))

    def test_existing_open_task_is_kept_on_re_review(self):
        case = self.create("fu-r", iso(datetime(2026, 9, 1, tzinfo=timezone.utc)))
        first = self.review(case, serious=True)["followup_task"]
        again = self.review(self.svc.get_case(case["id"], "global_admin", "")["case"], serious=False)
        self.assertEqual(again["followup_task"]["id"], first["id"])
        self.assertEqual(len(self.open_tasks(case["id"])), 1)

    def test_submit_followup_closes_current_and_reschedules_next(self):
        case = self.create("fu-x", iso(utcnow()))
        first = self.review(case, serious=True)["followup_task"]
        later = iso(utcnow() + timedelta(days=5))
        result = self.svc.submit_followup_task(
            first["id"], "lead-cn", "regional_lead", "CN",
            {"content": "补充病历", "source": "email", "expected_revision": 2, "received_at": later},
        )
        self.assertEqual(result["submitted_task_id"], first["id"])
        nxt = result["next_followup_task"]
        self.assertEqual(nxt["due_at"], iso(parse_time(later) + timedelta(days=30)))
        detail = self.svc.get_case(case["id"], "global_admin", "")
        tasks = {t["id"]: t for t in detail["followup_tasks"]}
        self.assertEqual(tasks[first["id"]]["status"], "done")
        self.assertEqual(tasks[nxt["id"]]["status"], "pending")
        self.assertEqual(len(self.open_tasks(case["id"])), 1)
        # 通过案例随访接口提交同样关闭当前任务并续排
        fresh = self.svc.get_case(case["id"], "global_admin", "")["case"]
        self.svc.add_followup(case["id"], "reporter-a", "reporter", "CN",
                              {"content": "再次补充", "source": "phone", "expected_revision": fresh["revision"]})
        detail = self.svc.get_case(case["id"], "global_admin", "")
        self.assertEqual(len(detail["followup_tasks"]), 3)
        self.assertEqual(len(self.open_tasks(case["id"])), 1)
        # 任务重复提交被拒绝
        with self.assertRaises(ApiError) as ctx:
            self.svc.submit_followup_task(
                first["id"], "lead-cn", "regional_lead", "CN",
                {"content": "重复", "source": "email", "expected_revision": 4},
            )
        self.assertEqual(ctx.exception.code, "followup_task_closed")

    def test_merged_case_task_queryable_but_not_submittable(self):
        received = iso(datetime(2026, 9, 1, tzinfo=timezone.utc))
        source = self.create("fu-ms", received)
        s_task = self.review(source, serious=True)["followup_task"]
        target = self.create("fu-mt", received)
        t_task = self.review(target, serious=False)["followup_task"]
        self.svc.merge_cases(source["id"], "admin", "global_admin", {"target_case_id": target["id"]})
        # 原任务仍可查询
        detail = self.svc.get_case(source["id"], "global_admin", "")
        self.assertEqual([t["id"] for t in detail["followup_tasks"]], [s_task["id"]])
        # 但不能提交
        with self.assertRaises(ApiError) as ctx:
            self.svc.submit_followup_task(
                s_task["id"], "admin", "global_admin", "",
                {"content": "x", "source": "email", "expected_revision": 2},
            )
        self.assertEqual(ctx.exception.code, "case_merged")
        # 目标沿用来源更早的待随访（30 天早于 90 天），且只保留一个未关闭任务
        tdetail = self.svc.get_case(target["id"], "global_admin", "")
        open_ids = [t["id"] for t in tdetail["followup_tasks"] if t["status"] == "pending"]
        self.assertEqual(len(open_ids), 1)
        self.assertEqual(tdetail["followup_tasks"][-1]["due_at"], s_task["due_at"])
        self.assertEqual({t["id"] for t in tdetail["followup_tasks"] if t["status"] == "superseded"}, {t_task["id"]})
        # 待随访列表不含已合并案例
        listed = self.svc.list_followup_tasks("global_admin", "", {})
        case_ids = {t["case_id"] for t in listed}
        self.assertNotIn(source["id"], case_ids)
        self.assertIn(target["id"], case_ids)

    def test_merge_keeps_target_task_when_it_is_earlier(self):
        source = self.create("fu-es", iso(datetime(2026, 9, 1, tzinfo=timezone.utc)))
        s_task = self.review(source, serious=False)["followup_task"]
        target = self.create("fu-et", iso(datetime(2026, 8, 1, tzinfo=timezone.utc)))
        t_task = self.review(target, serious=True)["followup_task"]
        self.svc.merge_cases(source["id"], "admin", "global_admin", {"target_case_id": target["id"]})
        self.assertEqual([t["id"] for t in self.open_tasks(target["id"])], [t_task["id"]])

    def test_merge_copies_due_date_when_target_has_no_open_task(self):
        source = self.create("fu-cs", iso(utcnow()))
        s_task = self.review(source, serious=True)["followup_task"]
        target = self.create("fu-ct", iso(utcnow()))  # 未医学裁定，没有随访任务
        self.svc.merge_cases(source["id"], "admin", "global_admin", {"target_case_id": target["id"]})
        open_ids = self.open_tasks(target["id"])
        self.assertEqual(len(open_ids), 1)
        self.assertEqual(open_ids[0]["due_at"], s_task["due_at"])

    def test_followup_task_overdue_listing_and_region_scope(self):
        old = self.create("fu-o", iso(datetime(2026, 1, 1, tzinfo=timezone.utc)))
        self.review(old, serious=False)  # 到期 2026-04-01，已逾期
        fresh = self.create("fu-f", iso(utcnow()))
        self.review(fresh, serious=False)
        all_tasks = self.svc.list_followup_tasks("regional_lead", "CN", {})
        self.assertEqual(len(all_tasks), 2)
        overdue = self.svc.list_followup_tasks("regional_lead", "CN", {"overdue": ["1"]})
        self.assertEqual([t["case_id"] for t in overdue], [old["id"]])
        self.assertTrue(all(t["overdue"] for t in overdue))
        self.assertEqual(self.svc.list_followup_tasks("regional_lead", "US", {}), [])
        self.assertIn("followup_tasks", self.svc.state("regional_lead", "CN"))


if __name__ == "__main__":
    unittest.main()
