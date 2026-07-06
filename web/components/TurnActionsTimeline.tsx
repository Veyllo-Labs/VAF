'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

import { ReactNode, useLayoutEffect, useRef } from 'react';
import { ChevronRight } from 'lucide-react';
import { AgentAvatar, type AvatarMode } from '@/components/AgentAvatar';
import { useIsMobile } from '@/hooks/useIsMobile';
import { cn } from '@/lib/utils';

// One entry on the timeline: the turn's thinking (first) followed by each tool call. `state`
// drives the rail dot colour (pending → gray, done → green, error → red) and which item the
// avatar "walks" to (the last pending one). `node` is the fully-wired card rendered by the parent
// (a ThinkingDetails for `think`, a ToolMessage for `tool`) — this component only owns layout +
// the rail + the avatar walk, never the card internals.
export interface TimelineAction {
    key: string;
    kind: 'think' | 'tool' | 'say';
    state: 'pending' | 'done' | 'error';
    node: ReactNode;
}

interface Props {
    actions: TimelineAction[];
    avatarMode: AvatarMode;
    avatarDim?: boolean;
    /** latest turn while still generating — only then does the avatar walk the rail */
    isLive: boolean;
    expanded: boolean;
    onToggle: () => void;
    /** the answer bubble, rendered below the timeline (single shared avatar gutter) */
    children?: ReactNode;
}

// rail dot geometry (ported from the approved mockup): the dot sits `top:13px` inside its item and
// is 11px tall, so its centre is 13 + 11/2 = 18.5px below the item's top.
const RAIL_DOT_CENTER = 13 + 11 / 2;

export function TurnActionsTimeline({ actions, avatarMode, avatarDim, isLive, expanded, onToggle, children }: Props) {
    const isMobile = useIsMobile();
    const rowRef = useRef<HTMLDivElement | null>(null);
    const avaRef = useRef<HTMLDivElement | null>(null);
    const railRef = useRef<HTMLSpanElement | null>(null);
    const itemRefs = useRef<(HTMLDivElement | null)[]>([]);

    // The avatar walks to the active action = the last not-yet-done item (the run is sequential),
    // only on the live turn while expanded. BETWEEN steps (a tool just finished, the next think/tool
    // not added yet) nothing is pending — keep the avatar at the LAST point instead of snapping back
    // up. It only returns to the top when collapsed or on a finished/historical turn.
    let activeIdx = -1;
    if (isLive && expanded && actions.length) {
        for (let k = actions.length - 1; k >= 0; k--) {
            if (actions[k].state === 'pending') { activeIdx = k; break; }
        }
        if (activeIdx < 0) activeIdx = actions.length - 1;
    }

    // Position the avatar (centre on the active dot) + grow the rail to the last dot. Runs after
    // every render and on size changes (the thinking text streams, tool cards expand), so the
    // avatar stays glued to the active point and the line follows.
    const reflow = () => {
        const row = rowRef.current, ava = avaRef.current, rail = railRef.current;
        if (!row) return;
        const items = itemRefs.current.filter(Boolean) as HTMLDivElement[];
        if (rail) {
            rail.style.height = expanded && items.length
                ? Math.max(0, items[items.length - 1].offsetTop) + 'px'
                : '0px';
        }
        if (ava) {
            if (expanded && activeIdx >= 0 && items[activeIdx]) {
                const dotCenter = items[activeIdx].getBoundingClientRect().top + RAIL_DOT_CENTER;
                const y = dotCenter - row.getBoundingClientRect().top - (ava.offsetHeight || 36) / 2;
                ava.style.transform = `translateY(${Math.max(0, Math.round(y))}px)`;
            } else {
                ava.style.transform = 'translateY(0)';
            }
        }
    };

    useLayoutEffect(reflow);

    useLayoutEffect(() => {
        const el = rowRef.current;
        if (!el || typeof ResizeObserver === 'undefined') return;
        const ro = new ResizeObserver(() => reflow());
        ro.observe(el);
        return () => ro.disconnect();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [expanded, activeIdx, actions.length]);

    // Black/white circle scheme: thinking = solid black; a tool is a hollow ring while running or on
    // error, and fills solid gray once it completes successfully. An intermediate spoken line ('say' —
    // the agent talks between tool calls) is a hollow black ring: in the agent's own "black" voice
    // family like think, but distinct from think (solid) and from a running tool (gray ring).
    // Dark mode: gray-900 (#111827) and bg-white (flips to #202020) both vanish on the #181818
    // page, so the step dots get explicit dark: variants — active/content steps go BRIGHT, done
    // stays a clear mid-gray, pending is a faint near-bg ring.
    const circleClass = (a: TimelineAction) =>
        a.kind === 'think' ? 'bg-gray-900 border-gray-900 dark:bg-[#e6e6e6] dark:border-[#e6e6e6]'
            : a.kind === 'say' ? 'bg-white border-gray-900 dark:bg-transparent dark:border-[#e6e6e6]'
                : a.state === 'done' ? 'bg-gray-400 border-gray-400 dark:bg-[#6b6b6b] dark:border-[#6b6b6b]'
                    : 'bg-transparent border-gray-400 dark:border-[#4a4a4a]';

    const label = `${actions.length} ${actions.length === 1 ? 'action' : 'actions'}`;

    return (
        <div className="flex flex-col">
        <div ref={rowRef} className="flex gap-4 max-md:gap-2">
            <div
                ref={avaRef}
                className="shrink-0 self-start transition-transform duration-300 ease-out will-change-transform"
            >
                <AgentAvatar mode={avatarMode} dim={avatarDim} />
            </div>

            <div className="flex min-w-0 flex-1 flex-col">
                {/* toggle — ALWAYS present so an expanded turn can be collapsed again. Collapsed: the
                    circle-row (black = thinking, gray = done tool, ring = running/error). Expanded: a
                    compact header with a down chevron. */}
                <button
                    type="button"
                    onClick={onToggle}
                    className="group flex w-fit items-center gap-2 self-start rounded-lg px-2 py-1.5 text-xs text-gray-500 transition-colors hover:bg-gray-100"
                >
                    {!expanded && (
                        <span className="flex items-center gap-1.5">
                            {actions.map((a) => (
                                <span
                                    key={a.key}
                                    className={cn('inline-block h-[9px] w-[9px] rounded-full border-[1.8px]', circleClass(a))}
                                />
                            ))}
                        </span>
                    )}
                    <span>{label}</span>
                    <ChevronRight size={13} className={cn('text-gray-400 transition-transform', expanded ? 'rotate-90' : 'group-hover:translate-x-0.5')} />
                </button>

                {/* expanded: the actions with a left rail that grows down to the active dot. The
                    open/close uses ONE smooth animation — a grid-rows fold (1fr⇄0fr) animating the
                    real content height (no max-height guesswork, no second easing). */}
                <div
                    className="grid transition-[grid-template-rows] duration-300 ease-out"
                    style={{ gridTemplateRows: expanded ? '1fr' : '0fr' }}
                    aria-hidden={!expanded}
                >
                    <div className="overflow-hidden">
                        <div className="relative pl-[26px]">
                            <span
                                ref={railRef}
                                className="absolute left-[9px] top-[13px] w-[2px] bg-gray-200 transition-[height] duration-300 ease-out"
                                style={{ height: 0 }}
                            />
                            <div className="flex flex-col gap-[9px]">
                                {actions.map((a, idx) => (
                                    <div
                                        key={a.key}
                                        ref={(el) => { itemRefs.current[idx] = el; }}
                                        className="relative"
                                    >
                                        <span
                                            className={cn(
                                                'absolute left-[-21px] top-[13px] z-[1] h-[11px] w-[11px] rounded-full border-2 transition-colors duration-300',
                                                circleClass(a),
                                            )}
                                            style={{ boxShadow: '0 0 0 3px hsl(var(--background))' }}
                                        />
                                        {a.node}
                                    </div>
                                ))}
                            </div>
                        </div>
                    </div>
                </div>

                {/* desktop: the answer stays in the indented content column, unchanged */}
                {!isMobile && children}
            </div>
        </div>
        {/* mobile: the answer drops below the avatar row at full width — no left gutter and no
            negative-margin hack (the −ml hack clipped the avatar's top). */}
        {isMobile && children}
        </div>
    );
}
