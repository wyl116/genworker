# edition: baseline
"""Tests for contact parsing from PERSONA.md."""
from __future__ import annotations

import textwrap
from pathlib import Path

from src.worker.parser import parse_persona_md


def test_parse_contacts_and_contact_settings(tmp_path: Path):
    content = textwrap.dedent("""\
        ---
        identity:
          worker_id: "w1"
          name: "Worker"
        contacts:
          - person_id: "alice"
            name: "Alice"
            role: "PM"
            identities:
              - channel_type: "email"
                handle: "alice@example.com"
                email: "alice@example.com"
        contact_settings:
          context_limit: 3
        ---
        body
    """)
    path = tmp_path / "PERSONA.md"
    path.write_text(content, encoding="utf-8")

    worker = parse_persona_md(path)

    assert worker.configured_contacts[0].primary_name == "Alice"
    assert worker.contacts_config.context_limit == 3
