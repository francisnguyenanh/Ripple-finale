// Dialog (confirmations) and drawer (details) on the native <dialog> element.
// Why not Radix Dialog: its scroll lock injects a <style> element, which the strict CSP
// (style-src 'self') blocks. Native showModal() gives a focus trap, Esc, inert background and
// focus return to the opener without injecting styles.
//
// A confirmation dialog is always modal. A details drawer (side="right") is modal only on a phone-sized screen, where it covers the whole
// page; on a wider screen it is a NON-modal panel: the page behind stays usable (menu, links, Back), Esc closes it from anywhere, and any
// change of route closes it, so a person never has to hunt for a hidden Close button before they can go somewhere else.
import { X } from "lucide-react";
import * as React from "react";
import { cn } from "@/lib/cn";

export interface ModalProps {
  open: boolean;
  onClose: () => void;
  title: string;
  description: string;
  side?: "center" | "right";
  className?: string;
  children?: React.ReactNode;
}

const PHONE = "(max-width: 639px)";

export function Modal({ open, onClose, title, description, side = "center", className, children }: ModalProps) {
  const ref = React.useRef<HTMLDialogElement>(null);
  const uid = React.useId();
  const [modal, setModal] = React.useState(true);
  const onCloseRef = React.useRef(onClose);
  onCloseRef.current = onClose;

  React.useEffect(() => {
    const d = ref.current;
    if (!d) return;
    const asModal = side === "center" || window.matchMedia(PHONE).matches;
    setModal(asModal);
    const opener = document.activeElement as HTMLElement | null;
    if (open && !d.open) {
      if (asModal) d.showModal(); else d.show();
      if (!asModal) (d.querySelector("[data-autofocus]") as HTMLElement | null)?.focus();
    }
    if (!open && d.open) d.close();
    document.body.classList.toggle("modal-open", open && asModal);
    if (!open) return () => { document.body.classList.remove("modal-open"); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") { e.preventDefault(); onCloseRef.current(); } };
    const nav = () => onCloseRef.current();
    if (!asModal) { document.addEventListener("keydown", esc); window.addEventListener("hashchange", nav); }
    return () => {
      document.body.classList.remove("modal-open");
      document.removeEventListener("keydown", esc);
      window.removeEventListener("hashchange", nav);
      // a non-modal dialog does not give focus back by itself
      // (the drawer's content is already gone when this runs, so focus has fallen back to the page: give it back to the opener)
      const ae = document.activeElement;
      if (!asModal && opener && document.contains(opener) && (ae === document.body || !ae || ref.current?.contains(ae))) opener.focus();
    };
  }, [open, side]);

  return (
    <dialog
      ref={ref}
      aria-labelledby={`${uid}-t`}
      aria-describedby={`${uid}-d`}
      aria-modal={modal ? "true" : "false"}
      onClose={onClose}
      onClick={(e) => { if (e.target === ref.current) onClose(); }}
      className={cn(
        "rp-dialog bg-surface text-text border border-border shadow-pop p-0 m-0",
        side === "center"
          ? "fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 w-[min(32rem,calc(100vw-2rem))] rounded-lg"
          : "fixed right-0 top-0 left-auto h-full max-h-none w-[min(28rem,100vw)] rounded-l-lg max-sm:rounded-none overflow-auto",
        className,
      )}
    >
      {open && (
        <div className="p-6 relative anim-in">
          <h2 id={`${uid}-t`} className="text-xl font-semibold pr-8">{title}</h2>
          <p id={`${uid}-d`} className="text-sm text-muted mt-1">{description}</p>
          <div className="mt-4">{children}</div>
          <button type="button" data-autofocus aria-label="Close" onClick={onClose} className="absolute right-3 top-3 rounded-md p-2 hover:bg-surface-2 cursor-pointer">
            <X size={18} aria-hidden="true" />
          </button>
        </div>
      )}
    </dialog>
  );
}
