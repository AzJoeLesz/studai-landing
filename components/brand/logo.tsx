import { cn } from "@/lib/utils";

interface LogoProps {
  className?: string;
  size?: "sm" | "md" | "lg";
}

const sizes = {
  sm: "text-2xl",
  md: "text-4xl",
  lg: "text-5xl"
};

export function Logo({ className, size = "md" }: LogoProps) {
  return (
    <span
      className={cn(
        "font-serif font-medium tracking-tight text-foreground select-none",
        sizes[size],
        className
      )}
    >
      Stud<span className="text-primary">AI</span>
    </span>
  );
}
