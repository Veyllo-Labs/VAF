"use client";

import { useRef, useEffect, useCallback } from "react";

type CursorState = "default" | "pointer" | "text";

export function CustomCursor() {
  const mainCursorRef = useRef<HTMLDivElement>(null);
  const trailCursorRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const mainInnerRef = useRef<HTMLDivElement>(null);
  const trailInnerRef = useRef<HTMLDivElement>(null);

  const positionRef = useRef({ x: 0, y: 0 });
  const trailPositionRef = useRef({ x: 0, y: 0 });
  const isVisibleRef = useRef(false);
  const isClickingRef = useRef(false);
  const cursorStateRef = useRef<CursorState>("default");
  const rafRef = useRef<number | null>(null);

  const updateCursorDOM = useCallback(() => {
    const main = mainCursorRef.current;
    const trail = trailCursorRef.current;

    if (main) {
      const { x, y } = positionRef.current;
      main.style.transform = `translate(${x}px, ${y}px) translate(-50%, -50%) scale(${isClickingRef.current ? 0.8 : 1})`;
      main.style.opacity = isVisibleRef.current ? "1" : "0";
    }

    if (trail) {
      const { x, y } = trailPositionRef.current;
      trail.style.transform = `translate(${x}px, ${y}px) translate(-50%, -50%) scale(${isClickingRef.current ? 0.6 : 1})`;
      trail.style.opacity = isVisibleRef.current ? "0.5" : "0";
    }

    // Draw connecting line between cursor and trail
    const canvas = canvasRef.current;
    if (canvas) {
      const ctx = canvas.getContext("2d");
      if (ctx) {
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        if (isVisibleRef.current) {
          const { x: x1, y: y1 } = positionRef.current;
          const { x: x2, y: y2 } = trailPositionRef.current;
          const dx = x2 - x1;
          const dy = y2 - y1;
          const dist = Math.sqrt(dx * dx + dy * dy);
          if (dist < 12) return; // dots are touching, no line needed
          const r = 6; // dot radius
          const ux = dx / dist;
          const uy = dy / dist;
          ctx.beginPath();
          ctx.moveTo(x1 + ux * r, y1 + uy * r);
          ctx.lineTo(x2 - ux * r, y2 - uy * r);
          ctx.strokeStyle = "rgba(245, 166, 35, 0.5)";
          ctx.lineWidth = 1.5;
          ctx.stroke();
        }
      }
    }
  }, []);

  const applyCursorState = useCallback((state: CursorState) => {
    const mainInner = mainInnerRef.current;
    const trailInner = trailInnerRef.current;
    if (!mainInner || !trailInner) return;

    if (state === "pointer") {
      mainInner.style.width = "20px";
      mainInner.style.height = "20px";
      mainInner.style.borderRadius = "50%";
      mainInner.style.boxShadow = "2px 2px 0 #F5A623";
      trailInner.style.width = "40px";
      trailInner.style.height = "40px";
      trailInner.style.borderRadius = "50%";
      trailInner.style.backgroundColor = "#F5A623";
      trailInner.style.boxShadow = "2px 2px 0 #F5A623";
    } else if (state === "text") {
      mainInner.style.width = "3px";
      mainInner.style.height = "24px";
      mainInner.style.borderRadius = "0";
      mainInner.style.boxShadow = "1px 1px 0 #F5A623";
      trailInner.style.width = "3px";
      trailInner.style.height = "24px";
      trailInner.style.borderRadius = "0";
      trailInner.style.backgroundColor = "#F5A623";
      trailInner.style.boxShadow = "1px 1px 0 #F5A623";
    } else {
      mainInner.style.width = "12px";
      mainInner.style.height = "12px";
      mainInner.style.borderRadius = "50%";
      mainInner.style.boxShadow = "2px 2px 0 #F5A623";
      trailInner.style.width = "12px";
      trailInner.style.height = "12px";
      trailInner.style.borderRadius = "50%";
      trailInner.style.backgroundColor = "#F5A623";
      trailInner.style.boxShadow = "2px 2px 0 #F5A623";
    }
  }, []);

  const animateTrail = useCallback(() => {
    if (!isClickingRef.current) {
      const lerp = 0.15;
      trailPositionRef.current.x +=
        (positionRef.current.x - trailPositionRef.current.x) * lerp;
      trailPositionRef.current.y +=
        (positionRef.current.y - trailPositionRef.current.y) * lerp;
    }

    updateCursorDOM();
    rafRef.current = requestAnimationFrame(animateTrail);
  }, [updateCursorDOM]);

  useEffect(() => {
    const hasFineMouse = window.matchMedia("(pointer: fine)").matches;
    if (!hasFineMouse) return;

    // Size the canvas to the viewport
    const canvas = canvasRef.current;
    if (canvas) {
      canvas.width = window.innerWidth;
      canvas.height = window.innerHeight;
    }

    const handleResize = () => {
      if (canvasRef.current) {
        canvasRef.current.width = window.innerWidth;
        canvasRef.current.height = window.innerHeight;
      }
    };
    window.addEventListener("resize", handleResize);

    const handleMouseMove = (e: MouseEvent) => {
      positionRef.current = { x: e.clientX, y: e.clientY };
    };

    const updateCursorState = (e: MouseEvent) => {
      const target = e.target as HTMLElement;
      if (!target) return;

      const isTextInput =
        target.tagName === "INPUT" &&
        ["text", "email", "password", "search", "url", "tel", "number"].includes(
          (target as HTMLInputElement).type
        );
      const isTextArea = target.tagName === "TEXTAREA";
      const isContentEditable = target.isContentEditable;

      if (isTextInput || isTextArea || isContentEditable) {
        cursorStateRef.current = "text";
        applyCursorState("text");
        return;
      }

      // Walk up the DOM tree — handles text/icons nested inside buttons/links
      const isClickable = (() => {
        let el: HTMLElement | null = target;
        while (el && el !== document.body) {
          if (
            el.tagName === "A" ||
            el.tagName === "BUTTON" ||
            el.getAttribute("role") === "button" ||
            el.getAttribute("role") === "link" ||
            el.classList.contains("cursor-pointer")
          ) return true;
          el = el.parentElement;
        }
        return false;
      })();

      const newState: CursorState = isClickable ? "pointer" : "default";
      cursorStateRef.current = newState;
      applyCursorState(newState);
    };

    const handleMouseDown = () => {
      isClickingRef.current = true;
      if (cursorStateRef.current === "pointer") {
        const trailInner = trailInnerRef.current;
        const mainInner = mainInnerRef.current;
        if (trailInner) {
          trailInner.style.backgroundColor = "#FF8000";
          trailInner.style.boxShadow = "2px 2px 0 #FF8000";
        }
        if (mainInner) mainInner.style.boxShadow = "2px 2px 0 #FF8000";
      }
    };

    const handleMouseUp = () => {
      isClickingRef.current = false;
      if (cursorStateRef.current === "pointer") {
        const trailInner = trailInnerRef.current;
        const mainInner = mainInnerRef.current;
        if (trailInner) {
          trailInner.style.backgroundColor = "#F5A623";
          trailInner.style.boxShadow = "2px 2px 0 #F5A623";
        }
        if (mainInner) mainInner.style.boxShadow = "2px 2px 0 #F5A623";
      }
    };

    const handleMouseLeave = () => {
      isVisibleRef.current = false;
    };

    const handleMouseEnter = () => {
      isVisibleRef.current = true;
    };

    document.addEventListener("mousemove", handleMouseMove, { passive: true });
    document.addEventListener("mouseover", updateCursorState, { passive: true });
    document.addEventListener("mousedown", handleMouseDown);
    document.addEventListener("mouseup", handleMouseUp);
    document.documentElement.addEventListener("mouseleave", handleMouseLeave);
    document.documentElement.addEventListener("mouseenter", handleMouseEnter);

    rafRef.current = requestAnimationFrame(animateTrail);

    return () => {
      if (rafRef.current) cancelAnimationFrame(rafRef.current);
      window.removeEventListener("resize", handleResize);
      document.removeEventListener("mousemove", handleMouseMove);
      document.removeEventListener("mouseover", updateCursorState);
      document.removeEventListener("mousedown", handleMouseDown);
      document.removeEventListener("mouseup", handleMouseUp);
      document.documentElement.removeEventListener("mouseleave", handleMouseLeave);
      document.documentElement.removeEventListener("mouseenter", handleMouseEnter);
    };
  }, [animateTrail, applyCursorState]);

  if (typeof window !== "undefined" && !window.matchMedia("(pointer: fine)").matches) {
    return null;
  }

  return (
    <>
      {/* Connecting line canvas */}
      <canvas
        ref={canvasRef}
        className="fixed top-0 left-0 pointer-events-none z-[9997]"
      />

      {/* Main cursor — white dot with mix-blend-difference */}
      <div
        ref={mainCursorRef}
        className="fixed top-0 left-0 pointer-events-none z-[9999] mix-blend-difference will-change-transform"
        style={{ opacity: 0 }}
      >
        <div
          ref={mainInnerRef}
          style={{
            width: "12px",
            height: "12px",
            borderRadius: "50%",
            backgroundColor: "white",
            boxShadow: "2px 2px 0 #F5A623",
            transition: "width 0.2s ease, height 0.2s ease, box-shadow 0.2s ease",
          }}
        />
      </div>

      {/* Trail cursor — yellow-orange accent */}
      <div
        ref={trailCursorRef}
        className="fixed top-0 left-0 pointer-events-none z-[9998] will-change-transform"
        style={{ opacity: 0 }}
      >
        <div
          ref={trailInnerRef}
          style={{
            width: "12px",
            height: "12px",
            borderRadius: "50%",
            backgroundColor: "#F5A623",
            transition: "width 0.2s ease, height 0.2s ease, background-color 0.2s ease, box-shadow 0.2s ease",
          }}
        />
      </div>
    </>
  );
}
