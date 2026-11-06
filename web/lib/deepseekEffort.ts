// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
/**
 * DeepSeek's thinking effort in Settings (`deepseek_reasoning_effort`).
 *
 * The backend sends the effort on EVERY DeepSeek call, whichever job makes it: the
 * chat, the sub-agents, the thinking runs, the browser agent, the voice call or the
 * failover backup (`openai_request_params` gates on the provider of the call, not on
 * the main provider). So the control is shown whenever any of those jobs can reach
 * DeepSeek, not only when DeepSeek is the main provider - otherwise the setting
 * would be in force and invisible.
 */

/** The values the backend accepts; '' sends nothing (DeepSeek's own default, high). */
export const DEEPSEEK_EFFORTS = ['', 'low', 'high', 'max'] as const;

/**
 * Every config key that names the provider of a chat-model job. Guarded against
 * vaf/core/model_roles.py ROLE_CONFIG_KEYS by tests/test_deepseek_reasoning_effort.py,
 * so a new job cannot be missing here.
 */
export const DEEPSEEK_LANE_KEYS = [
    'provider',
    'subagent_provider',
    'thinking_provider',
    'vision_provider',
    'browser_agent_provider',
    'voice_agent_provider',
    'failover_backup_provider',
] as const;

/**
 * True when the DeepSeek effort can matter on this instance: a DeepSeek key is stored
 * or typed (every DeepSeek call needs one), or any job names DeepSeek as its provider.
 * The sub-agent provider counts only with its gate on, as in the backend
 * (config.subagent_provider_override).
 */
export function deepseekInUse(config: Record<string, unknown>, hasDeepseekKey: boolean): boolean {
    if (hasDeepseekKey) return true;
    return DEEPSEEK_LANE_KEYS.some((key) => {
        if (key === 'subagent_provider' && !config.subagent_use_separate_provider) return false;
        return String(config[key] ?? '').trim().toLowerCase() === 'deepseek';
    });
}

/**
 * What the select shows for a stored value, and whether that value is ignored.
 * The backend accepts '' and low/high/max in any case and spacing
 * (api_backend.check_deepseek_reasoning_effort), so `MAX` shows as Max. Anything
 * else (a hand-edited config) is ignored by the backend; the select keeps showing
 * it as the stale "(current)" entry so that choosing Standard is a real change
 * and saves '', and `invalid` drives the hint that says it is ignored.
 */
export function effortSelectValue(raw: unknown): { value: string; invalid: boolean } {
    const text = String(raw ?? '').trim();
    const lowered = text.toLowerCase();
    if ((DEEPSEEK_EFFORTS as readonly string[]).includes(lowered)) return { value: lowered, invalid: false };
    return { value: text, invalid: true };
}
