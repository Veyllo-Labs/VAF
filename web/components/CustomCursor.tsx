"use client";

import { useRef, useEffect, useCallback } from "react";

type CursorState = "default" | "pointer" | "text";

// ── READING ANIMATION CONSTANTS ──
const LINE_DURATION  = 2.5;
const LINES_PER_PAGE = 6;
const LINE_HEIGHT    = 22;
const SCAN_WIDTH     = 100;

// ── SEQUENCE STEP TYPES ──
type SeqStep =
  | { type: "move-el";  selector: string; fallback?: { x: number; y: number }; ms: number }
  | { type: "move-pos"; x: number; y: number; ms: number }
  | { type: "click" }
  | { type: "type"; text: string; charMs?: number }
  | { type: "wait"; ms: number }
  | { type: "clear-label" };

// ── TOOL SEQUENCE DEFINITIONS ──
// Only tools that do real UI navigation need a custom sequence.
// Generic tool input animation is handled inside ToolMessage itself.
function buildSequence(tool: string, args: Record<string, unknown>): SeqStep[] {
  const vw = window.innerWidth, vh = window.innerHeight;
  const mc = { x: vw / 2, y: vh / 2 };

  if (tool === "create_workflow") {
    const name  = String(args.name  ?? args.workflow_name ?? "New Workflow");
    const steps = Array.isArray(args.steps) ? args.steps : [];
    const firstStep = typeof steps[0] === "string"
      ? steps[0]
      : typeof steps[0] === "object" && steps[0] !== null
        ? String((steps[0] as Record<string,unknown>).input ?? (steps[0] as Record<string,unknown>).description ?? "")
        : "";
    return [
      { type: "move-el",  selector: '[data-agent-hint="nav-settings"]', fallback: { x: 24, y: vh * 0.88 }, ms: 600 },
      { type: "click" },
      { type: "wait",     ms: 350 },
      { type: "move-pos", x: mc.x - 150, y: mc.y - 120, ms: 500 },
      { type: "click" },
      { type: "type",     text: name, charMs: 32 },
      ...(firstStep ? [
        { type: "move-pos" as const, x: mc.x, y: mc.y + 30, ms: 350 },
        { type: "click"    as const },
        { type: "type"     as const, text: firstStep, charMs: 22 },
      ] : []),
      { type: "move-pos", x: mc.x + 170, y: mc.y + 220, ms: 400 },
      { type: "click" },
      { type: "clear-label" },
      { type: "wait", ms: 400 },
    ];
  }

  // No external cursor animation for other tools — ToolMessage card handles it
  return [];
}

export function CustomCursor() {
  const mainCursorRef  = useRef<HTMLDivElement>(null);
  const trailCursorRef = useRef<HTMLDivElement>(null);
  const canvasRef      = useRef<HTMLCanvasElement>(null);
  const mainInnerRef   = useRef<HTMLDivElement>(null);
  const trailInnerRef  = useRef<HTMLDivElement>(null);
  const typingLabelRef = useRef<HTMLDivElement>(null);

  const positionRef      = useRef({ x: 0, y: 0 });
  const trailPositionRef = useRef({ x: 0, y: 0 });
  const isVisibleRef     = useRef(false);
  const isClickingRef    = useRef(false);
  const cursorStateRef   = useRef<CursorState>("default");
  const rafRef           = useRef<number | null>(null);

  // ── LINE visibility (fades in when moving, out when still) ──
  const lineAlphaRef = useRef(0);
  const prevPosRef   = useRef({ x: 0, y: 0 });

  // ── AGENT / PDF reading mode ──
  const agentModeRef     = useRef(false);
  const agentPageTimeRef = useRef(0);
  const agentPageElRef   = useRef<Element | null>(null);
  const agentCenterRef   = useRef({ x: 0, y: 0 });

  // ── SEQUENCE / tool simulation ──
  const seqActiveRef     = useRef(false);
  const seqPendingEndRef = useRef(false);
  const seqTimersRef     = useRef<ReturnType<typeof setTimeout>[]>([]);
  const clickRipples     = useRef<{ x: number; y: number; t: number }[]>([]);

  const setDotOrange = (ti: HTMLDivElement) => {
    ti.style.transition = "background-color .3s, box-shadow .3s";
    ti.style.width  = "12px";
    ti.style.height = "12px";
    ti.style.backgroundColor = "#F5A623";
    ti.style.boxShadow = "2px 2px 0 #F5A623";
    ti.style.transform = "";
    requestAnimationFrame(() => {
      ti.style.transition = "width .3s, height .3s, background-color .3s, box-shadow .3s";
    });
  };

  // Helpers for the typing label (no state = no re-render)
  const showLabel = useCallback((text: string) => {
    const el = typingLabelRef.current;
    if (!el) return;
    el.textContent = text;
    el.style.display = "block";
  }, []);

  const hideLabel = useCallback(() => {
    const el = typingLabelRef.current;
    if (el) { el.style.display = "none"; el.textContent = ""; }
  }, []);

  // Cancel any in-progress sequence
  const cancelSequence = useCallback(() => {
    seqTimersRef.current.forEach(clearTimeout);
    seqTimersRef.current = [];
    seqActiveRef.current = false;
    hideLabel();
  }, [hideLabel]);

  // ── SEQUENCE PLAYER ──
  const playSequence = useCallback((steps: SeqStep[]) => {
    cancelSequence();
    seqActiveRef.current  = true;

    // Enter "sequence mode" (reuse agentMode for positioning)
    agentModeRef.current = true;
    const ti = trailInnerRef.current;
    if (ti) {
      ti.style.transition   = "background-color 0.3s ease, box-shadow 0.3s ease";
      ti.style.borderRadius = "50%";
      ti.style.width        = "14px";
      ti.style.height       = "14px";
      ti.style.backgroundColor = "#ffffff";
    }

    let cursor = 0;
    const advance = () => {
      if (!seqActiveRef.current || cursor >= steps.length) {
        seqActiveRef.current = false;
        hideLabel();
        const ti2 = trailInnerRef.current;
        agentModeRef.current = false;
        seqPendingEndRef.current = false;
        if (ti2) {
          setDotOrange(ti2);
        }
        return;
      }
      const step = steps[cursor++];

      if (step.type === "move-el") {
        const all = document.querySelectorAll(step.selector);
        const el  = all.length ? all[all.length - 1] : null;
        if (el) {
          const r = el.getBoundingClientRect();
          agentCenterRef.current = { x: r.left + r.width / 2, y: r.top + r.height / 2 };
        } else if (step.fallback) {
          agentCenterRef.current = { ...step.fallback };
        }
        const id = setTimeout(advance, step.ms);
        seqTimersRef.current.push(id);

      } else if (step.type === "move-pos") {
        agentCenterRef.current = { x: step.x, y: step.y };
        const id = setTimeout(advance, step.ms);
        seqTimersRef.current.push(id);

      } else if (step.type === "click") {
        // Ripple at current trail position
        clickRipples.current.push({ ...trailPositionRef.current, t: Date.now() });
        const id = setTimeout(advance, 280);
        seqTimersRef.current.push(id);

      } else if (step.type === "type") {
        const chars  = [...(step.text || "")];
        const delay  = step.charMs ?? 30;
        let   acc    = "";
        let   ci     = 0;
        const nextChar = () => {
          if (!seqActiveRef.current) return;
          if (ci >= chars.length) { const id2 = setTimeout(advance, 150); seqTimersRef.current.push(id2); return; }
          acc += chars[ci++];
          showLabel(acc);
          const id2 = setTimeout(nextChar, delay + Math.random() * 15);
          seqTimersRef.current.push(id2);
        };
        nextChar();

      } else if (step.type === "wait") {
        const id = setTimeout(advance, step.ms);
        seqTimersRef.current.push(id);

      } else if (step.type === "clear-label") {
        hideLabel();
        advance(); // immediate
      }
    };

    advance();
  }, [cancelSequence, hideLabel, showLabel]);

  // ── CURSOR DOM RENDERING ──
  const updateCursorDOM = useCallback(() => {
    const main  = mainCursorRef.current;
    const trail = trailCursorRef.current;

    if (main) {
      const { x, y } = positionRef.current;
      main.style.transform = `translate(${x}px,${y}px) translate(-50%,-50%) scale(${isClickingRef.current ? 0.8 : 1})`;
      main.style.opacity   = isVisibleRef.current ? "1" : "0";
    }

    if (trail) {
      const { x, y } = trailPositionRef.current;
      const s = agentModeRef.current ? 1 : (isClickingRef.current ? 0.6 : 1);
      trail.style.transform = `translate(${x}px,${y}px) translate(-50%,-50%) scale(${s})`;
      const op = agentModeRef.current ? 0.9 : (isVisibleRef.current ? 0.5 : 0);
      trail.style.opacity = String(op);
    }

    // Reset trail inner transform when not in agent mode
    const ti = trailInnerRef.current;
    if (ti && !agentModeRef.current) {
      ti.style.transform = "";
    }

    // Typing label follows trail dot
    const lbl = typingLabelRef.current;
    if (lbl && lbl.style.display !== "none") {
      const { x, y } = trailPositionRef.current;
      lbl.style.left = `${x + 18}px`;
      lbl.style.top  = `${y - 28}px`;
    }

    // Canvas: line + click ripples
    const canvas = canvasRef.current;
    if (canvas) {
      const ctx = canvas.getContext("2d");
      if (ctx) {
        ctx.clearRect(0, 0, canvas.width, canvas.height);

        // ── Connecting line (gradient white→transparent, visible only when moving) ──
        const { x: x1, y: y1 } = positionRef.current;
        const x2 = trailPositionRef.current.x, y2 = trailPositionRef.current.y;
        const la = lineAlphaRef.current;

        if (la > 0.01 && isVisibleRef.current) {
          const dx = x2 - x1, dy = y2 - y1;
          const dist = Math.sqrt(dx * dx + dy * dy);
          if (dist >= 12) {
            const r  = 6;
            const ux = dx / dist, uy = dy / dist;
            const sx = x1 + ux * r, sy = y1 + uy * r;
            const ex = x2 - ux * r, ey = y2 - uy * r;
            const grad = ctx.createLinearGradient(sx, sy, ex, ey);
            grad.addColorStop(0, `rgba(0,0,0,${(0.35 * la).toFixed(3)})`);
            grad.addColorStop(1, 'rgba(0,0,0,0)');
            ctx.beginPath();
            ctx.moveTo(sx, sy);
            ctx.lineTo(ex, ey);
            ctx.strokeStyle = grad;
            ctx.lineWidth   = 1;
            ctx.stroke();
          }
        }

        // Click ripples
        const now = Date.now();
        clickRipples.current = clickRipples.current.filter(r => now - r.t < 450);
        for (const r of clickRipples.current) {
          const p = (now - r.t) / 450;
          ctx.beginPath();
          ctx.arc(r.x, r.y, 14 + p * 28, 0, Math.PI * 2);
          ctx.strokeStyle = `rgba(245,166,35,${0.7 * (1 - p)})`;
          ctx.lineWidth   = 2.5 * (1 - p * 0.7);
          ctx.stroke();
        }
      }
    }
  }, []);

  // ── CURSOR STATE STYLING ──
  const applyCursorState = useCallback((state: CursorState) => {
    const mainInner = mainInnerRef.current, trailInner = trailInnerRef.current;
    if (!mainInner || !trailInner || agentModeRef.current) return;
    if (state === "pointer") {
      mainInner.style.cssText  = "width:20px;height:20px;border-radius:50%;background-color:white;box-shadow:2px 2px 0 #F5A623;transition:width .2s,height .2s,box-shadow .2s";
      trailInner.style.cssText = "width:40px;height:40px;border-radius:50%;background-color:#F5A623;box-shadow:2px 2px 0 #F5A623;transition:width .3s,height .3s,background-color .3s,box-shadow .3s";
    } else if (state === "text") {
      mainInner.style.cssText  = "width:3px;height:24px;border-radius:0;background-color:white;box-shadow:1px 1px 0 #F5A623;transition:width .2s,height .2s,box-shadow .2s";
      trailInner.style.cssText = "width:3px;height:24px;border-radius:0;background-color:#F5A623;box-shadow:1px 1px 0 #F5A623;transition:width .3s,height .3s,background-color .3s,box-shadow .3s";
    } else {
      mainInner.style.cssText  = "width:12px;height:12px;border-radius:50%;background-color:white;box-shadow:2px 2px 0 #F5A623;transition:width .2s,height .2s,box-shadow .2s";
      trailInner.style.cssText = "width:12px;height:12px;border-radius:50%;background-color:#F5A623;box-shadow:2px 2px 0 #F5A623;transition:width .3s,height .3s,background-color .3s,box-shadow .3s";
    }
  }, []);

  // ── ANIMATION LOOP ──
  const animateTrail = useCallback(() => {
    if (agentModeRef.current) {
      if (seqActiveRef.current) {
        // Sequence mode: lerp quickly to target center
        const tx = agentCenterRef.current.x, ty = agentCenterRef.current.y;
        trailPositionRef.current.x += (tx - trailPositionRef.current.x) * 0.10;
        trailPositionRef.current.y += (ty - trailPositionRef.current.y) * 0.10;
      } else if (agentPageElRef.current) {
        // PDF reading: sawtooth scan
        const el = agentPageElRef.current;
        let cx = agentCenterRef.current.x, cy = agentCenterRef.current.y;
        let sw = SCAN_WIDTH, lh = LINE_HEIGHT;
        const rect = el.getBoundingClientRect();
        if (rect.width > 0) {
          cx = rect.left + rect.width / 2;
          cy = rect.top  + rect.height * 0.12;
          sw = rect.width  * 0.28;
          lh = rect.height / (LINES_PER_PAGE + 1);
          agentCenterRef.current = { x: cx, y: cy };
        }
        const elapsed   = (Date.now() - agentPageTimeRef.current) / 1000;
        const lineIdx   = Math.floor(elapsed / LINE_DURATION);
        const linePhase = (elapsed % LINE_DURATION) / LINE_DURATION;
        const xRatio    = linePhase < 0.85 ? linePhase / 0.85 - 0.5 : ((1 - linePhase) / 0.15) - 0.5;
        const targetX   = cx + xRatio * sw;
        const targetY   = cy + (lineIdx % LINES_PER_PAGE) * lh;
        trailPositionRef.current.x += (targetX - trailPositionRef.current.x) * 0.06;
        trailPositionRef.current.y += (targetY - trailPositionRef.current.y) * 0.06;
      }
    } else if (!isClickingRef.current) {
      trailPositionRef.current.x += (positionRef.current.x - trailPositionRef.current.x) * 0.15;
      trailPositionRef.current.y += (positionRef.current.y - trailPositionRef.current.y) * 0.15;
    }

    // Fade line in when cursor moves, out when still
    const cp = positionRef.current, pp = prevPosRef.current;
    const moved = Math.sqrt((cp.x - pp.x) ** 2 + (cp.y - pp.y) ** 2) > 1.5;
    lineAlphaRef.current += ((moved ? 1 : 0) - lineAlphaRef.current) * (moved ? 0.2 : 0.05);
    prevPosRef.current = { x: cp.x, y: cp.y };

    updateCursorDOM();
    rafRef.current = requestAnimationFrame(animateTrail);
  }, [updateCursorDOM]);

  // ── INIT & EVENTS ──
  useEffect(() => {
    if (!window.matchMedia("(pointer: fine)").matches) return;

    const canvas = canvasRef.current;
    if (canvas) { canvas.width = window.innerWidth; canvas.height = window.innerHeight; }

    const onResize = () => {
      if (canvasRef.current) { canvasRef.current.width = window.innerWidth; canvasRef.current.height = window.innerHeight; }
    };
    window.addEventListener("resize", onResize);

    const onMove = (e: MouseEvent) => { positionRef.current = { x: e.clientX, y: e.clientY }; };

    const onOver = (e: MouseEvent) => {
      const t = e.target as HTMLElement;
      if (!t) return;
      const isText = (t.tagName === "INPUT" && ["text","email","password","search","url","tel","number"].includes((t as HTMLInputElement).type))
                  || t.tagName === "TEXTAREA" || t.isContentEditable;
      if (isText) { cursorStateRef.current = "text"; applyCursorState("text"); return; }
      const clickable = (() => {
        let el: HTMLElement | null = t;
        while (el && el !== document.body) {
          if (el.tagName === "A" || el.tagName === "BUTTON" || el.getAttribute("role") === "button"
              || el.getAttribute("role") === "link" || el.classList.contains("cursor-pointer")) return true;
          el = el.parentElement;
        }
        return false;
      })();
      const s: CursorState = clickable ? "pointer" : "default";
      cursorStateRef.current = s; applyCursorState(s);
    };

    const onDown = () => {
      isClickingRef.current = true;
      if (cursorStateRef.current === "pointer") {
        const ti = trailInnerRef.current, mi = mainInnerRef.current;
        if (ti) { ti.style.backgroundColor = "#FF8000"; ti.style.boxShadow = "2px 2px 0 #FF8000"; }
        if (mi) mi.style.boxShadow = "2px 2px 0 #FF8000";
      }
    };

    const onUp = () => {
      isClickingRef.current = false;
      if (cursorStateRef.current === "pointer") {
        const ti = trailInnerRef.current, mi = mainInnerRef.current;
        if (ti) { ti.style.backgroundColor = "#F5A623"; ti.style.boxShadow = "2px 2px 0 #F5A623"; }
        if (mi) mi.style.boxShadow = "2px 2px 0 #F5A623";
      }
    };

    const onLeave = () => { isVisibleRef.current = false; };
    const onEnter = () => { isVisibleRef.current = true; };

    // ── AGENT CURSOR EVENTS ──
    const onAgentCursor = (e: Event) => {
      const d = (e as CustomEvent).detail as {
        phase: string; page?: number; tool?: string; args?: Record<string,unknown>;
      };
      const ti = trailInnerRef.current;

      if (d.phase === "start") {
        // PDF reading mode
        agentModeRef.current     = true;
        agentPageTimeRef.current = Date.now();
        agentPageElRef.current   = document.querySelector('[data-pdf-page-container="1"]');
        const el = agentPageElRef.current;
        if (el) {
          el.scrollIntoView({ behavior: "smooth", block: "center" });
          const r = el.getBoundingClientRect();
          agentCenterRef.current = { x: r.left + r.width / 2, y: r.top + r.height * 0.12 };
        } else {
          agentCenterRef.current = { x: window.innerWidth * 0.52, y: window.innerHeight * 0.60 };
        }
        if (ti) {
          ti.style.transition     = "background-color .3s, box-shadow .3s";
          ti.style.borderRadius   = "50%"; ti.style.width = "16px"; ti.style.height = "16px";
          ti.style.backgroundColor = "#ffffff";
        }

      } else if (d.phase === "page" && d.page != null) {
        agentPageTimeRef.current = Date.now();
        const el = document.querySelector(`[data-pdf-page-container="${d.page}"]`);
        agentPageElRef.current = el;
        if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });

      } else if (d.phase === "end") {
        // PDF reading done
        cancelSequence();
        agentModeRef.current   = false;
        agentPageElRef.current = null;
        seqPendingEndRef.current = false;
        if (ti) {
          setDotOrange(ti);
        }

      } else if (d.phase === "tool-sequence" && d.tool) {
        const seq = buildSequence(d.tool, d.args ?? {});
        playSequence(seq);
      }
    };

    document.addEventListener("mousemove", onMove, { passive: true });
    document.addEventListener("mouseover", onOver, { passive: true });
    document.addEventListener("mousedown", onDown);
    document.addEventListener("mouseup",   onUp);
    document.documentElement.addEventListener("mouseleave", onLeave);
    document.documentElement.addEventListener("mouseenter", onEnter);
    window.addEventListener("agent-cursor", onAgentCursor);

    rafRef.current = requestAnimationFrame(animateTrail);

    return () => {
      if (rafRef.current) cancelAnimationFrame(rafRef.current);
      cancelSequence();
      window.removeEventListener("resize",       onResize);
      window.removeEventListener("agent-cursor", onAgentCursor);
      document.removeEventListener("mousemove",  onMove);
      document.removeEventListener("mouseover",  onOver);
      document.removeEventListener("mousedown",  onDown);
      document.removeEventListener("mouseup",    onUp);
      document.documentElement.removeEventListener("mouseleave", onLeave);
      document.documentElement.removeEventListener("mouseenter", onEnter);
    };
  }, [animateTrail, applyCursorState, cancelSequence, playSequence]);

  if (typeof window !== "undefined" && !window.matchMedia("(pointer: fine)").matches) return null;

  return (
    <>
      <canvas ref={canvasRef} className="fixed top-0 left-0 pointer-events-none z-[9997]" />

      {/* Floating typing label — appears next to the dot during sequence type steps */}
      <div
        ref={typingLabelRef}
        className="fixed pointer-events-none z-[9996] font-mono text-[11px] bg-black/80 text-yellow-400 px-2 py-0.5 rounded-md whitespace-nowrap max-w-[280px] overflow-hidden text-ellipsis"
        style={{ display: "none" }}
      />

      {/* Main cursor */}
      <div ref={mainCursorRef} className="fixed top-0 left-0 pointer-events-none z-[9999] mix-blend-difference will-change-transform" style={{ opacity: 0 }}>
        <div ref={mainInnerRef} style={{ width:"12px", height:"12px", borderRadius:"50%", backgroundColor:"white", boxShadow:"2px 2px 0 #F5A623", transition:"width .2s,height .2s,box-shadow .2s" }} />
      </div>

      {/* Trail cursor */}
      <div ref={trailCursorRef} className="fixed top-0 left-0 pointer-events-none z-[9998] will-change-transform" style={{ opacity: 0 }}>
        <div ref={trailInnerRef} style={{ width:"12px", height:"12px", borderRadius:"50%", backgroundColor:"#F5A623", transition:"width .3s,height .3s,background-color .3s,box-shadow .3s" }} />
      </div>
    </>
  );
}
