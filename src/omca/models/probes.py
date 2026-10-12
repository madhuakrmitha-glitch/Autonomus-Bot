"""Route capability probes (spec section 13, WP02).

A route serves application roles only after these pass for its current configuration
digest. Probe calls are real metered calls through the router.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from typing import Any

from PIL import Image, ImageDraw, ImageFont, PngImagePlugin

from ..config import ModelRoute
from .registry import ModelRegistry, required_probes
from .router import ModelCallError, ModelRouter
from .types import ImageInput, extract_json

PROBE_ORDER_NUMBER = 4721
PROBE_BUTTON_COLOR = "red"


@dataclass
class ProbeResult:
    probe: str
    passed: bool
    detail: dict[str, Any] = field(default_factory=dict)


def _font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # pragma: no cover - very old Pillow
        return ImageFont.load_default()


def probe_screenshot() -> bytes:
    """A synthetic web-page screenshot with a readable order number and a coloured button."""
    im = Image.new("RGB", (1280, 800), "white")
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, 1280, 70], fill=(30, 40, 60))
    d.text((24, 18), "Example Shop - Checkout", fill="white", font=_font(30))
    d.text((80, 160), f"Order {PROBE_ORDER_NUMBER}", fill="black", font=_font(64))
    d.text((80, 260), "Two items - delivery Friday", fill=(60, 60, 60), font=_font(32))
    d.rounded_rectangle([80, 380, 460, 470], radius=12, fill=(220, 30, 30))
    d.text((150, 402), "Continue", fill="white", font=_font(40))
    meta = PngImagePlugin.PngInfo()
    meta.add_text("omca-probe", json.dumps({"order_number": PROBE_ORDER_NUMBER, "button_color": PROBE_BUTTON_COLOR}))
    buf = io.BytesIO()
    im.save(buf, "PNG", pnginfo=meta)
    return buf.getvalue()


REPORT_TOOL = {
    "type": "function",
    "function": {
        "name": "report_finding",
        "description": "Report what the screenshot shows.",
        "parameters": {
            "type": "object",
            "properties": {
                "order_number": {"type": "integer", "description": "Order number visible on the page"},
                "button_color": {"type": "string", "description": "Colour of the main button"},
            },
            "required": ["order_number", "button_color"],
        },
    },
}

WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get current weather for a city.",
        "parameters": {
            "type": "object",
            "properties": {
                "city": {"type": "string"},
                "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]},
            },
            "required": ["city", "unit"],
        },
    },
}


async def run_probes(router: ModelRouter, registry: ModelRegistry, route_id: str) -> list[ProbeResult]:
    route = registry.get(route_id)
    results: list[ProbeResult] = []
    usages: list[Any] = []

    async def call(messages, **kw):
        resp = await router.call_route(route_id, messages, allow_unprobed=True, purpose="probe", caller="probe", **kw)
        usages.append(resp.usage)
        return resp

    async def probe(name: str, fn) -> None:
        try:
            passed, detail = await fn()
        except ModelCallError as exc:
            passed, detail = False, {"error": str(exc), "code": str(exc.code)}
        except Exception as exc:  # noqa: BLE001 - every failure is a failed probe
            passed, detail = False, {"error": f"{type(exc).__name__}: {exc}"}
        results.append(ProbeResult(name, passed, detail))

    async def p_text():
        r = await call([{"role": "user", "content": "Reply with exactly the single word PONG and nothing else."}],
                       max_output_tokens=64)
        return "PONG" in r.text.upper(), {"text": r.text[:200]}

    async def p_screenshot():
        img = ImageInput(probe_screenshot(), "image/png", label="Screenshot:")
        r = await call([{"role": "user", "content": (
            "Look at the screenshot. Answer only with JSON of the form "
            '{"order_number": <integer>, "button_color": "<colour name>"}.')}], images=[img], max_output_tokens=256)
        data = extract_json(r.text)
        ok = int(data.get("order_number", -1)) == PROBE_ORDER_NUMBER and PROBE_BUTTON_COLOR in str(
            data.get("button_color", "")).lower()
        return ok, {"answer": data, "image_transforms": [t.__dict__ for t in r.image_transforms]}

    async def p_image_tools():
        img = ImageInput(probe_screenshot(), "image/png")
        r = await call([{"role": "user", "content": "Inspect the screenshot and call report_finding with what you see."}],
                       images=[img], tools=[REPORT_TOOL], tool_choice="auto", max_output_tokens=256)
        tc = next((t for t in r.tool_calls if t.name == "report_finding"), None)
        ok = bool(tc and tc.arguments and int(tc.arguments.get("order_number", -1)) == PROBE_ORDER_NUMBER
                  and PROBE_BUTTON_COLOR in str(tc.arguments.get("button_color", "")).lower())
        return ok, {"tool_call": tc.arguments if tc else None, "text": r.text[:200]}

    async def p_structured():
        r = await call([{"role": "user", "content": "Use the get_weather tool for Pune in celsius."}],
                       tools=[WEATHER_TOOL], tool_choice="auto", max_output_tokens=128)
        tc = next((t for t in r.tool_calls if t.name == "get_weather"), None)
        ok = bool(tc and tc.parse_error is None and tc.arguments
                  and str(tc.arguments.get("city", "")).lower() == "pune" and tc.arguments.get("unit") == "celsius")
        return ok, {"tool_call": tc.raw_arguments if tc else None, "parse_error": tc.parse_error if tc else "no call"}

    async def p_continuation():
        messages = [
            {"role": "user", "content": "Use the get_weather tool for Pune in celsius, then tell me the temperature."},
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "call_probe_1", "type": "function",
                "function": {"name": "get_weather", "arguments": json.dumps({"city": "Pune", "unit": "celsius"})}}]},
            {"role": "tool", "tool_call_id": "call_probe_1", "content": json.dumps({"temperature_c": 31, "sky": "clear"})},
        ]
        r = await call(messages, tools=[WEATHER_TOOL], max_output_tokens=128)
        return "31" in r.text, {"text": r.text[:200]}

    async def p_bounded():
        r = await call([{"role": "user", "content": "Write a long essay about the history of printing."}],
                       max_output_tokens=16)
        out = (r.usage or {}).get("completion_tokens")
        ok = (out is not None and out <= 18) or (out is None and len(r.text) <= 16 * 8)
        return ok, {"completion_tokens": out, "finish_reason": r.finish_reason}

    async def p_usage():
        reported = [u for u in usages if u and u.get("prompt_tokens") is not None and u.get("completion_tokens") is not None]
        if reported:
            return True, {"reported_calls": len(reported), "of": len(usages)}
        return (not route.usage_reporting), {"note": "provider omitted usage; conservative reservations are kept",
                                             "usage_reporting": route.usage_reporting}

    async def p_disabled():
        disabled = route.model_copy(update={"enabled": False})
        state = registry.state(disabled)
        before = len(getattr(router.provider(route), "requests", []))
        cfg_routes = router.config.models.routes
        idx = cfg_routes.index(route)
        cfg_routes[idx] = disabled
        try:
            try:
                await router.call_route(route_id, [{"role": "user", "content": "ping"}], max_output_tokens=8)
                rejected = False
            except ModelCallError:
                rejected = True
        finally:
            cfg_routes[idx] = route
        no_network = len(getattr(router.provider(route), "requests", [])) == before
        return (not state.usable) and rejected and no_network, {"reasons": state.reasons}

    await probe("text", p_text)
    if route.supports("image"):
        await probe("screenshot_interpretation", p_screenshot)
        if route.supports_tools:
            await probe("image_with_tools", p_image_tools)
    if route.supports_tools:
        await probe("structured_tool_call", p_structured)
        await probe("tool_result_continuation", p_continuation)
    await probe("bounded_output", p_bounded)
    await probe("usage_reporting", p_usage)
    await probe("disabled_route_rejection", p_disabled)
    names = {r.probe for r in results}
    for missing in required_probes(route):
        if missing not in names:
            results.append(ProbeResult(missing, False, {"error": "route lacks the capability (tools or images)"}))
    for r in results:
        registry.record_probe(route, r.probe, r.passed, r.detail)
    return results


def probe_ok(results: list[ProbeResult], route: ModelRoute) -> bool:
    by = {r.probe: r.passed for r in results}
    return all(by.get(p, False) for p in required_probes(route))
