import Link from "next/link";

export type Field =
  | { kind: "select"; name: string; label: string; options: { value: string; label: string }[] }
  | { kind: "text"; name: string; label: string; placeholder?: string }
  | { kind: "number"; name: string; label: string; min?: number; max?: number; step?: number }
  | { kind: "checkbox"; name: string; label: string };

const input =
  "mt-1 block w-full rounded-md border border-line bg-page px-2 py-1.5 text-sm text-ink";

/** A plain GET form: filters live in the URL (shareable, back-button friendly, works
 * without JavaScript). One row above the content it scopes. */
export function FilterForm({
  action,
  fields,
  values,
}: {
  action: string;
  fields: Field[];
  values: Record<string, string | undefined>;
}) {
  return (
    <form
      method="get"
      action={action}
      role="search"
      aria-label="Filters"
      className="mb-4 flex flex-wrap items-end gap-3 rounded-xl border border-line bg-surface p-3"
    >
      {fields.map((f) => {
        const id = `f-${f.name}`;
        if (f.kind === "checkbox") {
          return (
            <div key={f.name} className="flex items-center gap-2 pb-1.5">
              <input
                id={id}
                type="checkbox"
                name={f.name}
                value="true"
                defaultChecked={values[f.name] === "true"}
                className="h-4 w-4"
              />
              <label htmlFor={id} className="text-sm text-ink-2">
                {f.label}
              </label>
            </div>
          );
        }
        return (
          <div key={f.name} className="min-w-[9rem]">
            <label htmlFor={id} className="text-xs font-medium text-ink-2">
              {f.label}
            </label>
            {f.kind === "select" ? (
              <select id={id} name={f.name} defaultValue={values[f.name] ?? ""} className={input}>
                {f.options.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            ) : f.kind === "number" ? (
              <input
                id={id}
                name={f.name}
                type="number"
                min={f.min}
                max={f.max}
                step={f.step ?? 1}
                defaultValue={values[f.name] ?? ""}
                className={input}
              />
            ) : (
              <input
                id={id}
                name={f.name}
                type="search"
                maxLength={120}
                placeholder={f.placeholder}
                defaultValue={values[f.name] ?? ""}
                className={input}
              />
            )}
          </div>
        );
      })}
      <div className="flex gap-2 pb-0.5">
        <button type="submit" className="rounded-md bg-action px-3 py-1.5 text-sm font-medium text-white hover:opacity-90">
          Apply
        </button>
        <Link href={action} className="rounded-md border border-line px-3 py-1.5 text-sm text-ink-2 hover:bg-raised">
          Reset
        </Link>
      </div>
    </form>
  );
}

export const CATEGORY_OPTIONS = [
  { value: "", label: "All categories" },
  { value: "ai", label: "Artificial intelligence" },
  { value: "technology", label: "Technology" },
  { value: "gaming", label: "Gaming" },
];
