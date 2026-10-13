"""Workflow registry. Each workflow is an async function over ``TaskContext``."""

from .browse import browser_task
from .desktop import desktop_workflow
from .research import research_report
from .video import video_analysis
from .chat import chat_workflow

WORKFLOWS = {
    "research_report": research_report,
    "browser_task": browser_task,
    "video_analysis": video_analysis,
    "desktop_workflow": desktop_workflow,
    "chat_workflow": chat_workflow,
}

__all__ = ["WORKFLOWS"]
