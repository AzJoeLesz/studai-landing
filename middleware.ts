import createMiddleware from "next-intl/middleware";

import { routing } from "./i18n/routing";

export default createMiddleware(routing);

export const config = {
  // Match all pathnames except static assets, API routes, and Next internals.
  matcher: ["/((?!api|_next|_vercel|.*\\..*).*)"]
};
