"use client";

export default function ErrorPage({ reset }: { error: Error; reset: () => void }) {
  // The error message is not shown: it may contain internal details.
  return (
    <div className="py-16 text-center" role="alert">
      <h1 className="text-2xl font-semibold text-ink">Something went wrong</h1>
      <p className="mt-2 text-sm text-ink-2">The page could not be rendered. No partial data is shown.</p>
      <button
        type="button"
        onClick={reset}
        className="mt-4 rounded-md border border-line px-3 py-1.5 text-sm hover:bg-raised"
      >
        Try again
      </button>
    </div>
  );
}
