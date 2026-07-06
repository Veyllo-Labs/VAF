// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
import Link from 'next/link';

// Custom 404. Without this, Next renders its built-in white error page, which would
// stay light in dark mode. Uses swapped utilities (bg-gray-50 / text-gray-*), so it
// flips with the theme automatically and stays byte-identical in light mode.
export default function NotFound() {
  return (
    <main className="h-screen flex flex-col items-center justify-center gap-4 bg-gray-50 text-gray-900 px-6 text-center">
      <p className="text-6xl font-bold tracking-tight text-gray-300">404</p>
      <h1 className="text-lg font-semibold">Page not found</h1>
      <p className="text-sm text-gray-500 max-w-sm">
        The page you are looking for does not exist or has been moved.
      </p>
      <Link
        href="/"
        className="mt-2 px-4 py-2 rounded-lg text-sm font-medium bg-gray-900 text-white hover:bg-gray-800 transition-colors dark:bg-[#e6e6e6] dark:text-[#181818] dark:hover:bg-white dark:shadow-none"
      >
        Back to VAF
      </Link>
    </main>
  );
}
