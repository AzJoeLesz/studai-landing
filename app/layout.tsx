import "./globals.css";

/**
 * Root layout.
 *
 * With next-intl + App Router, the `<html>` tag and all real layout logic
 * live in app/[locale]/layout.tsx — that's where we know the user's locale.
 * This file exists only because Next requires a root layout to load globals.
 */
export default function RootLayout({
  children
}: {
  children: React.ReactNode;
}) {
  return children;
}
