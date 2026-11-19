import { useEffect, useRef } from "react";

const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [contenteditable="true"], [tabindex]:not([tabindex="-1"])';

/**
 * Keyboard focus for a modal dialog (WCAG 2.4.3): while `open`, focus moves into the element the returned ref is on
 * (unless something in it already has it, e.g. an autoFocus field), Tab and Shift+Tab cycle inside it, and on close
 * focus returns to what had it before (usually the button that opened the dialog).
 */
export function useDialogFocus<T extends HTMLElement>(open: boolean) {
  const ref = useRef<T>(null);
  useEffect(() => {
    const box = ref.current;
    if (!open || !box) return;
    const before = document.activeElement as HTMLElement | null;
    const items = () => [...box.querySelectorAll<HTMLElement>(FOCUSABLE)].filter((el) => el.getClientRects().length > 0);
    if (!box.contains(document.activeElement)) (items()[0] ?? box).focus();

    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Tab") return;
      const list = items();
      if (!list.length) { e.preventDefault(); return; }
      const first = list[0], last = list[list.length - 1];
      if (e.shiftKey && (document.activeElement === first || !box.contains(document.activeElement))) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && (document.activeElement === last || !box.contains(document.activeElement))) { e.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      if (before && document.contains(before)) before.focus();
    };
  }, [open]);
  return ref;
}
