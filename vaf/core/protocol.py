# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
from typing import Optional, Any, Dict, List, Literal
from pydantic import BaseModel, Field
import uuid
import time

# --- Base Frames (Inspired by Clawdbot's RequestFrame/ResponseFrame) ---

class VAFMessage(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = Field(default_factory=time.time)
    source: str  # e.g., "cli", "discord", "web"
    target: str = "gateway" # e.g., "gateway", "agent", "broadcast"

class CommandRequest(VAFMessage):
    """A request to execute a command or prompt."""
    type: Literal["command.exec", "agent.prompt", "system.ping"]
    payload: Dict[str, Any]

class EventFrame(VAFMessage):
    """An event emitted by the system (logs, status updates)."""
    type: Literal["log", "status", "response", "error"]
    payload: Dict[str, Any]

# --- Specific Payloads ---

class AgentPromptPayload(BaseModel):
    """Payload for 'agent.prompt'."""
    text: str
    context: Optional[Dict[str, Any]] = None

class LogPayload(BaseModel):
    """Payload for 'log'."""
    level: Literal["info", "warning", "error", "debug"]
    message: str
    component: str

class SystemStatusPayload(BaseModel):
    """Payload for 'status'."""
    state: Literal["idle", "thinking", "acting", "offline"]
    active_agent: Optional[str] = None
