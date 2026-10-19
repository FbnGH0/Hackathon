"use client";

import { useMemo, useState } from "react";
import { BookOpen, ChevronDown, ExternalLink, FileText, Search } from "lucide-react";
import type { LmsCourse, LmsFeed } from "@/lib/lms-sync";
import type { Note } from "@/lib/types";
import styles from "./CoursesPanel.module.css";
import { useT } from "@/lib/i18n";
import { courseCopy } from "@/lib/course-copy";
import { demoCourses } from "@/lib/course-demo";

interface Props {
  feed: LmsFeed | null;
  notes: Note[];
  onToggleLesson: (noteId: string, lessonId: string) => void;
  onOpenNote: (noteId: string) => void;
  onAskCourse: (course: LmsCourse) => void;
}

type Row = { course: LmsCourse; total: number; submitted: number; unknown: number; missing: number };

function Track({ value, total, unknown = 0, label }: { value: number; total: number; unknown?: number; label: string }) {
  const { lang } = useT();
  const c = courseCopy[lang];
  const pct = total ? Math.round(value / total * 100) : 0;
  return <div className={styles.track} role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={total || 100} aria-valuenow={value} aria-valuetext={total ? `${value} / ${total}${unknown ? ` · ${unknown} ${c.unknownItems}` : ""}` : c.noData}>
    <span className={styles.fill} style={{ width: `${pct}%` }} />
    {!!unknown && <span className={styles.uncertain} style={{ width: `${unknown / total * 100}%` }} />}
  </div>;
}

export default function CoursesPanel({ feed, notes, onToggleLesson, onOpenNote, onAskCourse }: Props) {
  const { lang, setLang } = useT();
  const c = courseCopy[lang];
  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState<string | null>(null);
  const [showPreview, setShowPreview] = useState(true);
  const historical = !feed?.courses?.length && showPreview;
  const rows: Row[] = useMemo(() => (historical ? demoCourses : feed?.courses || []).map(course => ({ course, total: course.assignments.length,
    submitted: course.assignments.filter(a => a.submissionState === "submitted").length,
    unknown: course.assignments.filter(a => a.submissionState === "unknown").length,
    missing: course.assignments.filter(a => a.submissionState === "missing").length,
  })), [feed, historical]);
  const filtered = rows.filter(r => `${r.course.moduleName} ${r.course.school}`.toLowerCase().includes(query.trim().toLowerCase()));
  const totals = rows.reduce((s, r) => ({ total: s.total + r.total, submitted: s.submitted + r.submitted, unknown: s.unknown + r.unknown }), { total: 0, submitted: 0, unknown: 0 });
  const syncedAt = feed?.status?.syncedAt;

  return <section className={styles.panel} aria-label={c.title}>
    <header className={styles.header}>
      <div><h1>{c.title}</h1><p>{c.intro}</p></div>
      <div className={styles.sync}>{historical ? c.demoDate : syncedAt ? `${c.lastSync} ${new Date(syncedAt).toLocaleString(lang === "en" ? "en-GB" : "zh-CN", { timeZone: "Asia/Shanghai", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" })}` : c.waiting}<button className={styles.language} onClick={() => setLang(lang === "zh" ? "en" : "zh")}>{lang === "zh" ? "English" : "中文"}</button></div>
    </header>

    {historical && <div className={styles.notice}><BookOpen size={18} /><div><strong>{c.demoTitle}</strong><p>{c.demoBody}</p></div><button onClick={() => setShowPreview(false)}>{c.exit}</button></div>}
    {!historical && feed?.status && feed.status.status !== "ok" && <div className={styles.notice}><div><strong>{feed.status.status === "login_required" ? c.expired : c.partial}</strong><p>{c.retry}</p></div></div>}

    <div className={styles.toolbar}><div className={styles.summary}><strong>{rows.length} {c.courses}</strong><span>{totals.submitted} / {totals.total} {c.assignmentsDone}</span>{!!totals.unknown && <span>{totals.unknown} {c.unknownItems}</span>}</div><label className={styles.search}><Search size={16} /><input aria-label={c.search} placeholder={c.searchPlaceholder} value={query} onChange={e => setQuery(e.target.value)} /></label></div>

    <div className={styles.legend}><span><i className={styles.doneDot} />{c.submitted}</span><span><i className={styles.unknownDot} />{c.unknown}</span><span><i className={styles.todoDot} />{c.missing}</span></div>
    <div className={styles.list}>
      {filtered.map(({ course, total, submitted, unknown, missing }) => {
        const code = course.moduleName.match(/^[A-Z]\d+/)?.[0] || "Module";
        const name = course.moduleName.replace(/^[A-Z]\d+\s*/, "");
        const open = expanded === course.noteId;
        const lessons = course.lessons.filter(l => l.number !== null);
        const completedIds = new Set(notes.find(n => n.id === course.noteId)?.lmsCompletedLessons || []);
        const learned = lessons.filter(l => completedIds.has(l.id)).length;
        return <article key={course.noteId} className={styles.row}>
          <button className={styles.rowButton} aria-expanded={open} aria-controls={`course-${course.id}`} onClick={() => setExpanded(open ? null : course.noteId)}>
            <div className={styles.code}>{code}</div>
            <div className={styles.content}><div className={styles.title}><h2>{name}</h2><span>{course.school}</span></div>
              <div className={styles.progressLabel}><span>{c.submission}</span><span>{total ? `${submitted} / ${total} ${c.submitted}${unknown ? ` · ${unknown} ${c.unknown}` : ""}${missing ? ` · ${missing} ${c.missing}` : ""}` : c.noAssignments}</span></div>
              <Track value={submitted} total={total} unknown={unknown} label={`${course.moduleName} ${c.submission}`} />
              {!!lessons.length && <><div className={styles.progressLabel}><span>{c.learning}</span><span>{learned} / {lessons.length} {c.learned}</span></div><Track value={learned} total={lessons.length} label={`${course.moduleName} ${c.learning}`} /></>}
            </div>
            <div className={styles.percent}>{total ? `${Math.round(submitted / total * 100)}%` : "—"}<ChevronDown size={18} style={{ transform: open ? "rotate(180deg)" : undefined }} /></div>
          </button>
          {open && <div id={`course-${course.id}`} className={styles.details}>
            <div className={styles.detailHeader}><h3>{c.materials}</h3><div className={styles.actions}><button onClick={() => onAskCourse(course)}>{c.ask}</button>{!historical && <button onClick={() => onOpenNote(course.noteId)}><FileText size={15} />{c.notes}</button>}</div></div>
            <p className={styles.emptyDetail}>{course.lessons.flatMap(l => l.materials).filter(m => m.text?.trim()).length} {c.readable} · {course.lessons.flatMap(l => l.materials).filter(m => !m.text?.trim()).length} {c.unreadable}</p>
            {historical && <p className={styles.emptyDetail}>{c.demoLesson}</p>}
            {!course.lessons.length ? <p className={styles.emptyDetail}>{c.noLessons}</p> : course.lessons.map(lesson => <div key={lesson.id} className={styles.lesson}>
              {lesson.number !== null ? <label><input type="checkbox" disabled={historical} checked={completedIds.has(lesson.id)} onChange={() => onToggleLesson(course.noteId, lesson.id)} /><span>{lesson.kind ?? "Lesson"} {String(lesson.number).padStart(2, "0")}{lesson.endNumber && lesson.endNumber !== lesson.number ? `–${String(lesson.endNumber).padStart(2, "0")}` : ""} · {lesson.title}</span></label> : <strong>{c.resources} · {lesson.title}</strong>}
              <ul>{lesson.materials.map(m => <li key={m.id}><a href={m.url} target="_blank" rel="noreferrer">{m.title}<ExternalLink size={12} /></a><span>{m.filePath ? c.archived : c.schoolFile}</span></li>)}</ul>
            </div>)}
            {!!course.assignments.length && <><h3 className={styles.assignmentHeading}>{c.schoolAssignments}</h3><ul className={styles.assignments}>{course.assignments.map(a => <li key={a.id}><a href={a.url} target="_blank" rel="noreferrer">{a.title}<ExternalLink size={12} /></a><span>{c[a.submissionState === "submitted" ? "submitted" : a.submissionState === "missing" ? "missing" : "unknown"]}</span></li>)}</ul></>}
          </div>}
        </article>;
      })}
      {!filtered.length && <div className={styles.empty}><BookOpen size={32} /><h2>{rows.length ? c.notFound : c.empty}</h2><p>{rows.length ? c.searchTip : c.loginTip}</p>{!rows.length && <button onClick={() => setShowPreview(true)}>{c.showDemo}</button>}</div>}
    </div>
    <footer className={styles.footer}>{c.footer}</footer>
  </section>;
}
