"""Probe oracle: answers the capability probes from the probe image's metadata (test-only)."""

from __future__ import annotations

import base64
import io
import json

from PIL import Image

from omca.models.providers import fixture_reply


def probe_oracle(route, body):
    msgs = body["messages"]
    last = msgs[-1]
    tools = body.get("tools")
    imgs = []
    for m in msgs:
        if isinstance(m.get("content"), list):
            for p in m["content"]:
                if p.get("type") == "image_url":
                    im = Image.open(io.BytesIO(base64.b64decode(p["image_url"]["url"].split(",", 1)[1])))
                    imgs.append(json.loads(im.info.get("omca-probe", "{}")))
    if last["role"] == "tool":
        return fixture_reply("It is 31 degrees in Pune.")
    if tools and imgs:
        return fixture_reply(tool_calls=[{"name": "report_finding", "arguments": imgs[0]}])
    if tools:
        return fixture_reply(tool_calls=[{"name": "get_weather", "arguments": {"city": "Pune", "unit": "celsius"}}])
    if imgs:
        return fixture_reply(json.dumps(imgs[0]))
    if body["max_tokens"] <= 16:
        return fixture_reply("Printing began with", completion_tokens=16, finish_reason="length")
    return fixture_reply("PONG")
