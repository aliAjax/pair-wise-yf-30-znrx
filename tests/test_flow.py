import sys
import tempfile
import unittest
from pathlib import Path
from datetime import timedelta

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, PharmacovigilanceService, followup_due, iso, parse_time, utcnow


class PharmacovigilanceFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = PharmacovigilanceService(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, dedupe="intake-1", received=None):
        return self.svc.create_case(
            "reporter-a", "reporter", "CN",
            {"patient_ref": "P-1", "region": "CN", "product": "DrugA", "event_term": "肝损伤",
             "source": "email", "dedupe_key": dedupe, "received_at": received or iso(utcnow()), "serious": False},
        )["case"]

    def review(self, case, revision, serious, fatal=False):
        return self.svc.medical_review(
            case["id"], "reviewer-1", "medical_reviewer",
            {"expected_revision": revision, "serious": serious, "fatal": fatal,
             "causality": "possibly_related", "rationale": "已核验", "received_at": iso(utcnow())},
        )

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

    def test_followup_plan_after_medical_review(self):
        case = self.create("fu-1")
        self.assertEqual(self.svc.get_case(case["id"], "global_admin", "")["followup_tasks"], [])
        reviewed = self.review(case, 1, serious=False)
        task = reviewed["followup_task"]
        self.assertEqual(task["status"], "open")
        self.assertEqual(task["due_at"], iso(parse_time(case["received_at"]) + timedelta(days=90)))
        # 改判为严重后旧任务被取代，仍只留一个未关闭任务，按 30 天重排
        reviewed = self.review(case, 2, serious=True)
        self.assertEqual(reviewed["followup_task"]["due_at"], iso(parse_time(case["received_at"]) + timedelta(days=30)))
        tasks = self.svc.get_case(case["id"], "global_admin", "")["followup_tasks"]
        self.assertEqual([t["status"] for t in tasks], ["superseded", "open"])
        # 死亡案例同样按 30 天
        fatal_case = self.create("fu-1b")
        reviewed = self.review(fatal_case, 1, serious=True, fatal=True)
        self.assertEqual(reviewed["followup_task"]["due_at"], iso(parse_time(fatal_case["received_at"]) + timedelta(days=30)))

    def test_followup_closes_task_and_schedules_next(self):
        case = self.create("fu-2")
        self.review(case, 1, serious=True)
        first = self.svc.get_case(case["id"], "global_admin", "")["followup_tasks"][0]
        fu_time = iso(utcnow() + timedelta(days=5))
        result = self.svc.add_followup(
            case["id"], "reporter-a", "reporter", "CN",
            {"content": "症状缓解", "source": "email", "expected_revision": 2, "received_at": fu_time},
        )
        nxt = result["followup_task"]
        self.assertEqual(nxt["due_at"], iso(parse_time(fu_time) + timedelta(days=30)))
        tasks = self.svc.get_case(case["id"], "global_admin", "")["followup_tasks"]
        done = [t for t in tasks if t["status"] == "done"]
        open_ = [t for t in tasks if t["status"] == "open"]
        self.assertEqual(len(open_), 1)
        self.assertEqual(open_[0]["id"], nxt["id"])
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0]["id"], first["id"])
        self.assertEqual(done[0]["closed_by"], "reporter-a")

    def test_merge_keeps_targets_earlier_followup_task(self):
        base = utcnow()
        target = self.create("fu-3", received=iso(base - timedelta(days=40)))
        self.review(target, 1, serious=False)  # 到期 base+50 天，更早
        source = self.create("fu-4", received=iso(base))
        self.review(source, 1, serious=False)  # 到期 base+90 天
        target_task = self.svc.get_case(target["id"], "global_admin", "")["followup_tasks"][0]
        merged = self.svc.merge_cases(source["id"], "admin-1", "global_admin", {"target_case_id": target["id"]})
        # 目标已有更早待随访则沿用，不产生新任务
        self.assertEqual(merged["followup_task"]["id"], target_task["id"])
        target_tasks = self.svc.get_case(target["id"], "global_admin", "")["followup_tasks"]
        self.assertEqual([t["status"] for t in target_tasks], ["open"])
        # 源案例原任务保留可查，但已随合并关闭
        source_tasks = self.svc.get_case(source["id"], "global_admin", "")["followup_tasks"]
        self.assertEqual([t["status"] for t in source_tasks], ["merged"])
        # 已合并案例不能提交随访
        with self.assertRaises(ApiError) as ctx:
            self.svc.add_followup(source["id"], "reporter-a", "reporter", "CN",
                                  {"content": "x", "source": "email", "expected_revision": 3})
        self.assertEqual(ctx.exception.code, "case_merged")

    def test_merge_adopts_sources_earlier_followup_task(self):
        base = utcnow()
        target = self.create("fu-5", received=iso(base))
        self.review(target, 1, serious=False)  # 到期 base+90 天
        source = self.create("fu-6", received=iso(base - timedelta(days=40)))
        self.review(source, 1, serious=False)  # 到期 base+50 天，更早
        source_task = self.svc.get_case(source["id"], "global_admin", "")["followup_tasks"][0]
        merged = self.svc.merge_cases(source["id"], "admin-1", "global_admin", {"target_case_id": target["id"]})
        # 来源排期更早则被目标采用，目标原任务被取代
        self.assertEqual(merged["followup_task"]["due_at"], source_task["due_at"])
        target_tasks = self.svc.get_case(target["id"], "global_admin", "")["followup_tasks"]
        self.assertEqual([t["status"] for t in target_tasks], ["superseded", "open"])

    def test_followup_task_listing_and_permissions(self):
        case = self.create("fu-7")
        self.review(case, 1, serious=True)
        mine = self.svc.list_followup_tasks("regional_lead", "CN", {"status": ["open"]})
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["case_no"], case["case_no"])
        self.assertEqual(self.svc.list_followup_tasks("regional_lead", "US", {"status": ["open"]}), [])
        self.assertEqual(len(self.svc.list_followup_tasks("global_admin", "", {"case_id": [str(case["id"])]})), 1)
        with self.assertRaises(ApiError) as ctx:
            self.svc.list_followup_tasks("global_admin", "", {"status": ["bogus"]})
        self.assertEqual(ctx.exception.status, 400)


if __name__ == "__main__":
    unittest.main()
