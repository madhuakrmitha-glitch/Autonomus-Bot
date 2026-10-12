"""Workflow registry. Each workflow is an async function over ``TaskContext``."""

from .browse import browser_task
from .desktop import desktop_workflow
from .research import research_report
from .video import video_analysis

WORKFLOWS = {
    "research_report": research_report,
    "browser_task": browser_task,
    "video_analysis": video_analysis,
    "desktop_workflow": desktop_workflow,
}

__all__ = ["WORKFLOWS"]
