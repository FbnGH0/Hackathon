"""Pure normalization/export code. Source facts, never AI, decide submission state."""
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse


def component(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")[:140]
    if not value or value.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)], *[f"LPT{i}" for i in range(1, 10)]}:
        value = "_" + (value or "untitled")
    return value


def iso(value):
    if not value:
        return None
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("LMS timestamp is missing its timezone")
    return result.astimezone(timezone.utc).isoformat()


def lesson_spec(title: str):
    match = re.search(r"(?<![A-Za-z0-9])(?:lesson|lecture|session|课次|Ll?)\s*[-_ ]*0*(\d+)(?:\s*[-–_]\s*(?:L|lesson\s*)?0*(\d+))?", title, re.I)
    if not match:
        return None
    start, end = int(match[1]), int(match[2] or match[1])
    return (start, end) if end >= start else (start, start)


def week_spec(title: str):
    """Week folders as used by e.g. TP: "Week 1", "Week 1a", "WK01", "Week 1 (19-25 Oct)", "Pre-Week 3", "Week 1-2"."""
    match = re.search(r"(?<![A-Za-z0-9])(?:week|wk)\s*[-_ ]*0*(\d+)[a-z]?(?:\s*[-–_]\s*(?:week|wk)?\s*0*(\d+)(?![\d)]))?", title, re.I)
    if not match:
        return None
    start, end = int(match[1]), int(match[2] or match[1])
    return (start, end) if end >= start else (start, start)


def group_spec(title: str):
    """(kind, start, end) for a lesson or week folder. Lesson numbers win, so RP-style "Week 1 / Lesson 2" keeps lessons."""
    spec = lesson_spec(title)
    if spec is not None:
        return ("Lesson", *spec)
    spec = week_spec(title)
    if spec is not None:
        return ("Week", *spec)
    return None


def lesson_number(title: str):
    value = lesson_spec(title)
    return value[0] if value else None


def content_lessons(toc: dict, base: str, course_id: str) -> list:
    """Flatten Package/Week while preserving explicit lesson/week numbers and source hierarchy.

    Groups are "Lesson N" (RP style) or "Week N" (TP style); the nearest numbered folder wins.
    Non-lesson resources go in Resources, not a made-up course session.
    IDs disambiguate two school lessons with the same number.
    Week groups are only created once they hold a material, so a week that just wraps lessons stays invisible.
    """
    lessons = {}
    pending = {}

    def group_id(spec):
        kind, start, end = spec
        return f"{kind.lower()}-{start}-{end}"

    def ensure(gid):
        if gid not in lessons and gid in pending:
            lessons[gid] = pending.pop(gid)
        return gid

    def walk(node, ancestors, inherited=None):
        if node.get("IsHidden") or node.get("IsLocked"):
            return
        title = node.get("Title", "Resources")
        spec = group_spec(title)
        current = inherited
        if spec is not None:
            current = group_id(spec)
            entry = {"id": current, "kind": spec[0], "number": spec[1], "endNumber": spec[2], "title": title, "materials": []}
            if spec[0] == "Lesson":
                lessons.setdefault(current, entry)
            elif current not in lessons:
                pending.setdefault(current, entry)
        for topic in sorted(node.get("Topics", []), key=lambda t: t.get("SortOrder", 0)):
            if topic.get("IsHidden") or topic.get("IsLocked") or topic.get("IsBroken"):
                continue
            topic_spec = group_spec(topic.get("Title", ""))
            target = current
            if topic_spec is not None:
                kind, start, end = topic_spec
                target = group_id(topic_spec)
                if target not in lessons and target not in pending:
                    pending[target] = {"id": target, "kind": kind, "number": start, "endNumber": end, "title": f"{kind} {start}" if start == end else f"{kind}s {start}–{end}", "materials": []}
            if target is not None:
                ensure(target)
            if target is None:
                target = "resources"
                lessons.setdefault(target, {"id": target, "number": None, "title": "Resources", "materials": []})
            source_url = topic.get("Url", "")
            parsed = urlparse(source_url)
            # Only local content files. External links remain references, never receive LMS cookies.
            is_file = parsed.path.startswith("/content/") and (not parsed.netloc or parsed.netloc == urlparse(base).netloc)
            url = f"{base}/d2l/le/content/{course_id}/viewContent/{topic['TopicId']}/View"
            suffix = Path(unquote(parsed.path)).suffix if is_file else ""
            lessons[target]["materials"].append({
                "id": str(topic["TopicId"]), "title": topic["Title"], "url": url,
                "modifiedAt": topic.get("LastModifiedDate") or node.get("LastModifiedDate"),
                "sourcePath": " / ".join(ancestors + [title]), "downloadable": is_file,
                "filename": f"{component(topic['Title'])} [{topic['TopicId']}]{suffix}",
            })
        for child in sorted(node.get("Modules", []), key=lambda m: m.get("SortOrder", 0)):
            walk(child, ancestors + [title], current)

    for root in toc.get("Modules", []):
        walk(root, [])
    return sorted(lessons.values(), key=lambda l: (l["number"] is None, l["number"] or 0, l.get("kind") != "Week", l["id"]))


def submission_state(entities):
    if not isinstance(entities, list):
        return "unknown"
    if not entities:
        return "missing"
    # The endpoint can return an entity with an empty Submissions array.
    if any(isinstance(e, dict) and e.get("Submissions") for e in entities):
        return "submitted"
    if all(isinstance(e, dict) and isinstance(e.get("Submissions"), list) for e in entities):
        return "missing"
    return "unknown"


def assignment(folder, entities, base, course_id):
    return {"id": str(folder["Id"]), "title": folder["Name"], "dueAt": iso(folder.get("DueDate")),
            "submissionState": submission_state(entities),
            "url": f"{base}/d2l/lms/dropbox/user/folder_submit_files.d2l?db={folder['Id']}&ou={course_id}"}


def atomic_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def export_course(root: Path, school: str, course: dict):
    folder = root / component(school) / component(course["moduleName"])
    folder.mkdir(parents=True, exist_ok=True)
    used = {}
    lines = [f"# {course['moduleName']}", "", f"学校：{school}", "", "## 课程资料", ""]
    for lesson in course["lessons"]:
        name = f"{lesson.get('kind', 'Lesson')} {lesson['number']:02d}" if lesson["number"] is not None else "Resources"
        if lesson.get("endNumber") and lesson["endNumber"] != lesson["number"]:
            name += f"-{lesson['endNumber']:02d}"
        if name in used and used[name] != lesson["id"]:
            name += f" [{lesson['id']}]"
        used[name] = lesson["id"]
        lesson["directory"] = name
        lesson_dir = folder / name
        lesson_dir.mkdir(exist_ok=True)
        body = [f"# {lesson['title']}", "", f"Module：{course['moduleName']}", ""]
        for material in lesson["materials"]:
            body.extend([f"## {material['title']}", f"来源：{material['url']}", f"原目录：{material['sourcePath']}", ""])
            if material.get("filePath"):
                # Local relative Markdown links also work in Obsidian.
                body.extend([f"[打开课件](<{Path(material['filePath']).name}>)", ""])
            elif material.get("downloadable"):
                body.extend(["下载状态：待下载或下载失败，见同步警告。", ""])
        (lesson_dir / "index.md").write_text("\n".join(body), encoding="utf-8")
        lines.append(f"- [{name}](<{name}/index.md>) — {lesson['title']}")
    lines.extend(["", "## 作业提交状态", ""])
    labels = {"missing": "未提交", "submitted": "已提交", "unknown": "待核实"}
    for task in course["assignments"]:
        lines.append(f"- [{task['title']}]({task['url']})：{labels[task['submissionState']]}；截止：{task['dueAt'] or '未公布'}")
    (folder / "index.md").write_text("\n".join(lines), encoding="utf-8")
    atomic_json(folder / "manifest.json", course)
    return folder


class Ledger:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS downloads (id TEXT PRIMARY KEY, modified TEXT, path TEXT)")

    def needs_download(self, key, modified, path):
        row = self.db.execute("SELECT modified, path FROM downloads WHERE id=?", (key,)).fetchone()
        # No server modification metadata means refresh; do not silently freeze the file forever.
        return not modified or not row or row != (modified, str(path)) or not path.is_file()

    def downloaded(self, key, modified, path):
        self.db.execute("INSERT OR REPLACE INTO downloads VALUES (?,?,?)", (key, modified, str(path)))
        self.db.commit()

    def relocate(self, key, modified, target, root):
        """Reuse already downloaded bytes after improving a lesson mapping."""
        row = self.db.execute("SELECT modified, path FROM downloads WHERE id=?", (key,)).fetchone()
        if not row or row[0] != modified or target.exists():
            return
        old = Path(row[1]).resolve()
        target = target.resolve()
        root = root.resolve()
        if old != target and old.is_relative_to(root) and target.is_relative_to(root) and old.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            old.replace(target)
            self.downloaded(key, modified, target)

    def close(self):
        self.db.close()
