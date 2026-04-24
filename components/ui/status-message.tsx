import { cn } from "@/lib/utils";

type StatusType = "info" | "success" | "error";

interface StatusMessageProps {
  type?: StatusType;
  children: React.ReactNode;
  className?: string;
}

const variantClasses: Record<StatusType, string> = {
  info: "bg-muted text-muted-foreground border-border",
  success:
    "bg-success/10 text-success border-success/30 dark:text-success-foreground",
  error:
    "bg-destructive/10 text-destructive border-destructive/30 dark:text-destructive-foreground"
};

export function StatusMessage({
  type = "info",
  children,
  className
}: StatusMessageProps) {
  return (
    <p
      role={type === "error" ? "alert" : "status"}
      className={cn(
        "mt-4 rounded-md border px-3.5 py-2.5 text-sm leading-relaxed animate-fade-in",
        variantClasses[type],
        className
      )}
    >
      {children}
    </p>
  );
}
