import type { LmsSnapshot } from "./lms-schema";
import type { ChatMessage, ChatSession, DdlItem, Note } from "./types";

export interface LmsFeed {
  revision: number;
  ddls: DdlItem[];
  notes: Note[];
  sessions: ChatSession[];
  messages: ChatMessage[];
  courses?: LmsCourse[];
  status?: { status: string; syncedAt: string; warnings: string[] };
}

export type LmsCourse = LmsSnapshot["courses"][number] & { school: string; noteId: string };

const sourceKey = (s: LmsSnapshot) => new URL(s.baseUrl).hostname.replace(/[^a-zA-Z0-9_-]/g, "_");
const signature = (x: unknown) => JSON.stringify(x);

function localDue(value: string | null, timeZone: string) {
  if (!value) return { dueDate: "", dueTime: "" };
  const parts = new Intl.DateTimeFormat("en-GB", { timeZone, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).formatToParts(new Date(value));
  const get = (name: string) => parts.find(p => p.type === name)?.value || "";
  return { dueDate: `${get("year")}-${get("month")}-${get("day")}`, dueTime: `${get("hour")}:${get("minute")}` };
}

/** Deterministic facts are authoritative; AI only supplies a bounded optional brief. */
export function buildLmsFeed(snapshot: LmsSnapshot, previous: LmsFeed, now = Date.now()): LmsFeed {
  const key = sourceKey(snapshot);
  const prefix = `lms-${key}-`;
  const sessionId = `${prefix}session`;
  const tasks = new Map(previous.ddls.map(d => [d.id, d]));
  const notes = new Map(previous.notes.map(n => [n.id, n]));
  const messages = new Map(previous.messages.map(m => [m.id, m]));
  const courses = new Map((previous.courses || []).map(c => [c.noteId, c]));
  const changes: string[] = [];
  const nearDue: string[] = [];
  let reminderBand = 0;
  const day = new Intl.DateTimeFormat("en-CA", { timeZone: snapshot.timezone, year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date(now)).replace(/[^0-9]/g, "");
  const addMessage = (id: string, content: string) => {
    if (!messages.has(id)) messages.set(id, { id, sessionId, role: "assistant", content, timestamp: new Date(now) });
  };
  for (const course of snapshot.courses) {
    for (const assignment of course.assignments) {
      const id = `${prefix}${course.id}-assignment-${assignment.id}`;
      const { dueDate, dueTime } = localDue(assignment.dueAt, snapshot.timezone);
      const old = tasks.get(id);
      // Unreadable submission status must not regress the last confirmed status.
      const state = assignment.submissionState === "unknown" && old?.lmsSubmissionState ? old.lmsSubmissionState : assignment.submissionState;
      const done = state === "submitted";
      const task: DdlItem = { id, taskName: assignment.title, source: course.moduleName, weight: null,
        dueDate, dueTime, completed: done, status: done ? "done" : "todo", isGroupWork: false,
        description: `${snapshot.school} · ${snapshot.timezone} · ${assignment.submissionState === "unknown" ? "本轮提交状态待核实" : done ? "网站已确认提交" : "网站未找到提交记录"}`,
        priority: assignment.dueAt && new Date(assignment.dueAt).getTime() - now <= 86400000 && !done ? "high" : "med",
        tags: ["LMS", snapshot.school], lmsSubmissionState: state, lmsDueAt: assignment.dueAt,
        attachments: [{ id: `${id}-link`, kind: "url", label: "打开学校作业", ref: assignment.url }],
      };
      tasks.set(id, task);
      if (!old) changes.push(`${course.moduleName}：新增作业「${assignment.title}」（${state === "submitted" ? "网站已确认提交" : state === "missing" ? "未提交" : "提交状态待核实"}）`);
      else if (old.lmsDueAt !== assignment.dueAt) changes.push(`${assignment.title}：截止时间有更新`);
      else if (old.lmsSubmissionState !== state) changes.push(`${assignment.title}：${state === "submitted" ? "已提交" : "提交状态有更新"}`);
      if (assignment.submissionState === "missing" && assignment.dueAt) {
        const left = new Date(assignment.dueAt).getTime() - now;
        if (left <= 86400000) {
          const band = left <= 0 ? 3 : left <= 21600000 ? 2 : 1;
          reminderBand = Math.max(reminderBand, band);
          nearDue.push(`- ${assignment.title}（${course.moduleName}）：${left <= 0 ? "已逾期" : `约 ${Math.ceil(left / 3600000)} 小时后截止`}，${dueDate} ${dueTime} ${snapshot.timezone}；[查看](${assignment.url})`);
        }
      }
    }
    const noteId = `${prefix}${course.id}-index`;
    courses.set(noteId, { ...course, school: snapshot.school, noteId });
    const content = [`# ${course.moduleName}`, "", `学校：${snapshot.school}`, "", ...course.lessons.flatMap(l => [
      `## ${l.number === null ? "Resources" : `${l.kind ?? "Lesson"} ${String(l.number).padStart(2, "0")}${l.endNumber && l.endNumber !== l.number ? `-${String(l.endNumber).padStart(2, "0")}` : ""}`} · ${l.title}`,
      ...l.materials.map(m => `- [${m.title}](${m.url})${m.filePath ? ` · 已归档：${m.filePath}` : ""}`), "",
    ]), "## 学校通知", "", ...course.announcements.map(a => `- [${a.title}](${a.url})\n${a.text}`)].join("\n");
    const oldNote = notes.get(noteId);
    const materials = course.lessons.flatMap(l => l.materials);
    const oldMaterials = new Map((oldNote?.lmsMaterials || []).map(m => [m.id, m]));
    const changed = materials.filter(m => !oldMaterials.has(m.id) || oldMaterials.get(m.id)?.modifiedAt !== m.modifiedAt);
    if (changed.length) changes.push(`${course.moduleName}：${changed.length} 份课件新增或更新`);
    const announcementVersion = signature(course.announcements);
    if (course.announcements.length && oldNote?.lmsAnnouncements !== announcementVersion) changes.push(`${course.moduleName}：学校通知有更新`);
    notes.set(noteId, { id: noteId, title: course.moduleName, content, tags: ["LMS", snapshot.school], lmsCompletedLessons: oldNote?.lmsCompletedLessons,
      createdAt: oldNote?.createdAt || now, updatedAt: oldNote?.content === content ? oldNote.updatedAt : now,
      lmsMaterials: materials.map(m => ({ id: m.id, modifiedAt: m.modifiedAt || null })), lmsAnnouncements: announcementVersion });
  }
  if (changes.length) addMessage(`${prefix}changes-${snapshot.syncedAt.replace(/[^0-9]/g, "")}`,
    `学校同步完成：\n\n${changes.slice(0, 20).map(c => `- ${c}`).join("\n")}${changes.length > 20 ? `\n另有 ${changes.length - 20} 项更新，详见任务和课程笔记。` : ""}`);
  if (nearDue.length) addMessage(`${prefix}reminder-${day}-${reminderBand}`,
    `还有 ${nearDue.length} 项作业未提交：\n\n${nearDue.slice(0, 20).join("\n")}\n\n先处理最近截止的一项，再安排其他任务。`);
  const oldStatus = previous.status;
  if (snapshot.status === "login_required" && oldStatus?.status !== "login_required") {
    addMessage(`${prefix}login-${now}`, "学校登录已失效，课程同步暂停。请在同步器中重新登录；现有课件和作业记录已保留。");
  } else if (snapshot.status !== "ok" && snapshot.status !== "login_required" && signature(oldStatus?.warnings) !== signature(snapshot.warnings)) {
    addMessage(`${prefix}warning-${now}`, `学校同步需要检查：\n\n${snapshot.warnings.slice(0, 10).map(w => `- ${w}`).join("\n")}`);
  }
  const sessions = previous.sessions.filter(s => s.id !== sessionId);
  sessions.push({ id: sessionId, title: `${snapshot.school} · 学习提醒`, createdAt: previous.sessions.find(s => s.id === sessionId)?.createdAt || now,
    updatedAt: messages.size !== previous.messages.length ? now : previous.sessions.find(s => s.id === sessionId)?.updatedAt || now });
  return { revision: previous.revision + 1, ddls: Array.from(tasks.values()), notes: Array.from(notes.values()), courses: Array.from(courses.values()), sessions,
    messages: Array.from(messages.values()).slice(-300), status: { status: snapshot.status, syncedAt: snapshot.syncedAt, warnings: snapshot.warnings } };
}

/** Preserve edits on ordinary tasks; submission status changes are school facts. */
export function mergeLmsTasks(local: DdlItem[], incoming: DdlItem[]) {
  const result = new Map(local.map(d => [d.id, d]));
  for (const task of incoming) {
    const old = result.get(task.id);
    result.set(task.id, old ? { ...old, ...task, notes: old.notes, noteId: old.noteId,
      status: old.lmsSubmissionState === task.lmsSubmissionState ? old.status : task.status,
      completed: old.lmsSubmissionState === task.lmsSubmissionState ? old.completed : task.completed } : task);
  }
  return Array.from(result.values());
}

export function mergeLmsRows<T extends { id: string }>(local: T[], incoming: T[]) {
  const result = new Map(local.map(row => [row.id, row]));
  for (const row of incoming) result.set(row.id, row);
  return Array.from(result.values());
}

export function mergeLmsNotes(local: Note[], incoming: Note[]): Note[] {
  const old = new Map(local.map(n => [n.id, n]));
  return mergeLmsRows(local, incoming.map(n => ({ ...n,
    lmsCompletedLessons: old.get(n.id)?.lmsCompletedLessons ?? n.lmsCompletedLessons })));
}
