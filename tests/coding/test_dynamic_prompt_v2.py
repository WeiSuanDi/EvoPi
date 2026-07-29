from __future__ import annotations

from evopi.coding import CodingHarness
from evopi.coding.prompts import build_system_prompt
from evopi.core.tool import Tool


class _Model:
    name = "test"
    context_window = 0

    async def stream(self, context):
        if False:
            yield


def _tool(
    name: str,
    *,
    effect: str,
    snippet: str | None = None,
    guidelines: list[str] | None = None,
) -> Tool:
    metadata: dict[str, object] = {"effects": [effect]}
    if snippet is not None:
        metadata["prompt_snippet"] = snippet
    if guidelines is not None:
        metadata["prompt_guidelines"] = guidelines
    return Tool(
        name=name,
        description=f"{name} first line\nignored details",
        parameters={"type": "object", "properties": {}},
        handler=lambda: None,
        metadata=metadata,
    )


def test_prompt_uses_snippets_deduplicated_guidelines_and_workspace(tmp_path) -> None:
    prompt = build_system_prompt(
        [
            _tool(
                "reader",
                effect="read",
                snippet="Read selected files safely.",
                guidelines=["Inspect before changing.", "Shared rule."],
            ),
            _tool(
                "writer",
                effect="write",
                guidelines=["Shared rule.", "Use exact edits."],
            ),
        ],
        workspace=tmp_path,
    )

    assert "Read selected files safely." in prompt
    assert "writer first line" in prompt
    assert prompt.count("Shared rule.") == 1
    assert "Use exact edits." in prompt
    assert "verify writes" in prompt.lower()
    assert str(tmp_path.resolve()) == prompt.splitlines()[-1]
    assert "/help" not in prompt
    assert "Policy may block" in prompt


def test_prompt_conditions_guidelines_on_active_effects(tmp_path) -> None:
    read_only = build_system_prompt(
        [_tool("reader", effect="read")],
        workspace=tmp_path,
    )
    empty = build_system_prompt([], workspace=tmp_path)

    assert "verify writes" not in read_only.lower()
    assert "Available tools: none." in empty
    assert "write_file" not in empty


def test_system_replacement_and_append_have_fixed_order(tmp_path) -> None:
    replacement = CodingHarness(
        model=_Model(),
        workspace=tmp_path,
        memory_path=None,
        system_prompt="replacement",
        append_system_prompt="appendix",
    )
    generated = CodingHarness(
        model=_Model(),
        workspace=tmp_path,
        memory_path=None,
        append_system_prompt="appendix",
        tool_names={"read_file"},
    )

    assert replacement.system_prompt == "replacement\n\nappendix"
    assert "Available Tools" not in replacement.system_prompt
    assert generated.system_prompt.endswith("appendix")
    assert "`read_file`" in generated.system_prompt
    assert "`write_file`" not in generated.system_prompt


def test_dynamic_prompt_refreshes_after_tool_ceiling_change(tmp_path) -> None:
    harness = CodingHarness(
        model=_Model(),
        workspace=tmp_path,
        memory_path=None,
    )

    harness.configure_tool_ceiling(include_names={"read_file"})

    assert "`read_file`" in harness.system_prompt
    assert "`shell_command`" not in harness.system_prompt
