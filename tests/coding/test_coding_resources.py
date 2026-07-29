from __future__ import annotations

from evopi.coding import CodingHarness
from evopi.memory import MemoryEntry, MemoryStore


class _Model:
    name = "test"
    context_window = 0

    async def stream(self, context):
        if False:
            yield


def test_coding_resources_are_public_and_do_not_expose_memory_content(
    tmp_path,
) -> None:
    memory_path = tmp_path / "memory.json"
    store = MemoryStore(memory_path)
    store.add(MemoryEntry(content="private memory"))
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "demo.md").write_text(
        "---\nname: demo\nversion: 1.2.3\nrisk_level: medium\n---\nsecret skill body",
        encoding="utf-8",
    )

    harness = CodingHarness(
        model=_Model(),
        workspace=tmp_path,
        memory_path=memory_path,
        skills_root=skills,
        enable_subagent=True,
    )

    resources = harness.resources
    assert resources.memory.enabled is True
    assert resources.memory.entry_count == 1
    assert resources.subagent_enabled is True
    assert [(skill.name, skill.version, skill.risk_level) for skill in resources.skills] == [
        ("demo", "1.2.3", "medium")
    ]
    assert "private memory" not in repr(resources)
    assert "secret skill body" not in repr(resources)
