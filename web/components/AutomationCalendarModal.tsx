'use client';

import React, { useState, useEffect } from 'react';
import { X, ChevronRight } from 'lucide-react';
import { cn } from '@/lib/utils';

export interface AutomationCalendarModalProps {
    isOpen: boolean;
    onClose: () => void;
    currentUser?: { username?: string };
}

export default function AutomationCalendarModal({ isOpen, onClose, currentUser }: AutomationCalendarModalProps) {
    const [automationCalendarViewDate, setAutomationCalendarViewDate] = useState(() => new Date());
    const [selectedDayForView, setSelectedDayForView] = useState<Date | null>(null);

    useEffect(() => {
        if (!isOpen) return;
        const handleKeyDown = (e: KeyboardEvent) => {
            if (e.key !== 'Escape') return;
            if (selectedDayForView) {
                setSelectedDayForView(null);
                e.stopPropagation();
                e.preventDefault();
                return;
            }
            onClose();
        };
        window.addEventListener('keydown', handleKeyDown);
        return () => window.removeEventListener('keydown', handleKeyDown);
    }, [isOpen, selectedDayForView, onClose]);

    if (!isOpen) return null;

    return (
        <div className="fixed inset-0 z-[60] flex items-center justify-center p-4" onClick={onClose}>
            <div className="absolute inset-0 bg-black/40 backdrop-blur-sm" />
            <div
                className="relative w-full max-w-[95vw] h-[90vh] rounded-2xl shadow-2xl border border-gray-200 flex flex-col animate-in fade-in zoom-in-95 duration-200 overflow-hidden bg-white"
                onClick={(e) => e.stopPropagation()}
            >
                {/* Planner header: User, Months, Title + Close */}
                <div className="flex items-center shrink-0 px-4 py-3 border-b border-gray-200 gap-4">
                    <div className="flex items-center gap-2 shrink-0">
                        <div className="w-9 h-9 rounded-full bg-gray-200 flex items-center justify-center text-gray-700 font-semibold text-sm">
                            {(currentUser?.username ?? 'User').slice(0, 1).toUpperCase()}
                        </div>
                        <span className="text-sm font-medium text-gray-900 truncate">{currentUser?.username ?? 'User'}</span>
                    </div>
                    <div className="flex flex-wrap items-center justify-center gap-1.5 flex-1 min-w-0">
                        {['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'].map((label, i) => {
                            const isSelected = automationCalendarViewDate.getMonth() === i;
                            const isActualMonth = automationCalendarViewDate.getFullYear() === new Date().getFullYear() && new Date().getMonth() === i;
                            return (
                                <button
                                    key={label}
                                    type="button"
                                    onClick={() => setAutomationCalendarViewDate(d => new Date(d.getFullYear(), i))}
                                    className={cn(
                                        'px-3.5 py-2 rounded-lg text-xs font-medium transition-colors',
                                        isSelected ? 'bg-gray-900 text-white' : 'bg-gray-100 text-gray-700 hover:bg-gray-200',
                                        isActualMonth && 'ring-2 ring-red-500'
                                    )}
                                >
                                    {label}
                                </button>
                            );
                        })}
                    </div>
                    <div className="flex items-center gap-2 shrink-0">
                        <h2 className="text-lg font-bold text-gray-900 truncate">Automations {automationCalendarViewDate.getFullYear()}</h2>
                        <button onClick={onClose} className="p-2 hover:bg-gray-100 rounded-lg transition-colors text-gray-500 hover:text-gray-700" title="Close">
                            <X size={18} />
                        </button>
                    </div>
                </div>
                {/* Body: To-do (left) + Calendar (center) */}
                <div className="flex-1 flex gap-4 min-h-0 p-4 overflow-auto">
                    <div
                        className="min-w-[220px] w-[220px] shrink-0 rounded-xl border-2 border-dashed border-gray-200 bg-gray-50 p-4 flex flex-col min-h-[200px]"
                        style={{ backgroundImage: 'radial-gradient(circle, #d1d5db 1.5px, transparent 1.5px)', backgroundSize: '10px 10px' }}
                    >
                        <h3 className="text-sm font-medium text-gray-700 mb-1.5 shrink-0">To-do list</h3>
                        <p className="text-xs text-gray-500 shrink-0">Quick tasks beside your calendar.</p>
                        <div className="flex-1 min-h-[100px]" />
                    </div>
                    <div className="flex-1 flex flex-col min-w-0 min-h-0 rounded-xl border-2 border-dashed border-gray-200 p-3 overflow-hidden">
                        {selectedDayForView ? (
                            (() => {
                                const now = new Date();
                                const isToday = selectedDayForView.getFullYear() === now.getFullYear() &&
                                    selectedDayForView.getMonth() === now.getMonth() &&
                                    selectedDayForView.getDate() === now.getDate();
                                const currentHour = now.getHours();
                                const currentTimeStr = now.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', hour12: false });
                                return (
                                    <>
                                        <div className="flex items-center justify-between gap-2 shrink-0 mb-3">
                                            <button
                                                type="button"
                                                onClick={() => setSelectedDayForView(null)}
                                                className="flex items-center gap-1.5 px-2.5 py-1.5 text-sm font-medium text-gray-600 hover:bg-gray-100 rounded-lg transition-colors"
                                            >
                                                <ChevronRight size={16} className="rotate-180" />
                                                Monat
                                            </button>
                                            <h3 className="text-sm font-semibold text-gray-900">
                                                {selectedDayForView.toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' })}
                                            </h3>
                                        </div>
                                        <div className="flex-1 overflow-auto min-h-0 border border-gray-200 rounded-lg bg-white">
                                            {Array.from({ length: 24 }, (_, h) => {
                                                const isCurrentHourSlot = isToday && h === currentHour;
                                                return (
                                                    <div
                                                        key={h}
                                                        className={cn(
                                                            'flex items-center gap-3 px-3 py-2.5 border-b border-gray-100 last:border-b-0 min-h-[48px] rounded-lg transition-all duration-200 cursor-pointer',
                                                            'hover:shadow-[0_0_16px_4px_rgba(0,0,0,0.12)] hover:z-10',
                                                            !isCurrentHourSlot && 'hover:bg-gray-50/80',
                                                            isCurrentHourSlot && 'ring-2 ring-red-500 ring-inset bg-red-50/30'
                                                        )}
                                                    >
                                                        <span className={cn('text-sm font-medium w-12 shrink-0', isCurrentHourSlot ? 'text-red-600 font-semibold' : 'text-gray-500')}>
                                                            {String(h).padStart(2, '0')}:00
                                                            {isCurrentHourSlot && (
                                                                <span className="block text-xs font-normal text-red-600 mt-0.5">{currentTimeStr}</span>
                                                            )}
                                                        </span>
                                                        <div className={cn(
                                                            'flex-1 min-h-[32px] rounded border border-dashed',
                                                            isCurrentHourSlot ? 'bg-red-50/50 border-red-200' : 'bg-gray-50/50 border-gray-200'
                                                        )} />
                                                    </div>
                                                );
                                            })}
                                        </div>
                                    </>
                                );
                            })()
                        ) : (
                            (() => {
                                const y = automationCalendarViewDate.getFullYear();
                                const m = automationCalendarViewDate.getMonth();
                                const firstWeekday = (new Date(y, m, 1).getDay() + 6) % 7;
                                const daysInMonth = new Date(y, m + 1, 0).getDate();
                                const numRows = Math.ceil((firstWeekday + daysInMonth) / 7);
                                return (
                                    <div
                                        className="grid grid-cols-7 gap-px bg-gray-400 rounded-xl flex-1 min-h-0 w-full"
                                        style={{ gridTemplateRows: `auto repeat(${numRows}, minmax(0, 1fr))` }}
                                    >
                                        {['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].map(day => (
                                            <div key={day} className="bg-gray-50 flex items-center justify-center text-xs font-medium text-gray-500 uppercase tracking-wide py-1.5">
                                                {day}
                                            </div>
                                        ))}
                                        {(() => {
                                            const today = new Date();
                                            const isCurrentMonth = today.getFullYear() === y && today.getMonth() === m;
                                            const cells: (number | null)[] = [
                                                ...Array(firstWeekday).fill(null),
                                                ...Array.from({ length: daysInMonth }, (_, i) => i + 1)
                                            ];
                                            while (cells.length % 7 !== 0) cells.push(null);
                                            return cells.map((day, i) => {
                                                const isSunday = (i % 7) === 6;
                                                return (
                                                    <div
                                                        key={i}
                                                        role="button"
                                                        tabIndex={0}
                                                        onClick={day !== null ? () => setSelectedDayForView(new Date(y, m, day)) : undefined}
                                                        onKeyDown={day !== null ? (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setSelectedDayForView(new Date(y, m, day!)); } } : undefined}
                                                        className={cn(
                                                            'min-h-0 flex items-center justify-center text-sm rounded-lg transition-all duration-200',
                                                            day === null
                                                                ? 'bg-gray-200 text-gray-400 cursor-default'
                                                                : 'bg-white text-gray-700 cursor-pointer hover:bg-gray-50 hover:scale-105 hover:shadow-[0_0_16px_4px_rgba(0,0,0,0.12)] hover:z-10',
                                                            day !== null && isSunday && 'text-red-600',
                                                            day !== null && isCurrentMonth && day === today.getDate() && 'font-semibold text-gray-900 ring-2 ring-red-500 ring-inset'
                                                        )}
                                                    >
                                                        {day ?? ''}
                                                    </div>
                                                );
                                            });
                                        })()}
                                    </div>
                                );
                            })()
                        )}
                    </div>
                </div>
                <div
                    className="shrink-0 mx-4 mb-4 rounded-xl border-2 border-dashed border-gray-200 bg-gray-50 flex flex-col items-center justify-center h-[150px] py-3"
                    style={{ backgroundImage: 'radial-gradient(circle, #d1d5db 1.5px, transparent 1.5px)', backgroundSize: '10px 10px' }}
                >
                    <span className="text-sm font-medium text-gray-600">Note</span>
                </div>
            </div>
        </div>
    );
}
