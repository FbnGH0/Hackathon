import { z } from "zod";

const id = z.string().regex(/^[a-zA-Z0-9_-]{1,100}$/);
const title = z.string().min(1).max(500);
const url = z.string().url().max(2000).refine(v => new URL(v).protocol === "https:");
export const lmsSnapshotSchema = z.object({
  school: title,
  baseUrl: url,
  timezone: z.string().max(80).refine(v => { try { new Intl.DateTimeFormat("en", { timeZone: v }); return true; } catch { return false; } }),
  syncedAt: z.string().datetime({ offset: true }),
  enableAiBrief: z.boolean().default(true),
  status: z.enum(["ok", "partial", "login_required", "error"]),
  warnings: z.array(z.string().max(1000)).max(200),
  courses: z.array(z.object({
    id, moduleName: title,
    lessons: z.array(z.object({
      id, number: z.number().int().nonnegative().nullable(), title,
      kind: z.enum(["Lesson", "Week"]).optional(),
      endNumber: z.number().int().nonnegative().optional(),
      directory: z.string().max(200).optional(),
      materials: z.array(z.object({
        id, title, url, modifiedAt: z.string().max(100).nullable().optional(),
        filePath: z.string().max(2000).optional(),
        text: z.string().max(20000).optional(),
        textStatus: z.enum(["ready", "truncated", "empty", "unsupported", "error"]).optional(),
      })).max(500),
    })).max(500),
    assignments: z.array(z.object({
      id, title, url,
      dueAt: z.string().datetime({ offset: true }).nullable(),
      submissionState: z.enum(["missing", "submitted", "unknown"]),
    })).max(1000),
    announcements: z.array(z.object({
      id, title, text: z.string().max(8000), url,
      modifiedAt: z.string().max(100).nullable().optional(),
    })).max(500),
  })).max(100),
}).superRefine((data, ctx) => {
  const textLength = data.courses.reduce((sum, c) => sum + c.lessons.reduce((n, l) => n + l.materials.reduce((m, file) => m + (file.text?.length || 0), 0), 0), 0);
  if (textLength > 1_200_000) ctx.addIssue({ code: "custom", message: "Course content exceeds the text budget" });
  const origin = new URL(data.baseUrl).origin;
  for (const course of data.courses) {
    for (const item of [...course.assignments, ...course.announcements, ...course.lessons.flatMap(l => l.materials)]) {
      if (new URL(item.url).origin !== origin) ctx.addIssue({ code: "custom", message: "LMS links must match the school origin" });
    }
  }
});

export type LmsSnapshot = z.infer<typeof lmsSnapshotSchema>;
