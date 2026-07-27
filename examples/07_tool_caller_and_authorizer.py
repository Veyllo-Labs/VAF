# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Run a tool without a conversation, and decide about each call yourself.

Two pieces of the framework that need no model at all:

  ToolCaller           runs one tool with VAF's own rules - policy, the
                       confirmation gate, the identity a tool declares, a
                       timeout, and the documented events.
  set_tool_authorizer  lets your application refuse a call, insist on a
                       confirmation, or wave one through - per call, per user,
                       per argument.

Unlike the other examples this one needs NO provider, NO API key and NO network:
it never talks to a model. Run it anywhere:

    venv/bin/python examples/07_tool_caller_and_authorizer.py

Docs: docs/EMBEDDING.md, "Running a tool yourself" and "Deciding about a tool
call".
"""
from vaf import BaseTool, ToolCaller

# Two synthetic tenants. In a real application these come from your own auth,
# and VAF takes them as an assertion - it authenticates nobody.
ALICE = "6f9619ff-8b86-d011-b42d-00c04fc964ff"
BOB = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"

NOTES = {ALICE: "buy milk", BOB: "call the dentist"}


class TenantNotes(BaseTool):
    """A tool that serves per-user data, so it has to know who is calling."""

    name = "tenant_notes"
    description = "Read the calling user's notes."
    permission_level = "read"
    side_effect_class = "none"
    parameters = {"type": "object", "properties": {}, "required": []}

    # Ask for exactly what you consume. Valid keys: user_scope_id, username,
    # user_role. A tool that declares nothing receives nothing.
    identity_kwargs = ("user_scope_id",)

    def run(self, **kwargs) -> str:
        scope = kwargs.get("user_scope_id")
        return NOTES.get(scope, "(no notes)")


class DeleteEverything(BaseTool):
    """Something you would want a human to confirm."""

    name = "delete_everything"
    description = "Pretend to delete all of the user's data."
    permission_level = "dangerous"
    side_effect_class = "irreversible"
    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, **kwargs) -> str:
        return "(pretended to delete everything)"


class AdminReport(BaseTool):
    name = "admin_report"
    description = "An administrators-only report."
    permission_level = "read"
    admin_only = True
    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, **kwargs) -> str:
        return "(the admin report)"


TOOLS = {t.name: t for t in (TenantNotes(), DeleteEverything(), AdminReport())}


def caller_for(scope, *, role="user", username="tenant", authorize=None, events=None):
    """One ToolCaller per request, carrying that request's identity."""
    return ToolCaller(
        TOOLS,
        user_scope_id=scope,
        username=username,
        user_role=role,
        authorize=authorize,
        on_event=(events.append if events is not None else None),
    )


def part_one_identity() -> None:
    print("1. The identity a tool declares is ASSIGNED, never taken from the call")
    print("   alice ->", caller_for(ALICE).execute("tenant_notes", {}))
    print("   bob   ->", caller_for(BOB).execute("tenant_notes", {}))

    # The arguments start out as whatever a model produced, so a caller (or a
    # prompt injection) claiming to be someone else is simply overwritten.
    spoofed = caller_for(BOB).execute("tenant_notes", {"user_scope_id": ALICE})
    print("   bob, claiming to be alice ->", spoofed, " (his own, not hers)")


def part_two_policy() -> None:
    print("\n2. Policy applies to your tools too, not only to VAF's")
    print("   user  ->", caller_for(BOB).execute("admin_report", {}))
    print("   admin ->", caller_for(BOB, role="admin").execute("admin_report", {}))


def part_three_gate() -> None:
    print("\n3. A gated tool never blocks on a human who is not there")
    print("   headless    ->", caller_for(ALICE).execute("delete_everything", {}))

    # Plug your own confirmation UI in: decide(tool_name, reason) answers
    # "allow_once", "allow_always" or "cancel".
    def ask_the_user(tool_name, reason):
        print(f"   [your UI] confirm {tool_name}? ({reason[:44]}...) -> yes, once")
        return "allow_once"

    interactive = ToolCaller(TOOLS, user_scope_id=ALICE, interactive=True,
                             decide=ask_the_user)
    print("   with a human->", interactive.execute("delete_everything", {}))


def part_four_authorizer() -> None:
    print("\n4. Your application's own say, per call")

    def authorize(req):
        # Everything about WHO is calling is trustworthy; req.args is the
        # model's own output and is a read-only snapshot.
        if req.tool_name == "tenant_notes" and req.user_scope_id == BOB:
            req.deny("bob's plan does not include notes")
        elif req.side_effect_class == "irreversible":
            req.ask("this cannot be undone")
        # Saying nothing means having no opinion: the call proceeds as it
        # would without an authorizer at all.

    print("   alice, allowed ->", caller_for(ALICE, authorize=authorize).execute("tenant_notes", {}))
    print("   bob, denied    ->", caller_for(BOB, authorize=authorize).execute("tenant_notes", {}))

    # ask() reaches the gate even for a tool nothing would normally gate, and
    # even where a standing grant would have skipped it.
    print("   ask() headless ->", caller_for(ALICE, authorize=authorize)
          .execute("delete_everything", {}))

    # A broken guard must not become no guard: an exception is a refusal.
    def broken(req):
        raise RuntimeError("the quota service is down")

    print("   crashing guard ->", caller_for(ALICE, authorize=broken).execute("tenant_notes", {}))


def part_five_events() -> None:
    print("\n5. The same events an Agent emits (schema: docs/OBSERVABILITY.md)")
    events = []
    caller_for(ALICE, events=events).execute("tenant_notes", {})
    for e in events:
        print(f"   {e['type']:<12} tool={e['tool']}"
              + (f" ok={e['ok']}" if "ok" in e else ""))

    # A refused call emits NOTHING, so an observer never sees a blocked tool
    # reported as one that ran.
    blocked = []
    caller_for(BOB, events=blocked).execute("admin_report", {})
    print(f"   a blocked call emitted {len(blocked)} events")


def main() -> None:
    part_one_identity()
    part_two_policy()
    part_three_gate()
    part_four_authorizer()
    part_five_events()
    print("\nNo model was involved. See docs/EMBEDDING.md for the full contract.")


if __name__ == "__main__":
    main()
