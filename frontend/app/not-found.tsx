import Link from "next/link";

export default function NotFound() {
  return (
    <div className="py-16 text-center">
      <h1 className="text-2xl font-semibold text-ink">Page not found</h1>
      <p className="mt-2 text-sm text-ink-2">The page or record does not exist.</p>
      <Link href="/" className="mt-4 inline-block text-accent hover:underline">
        Back to the overview
      </Link>
    </div>
  );
}
