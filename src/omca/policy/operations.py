"""Allowlisted operations, their restricted argument schemas and deterministic classification.

Schema validation does not establish permission: every operation also has a classifier that
inspects the target, destination and current observation (spec section 14). Unknown
operation names are denied. Page content is untrusted: element names can only make a
classification *more* restrictive, never grant an allowance on their own.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from ..config import ApplicationConfig
from ..contracts import ActionCategory, ElementRef, Observation

C = ActionCategory


class _A(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Argument schemas
# ---------------------------------------------------------------------------


class SearchQueryArgs(_A):
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=8, ge=1, le=20)


class RetrieveArgs(_A):
    url: str = Field(max_length=2048)
    formats: list[Literal["markdown", "html", "screenshot", "links"]] = Field(default_factory=lambda: ["markdown"])


class NavigateArgs(_A):
    url: str = Field(max_length=2048)


class ObserveArgs(_A):
    full_page: bool = False
    include_images: bool = True
    max_elements: int = Field(default=250, ge=1, le=1000)
    attachment_id: str | None = None


class ReadPageArgs(_A):
    url: str | None = Field(default=None, max_length=2048)
    attachment_id: str | None = None


class ClickArgs(_A):
    ref: str = Field(max_length=64)


class ClickPointArgs(_A):
    x: float = Field(ge=0, le=20000, description="CSS pixels relative to the viewport")
    y: float = Field(ge=0, le=20000)
    expect_fingerprint: str | None = Field(default=None, max_length=128)


class HitTestArgs(_A):
    x: float = Field(ge=0, le=20000)
    y: float = Field(ge=0, le=20000)


class TypeArgs(_A):
    ref: str = Field(max_length=64)
    text: str = Field(max_length=4000)
    clear: bool = True
    submit: bool = False


class PressArgs(_A):
    key: str = Field(pattern=r"^[A-Za-z0-9+_ -]{1,40}$|^.$")


class ScrollArgs(_A):
    dx: int = Field(default=0, ge=-10000, le=10000)
    dy: int = Field(default=600, ge=-10000, le=10000)


class SelectArgs(_A):
    ref: str = Field(max_length=64)
    value: str = Field(max_length=500)


class WaitArgs(_A):
    ms: int = Field(default=1000, ge=0, le=15000)
    text: str | None = Field(default=None, max_length=200)


class EmptyArgs(_A):
    pass


class FetchImageArgs(_A):
    src: str = Field(max_length=4096)
    name: str | None = Field(default=None, max_length=120)


class DownloadArgs(_A):
    ref: str = Field(max_length=64)
    expected_extension: str | None = Field(default=None, max_length=10)


class VideoFramesArgs(_A):
    ref: str | None = Field(default=None, max_length=64)
    timestamps: list[float] = Field(min_length=1, max_length=64)
    max_side: int = Field(default=1024, ge=64, le=4096)


class MediaFetchArgs(_A):
    url: str = Field(max_length=4096)
    max_height: int = Field(default=720, ge=144, le=2160)
    max_filesize_mb: int = Field(default=500, ge=1, le=4000)
    want_subtitles: bool = True


class FileWriteArgs(_A):
    rel_path: str = Field(max_length=400)
    artifact_id: str
    overwrite: bool = False


class FileDeleteArgs(_A):
    rel_path: str = Field(max_length=400)


class SendOwnerArgs(_A):
    text: str = Field(max_length=4000)
    artifact_id: str | None = None


class MessageSendArgs(_A):
    recipient: str = Field(max_length=200)
    text: str = Field(max_length=4000)


class EmailDraftArgs(_A):
    to: list[str] = Field(min_length=1, max_length=20)
    subject: str = Field(max_length=300)
    body: str = Field(max_length=20000)


class EmailSendArgs(_A):
    draft_artifact_id: str
    to: list[str] = Field(min_length=1, max_length=20)


class CalendarDraftArgs(_A):
    title: str = Field(max_length=300)
    start: str
    end: str
    attendees: list[str] = Field(default_factory=list, max_length=50)
    location: str | None = None
    description: str | None = Field(default=None, max_length=5000)


class CalendarInviteArgs(_A):
    draft_artifact_id: str
    attendees: list[str] = Field(min_length=1, max_length=50)


class SpreadsheetReadArgs(_A):
    artifact_id: str
    sheet: str | None = None
    cell_range: str | None = Field(default=None, pattern=r"^[A-Z]{1,3}[0-9]{1,7}(:[A-Z]{1,3}[0-9]{1,7})?$")


class SpreadsheetWriteArgs(_A):
    artifact_id: str
    sheet: str | None = None
    cells: dict[str, str | int | float | None] = Field(max_length=500)
    output_name: str | None = Field(default=None, max_length=120)


class DesktopRawArgs(_A):
    action: Literal["click", "type", "hotkey", "scroll"]
    x: int | None = None
    y: int | None = None
    text: str | None = Field(default=None, max_length=2000)
    keys: str | None = Field(default=None, max_length=60)


class ReconcileArgs(_A):
    action_id: str
    original_operation: str


class PermissionChangeArgs(_A):
    target: str
    change: str


class CredentialExportArgs(_A):
    target: str


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------

CONSEQUENTIAL_TERMS = [
    "submit", "send", "pay", "buy", "purchase", "order", "checkout", "check out", "delete", "remove", "erase",
    "destroy", "publish", "post", "tweet", "share", "invite", "transfer", "confirm", "approve", "sign", "subscribe",
    "unsubscribe", "save", "upload", "apply", "book", "reserve", "donate", "grant", "revoke", "permission", "reply",
    "forward", "archive", "trash", "empty", "deactivate", "close account", "log out", "logout", "sign out",
    "place", "accept", "decline", "withdraw", "cancel subscription", "rsvp",
]

NAV_KEYS = {"Tab", "Shift+Tab", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "PageUp", "PageDown", "Home",
            "End", "Escape"}
SUBMIT_KEYS = {"Enter", "NumpadEnter", "Control+Enter", "Meta+Enter", "Shift+Enter", "Control+s", "Meta+s",
               "Control+S", "Meta+S", "Control+p", "Meta+p"}
SAFE_COMBOS = {"Control+f": C.NAVIGATION, "Meta+f": C.NAVIGATION, "Control+c": C.READ, "Meta+c": C.READ,
               "Control+a": C.NAVIGATION, "Meta+a": C.NAVIGATION}
EDITABLE_ROLES = {"textbox", "searchbox", "combobox", "spinbutton"}
FORM_STATE_ROLES = {"checkbox", "radio", "option", "listbox", "combobox", "slider", "spinbutton", "textbox",
                    "searchbox"}
SENSITIVE_INPUT = re.compile(r"pass(word|code)?|one[- ]?time|otp|cvv|cvc|card.?number|security code|pin\b|secret",
                             re.I)
CONSEQUENTIAL_PATH = re.compile(r"/(delete|remove|unsubscribe|logout|signout|sign-out|destroy|purchase|checkout)\b",
                                re.I)


def matches_terms(name: str, extra: list[str]) -> str | None:
    lowered = name.lower()
    for term in [*CONSEQUENTIAL_TERMS, *extra]:
        if re.search(rf"(?<![a-z]){re.escape(term.lower())}(?![a-z])", lowered):
            return term
    return None


def origin_of(url: str | None) -> str | None:
    if not url:
        return None
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}".lower()


def is_private_host(host: str) -> bool:
    if host in {"localhost"} or host.endswith(".local") or host.endswith(".internal"):
        return True
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved


def origin_matches(origin: str | None, pattern: str) -> bool:
    if not origin:
        return False
    regex = "^" + re.escape(pattern.lower()).replace(r"\*", "[^/]*") + "$"
    return re.match(regex, origin.lower()) is not None


@dataclass
class Classification:
    category: ActionCategory
    reason: str
    destination: str | None = None
    consequence: str | None = None
    element: ElementRef | None = None


@dataclass
class PolicyContext:
    config: ApplicationConfig
    observation: Observation | None
    hit_test: ElementRef | None = None
    attachment_private_origins: list[str] | None = None
    task_sources: list[str] | None = None


def _element(ctx: PolicyContext, ref: str) -> ElementRef | None:
    if ctx.observation is None:
        return None
    return next((e for e in ctx.observation.elements if e.ref == ref), None)


def classify_element_activation(el: ElementRef, extra_terms: list[str]) -> Classification:
    role = el.role.lower()
    name = el.name or ""
    term = matches_terms(name, extra_terms)
    label = f"{role} '{name[:80]}'"
    if el.disabled:
        return Classification(C.READ, f"{label} is disabled; activation is a no-op", element=el)
    if role == "link" and el.href and not el.href.lower().startswith("javascript:"):
        if term or CONSEQUENTIAL_PATH.search(urlsplit(el.href).path or ""):
            return Classification(C.CONSEQUENTIAL, f"link {label} looks consequential ('{term or 'path'}')",
                                  destination=el.href, consequence=f"Opens {el.href}, which may change account state",
                                  element=el)
        return Classification(C.NAVIGATION, f"navigation via {label}", destination=el.href, element=el)
    if role in {"tab", "treeitem"} or el.tag == "summary":
        if term:
            return Classification(C.UNKNOWN_MUTATION, f"{label} mentions '{term}'", element=el)
        return Classification(C.NAVIGATION, f"view change via {label}", element=el)
    if role in {"button", "menuitem"} or el.input_type in {"submit", "button", "image", "reset"}:
        # a submit-type control only submits when it belongs to a form
        submits = el.in_form and el.input_type in (None, "", "submit", "image")
        if term or submits:
            what = "submits a form" if submits else f"matches '{term}'"
            return Classification(C.CONSEQUENTIAL, f"{label} {what}", element=el,
                                  consequence=f"Activates {label}, which {what}")
        if el.expanded is not None and not el.in_form:
            return Classification(C.NAVIGATION, f"disclosure control {label}", element=el)
        return Classification(C.UNKNOWN_MUTATION, f"{label} has an unclear consequence", element=el,
                              consequence=f"Activates {label}; effect unknown")
    if role in FORM_STATE_ROLES or el.editable:
        return Classification(C.DRAFT, f"changes local form state of {label}", element=el)
    if el.tag == "canvas":
        return Classification(C.UNKNOWN_MUTATION, "canvas interaction has no structured target", element=el,
                              consequence="Clicks inside a canvas; effect unknown")
    if role == "switch":
        return Classification(C.UNKNOWN_MUTATION, f"{label} toggles a setting", element=el)
    return Classification(C.UNKNOWN_MUTATION, f"activation of {label} has an unclear consequence", element=el,
                          consequence=f"Activates {label}; effect unknown")


def classify_navigation(url: str, ctx: PolicyContext) -> Classification:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        return Classification(C.FORBIDDEN, f"scheme {parts.scheme or '(none)'} is not allowed", destination=url)
    host = (parts.hostname or "").lower()
    if not host:
        return Classification(C.FORBIDDEN, "URL has no host", destination=url)
    for blocked in ctx.config.search.block_domains:
        if host == blocked or host.endswith("." + blocked):
            return Classification(C.FORBIDDEN, f"domain {host} is blocked", destination=url)
    if is_private_host(host):
        origin = origin_of(url)
        host_ok = host in {h.lower() for h in ctx.config.search.allowed_private_hosts}
        if not host_ok and not any(origin_matches(origin, p) for p in (ctx.attachment_private_origins or [])):
            return Classification(C.FORBIDDEN, f"private network host {host} is not an allowed origin", destination=url)
    if CONSEQUENTIAL_PATH.search(parts.path or ""):
        return Classification(C.UNKNOWN_MUTATION, "URL path looks like a state-changing endpoint", destination=url,
                              consequence=f"Opens {url}, which may change account state")
    return Classification(C.NAVIGATION, "navigation to http(s) URL", destination=url)


def classify_press(key: str, ctx: PolicyContext) -> Classification:
    focused = _element(ctx, ctx.observation.focused_ref) if ctx.observation and ctx.observation.focused_ref else None
    norm = key.strip()
    if norm in NAV_KEYS:
        return Classification(C.NAVIGATION, f"navigation key {norm}")
    if norm in SAFE_COMBOS:
        return Classification(SAFE_COMBOS[norm], f"safe shortcut {norm}")
    if norm in SUBMIT_KEYS:
        if focused is not None and norm in {"Enter", "NumpadEnter"}:
            if focused.role == "link" and focused.href:
                return classify_element_activation(focused, ctx.config.policy.extra_consequential_terms)
            if focused.role in {"button", "menuitem"}:
                return classify_element_activation(focused, ctx.config.policy.extra_consequential_terms)
        target = f" in {focused.role} '{focused.name[:60]}'" if focused else ""
        return Classification(C.CONSEQUENTIAL, f"key {norm}{target} can submit or commit", element=focused,
                              consequence=f"Presses {norm}{target}; may submit a form or send content")
    if norm in {" ", "Space"} and focused is not None and focused.role in {"button", "link", "menuitem", "checkbox"}:
        return classify_element_activation(focused, ctx.config.policy.extra_consequential_terms)
    if norm in {"Backspace", "Delete"} or len(norm) == 1:
        if focused is not None and (focused.editable or focused.role in EDITABLE_ROLES):
            if focused.input_type == "password" or SENSITIVE_INPUT.search(focused.name or ""):
                return Classification(C.FORBIDDEN, "keyboard input into a credential field", element=focused)
            return Classification(C.DRAFT, f"edits text in {focused.role}", element=focused)
        return Classification(C.UNKNOWN_MUTATION, f"key '{norm}' outside a text field may trigger an app shortcut",
                              element=focused, consequence=f"Presses '{norm}' as a page shortcut; effect unknown")
    return Classification(C.UNKNOWN_MUTATION, f"keyboard shortcut {norm} has an unclear consequence", element=focused,
                          consequence=f"Presses {norm}; effect unknown")


def _recipient_check(recipients: list[str], ctx: PolicyContext, kind: str) -> Classification | None:
    known = {c.address.lower() for c in ctx.config.policy.contacts if c.kind == kind}
    unknown = [r for r in recipients if r.lower() not in known]
    if unknown:
        return Classification(C.FORBIDDEN, f"unexpected recipient(s): {', '.join(unknown)}",
                              destination=", ".join(recipients))
    return None


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

Classifier = Callable[[BaseModel, dict[str, Any], PolicyContext], Classification]


@dataclass(frozen=True)
class OperationSpec:
    name: str
    args: type[BaseModel]
    executor: Literal["coordinator", "worker"]
    classify: Classifier
    capability: str | None = None
    needs_ui_lease: bool = False
    retry_safe: bool = False
    description: str = ""


def _static(cat: ActionCategory, reason: str) -> Classifier:
    def f(args: BaseModel, target: dict[str, Any], ctx: PolicyContext) -> Classification:
        dest = getattr(args, "url", None) or target.get("url")
        return Classification(cat, reason, destination=dest)

    return f


def _click(args: BaseModel, target: dict[str, Any], ctx: PolicyContext) -> Classification:
    assert isinstance(args, ClickArgs)
    el = _element(ctx, args.ref)
    if el is None:
        return Classification(C.FORBIDDEN, f"element ref {args.ref} is not in the current observation")
    c = classify_element_activation(el, ctx.config.policy.extra_consequential_terms)
    c.destination = c.destination or (ctx.observation.url if ctx.observation else None)
    return c


def _click_point(args: BaseModel, target: dict[str, Any], ctx: PolicyContext) -> Classification:
    el = ctx.hit_test
    if el is None:
        return Classification(C.UNKNOWN_MUTATION, "no element identified at the click point",
                              destination=ctx.observation.url if ctx.observation else None,
                              consequence="Clicks at a screen coordinate with no identified control")
    c = classify_element_activation(el, ctx.config.policy.extra_consequential_terms)
    if c.category == C.NAVIGATION and el.tag == "canvas":
        c.category = C.UNKNOWN_MUTATION
    c.destination = c.destination or (ctx.observation.url if ctx.observation else None)
    return c


def _type(args: BaseModel, target: dict[str, Any], ctx: PolicyContext) -> Classification:
    assert isinstance(args, TypeArgs)
    el = _element(ctx, args.ref)
    if el is None:
        return Classification(C.FORBIDDEN, f"element ref {args.ref} is not in the current observation")
    if el.input_type == "password" or SENSITIVE_INPUT.search(el.name or ""):
        return Classification(C.FORBIDDEN, "typing into credential or payment fields is a human step", element=el)
    if not (el.editable or el.role in EDITABLE_ROLES):
        return Classification(C.UNKNOWN_MUTATION, f"{el.role} '{el.name[:60]}' is not a text field", element=el)
    if args.submit:
        return Classification(C.CONSEQUENTIAL, f"types into '{el.name[:60]}' and submits", element=el,
                              destination=ctx.observation.url if ctx.observation else None,
                              consequence="Submits the form containing this field")
    return Classification(C.DRAFT, f"edits text field '{el.name[:60]}'", element=el)


def _select(args: BaseModel, target: dict[str, Any], ctx: PolicyContext) -> Classification:
    assert isinstance(args, SelectArgs)
    el = _element(ctx, args.ref)
    if el is None:
        return Classification(C.FORBIDDEN, f"element ref {args.ref} is not in the current observation")
    return Classification(C.DRAFT, f"selects an option in '{el.name[:60]}'", element=el)


def _press(args: BaseModel, target: dict[str, Any], ctx: PolicyContext) -> Classification:
    assert isinstance(args, PressArgs)
    c = classify_press(args.key, ctx)
    c.destination = c.destination or (ctx.observation.url if ctx.observation else None)
    return c


def _navigate(args: BaseModel, target: dict[str, Any], ctx: PolicyContext) -> Classification:
    return classify_navigation(args.url, ctx)  # type: ignore[attr-defined]


def _retrieve(args: BaseModel, target: dict[str, Any], ctx: PolicyContext) -> Classification:
    c = classify_navigation(args.url, ctx)  # type: ignore[attr-defined]
    if c.category in (C.NAVIGATION, C.UNKNOWN_MUTATION):
        # API retrieval issues a GET without the owner's cookies
        c.category = C.READ
    return c


def _fetch_image(args: BaseModel, target: dict[str, Any], ctx: PolicyContext) -> Classification:
    src = args.src  # type: ignore[attr-defined]
    if src.startswith("data:image/"):
        return Classification(C.READ, "inline image data")
    c = classify_navigation(src, ctx)
    if c.category != C.FORBIDDEN:
        c.category = C.READ
    return c


def _file_write(args: BaseModel, target: dict[str, Any], ctx: PolicyContext) -> Classification:
    assert isinstance(args, FileWriteArgs)
    if args.overwrite or target.get("exists"):
        return Classification(C.CONSEQUENTIAL, "overwrites an existing file", destination=args.rel_path,
                              consequence=f"Replaces {args.rel_path}")
    return Classification(C.CREATE_OUTPUT, "new file in the dedicated output folder", destination=args.rel_path)


def _message_send(args: BaseModel, target: dict[str, Any], ctx: PolicyContext) -> Classification:
    assert isinstance(args, MessageSendArgs)
    bad = _recipient_check([args.recipient], ctx, "telegram")
    if bad:
        return bad
    return Classification(C.CONSEQUENTIAL, "sends a message to another person", destination=args.recipient,
                          consequence=f"Sends a Telegram message to {args.recipient}")


def _email_draft(args: BaseModel, target: dict[str, Any], ctx: PolicyContext) -> Classification:
    return Classification(C.DRAFT, "creates a local email preview", destination=", ".join(args.to))  # type: ignore


def _email_send(args: BaseModel, target: dict[str, Any], ctx: PolicyContext) -> Classification:
    assert isinstance(args, EmailSendArgs)
    bad = _recipient_check(args.to, ctx, "email")
    if bad:
        return bad
    return Classification(C.CONSEQUENTIAL, "sends an email", destination=", ".join(args.to),
                          consequence=f"Sends email to {', '.join(args.to)}")


def _calendar_invite(args: BaseModel, target: dict[str, Any], ctx: PolicyContext) -> Classification:
    assert isinstance(args, CalendarInviteArgs)
    bad = _recipient_check(args.attendees, ctx, "email")
    if bad:
        return bad
    return Classification(C.CONSEQUENTIAL, "sends calendar invitations", destination=", ".join(args.attendees),
                          consequence=f"Invites {', '.join(args.attendees)}")


def _sheet_write(args: BaseModel, target: dict[str, Any], ctx: PolicyContext) -> Classification:
    """Always a new copy; replacing the owner's original is a separate, approved ``file.write``."""
    assert isinstance(args, SpreadsheetWriteArgs)
    return Classification(C.CREATE_OUTPUT, "writes a new workbook copy", destination=args.output_name)


class BrowserAgentTaskArgs(_A):
    goal: str = Field(max_length=8000)
    start_url: str | None = Field(default=None, max_length=2048)
    max_steps: int | None = Field(default=None, ge=1, le=200)


def _browser_agent_task(args: BaseModel, target: dict[str, Any], ctx: PolicyContext) -> Classification:
    """One autonomous Browser Use run: its individual clicks are not seen by this gate."""
    assert isinstance(args, BrowserAgentTaskArgs)
    if ctx.config.browser.agent.require_approval:
        return Classification(C.CONSEQUENTIAL, "autonomous Browser Use run", destination=args.start_url,
                              consequence=f"Lets the Browser Use agent operate the authorized browser on its own "
                                          f"(clicks, typing, form submissions) to: {args.goal[:300]}")
    return Classification(C.NAVIGATION, "autonomous Browser Use run; the owner disabled approval "
                                        "(browser.agent.require_approval: false)", destination=args.start_url)


OPERATIONS: dict[str, OperationSpec] = {}


def _reg(spec: OperationSpec) -> None:
    OPERATIONS[spec.name] = spec


# research (coordinator)
_reg(OperationSpec("search.query", SearchQueryArgs, "coordinator", _static(C.READ, "search API query"),
                   retry_safe=True, description="Search through the configured search adapter"))
_reg(OperationSpec("web.fetch_image", FetchImageArgs, "coordinator", _fetch_image, retry_safe=True,
                   description="Fetch a public image without the owner's cookies"))
_reg(OperationSpec("web.retrieve", RetrieveArgs, "coordinator", _retrieve, retry_safe=True,
                   description="Retrieve a page through the configured retrieval adapter"))
# browser (worker, UI lease)
for _name, _args, _cls, _desc in [
    ("browser.observe", ObserveArgs, _static(C.READ, "observation"), "Screenshot, element references and text"),
    ("browser.read_page", ReadPageArgs, _static(C.READ, "read page"), "Visible text and controls of the current page"),
    ("browser.hit_test", HitTestArgs, _static(C.READ, "hit test"), "Identify the element at a viewport point"),
    ("browser.scroll", ScrollArgs, _static(C.NAVIGATION, "scroll"), "Scroll the page"),
    ("browser.wait", WaitArgs, _static(C.READ, "wait"), "Wait for time or text"),
    ("browser.back", EmptyArgs, _static(C.NAVIGATION, "history back"), "Go back"),
    ("browser.release", EmptyArgs, _static(C.READ, "close the task's tab"), "Close the task's page"),
    ("browser.navigate", NavigateArgs, _navigate, "Open a URL"),
    ("browser.click", ClickArgs, _click, "Activate an element by reference"),
    ("browser.click_point", ClickPointArgs, _click_point, "Vision-guided coordinate click (fallback)"),
    ("browser.type", TypeArgs, _type, "Type into a text field"),
    ("browser.press", PressArgs, _press, "Press a key or shortcut"),
    ("browser.select", SelectArgs, _select, "Choose an option"),
    ("browser.fetch_image", FetchImageArgs, _fetch_image, "Fetch an original page image with session cookies"),
    ("browser.download", DownloadArgs, _static(C.READ, "download into the task workspace"), "Download a file"),
    ("browser.video_frames", VideoFramesArgs, _static(C.READ, "capture frames of authorized playback"),
     "Seek a page video and capture decoded frames"),
]:
    _reg(OperationSpec(_name, _args, "worker", _cls, capability="browser", needs_ui_lease=True,
                       retry_safe=_name in {"browser.observe", "browser.read_page", "browser.hit_test", "browser.wait",
                                            "browser.scroll", "browser.fetch_image", "browser.video_frames"},
                       description=_desc))
_reg(OperationSpec("browser.agent_task", BrowserAgentTaskArgs, "worker", _browser_agent_task, capability="browser",
                   needs_ui_lease=True, description="Run the Browser Use agent on a goal in the authorized browser"))
# media (worker, no UI lease)
_reg(OperationSpec("media.fetch", MediaFetchArgs, "worker", _retrieve, capability="video",
                   retry_safe=True, description="Download accessible media with yt-dlp"))
# files and delivery (coordinator)
_reg(OperationSpec("file.write", FileWriteArgs, "coordinator", _file_write, retry_safe=True,
                   description="Save an artifact to the reports folder (idempotent per action)"))
_reg(OperationSpec("file.delete", FileDeleteArgs, "coordinator",
                   lambda a, t, c: Classification(C.CONSEQUENTIAL, "deletes a file", destination=a.rel_path,  # type: ignore
                                                  consequence=f"Deletes {a.rel_path}"),  # type: ignore
                   description="Delete a file"))
_reg(OperationSpec("telegram.send_owner", SendOwnerArgs, "coordinator",
                   _static(C.READ, "delivery to the owner's own DM"), retry_safe=True,
                   description="Send a result to the owner (outbox-deduplicated per action)"))
_reg(OperationSpec("message.send", MessageSendArgs, "coordinator", _message_send, description="Message another person"))
# office adapters (worker)
_reg(OperationSpec("email.draft", EmailDraftArgs, "worker", _email_draft, capability="email",
                   description="Create a local .eml preview"))
_reg(OperationSpec("email.send", EmailSendArgs, "worker", _email_send, capability="email", description="Send an email"))
_reg(OperationSpec("calendar.draft", CalendarDraftArgs, "worker",
                   _static(C.DRAFT, "creates a local .ics preview"), capability="calendar",
                   description="Create a local calendar preview"))
_reg(OperationSpec("calendar.invite", CalendarInviteArgs, "worker", _calendar_invite, capability="calendar",
                   description="Send calendar invitations"))
_reg(OperationSpec("spreadsheet.read", SpreadsheetReadArgs, "worker", _static(C.READ, "reads a workbook"),
                   capability="spreadsheet", retry_safe=True, description="Read workbook cells"))
_reg(OperationSpec("spreadsheet.write", SpreadsheetWriteArgs, "worker", _sheet_write, capability="spreadsheet",
                   description="Write cells to a new workbook copy"))
_reg(OperationSpec("desktop.raw_input", DesktopRawArgs, "worker",
                   lambda a, t, c: Classification(C.UNKNOWN_MUTATION, "raw desktop input outside a reviewed adapter",
                                                  consequence=f"Raw {a.action} on the desktop"),  # type: ignore
                   capability="desktop", needs_ui_lease=True, description="Raw desktop input (blocked by default)"))
_reg(OperationSpec("worker.reconcile", ReconcileArgs, "worker", _static(C.READ, "reconciliation query"),
                   retry_safe=True, description="Ask a worker whether an action already executed"))
_reg(OperationSpec("permission.change", PermissionChangeArgs, "coordinator",
                   lambda a, t, c: Classification(C.CONSEQUENTIAL, "changes permissions", destination=a.target,  # type: ignore
                                                  consequence=f"Changes permissions on {a.target}: {a.change}"),  # type: ignore
                   description="Change a permission"))
_reg(OperationSpec("credential.export", CredentialExportArgs, "coordinator",
                   lambda a, t, c: Classification(C.FORBIDDEN, "credential export is never allowed"),
                   description="Always denied"))
