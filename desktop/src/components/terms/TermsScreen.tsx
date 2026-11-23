import React, { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { commands, type TermsText } from "@/bindings";
import { Button } from "../ui/Button";
import { ScreenTitle } from "../onboarding/OnboardingShell";

/** **bold** only; everything else is plain text. */
const inline = (text: string): React.ReactNode[] =>
  text.split("**").map((part, i) => (i % 2 ? <b key={i}>{part}</b> : part));

/**
 * A small, safe reader for TERMS.md (the same rules as the Windows app):
 * "## " headings, "- " lists, "---" rules and paragraphs. The title and the
 * version line are shown in the screen's header instead.
 */
const renderTerms = (markdown: string): React.ReactNode[] => {
  const out: React.ReactNode[] = [];
  markdown
    .replace(/^﻿/, "")
    .split(/\n\s*\n/)
    .forEach((block, b) => {
      const lines = block
        .split("\n")
        .map((l) => l.trimEnd())
        .filter((l) => l.trim());
      if (!lines.length) return;
      if (lines.every((l) => l.trim() === "---")) {
        out.push(<hr key={b} className="my-4 border-line" />);
        return;
      }
      if (lines[0].startsWith("# ") || /^Version \S+ /.test(lines[0])) return;
      if (lines[0].startsWith("## ")) {
        out.push(
          <h2
            key={`${b}h`}
            className="mt-5 mb-2 font-display text-row font-[650] text-text"
          >
            {lines[0].slice(3)}
          </h2>,
        );
        lines.shift();
        if (!lines.length) return;
      }
      if (lines.every((l) => l.startsWith("- "))) {
        out.push(
          <ul key={b} className="my-2 list-disc ps-5">
            {lines.map((l, i) => (
              <li key={i}>{inline(l.slice(2))}</li>
            ))}
          </ul>,
        );
        return;
      }
      out.push(
        <p key={b} className="my-2">
          {inline(lines.join(" "))}
        </p>,
      );
    });
  return out;
};

interface TermsScreenProps {
  /**
   * "gate": the terms are not accepted yet, so the screen covers the window
   * with "Quit" and "I agree". "view": Settings > About, with "Close".
   */
  mode: "gate" | "view";
  /** Gate: called once the agreement is saved. */
  onAccepted?: () => void;
  /** View: called by "Close". */
  onClose?: () => void;
}

/** The Terms of Use, as the Windows app shows them. */
export const TermsScreen: React.FC<TermsScreenProps> = ({
  mode,
  onAccepted,
  onClose,
}) => {
  const { t } = useTranslation();
  const [terms, setTerms] = useState<TermsText | null>(null);
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState(false);
  // A status line set by "I agree" or "Quit"; until then the screen's own
  // hint, worded in the current language.
  const [say, setSay] = useState<string | null>(null);
  const body = useRef<HTMLDivElement>(null);
  const actions = useRef<HTMLDivElement>(null);

  // View mode: Escape closes, as "Close" does.
  useEffect(() => {
    if (mode !== "view") return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose?.();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [mode, onClose]);

  useEffect(() => {
    let live = true;
    commands
      .getTerms()
      .then((text) => {
        if (!live) return;
        setTerms(text);
        // Focus the text, not "I agree": a stray Enter or Space (the window
        // may open while someone is typing) must not accept terms nobody has
        // read, and the arrow keys and Page Down scroll the text at once.
        requestAnimationFrame(() => {
          body.current?.scrollTo({ top: 0 });
          if (mode === "gate") body.current?.focus({ preventScroll: true });
          else actions.current?.querySelector("button")?.focus();
        });
      })
      .catch((e) => {
        console.error("Failed to load the Terms of Use:", e);
        if (!live) return;
        setFailed(true);
      });
    return () => {
      live = false;
    };
  }, [mode]);

  const agree = async () => {
    if (busy || !terms) return;
    setBusy(true);
    setSay(t("terms.saving"));
    const result = await commands.acceptTerms(terms.version);
    if (result.status === "error") {
      setBusy(false);
      setSay(result.error);
      return;
    }
    // Ready again in case the app lands back on this screen.
    setBusy(false);
    setSay(null);
    onAccepted?.();
  };

  const quit = async () => {
    setSay(t("terms.quitting"));
    await commands.quitApp();
  };

  return (
    <div
      className={`${mode === "view" ? "fixed inset-0 z-50" : "h-screen w-full"} flex overflow-hidden bg-background select-none cursor-default`}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="terms-title"
        className="m-auto flex h-[min(600px,100vh)] w-[min(720px,100vw)] flex-col px-[28px] py-[20px] font-ui text-body leading-[1.6] text-text antialiased"
      >
        <div className="flex-none">
          <ScreenTitle className="text-start">
            <span id="terms-title">{t("terms.title")}</span>
          </ScreenTitle>
          <p className="mt-[4px] mb-0 text-small text-ink-3">
            {terms?.version_line ?? ""}
          </p>
        </div>
        <div
          ref={body}
          role="region"
          tabIndex={0}
          aria-label={t("terms.regionLabel")}
          className="mt-[14px] min-h-0 flex-1 select-text overflow-y-auto rounded-lg border border-line bg-wash px-[18px] py-[10px] text-small leading-[1.55] focus:outline-none focus-visible:ring-1 focus-visible:ring-logo-primary"
        >
          {terms ? (
            renderTerms(terms.text)
          ) : (
            <p className="my-2 text-ink-3">
              {failed ? t("terms.loadFailed") : t("terms.loading")}
            </p>
          )}
        </div>
        <div className="flex-none pt-[12px]">
          {mode === "gate" && (
            <p className="mt-0 mb-[10px] text-small text-ink-2">
              {t("terms.agreeNote")}
            </p>
          )}
          <div ref={actions} className="flex items-center gap-[12px]">
            <span
              className="min-w-0 flex-1 text-small text-ink-3"
              aria-live="polite"
            >
              {say ??
                (mode !== "gate"
                  ? ""
                  : failed
                    ? t("terms.loadFailed")
                    : t("terms.gateHint"))}
            </span>
            {mode === "gate" ? (
              <>
                <Button variant="outline" size="lg" onClick={quit}>
                  {t("terms.quit")}
                </Button>
                <Button
                  variant="primary"
                  size="lg"
                  disabled={!terms || busy}
                  onClick={agree}
                >
                  {t("terms.agree")}
                </Button>
              </>
            ) : (
              <Button variant="primary" size="lg" onClick={onClose}>
                {t("terms.close")}
              </Button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
};
