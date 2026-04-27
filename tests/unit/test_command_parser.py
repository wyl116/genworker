# edition: baseline
import pytest

from src.channels.commands import CommandParser, CommandSpec, CommandRegistry


async def _noop(ctx):
    return "ok"


def test_command_parser_rejects_command_above_current_trust_level():
    registry = CommandRegistry()
    registry.register(
        CommandSpec(
            name="restricted",
            description="restricted command",
            handler=_noop,
            required_trust_level="FULL",
        )
    )
    parser = CommandParser(registry)

    match = parser.try_parse(
        text="/restricted do something",
        prefix="/",
        channel_type="feishu",
        trust_level="BASIC",
    )

    assert match is None


def test_command_parser_allows_command_at_or_above_required_trust_level():
    registry = CommandRegistry()
    registry.register(
        CommandSpec(
            name="restricted",
            description="restricted command",
            handler=_noop,
            required_trust_level="STANDARD",
        )
    )
    parser = CommandParser(registry)

    match = parser.try_parse(
        text="/restricted do something",
        prefix="/",
        channel_type="feishu",
        trust_level="FULL",
    )

    assert match is not None
    assert match.spec.name == "restricted"
    assert match.args["argv"] == ("do", "something")
