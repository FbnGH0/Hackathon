import json
import tempfile
import unittest
from pathlib import Path
from core import assignment, component, content_lessons, export_course, iso, Ledger, submission_state, lesson_spec, week_spec
from secrets_store import save_secret, load_secret


class SyncTests(unittest.TestCase):
    def test_actual_school_package_week_lesson_nesting(self):
        toc = {"Modules": [{"ModuleId": 1, "Title": "Lesson Package One", "Modules": [
            {"ModuleId": 2, "Title": "Week 1", "Modules": [
                {"ModuleId": 3, "Title": "Lesson 1", "Topics": [{"TopicId": 10, "Title": "Student Slides", "Url": "/content/enforced/865693/slides.zip"}]},
                {"ModuleId": 4, "Title": "Lesson 2", "Topics": [{"TopicId": 11, "Title": "Lesson 2", "Url": "https://example.com/video"}]},
                {"ModuleId": 5, "Title": "Lesson 3", "IsLocked": True, "Topics": [{"TopicId": 12, "Title": "Not released", "Url": "/content/notreleased.pdf"}]},
            ]}]}]}
        lessons = content_lessons(toc, "https://rplms.polite.edu.sg", "865693")
        self.assertEqual([l["number"] for l in lessons], [1, 2])
        self.assertTrue(lessons[0]["materials"][0]["downloadable"])
        self.assertFalse(lessons[1]["materials"][0]["downloadable"])
        self.assertIn("Week 1", lessons[0]["materials"][0]["sourcePath"])

    def test_tp_style_week_folders(self):
        toc = {"Modules": [
            {"ModuleId": 1, "Title": "Lesson Materials", "Modules": [
                {"ModuleId": 2, "Title": "Week 1: Supervised Learning 1", "Topics": [{"TopicId": 20, "Title": "Slides", "Url": "/content/a.pptx"}]},
                {"ModuleId": 3, "Title": "Week 1 Tutorial (F2F)", "Topics": [{"TopicId": 21, "Title": "Tutorial", "Url": "/content/b.pdf"}]},
                {"ModuleId": 4, "Title": "WK02 - AWS IoT Setup", "Topics": [{"TopicId": 22, "Title": "Guide", "Url": "/content/c.pdf"}]},
                {"ModuleId": 5, "Title": "Week 5 Learning Materials", "Modules": [
                    {"ModuleId": 6, "Title": "Lesson 1 - Flipped Learning", "Topics": [{"TopicId": 23, "Title": "Video", "Url": "/content/d.mp4"}]}]},
                {"ModuleId": 7, "Title": "Week 3a: ListView", "Topics": [{"TopicId": 24, "Title": "Notes", "Url": "/content/e.pdf"}]},
            ]},
            {"ModuleId": 8, "Title": "Assessment", "Topics": [{"TopicId": 25, "Title": "Brief", "Url": "/content/f.pdf"}]},
        ]}
        lessons = content_lessons(toc, "https://tplms.polite.edu.sg", "1")
        self.assertEqual([(l.get("kind"), l["number"]) for l in lessons], [("Week", 1), ("Lesson", 1), ("Week", 2), ("Week", 3), (None, None)])
        self.assertEqual(len(lessons[0]["materials"]), 2)
        self.assertEqual(week_spec("Week 1 (19-25 Oct)"), (1, 1))
        self.assertEqual(week_spec("Pre-Week 3 Lesson"), (3, 3))
        self.assertEqual(week_spec("Week 11b: Applied AI"), (11, 11))
        self.assertEqual(week_spec("Week 1-2 Revision"), (1, 2))
        self.assertIsNone(week_spec("Weekly Quiz"))

    def test_submission_and_absolute_deadline(self):
        self.assertEqual(lesson_spec("G133_L06_Lesson Slides"), (6, 6))
        self.assertEqual(lesson_spec("Ll06 Student"), (6, 6))
        self.assertEqual(lesson_spec("L23_L24 Student"), (23, 24))
        self.assertEqual(lesson_spec("Week 1-7 (Lesson 1 - 14)"), (1, 14))
        self.assertEqual(submission_state(None), "unknown")
        self.assertEqual(submission_state([]), "missing")
        self.assertEqual(submission_state([{"Submissions": []}]), "missing")
        self.assertEqual(submission_state([{"Submissions": [{"Id": 1}]}]), "submitted")
        self.assertEqual(submission_state([{"Error": "forbidden"}]), "unknown")
        self.assertEqual(iso("2026-10-01T23:59:00+08:00"), "2026-10-01T15:59:00+00:00")
        with self.assertRaises(ValueError):
            iso("2026-10-01T23:59:00")

    def test_export_collision_incremental_and_encrypted_session(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            course = {"id": "865693", "moduleName": "C245 Data Analytics with GenAI", "assignments": [],
                      "lessons": [{"id": "one", "number": 1, "title": "Lesson 1", "materials": []},
                                  {"id": "two", "number": 1, "title": "Lesson 1", "materials": []}]}
            folder = export_course(root, "Republic Polytechnic", course)
            self.assertTrue((folder / "Lesson 01" / "index.md").is_file())
            self.assertTrue((folder / "Lesson 01 [two]" / "index.md").is_file())
            export_course(root, "Republic Polytechnic", course)
            self.assertEqual(len(list(folder.glob("Lesson*"))), 2)
            self.assertNotIn("/", component("../../escape"))
            self.assertEqual(component("CON"), "_CON")
            secret = root / "session.enc"
            save_secret(secret, {"cookies": [{"value": "private-test-cookie"}]})
            self.assertNotIn(b"private-test-cookie", secret.read_bytes())
            self.assertEqual(load_secret(secret)["cookies"][0]["value"], "private-test-cookie")
            file = root / "slides.pdf"
            file.write_bytes(b"test")
            ledger = Ledger(root / "test.sqlite")
            ledger.downloaded("topic-1", "v1", file)
            self.assertFalse(ledger.needs_download("topic-1", "v1", file))
            self.assertTrue(ledger.needs_download("topic-1", "v2", file))
            self.assertTrue(ledger.needs_download("topic-1", None, file))
            ledger.close()


if __name__ == "__main__":
    unittest.main()
