"""Workflow registry. Each workflow is an async function over ``TaskContext``."""

from .browse import browser_task
from .chat import chat_workflow
from .desktop import desktop_workflow
from .general import general_workflow
from .research import research_report
from .video import video_analysis

WORKFLOWS = {
    "research_report": research_report,
    "browser_task": browser_task,
    "video_analysis": video_analysis,
    "desktop_workflow": desktop_workflow,
    "chat_workflow": chat_workflow,
    "general_workflow": general_workflow,
}

__all__ = ["WORKFLOWS"]
