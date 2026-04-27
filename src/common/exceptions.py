"""
Custom exception module for genworker.
"""


class LittlewangException(Exception):
    """Base exception class."""
    pass


class AgentException(LittlewangException):
    """Agent-related exception."""
    pass


class MCPException(LittlewangException):
    """MCP-related exception."""
    pass


class LLMException(LittlewangException):
    """LLM call exception."""
    pass


class ConfigException(LittlewangException):
    """Configuration exception."""
    pass


class ToolException(LittlewangException):
    """Tool invocation exception."""
    pass


class WorkerException(LittlewangException):
    """Worker-related exception."""
    pass


class SkillException(LittlewangException):
    """Skill-related exception."""
    pass
