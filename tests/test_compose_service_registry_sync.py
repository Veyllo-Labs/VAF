# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The service registry and the compose file describe the same stack.

Two places name the containers: docker-compose.memory.yml, which decides what
actually runs, and vaf/core/service_stack.py SERVICES, which every status and
repair path reads. A service added to one and forgotten in the other is
invisible in the dialog and never repaired, so this test fails on drift instead
of a rule in prose asking someone to remember.
"""
import re
from pathlib import Path

from vaf.core.service_stack import (
    CORE_SERVICES,
    COMPOSE_FILENAME,
    OPTIONAL_SERVICES,
    SERVICES,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _parse_compose() -> dict:
    """Service key -> {container_name, host_ports} straight from the compose file.

    Hand-parsed on purpose: pyyaml is not a hard dependency of the test suite,
    and the three things we compare (service keys, container names, published
    host ports) are each one unambiguous line.
    """
    text = (REPO_ROOT / COMPOSE_FILENAME).read_text(encoding="utf-8")
    lines = text.splitlines()
    services: dict = {}
    current = None
    in_services = False
    for line in lines:
        if re.match(r"^services:\s*$", line):
            in_services = True
            continue
        if in_services and re.match(r"^[a-zA-Z_-]+:\s*$", line):
            break  # a new top-level block (volumes:, networks:) ends the services
        if not in_services:
            continue
        m = re.match(r"^  ([a-zA-Z0-9_-]+):\s*$", line)
        if m:
            current = m.group(1)
            services[current] = {"container_name": None, "host_ports": []}
            continue
        if current is None:
            continue
        m = re.match(r"^\s+container_name:\s*(\S+)\s*$", line)
        if m:
            services[current]["container_name"] = m.group(1)
            continue
        m = re.match(r'^\s+-\s*"127\.0\.0\.1:\$\{[A-Z_]+:-(\d+)\}:\d+"\s*$', line)
        if m:
            services[current]["host_ports"].append(int(m.group(1)))
    return services


def test_registry_covers_every_compose_service():
    compose = _parse_compose()
    assert compose, "the compose file parsed to nothing - the parser or the file moved"
    assert set(compose) == {s.service_key for s in SERVICES}


def test_container_names_match_compose():
    compose = _parse_compose()
    for spec in SERVICES:
        assert compose[spec.service_key]["container_name"] == spec.container_name


def test_default_ports_match_the_published_ports():
    compose = _parse_compose()
    for spec in SERVICES:
        published = compose[spec.service_key]["host_ports"]
        if not published:
            assert spec.default_port == 0, (
                f"{spec.service_key} publishes no port, so the registry must not "
                f"claim one"
            )
            continue
        assert spec.default_port in published, (
            f"{spec.service_key} publishes {published}, registry says {spec.default_port}"
        )


def test_core_and_optional_partition_the_registry():
    assert set(CORE_SERVICES) | set(OPTIONAL_SERVICES) == {s.service_key for s in SERVICES}
    assert not set(CORE_SERVICES) & set(OPTIONAL_SERVICES)


def test_every_service_with_a_port_names_where_it_is_configured():
    """A port VAF cannot look up is a port it cannot compare, and the mismatch
    check would silently pass for that service."""
    for spec in SERVICES:
        if spec.default_port:
            assert spec.config_url_key or spec.env_url_var, spec.service_key
