'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

import { useEffect, useState, type ReactNode } from 'react';
import { useTranslations } from 'next-intl';
import type { ChangelogEntry, ChangeKind } from '@/lib/changelog';
import { useThemeStore } from '@/lib/themeStore';

// Veyllo design tokens (the app's Tailwind lacks these exact ones, so inline like NotificationsModal).
// This whole modal is styled through this JS palette, which the Tailwind dark-palette swap cannot
// see — so it carries its own dark variant, selected via the theme store. C_LIGHT keeps the
// original values byte-for-byte (light mode stays pixel-identical); C_DARK maps onto the brand
// dark tokens (panel #0f131c, control #161c29, text #e6e9ef, dim #8b93a7, borders #1e2533/#2a3344).
const C_LIGHT = {
  surface: '#ffffff', fg: '#111827', muted: '#5b6472', faint: '#9aa3b2',
  line: '#e7e9ee', surface3: '#f1f4f8', ink: '#2a3142', accent: '#1d4ed8',
  accentSoft: 'rgba(29,78,216,.08)',
  // ink glyph details drawn ON the ink shape (exclamation mark / orb interior)
  inkContrast: '#ffffff',
  // soft radial halo behind the icon
  halo: 'rgba(17,24,39,.10)',
  titleGradient: 'linear-gradient(180deg,#111827 30%,#354155)',
  // the near-black CTA button keeps its brand color in dark mode; the border is the
  // "subtle dark border polish" so it still reads against the dark card (user decision).
  buttonBorder: 'transparent',
};
const C_DARK: typeof C_LIGHT = {
  surface: '#0f131c', fg: '#e6e9ef', muted: '#8b93a7', faint: '#6d7689',
  line: '#1e2533', surface3: '#161c29', ink: '#e6e9ef', accent: '#3b82f6',
  accentSoft: 'rgba(59,130,246,.16)',
  inkContrast: '#0f131c',
  halo: 'rgba(230,233,239,.08)',
  titleGradient: 'linear-gradient(180deg,#e6e9ef 30%,#b9c0cf)',
  buttonBorder: '#2a3344',
};

const READ_SECONDS = 4;
const SECTION_ORDER: ChangeKind[] = ['new', 'improved', 'fixed', 'removed'];

export interface AnnouncementModalProps {
  isOpen: boolean;
  onClose: () => void;
  variant: 'intro' | 'changelog';
  /** e.g. "v2.6" — derived from /api/version (see lib/version.ts). */
  versionDisplay: string;
  /** e.g. "Open Alpha" | "Beta" | null. */
  channel: string | null;
  /** the changelog entry to render (changelog variant only). */
  entry?: ChangelogEntry | null;
}

export default function AnnouncementModal({
  isOpen, onClose, variant, versionDisplay, channel, entry,
}: AnnouncementModalProps) {
  const t = useTranslations('modals.announcement');
  const [secs, setSecs] = useState(0);

  // 4-second read gate for the intro variant; resets every time the modal (re)opens.
  useEffect(() => {
    if (!isOpen || variant !== 'intro') return;
    setSecs(READ_SECONDS);
    const id = setInterval(() => {
      setSecs((s) => {
        if (s <= 1) { clearInterval(id); return 0; }
        return s - 1;
      });
    }, 1000);
    return () => clearInterval(id);
  }, [isOpen, variant]);

  // Pick the palette for the active theme (hook must run before the early return).
  const C = useThemeStore((s) => s.theme) === 'dark' ? C_DARK : C_LIGHT;

  if (!isOpen) return null;

  const badge = channel ? `${channel} · ${versionDisplay}` : versionDisplay;
  const waiting = variant === 'intro' && secs > 0;
  const bold = { b: (chunks: ReactNode) => <b style={{ color: C.fg, fontWeight: 640 }}>{chunks}</b> };

  return (
    <div
      role="dialog"
      aria-modal="true"
      onClick={waiting ? undefined : onClose}
      style={{
        position: 'fixed', inset: 0, zIndex: 1000, display: 'flex',
        alignItems: 'center', justifyContent: 'center', padding: 22,
        background: 'rgba(16,24,40,.40)', backdropFilter: 'blur(4px)',
        WebkitBackdropFilter: 'blur(4px)',
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          // Column layout so long release notes scroll INSIDE the card while the
          // close button and the Got-it footer stay visible (previously the whole
          // card scrolled and the actions moved out of view).
          position: 'relative', width: '100%', maxWidth: 480, background: C.surface,
          border: `1px solid ${C.line}`, borderRadius: 26, overflow: 'hidden', maxHeight: '88vh',
          display: 'flex', flexDirection: 'column',
          boxShadow: '0 30px 70px -24px rgba(16,24,40,.22)',
          animation: 'vafAnnPop .24s cubic-bezier(.22,1,.36,1)', fontFamily: 'inherit',
        }}
      >
        <style>{'@keyframes vafAnnPop{from{opacity:0;transform:translateY(10px) scale(.985)}to{opacity:1;transform:none}}'}</style>

        <button
          onClick={onClose}
          aria-label="Close"
          style={{
            position: 'absolute', top: 16, right: 16, width: 32, height: 32, borderRadius: 8,
            border: 'none', background: 'transparent', color: C.faint, cursor: 'pointer',
            fontSize: 15, lineHeight: 1, display: 'flex', alignItems: 'center', justifyContent: 'center',
          }}
        >✕</button>

        <div style={{ padding: '30px 32px 8px', textAlign: 'center', overflowY: 'auto', flex: '1 1 auto', minHeight: 0 }}>
          {badge && (
            <span style={{
              display: 'inline-flex', alignItems: 'center', gap: 8, fontSize: 12.5, fontWeight: 600,
              letterSpacing: '.04em', color: C.muted, padding: '6px 12px', border: `1px solid ${C.line}`,
              borderRadius: 999, background: C.surface, boxShadow: '0 1px 2px rgba(16,24,40,.05)',
            }}>
              <span style={{ width: 6, height: 6, borderRadius: '50%', background: C.accent }} />
              {badge}
            </span>
          )}

          {/* Icon: dark filled warning triangle (intro) or agent orb (changelog) */}
          <div style={{ position: 'relative', height: 78, display: 'grid', placeItems: 'center', margin: '18px 0 14px' }}>
            <span style={{
              position: 'absolute', width: 140, height: 140, borderRadius: '50%',
              background: 'radial-gradient(circle, rgba(17,24,39,.10), transparent 62%)',
            }} />
            {variant === 'intro' ? (
              <svg width={50} height={50} viewBox="0 0 24 24" style={{ position: 'relative', filter: 'drop-shadow(0 8px 18px rgba(16,24,40,.20))' }}>
                <path
                  d="M10.29 3.7 2.3 17.6a2 2 0 0 0 1.73 3h15.94a2 2 0 0 0 1.73-3L13.71 3.7a2 2 0 0 0-3.42 0Z"
                  fill={C.ink} stroke={C.ink} strokeWidth={1.3} strokeLinejoin="round"
                />
                <line x1="12" y1="9.6" x2="12" y2="14" stroke="#fff" strokeWidth={1.9} strokeLinecap="round" />
                <circle cx="12" cy="17" r="1.15" fill="#fff" />
              </svg>
            ) : (
              <span style={{
                position: 'relative', width: 30, height: 30, borderRadius: '50%', background: C.ink,
                boxShadow: `0 0 0 8px ${C.accentSoft}, 0 8px 20px -6px rgba(16,24,40,.35)`,
              }} />
            )}
          </div>

          <h1 style={{
            fontSize: 22, fontWeight: 680, letterSpacing: '-.02em', lineHeight: 1.15, margin: '0 0 8px',
            background: 'linear-gradient(180deg,#111827 30%,#354155)', WebkitBackgroundClip: 'text',
            backgroundClip: 'text', color: 'transparent',
          }}>
            {variant === 'intro' ? t('intro.title') : t('changelog.title', { version: entry ? `v${entry.version}` : versionDisplay })}
          </h1>

          <p style={{ fontSize: 14.5, color: C.muted, lineHeight: 1.6, margin: '0 auto', maxWidth: '40ch' }}>
            {variant === 'intro' ? t('intro.lead') : t('changelog.lead')}
          </p>

          {variant === 'intro' ? (
            <>
              <ul style={{
                listStyle: 'none', margin: '18px auto 4px', padding: 0, maxWidth: '38ch',
                display: 'flex', flexDirection: 'column', gap: 11, textAlign: 'left',
              }}>
                {[1, 2, 3].map((n) => (
                  <li key={n} style={{ display: 'flex', gap: 11, fontSize: 14, color: C.muted, lineHeight: 1.45 }}>
                    <span style={{
                      flex: '0 0 auto', width: 23, height: 23, borderRadius: '50%', background: C.surface3,
                      border: `1px solid ${C.line}`, color: C.fg, display: 'grid', placeItems: 'center',
                      fontSize: 12, fontWeight: 650, lineHeight: 1, fontVariantNumeric: 'tabular-nums',
                      boxShadow: '0 1px 2px rgba(16,24,40,.05)',
                    }}>{n}</span>
                    <span>{t.rich(`intro.point${n}`, bold)}</span>
                  </li>
                ))}
              </ul>
              <p style={{ fontSize: 14.5, color: C.muted, lineHeight: 1.6, margin: '14px auto 0', maxWidth: '40ch' }}>
                {t('intro.outro')}
              </p>
            </>
          ) : (
            <div style={{ margin: '18px auto 4px', maxWidth: '40ch', textAlign: 'left', display: 'flex', flexDirection: 'column', gap: 16 }}>
              {SECTION_ORDER.map((kind) => {
                const sec = entry?.sections.find((s) => s.kind === kind);
                if (!sec || sec.items.length === 0) return null;
                const isNew = kind === 'new';
                return (
                  <div key={kind}>
                    <div style={{
                      fontSize: 11.5, fontWeight: 650, letterSpacing: '.08em', textTransform: 'uppercase',
                      color: isNew ? C.accent : C.faint, marginBottom: 7, display: 'flex', alignItems: 'center', gap: 7,
                    }}>
                      <span style={{ width: 6, height: 6, borderRadius: '50%', background: isNew ? C.accent : C.faint }} />
                      {t(`sections.${kind}`)}
                    </div>
                    <ul style={{ listStyle: 'none', margin: 0, padding: 0, display: 'flex', flexDirection: 'column', gap: 6 }}>
                      {sec.items.map((item, i) => (
                        <li key={i} style={{ display: 'flex', gap: 9, fontSize: 14, color: C.muted, lineHeight: 1.45 }}>
                          <span style={{ flex: '0 0 auto', marginTop: 8, width: 5, height: 5, borderRadius: '50%', background: C.line }} />
                          <span>{item}</span>
                        </li>
                      ))}
                    </ul>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        <div style={{ padding: '20px 32px 26px', flex: '0 0 auto' }}>
          <button
            onClick={onClose}
            disabled={waiting}
            style={{
              display: 'inline-flex', alignItems: 'center', justifyContent: 'center', gap: 8, width: '100%',
              height: 46, borderRadius: 999, border: '1px solid transparent', cursor: waiting ? 'default' : 'pointer',
              fontFamily: 'inherit', fontSize: 14.5, fontWeight: 600, fontVariantNumeric: 'tabular-nums',
              background: waiting ? C.surface3 : '#111827', color: waiting ? C.faint : '#fff',
              boxShadow: waiting ? 'none' : '0 6px 28px -8px rgba(16,24,40,.10)', transition: 'background-color .18s',
            }}
          >
            {variant === 'intro'
              ? (waiting ? `${t('intro.button')} · ${secs}` : t('intro.button'))
              : t('changelog.button')}
          </button>
          <p style={{ margin: '12px 0 0', fontSize: 12.5, color: C.faint, textAlign: 'center' }}>
            {variant === 'intro' ? t('intro.note') : t('changelog.note')}
          </p>
        </div>
      </div>
    </div>
  );
}
