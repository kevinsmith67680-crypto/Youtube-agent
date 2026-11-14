import Link from "next/link";

import { withParams } from "@/lib/format";

export function Pagination({
  basePath,
  params,
  page,
  pageSize,
  total,
}: {
  basePath: string;
  params: Record<string, string | undefined>;
  page: number;
  pageSize: number;
  total: number;
}) {
  const pages = Math.max(1, Math.ceil(total / pageSize));
  const from = total === 0 ? 0 : (page - 1) * pageSize + 1;
  const to = Math.min(total, page * pageSize);
  const link = "rounded-md border border-line px-3 py-1.5 text-sm hover:bg-raised";
  return (
    <nav aria-label="Pagination" className="mt-4 flex flex-wrap items-center justify-between gap-3">
      <p className="text-sm text-ink-2" aria-live="polite">
        {total === 0 ? "No results" : `Showing ${from}–${to} of ${total}`}
      </p>
      <div className="flex gap-2">
        {page > 1 ? (
          <Link className={link} href={`${basePath}${withParams(params, { page: page - 1 })}`} rel="prev">
            Previous
          </Link>
        ) : (
          <span className={`${link} opacity-50`} aria-disabled="true">
            Previous
          </span>
        )}
        <span className="px-2 py-1.5 text-sm text-ink-2">
          Page {page} of {pages}
        </span>
        {page < pages ? (
          <Link className={link} href={`${basePath}${withParams(params, { page: page + 1 })}`} rel="next">
            Next
          </Link>
        ) : (
          <span className={`${link} opacity-50`} aria-disabled="true">
            Next
          </span>
        )}
      </div>
    </nav>
  );
}
