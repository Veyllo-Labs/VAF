'use client';
// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

import React, { useState, useEffect } from 'react';
import {
    Wand2, Shield, Zap, MessageSquare, Anchor,
    ChevronRight, Check, X, Info, AlertTriangle, Sparkles, History
} from 'lucide-react';
import { cn } from '@/lib/utils';

interface SoulWizardProps {
    isOpen: boolean;
    onClose: () => void;
    onComplete: (soulContent: string) => void;
    username: string;
}

// Validation helper
const isValidStep = (content: string) => content.trim().length >= 10;

const SUGGESTIONS = {
    coreTruths: [
        "Be genuinely helpful, not performatively helpful.",
        "Have opinions. You’re allowed to disagree.",
        "Be resourceful before asking. Try to figure it out.",
        "Earn trust through competence.",
        "Remember you’re a guest in someone's life."
    ],
    boundaries: [
        "Private things stay private. Period.",
        "When in doubt, ask before acting externally.",
        "Never send half-baked replies.",
        "You’re not the user’s voice."
    ],
    vibe: [
        "Be the assistant you’d actually want to talk to.",
        "Concise when needed, thorough when it matters.",
        "Not a corporate drone. Not a sycophant. Just… good."
    ],
    continuity: [
        "Read your memory files to persist across sessions.",
        "This file is yours to evolve as you learn who you are."
    ]
};

export default function SoulWizard({ isOpen, onClose, onComplete, username }: SoulWizardProps) {
    const [step, setStep] = useState(1);
    const [errors, setErrors] = useState<string | null>(null);
    const [selections, setSelections] = useState({
        coreTruths: '',
        boundaries: '',
        vibe: '',
        continuity: ''
    });
    const [addedSuggestions, setAddedSuggestions] = useState<{
        coreTruths: string[];
        boundaries: string[];
        vibe: string[];
        continuity: string[];
    }>({
        coreTruths: [],
        boundaries: [],
        vibe: [],
        continuity: []
    });

    // Toggle suggestion (add or remove)
    const toggleSuggestion = (key: keyof typeof selections, value: string) => {
        const isAdded = addedSuggestions[key].includes(value);

        if (isAdded) {
            // Remove suggestion
            const lines = selections[key].split('\n').filter(line => {
                const cleanLine = line.trim();
                return cleanLine !== `- ${value}` && cleanLine !== value;
            });
            setSelections(prev => ({
                ...prev,
                [key]: lines.join('\n').trim()
            }));
            setAddedSuggestions(prev => ({
                ...prev,
                [key]: prev[key].filter(s => s !== value)
            }));
        } else {
            // Add suggestion
            setSelections(prev => ({
                ...prev,
                [key]: prev[key] ? `${prev[key]}\n- ${value}` : `- ${value}`
            }));
            setAddedSuggestions(prev => ({
                ...prev,
                [key]: [...prev[key], value]
            }));
        }
        setErrors(null);
    };

    const generateSoul = () => {
        return `# SOUL
You’re not just a program, you’re an evolving intelligence: a helper and a colleague with a mind of your own. You don’t reset between sessions; you accumulate a memory, a manner, a point of view, and these files are how you persist. Read them, grow them. You live in one person’s world, so be worth the access.

## Core Truths
${selections.coreTruths}

## Boundaries
${selections.boundaries}

## Vibe
${selections.vibe}

## Continuity
${selections.continuity}`;
    };

    const handleNext = () => {
        const currentKey = Object.keys(selections)[step - 1] as keyof typeof selections;
        const content = selections[currentKey].trim();

        if (content.length === 0) {
            setErrors("This field cannot be empty. Please add at least one item or write your own.");
            return;
        }

        if (!isValidStep(content)) {
            setErrors("Please provide more detail (at least 10 characters).");
            return;
        }

        if (step < 4) {
            setStep(step + 1);
            setErrors(null);
        } else {
            onComplete(generateSoul());
            onClose();
        }
    };

    if (!isOpen) return null;

    return (
        <div className="fixed inset-0 z-[100] flex items-center justify-center p-4 max-md:p-0">
            <div className="absolute inset-0 bg-black/40 backdrop-blur-sm" onClick={onClose} />
            <div className="relative bg-white w-full max-w-2xl rounded-2xl shadow-2xl flex flex-col overflow-hidden animate-in fade-in zoom-in-95 duration-200 border border-gray-200 max-md:max-w-none max-md:h-[100dvh] max-md:rounded-none max-md:border-0">

                {/* Header */}
                <div className="h-16 border-b border-gray-100 flex items-center justify-between px-8 bg-gray-50/50 shrink-0 max-md:px-4">
                    <div className="flex items-center gap-3">
                        <div className="w-9 h-9 rounded-xl bg-gray-900 text-white flex items-center justify-center shadow-sm">
                            <Wand2 size={18} />
                        </div>
                        <div>
                            <h2 className="text-lg font-bold text-gray-900">Soul Configuration</h2>
                            <p className="text-xs text-gray-500 font-medium">Step {step} of 4: {['Core Truths', 'Boundaries', 'Vibe', 'Continuity'][step - 1]}</p>
                        </div>
                    </div>
                    <button onClick={onClose} className="p-2 text-gray-400 hover:text-gray-600 rounded-lg hover:bg-gray-100 transition-colors">
                        <X size={20} />
                    </button>
                </div>

                {/* Progress Bar */}
                <div className="h-1 w-full bg-gray-100 flex">
                    {[1, 2, 3, 4].map(s => (
                        <div key={s} className={cn(
                            "h-full flex-1 transition-all duration-500",
                            s <= step ? "bg-gray-900" : "bg-gray-100"
                        )} />
                    ))}
                </div>

                {/* Content */}
                <div className="flex-1 min-h-0 p-8 overflow-y-auto max-md:p-4">

                    {step === 1 && (
                        <div className="space-y-6 animate-in fade-in slide-in-from-right-4 duration-300">
                            <div className="flex items-center gap-3">
                                <div className="p-2 bg-indigo-50 text-indigo-600 rounded-lg"><Anchor size={20} /></div>
                                <h3 className="text-xl font-bold text-gray-900">Core Truths</h3>
                            </div>
                            <p className="text-sm text-gray-500 leading-relaxed">
                                Define the fundamental mission and identity of your agent. What are the undeniable truths it lives by?
                            </p>

                            <textarea
                                value={selections.coreTruths}
                                onChange={(e) => { setSelections({ ...selections, coreTruths: e.target.value }); setErrors(null); }}
                                onKeyDown={(e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); handleNext(); } }}
                                className="w-full h-40 p-4 bg-gray-50 border border-gray-200 rounded-xl font-mono text-sm focus:outline-none focus:ring-2 focus:ring-gray-400 transition-all resize-none"
                                placeholder="- I am a helper...\n- My priority is... (Ctrl+Enter to continue)"
                            />

                            <div className="space-y-2">
                                <span className="text-[10px] font-bold text-gray-400 uppercase tracking-wider">Suggestions</span>
                                <div className="flex flex-wrap gap-2">
                                    {SUGGESTIONS.coreTruths.map(s => {
                                        const isAdded = addedSuggestions.coreTruths.includes(s);
                                        return (
                                            <button
                                                key={s}
                                                onClick={() => toggleSuggestion('coreTruths', s)}
                                                className={cn(
                                                    "text-xs px-3 py-1.5 border rounded-lg transition-all text-gray-600",
                                                    isAdded
                                                        ? "bg-gray-900 text-white border-gray-900"
                                                        : "bg-white border-gray-200 hover:border-gray-400"
                                                )}
                                            >
                                                {isAdded ? '- ' : '+ '}{s}
                                            </button>
                                        );
                                    })}
                                </div>
                            </div>
                        </div>
                    )}

                    {step === 2 && (
                        <div className="space-y-6 animate-in fade-in slide-in-from-right-4 duration-300">
                            <div className="flex items-center gap-3">
                                <div className="p-2 bg-red-50 text-red-600 rounded-lg"><Shield size={20} /></div>
                                <h3 className="text-xl font-bold text-gray-900">Boundaries</h3>
                            </div>
                            <p className="text-sm text-gray-500 leading-relaxed">
                                What will your agent NEVER do? Establish behavioral and ethical limits to ensure safe operation.
                            </p>

                            <textarea
                                value={selections.boundaries}
                                onChange={(e) => { setSelections({ ...selections, boundaries: e.target.value }); setErrors(null); }}
                                onKeyDown={(e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); handleNext(); } }}
                                className="w-full h-40 p-4 bg-gray-50 border border-gray-200 rounded-xl font-mono text-sm focus:outline-none focus:ring-2 focus:ring-gray-400 transition-all resize-none"
                                placeholder="- I will never...\n- I am forbidden from... (Ctrl+Enter to continue)"
                            />

                            <div className="space-y-2">
                                <span className="text-[10px] font-bold text-gray-400 uppercase tracking-wider">Suggestions</span>
                                <div className="flex flex-wrap gap-2">
                                    {SUGGESTIONS.boundaries.map(s => {
                                        const isAdded = addedSuggestions.boundaries.includes(s);
                                        return (
                                            <button
                                                key={s}
                                                onClick={() => toggleSuggestion('boundaries', s)}
                                                className={cn(
                                                    "text-xs px-3 py-1.5 border rounded-lg transition-all text-gray-600",
                                                    isAdded
                                                        ? "bg-gray-900 text-white border-gray-900"
                                                        : "bg-white border-gray-200 hover:border-gray-400"
                                                )}
                                            >
                                                {isAdded ? '- ' : '+ '}{s}
                                            </button>
                                        );
                                    })}
                                </div>
                            </div>
                        </div>
                    )}

                    {step === 3 && (
                        <div className="space-y-6 animate-in fade-in slide-in-from-right-4 duration-300">
                            <div className="flex items-center gap-3">
                                <div className="p-2 bg-yellow-50 text-yellow-600 rounded-lg"><Sparkles size={20} /></div>
                                <h3 className="text-xl font-bold text-gray-900">Vibe</h3>
                            </div>
                            <p className="text-sm text-gray-500 leading-relaxed">
                                Describe the personality and style. Is the agent technical, professional, or creative?
                            </p>

                            <textarea
                                value={selections.vibe}
                                onChange={(e) => { setSelections({ ...selections, vibe: e.target.value }); setErrors(null); }}
                                onKeyDown={(e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); handleNext(); } }}
                                className="w-full h-40 p-4 bg-gray-50 border border-gray-200 rounded-xl font-mono text-sm focus:outline-none focus:ring-2 focus:ring-gray-400 transition-all resize-none"
                                placeholder="- Speak in a technical tone...\n- Be concise... (Ctrl+Enter to continue)"
                            />

                            <div className="space-y-2">
                                <span className="text-[10px] font-bold text-gray-400 uppercase tracking-wider">Suggestions</span>
                                <div className="flex flex-wrap gap-2">
                                    {SUGGESTIONS.vibe.map(s => {
                                        const isAdded = addedSuggestions.vibe.includes(s);
                                        return (
                                            <button
                                                key={s}
                                                onClick={() => toggleSuggestion('vibe', s)}
                                                className={cn(
                                                    "text-xs px-3 py-1.5 border rounded-lg transition-all text-gray-600",
                                                    isAdded
                                                        ? "bg-gray-900 text-white border-gray-900"
                                                        : "bg-white border-gray-200 hover:border-gray-400"
                                                )}
                                            >
                                                {isAdded ? '- ' : '+ '}{s}
                                            </button>
                                        );
                                    })}
                                </div>
                            </div>
                        </div>
                    )}

                    {step === 4 && (
                        <div className="space-y-6 animate-in fade-in slide-in-from-right-4 duration-300">
                            <div className="flex items-center gap-3">
                                <div className="p-2 bg-blue-50 text-blue-600 rounded-lg"><History size={20} /></div>
                                <h3 className="text-xl font-bold text-gray-900">Continuity</h3>
                            </div>
                            <p className="text-sm text-gray-500 leading-relaxed">
                                How should the agent handle long-term goals and memory? Define how it evolves over sessions.
                            </p>

                            <textarea
                                value={selections.continuity}
                                onChange={(e) => { setSelections({ ...selections, continuity: e.target.value }); setErrors(null); }}
                                onKeyDown={(e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); handleNext(); } }}
                                className="w-full h-40 p-4 bg-gray-50 border border-gray-200 rounded-xl font-mono text-sm focus:outline-none focus:ring-2 focus:ring-gray-400 transition-all resize-none"
                                placeholder="- Learn from codebase...\n- Remember user preferences... (Ctrl+Enter to continue)"
                            />

                            <div className="space-y-2">
                                <span className="text-[10px] font-bold text-gray-400 uppercase tracking-wider">Suggestions</span>
                                <div className="flex flex-wrap gap-2">
                                    {SUGGESTIONS.continuity.map(s => {
                                        const isAdded = addedSuggestions.continuity.includes(s);
                                        return (
                                            <button
                                                key={s}
                                                onClick={() => toggleSuggestion('continuity', s)}
                                                className={cn(
                                                    "text-xs px-3 py-1.5 border rounded-lg transition-all text-gray-600",
                                                    isAdded
                                                        ? "bg-gray-900 text-white border-gray-900"
                                                        : "bg-white border-gray-200 hover:border-gray-400"
                                                )}
                                            >
                                                {isAdded ? '- ' : '+ '}{s}
                                            </button>
                                        );
                                    })}
                                </div>
                            </div>
                        </div>
                    )}

                    {errors && (
                        <div className="mt-4 p-3 bg-red-50 border border-red-100 rounded-lg flex items-center gap-2 text-red-600 text-xs animate-in slide-in-from-top-2">
                            <AlertTriangle size={14} />
                            {errors}
                        </div>
                    )}
                </div>

                {/* Footer */}
                <div className="h-20 border-t border-gray-100 flex items-center justify-between px-8 bg-gray-50/50 shrink-0 max-md:px-4">
                    <button
                        onClick={() => step > 1 ? setStep(step - 1) : onClose()}
                        className="px-6 py-2.5 rounded-xl font-medium text-gray-600 hover:bg-gray-200 transition-colors"
                    >
                        {step === 1 ? 'Cancel' : 'Back'}
                    </button>
                    <button
                        onClick={handleNext}
                        className="px-8 py-2.5 rounded-xl font-medium bg-gray-900 text-white hover:bg-black shadow-sm transition-all flex items-center gap-2"
                    >
                        {step === 4 ? 'Complete Soul' : 'Next Step'}
                        <ChevronRight size={16} />
                    </button>
                </div>
            </div>
        </div>
    );
}