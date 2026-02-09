"""
Standalone tool for updating the current user's identity (user_identity.json).

Used so the LLM can set name, language, location (city/country), preferences, do's and don'ts
when the user says e.g. "call me Mert", "I prefer German", or "I'm based in Berlin, Germany".
"""

from vaf.tools.base import BaseTool


class UpdateUserIdentityTool(BaseTool):
    """
    Update the current user's identity (name, language, preferences, do's and don'ts).
    Use when the user tells you their name, preferred language, what they like or prefer,
    or what you should do or avoid (e.g. "call me Mert", "I prefer German", "always be concise",
    "don't use emojis"). This keeps the User identity block in your system prompt accurate
    so you can greet them correctly (e.g. "Hey Mert") and respect their preferences.
    """
    name = "update_user_identity"
    description = (
        "Update the current user's identity so you know who you're talking to and how they like to be treated. "
        "Use when the user says their name, language, location (city/country), preferences, or do's/don'ts (e.g. 'call me X', 'I prefer German', "
        "'I'm in Berlin' or 'I'm based in Munich, Germany', 'always be concise', 'don't use emojis'). Use also when the user says which channel to use for proactive messages "
        "(e.g. 'send it to Telegram' -> main_messenger='telegram'). You can set name, language, city, country, main_messenger, timezone, date_format, time_format, and add/remove preferences, do's, and don'ts. "
        "Location (city/country) and timezone help you answer context-aware questions (e.g. weather, local time). date_format (e.g. dd.mm.yyyy) and time_format (24h/12h) are used when showing dates and times. "
        "This updates the User identity block in your system prompt so you can greet them correctly (e.g. 'Hey Mert') and follow their rules."
    )
    parameters = {
        "type": "object",
        "properties": {
            "city": {
                "type": "string",
                "description": "User's city (e.g. 'Berlin', 'Munich'). Use when user says where they live or are based."
            },
            "country": {
                "type": "string",
                "description": "User's country (e.g. 'Germany', 'DE'). Use when user says where they live or are based."
            },
            "main_messenger": {
                "type": "string",
                "enum": ["telegram", "discord", "slack"],
                "description": "Preferred channel for proactive messages. Set when user says e.g. 'send it via Telegram' or 'use Discord'."
            },
            "name": {
                "type": "string",
                "description": "Display name for the user (e.g. 'Mert'). Omit to keep current."
            },
            "language": {
                "type": "string",
                "description": "Preferred language code (e.g. 'de', 'en'). Omit to keep current."
            },
            "add_preference": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Preference(s) to add (e.g. ['prefers short answers', 'likes code examples'])."
            },
            "remove_preference": {
                "type": "string",
                "description": "Exact text of one preference to remove. Omit to remove none."
            },
            "add_do": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Do(s) to add (e.g. ['always be concise', 'use bullet points for lists'])."
            },
            "remove_do": {
                "type": "string",
                "description": "Exact text of one 'do' rule to remove."
            },
            "add_dont": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Don't(s) to add (e.g. ['don\'t use emojis', 'avoid jargon'])."
            },
            "remove_dont": {
                "type": "string",
                "description": "Exact text of one 'don\'t' rule to remove."
            },
            "timezone": {
                "type": "string",
                "description": "IANA timezone (e.g. 'Europe/Berlin', 'America/New_York'). Use when user says their timezone or how they want times shown."
            },
            "date_format": {
                "type": "string",
                "description": "Preferred date format (e.g. 'dd.mm.yyyy', 'yyyy-mm-dd', 'mm/dd/yyyy'). Use when user says how they want dates shown."
            },
            "time_format": {
                "type": "string",
                "enum": ["24h", "12h"],
                "description": "Preferred time format: 24h or 12h. Use when user says they want 24-hour or 12-hour (AM/PM) time."
            }
        },
        "required": []
    }

    def run(self, **kwargs) -> str:
        username = kwargs.get("username") or "admin"
        name = (kwargs.get("name") or "").strip() or None
        language = (kwargs.get("language") or "").strip() or None
        city = (kwargs.get("city") or "").strip() or None
        country = (kwargs.get("country") or "").strip() or None
        add_preference = kwargs.get("add_preference")
        remove_preference = (kwargs.get("remove_preference") or "").strip() or None
        add_do = kwargs.get("add_do")
        remove_do = (kwargs.get("remove_do") or "").strip() or None
        add_dont = kwargs.get("add_dont")
        remove_dont = (kwargs.get("remove_dont") or "").strip() or None
        main_messenger_raw = (kwargs.get("main_messenger") or "").strip().lower() or None
        main_messenger = main_messenger_raw if main_messenger_raw in ("telegram", "discord", "slack") else None
        timezone = (kwargs.get("timezone") or "").strip() or None
        date_format = (kwargs.get("date_format") or "").strip() or None
        time_format_raw = (kwargs.get("time_format") or "").strip().lower() or None
        time_format = time_format_raw if time_format_raw in ("24h", "12h") else None

        def _norm_list(val):
            if val is None:
                return []
            if isinstance(val, str):
                return [val.strip()] if val.strip() else []
            return [str(x).strip() for x in val if str(x).strip()]

        add_preference = _norm_list(add_preference)
        add_do = _norm_list(add_do)
        add_dont = _norm_list(add_dont)

        if not any([name, language, city is not None, country is not None, main_messenger is not None, timezone is not None, date_format is not None, time_format is not None, add_preference, remove_preference, add_do, remove_do, add_dont, remove_dont]):
            return "No updates provided. Pass at least one of: name, language, city, country, main_messenger, timezone, date_format, time_format, add_preference, remove_preference, add_do, remove_do, add_dont, remove_dont."

        try:
            from datetime import datetime
            from vaf.auth.user_workspace import get_user_workspace
            ws = get_user_workspace(username)
            user_identity = ws.get_user_identity()
            if name is not None:
                user_identity["name"] = name
            if language is not None:
                user_identity["preferred_language"] = language
            if main_messenger is not None:
                user_identity["main_messenger"] = main_messenger
            if city is not None:
                user_identity["city"] = city
            if country is not None:
                user_identity["country"] = country
            if timezone is not None:
                user_identity["timezone"] = timezone
            if date_format is not None:
                user_identity["date_format"] = date_format
            if time_format is not None:
                user_identity["time_format"] = time_format
            for p in add_preference:
                if p and p not in user_identity["preferences"]:
                    user_identity["preferences"].append(p)
            if remove_preference and remove_preference in user_identity["preferences"]:
                user_identity["preferences"].remove(remove_preference)
            for d in add_do:
                if d and d not in user_identity["dos"]:
                    user_identity["dos"].append(d)
            if remove_do and remove_do in user_identity["dos"]:
                user_identity["dos"].remove(remove_do)
            for d in add_dont:
                if d and d not in user_identity["donts"]:
                    user_identity["donts"].append(d)
            if remove_dont and remove_dont in user_identity["donts"]:
                user_identity["donts"].remove(remove_dont)

            # Timestamp for this change (same time source as system prompt)
            parts = []
            if name is not None:
                parts.append("name")
            if language is not None:
                parts.append("language")
            if main_messenger is not None:
                parts.append("main_messenger")
            if city is not None:
                parts.append("city")
            if country is not None:
                parts.append("country")
            if timezone is not None:
                parts.append("timezone")
            if date_format is not None:
                parts.append("date_format")
            if time_format is not None:
                parts.append("time_format")
            if add_preference or remove_preference:
                parts.append("preference")
            if add_do or remove_do:
                parts.append("do")
            if add_dont or remove_dont:
                parts.append("dont")
            summary = ", ".join(parts) if parts else "update"
            change_log = user_identity.get("change_log")
            if not isinstance(change_log, list):
                change_log = []
            change_log.append({"at": datetime.now().isoformat(), "action": summary})
            user_identity["change_log"] = change_log[-50:]  # keep last 50
            ws.save_user_identity(user_identity)
            return "User identity updated. You can use this in your next reply (e.g. greet by name, follow preferences and do's/don'ts)."
        except Exception as e:
            return f"Error updating user identity: {e}"
