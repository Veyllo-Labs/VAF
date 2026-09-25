// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
'use client';

import { Check } from 'lucide-react';
import { useTranslations } from 'next-intl';
import { cn } from '@/lib/utils';
import { pickedIndex, type AskQuestion } from './askContract';

interface Props {
    asks: AskQuestion[];
    /** The person's next message after this turn, or null while nobody has answered. */
    reply: string | null;
    /** Whether a click may send now (nothing is generating). */
    canPick: boolean;
    onPick: (option: string) => void;
}

/**
 * The options of a question the agent asked (vaf/tools/ask_user.py), under the turn's answer.
 * A click sends the option as the person's next message, exactly as if they had typed it, so
 * the agent reads a reply and nothing else. Once the chat has a reply, the buttons stay as the
 * record: the picked one marked, the rest dimmed - also after a reload, because both halves
 * come from the history.
 */
export default function AskChoices({ asks, reply, canPick, onPick }: Props) {
    const t = useTranslations('main');
    if (!asks.length) return null;
    const answered = reply !== null;
    return (
        <div className="flex flex-col gap-2 pt-2">
            {asks.map((q, qi) => {
                const picked = pickedIndex(q.options, reply);
                return (
                    <div key={qi} className="flex flex-wrap gap-2">
                        {q.options.map((opt, i) => (
                            <button key={i} type="button" disabled={answered || !canPick}
                                onClick={() => onPick(opt)}
                                className={cn(
                                    'px-3 py-1.5 text-sm rounded-md inline-flex items-center gap-1.5 text-left transition-colors',
                                    answered && i === picked
                                        // The house's primary button, spelled out in both tones
                                        // (see HeldSendCard.tsx): the record of the pick.
                                        ? 'bg-gray-900 text-white dark:bg-[#e6e6e6] dark:text-[#181818]'
                                        : 'border border-gray-300 dark:border-[#3a3a3a] bg-white dark:bg-[#1f1f1f] text-gray-800 dark:text-gray-200 hover:bg-gray-100 dark:hover:bg-[#2a2a2a]',
                                    answered && i !== picked && 'opacity-50',
                                    !answered && !canPick && 'opacity-60',
                                    'disabled:cursor-default disabled:hover:bg-white dark:disabled:hover:bg-[#1f1f1f]',
                                    answered && i === picked && 'disabled:hover:bg-gray-900 dark:disabled:hover:bg-[#e6e6e6]',
                                )}>
                                {answered && i === picked && <Check className="w-3.5 h-3.5 shrink-0" />}
                                <span>{opt}</span>
                            </button>
                        ))}
                    </div>
                );
            })}
            {!answered && (
                <span className="text-xs text-gray-500 dark:text-[#9a9a9a]">{t('askPickHint')}</span>
            )}
        </div>
    );
}
