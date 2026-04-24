"use client";

import type { LucideIcon } from "lucide-react";

import { cn } from "@/lib/utils";
import { Link, usePathname } from "@/i18n/navigation";

interface NavItemProps {
  href: string;
  label: string;
  icon: LucideIcon;
  onClick?: () => void;
}

export function NavItem({ href, label, icon: Icon, onClick }: NavItemProps) {
  const pathname = usePathname();
  const isActive =
    pathname === href || (href !== "/dashboard" && pathname.startsWith(href));

  return (
    <Link
      href={href}
      onClick={onClick}
      className={cn(
        "group flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors duration-200 ease-brand",
        isActive
          ? "bg-accent text-foreground"
          : "text-muted-foreground hover:bg-accent/60 hover:text-foreground"
      )}
      aria-current={isActive ? "page" : undefined}
    >
      <Icon
        className={cn(
          "h-4 w-4 shrink-0 transition-colors",
          isActive ? "text-primary" : "text-muted-foreground"
        )}
        aria-hidden
      />
      <span className="truncate">{label}</span>
    </Link>
  );
}
