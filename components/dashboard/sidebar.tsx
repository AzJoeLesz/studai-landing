"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { motion, AnimatePresence } from "framer-motion";
import {
  MessageSquare,
  Settings,
  LogOut,
  Menu,
  X,
  type LucideIcon
} from "lucide-react";

import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Logo } from "@/components/brand/logo";
import { LanguageSwitcher } from "@/components/language-switcher";
import { NavItem } from "@/components/dashboard/nav-item";
import { getSupabaseBrowserClient } from "@/lib/supabase/client";
import { useRouter } from "@/i18n/navigation";

interface NavEntry {
  href: string;
  labelKey: "sessions" | "settings";
  icon: LucideIcon;
}

const NAV_ENTRIES: NavEntry[] = [
  { href: "/dashboard/sessions", labelKey: "sessions", icon: MessageSquare },
  { href: "/dashboard/settings", labelKey: "settings", icon: Settings }
];

interface SidebarProps {
  user: {
    email?: string;
    displayName?: string;
  };
}

export function Sidebar({ user }: SidebarProps) {
  const t = useTranslations("nav");
  const router = useRouter();
  const supabase = getSupabaseBrowserClient();
  const [mobileOpen, setMobileOpen] = useState(false);
  const [signingOut, setSigningOut] = useState(false);

  // Close the drawer whenever the route changes or we switch to desktop.
  useEffect(() => {
    const onResize = () => {
      if (window.innerWidth >= 768) setMobileOpen(false);
    };
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  async function handleSignOut() {
    setSigningOut(true);
    await supabase.auth.signOut();
    setSigningOut(false);
    router.replace("/");
  }

  const displayName =
    user.displayName?.trim() ||
    user.email?.split("@")[0] ||
    "—";

  return (
    <>
      {/* Mobile top bar */}
      <div className="flex items-center justify-between border-b border-border bg-background/90 px-4 py-3 backdrop-blur md:hidden">
        <Logo size="sm" />
        <Button
          type="button"
          variant="ghost"
          size="icon"
          onClick={() => setMobileOpen(true)}
          aria-label={t("openMenu")}
        >
          <Menu className="h-5 w-5" />
        </Button>
      </div>

      {/* Mobile drawer */}
      <AnimatePresence>
        {mobileOpen && (
          <>
            <motion.div
              key="backdrop"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.2 }}
              className="fixed inset-0 z-40 bg-foreground/30 backdrop-blur-sm md:hidden"
              onClick={() => setMobileOpen(false)}
              aria-hidden
            />
            <motion.aside
              key="drawer"
              initial={{ x: "-100%" }}
              animate={{ x: 0 }}
              exit={{ x: "-100%" }}
              transition={{ duration: 0.25, ease: [0.22, 1, 0.36, 1] }}
              className="fixed inset-y-0 left-0 z-50 flex w-72 flex-col border-r border-border bg-background md:hidden"
            >
              <SidebarContent
                user={{ email: user.email, displayName }}
                onNavClick={() => setMobileOpen(false)}
                onSignOut={handleSignOut}
                signingOut={signingOut}
                onClose={() => setMobileOpen(false)}
                closeLabel={t("closeMenu")}
              />
            </motion.aside>
          </>
        )}
      </AnimatePresence>

      {/* Desktop sidebar */}
      <aside
        className={cn(
          "sticky top-0 hidden h-screen w-64 shrink-0 flex-col border-r border-border bg-background md:flex"
        )}
      >
        <SidebarContent
          user={{ email: user.email, displayName }}
          onNavClick={() => {}}
          onSignOut={handleSignOut}
          signingOut={signingOut}
        />
      </aside>
    </>
  );
}

interface SidebarContentProps {
  user: { email?: string; displayName: string };
  onNavClick: () => void;
  onSignOut: () => void;
  signingOut: boolean;
  onClose?: () => void;
  closeLabel?: string;
}

function SidebarContent({
  user,
  onNavClick,
  onSignOut,
  signingOut,
  onClose,
  closeLabel
}: SidebarContentProps) {
  const t = useTranslations("nav");

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between px-5 py-5">
        <Logo size="sm" />
        {onClose && (
          <Button
            type="button"
            variant="ghost"
            size="icon"
            onClick={onClose}
            aria-label={closeLabel}
          >
            <X className="h-5 w-5" />
          </Button>
        )}
      </div>

      <nav className="flex-1 overflow-y-auto px-3 py-2">
        <ul className="flex flex-col gap-1">
          {NAV_ENTRIES.map((entry) => (
            <li key={entry.href}>
              <NavItem
                href={entry.href}
                label={t(entry.labelKey)}
                icon={entry.icon}
                onClick={onNavClick}
              />
            </li>
          ))}
        </ul>
      </nav>

      <div className="border-t border-border px-4 py-4">
        <div className="mb-3 flex items-center gap-3">
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-primary/15 text-sm font-medium text-primary">
            {initials(user.displayName)}
          </div>
          <div className="min-w-0 flex-1">
            <p className="truncate text-sm font-medium text-foreground">
              {user.displayName}
            </p>
            {user.email && (
              <p className="truncate text-xs text-muted-foreground">
                {user.email}
              </p>
            )}
          </div>
        </div>

        <div className="mb-3 flex">
          <LanguageSwitcher />
        </div>

        <Button
          type="button"
          variant="ghost"
          className="w-full justify-start text-muted-foreground"
          onClick={onSignOut}
          disabled={signingOut}
        >
          <LogOut className="h-4 w-4" />
          {t("signOut")}
        </Button>
      </div>
    </div>
  );
}

function initials(name: string): string {
  const trimmed = name.trim();
  if (!trimmed || trimmed === "—") return "•";
  const parts = trimmed.split(/\s+/);
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}
