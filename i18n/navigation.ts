import { createNavigation } from "next-intl/navigation";

import { routing } from "./routing";

/**
 * Locale-aware navigation helpers.
 *
 * Always import Link, useRouter, usePathname, redirect from here —
 * never directly from "next/link" or "next/navigation". These versions
 * automatically preserve / swap the /hu or /en prefix.
 */
export const { Link, redirect, usePathname, useRouter, getPathname } =
  createNavigation(routing);
