// 数据契约（v1）：与 src/rebas/render/export.py 对应。
// web/data/ 由 `rebas render` 导出，构建期静态读取，产物零运行时 JS。

import siteJson from "../../data/site.json";

export type Kind = "paper" | "news" | "repo";

export interface SourceRef {
  title: string;
  url: string;
  source: string;
  kind: Kind;
}

export interface Topic {
  key: string;
  title: string;
  kind: Kind;
  slot: string;
  is_feature: boolean;
  summary: string;
  meta: string[];
  update_of: string | null;
  /** 报道页文件名；null = 无正文报道页，回退原文外链 */
  page: string | null;
  /** 首条材料原文链接；null = 极端情况无材料（前端渲染纯文本标题） */
  url: string | null;
  source: string;
  /** 配图 URL；null = 无图，渲染纯文字版式 */
  image: string | null;
  /** 多图排版（艺术/设计板块）：上游图库 ≥2 张时给出（含 image），否则缺省 */
  images?: string[];
  body_html?: string;
  read_minutes?: number;
  sources?: SourceRef[];
}

export interface BoardMeta {
  id: string;
  name: string;
  name_full: string;
  en: string;
}

export interface Board extends BoardMeta {
  headline: Topic | null;
  features: Topic[];
  briefs: Topic[];
}

export interface Issue {
  date: string;
  no: number;
  weekday: string;
  prev: string | null;
  next: string | null;
  boards: Board[];
}

export interface IssueIndexEntry {
  date: string;
  no: number;
  weekday: string;
  titles: string[];
  /** true = 超出保留窗口，只在归档存目，无页面 */
  archived: boolean;
}

export interface SiteMeta {
  name: string;
  tagline: string;
  boards: BoardMeta[];
  issues: IssueIndexEntry[];
  latest: string | null;
}

export const site = siteJson as SiteMeta;

const issueModules = import.meta.glob<Issue>("../../data/issues/*.json", {
  eager: true,
  import: "default",
});

export const issues: Issue[] = Object.values(issueModules).sort((a, b) =>
  a.date.localeCompare(b.date),
);

export const latestIssue: Issue | undefined = issues[issues.length - 1];

export function issueHref(boardId: string, date: string): string {
  return `${boardId}-${date}.html`;
}

/** 某期的首页（往期页）；最新一期也有稳定的 issue-*.html 页面 */
export function issueHomeHref(date: string): string {
  return `issue-${date}.html`;
}

export function topicCount(issue: Issue): number {
  return issue.boards.reduce(
    (n, b) => n + (b.headline ? 1 : 0) + b.features.length + b.briefs.length,
    0,
  );
}
