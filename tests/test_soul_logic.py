
import pytest
from vaf.auth.user_workspace import UserWorkspace
from vaf.core.config import Config
import os
import shutil

def test_soul_structure_generation():
    """Verify that the SOUL structure contains the required OpenClaw sections."""
    username = "testuser"
    workspace = UserWorkspace(username)
    
    # Mock selections from Wizard
    core_truths = "I am a helpful AI."
    boundaries = "I will not hurt humans."
    vibe = "Professional and sharp."
    continuity = "I remember user preferences."
    
    soul_content = f"""# SOUL of {username}

## 1. Core Truths
{core_truths}

## 2. Boundaries
{boundaries}

## 3. Vibe
{vibe}

## 4. Continuity
{continuity}"""

    assert "## 1. Core Truths" in soul_content
    assert "## 2. Boundaries" in soul_content
    assert "## 3. Vibe" in soul_content
    assert "## 4. Continuity" in soul_content
    assert username in soul_content

def test_user_workspace_initialization():
    """Verify that a new workspace is created with default values."""
    username = "new_random_user"
    ws = UserWorkspace(username)
    ws.ensure_exists()
    
    assert ws.base_dir.exists()
    assert ws.identity_file.exists()
    assert ws.soul_file.exists()

    identity = ws.get_identity()
    assert "emoji" in identity
    assert len(identity["emoji"]) > 0
    
    # Cleanup
    shutil.rmtree(ws.base_dir)
