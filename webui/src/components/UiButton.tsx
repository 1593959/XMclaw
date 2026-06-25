import type { ButtonHTMLAttributes, ReactNode } from "react";

type ButtonTone = "neutral" | "danger" | "accent";

const toneClass: Record<ButtonTone, string> = {
  neutral: "text-mc-faint hover:text-mc-text hover:border-mc-accent/50",
  danger: "text-mc-faint hover:text-mc-err hover:border-mc-err/50",
  accent: "text-mc-accent border-mc-accent/40 hover:bg-mc-accent/10",
};

export function IconButton({
  label,
  title,
  tone = "neutral",
  className = "",
  children,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  label: string;
  title?: string;
  tone?: ButtonTone;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      title={title || label}
      className={
        "inline-flex items-center justify-center rounded border border-mc-border " +
        "transition-colors cursor-pointer focus-visible:outline-none focus-visible:ring-2 " +
        "focus-visible:ring-mc-accent/60 disabled:cursor-not-allowed disabled:opacity-50 " +
        toneClass[tone] +
        " " +
        className
      }
      {...props}
    >
      {children}
    </button>
  );
}

export function ToggleButton({
  pressed,
  label,
  title,
  className = "",
  children,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  pressed: boolean;
  label: string;
  title?: string;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      aria-pressed={pressed}
      title={title || label}
      className={
        "inline-flex items-center justify-center rounded transition-colors cursor-pointer " +
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-mc-accent/60 " +
        "disabled:cursor-not-allowed disabled:opacity-50 " +
        className
      }
      {...props}
    >
      {children}
    </button>
  );
}
