import Link from "next/link";

export default function NotFound() {
  return (
    <div className="flex min-h-dvh flex-col items-center justify-center px-4 text-center">
      <p className="text-sm font-medium text-accent">404</p>
      <h1 className="mt-2 text-2xl font-semibold">Page not found</h1>
      <p className="mt-2 text-sm text-muted">The page you&apos;re looking for doesn&apos;t exist.</p>
      <Link href="/" className="mt-6 text-sm font-medium text-accent hover:underline">
        Go home
      </Link>
    </div>
  );
}
