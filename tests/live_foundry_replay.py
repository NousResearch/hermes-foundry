"""Opt-in synthetic inference canary, run inside the release image.

Requires FOUNDRY_REPLAY_ENDPOINT and FOUNDRY_REPLAY_TOKEN. Makes model requests only;
tool results are synthetic. No directory, file, mail, permission or routine operations.
Never prints model content, credentials, or encrypted reasoning.
"""
import json
import os
import urllib.request

from agent.codex_responses_adapter import (
    _chat_messages_to_responses_input,
    _preflight_codex_input_items,
)


def call(items, **options):
    body = {
        "model": os.environ.get("FOUNDRY_REPLAY_MODEL", "gpt-5.6-terra"),
        "store": False,
        "reasoning": {"effort": "medium"},
        "include": ["reasoning.encrypted_content"],
        "input": items,
        **options,
    }
    request = urllib.request.Request(
        os.environ["FOUNDRY_REPLAY_ENDPOINT"].rstrip("/") + "/responses",
        data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + os.environ["FOUNDRY_REPLAY_TOKEN"],
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        return json.load(response)


def main():
    user = {"role": "user", "content":
        "What is the smallest positive integer divisible by every integer 1 through 20? Think carefully."}
    first = call([user])
    reasoning = [item for item in first["output"] if item["type"] == "reasoning"
                 and item.get("encrypted_content")]
    assert reasoning, "Test inconclusive: model did not produce encrypted reasoning"
    messages = [user, {"role": "assistant", "content": "",
        "codex_reasoning_items": reasoning,
        "codex_message_items": [x for x in first["output"] if x["type"] == "message"]},
        {"role": "user", "content": "Use the test_lookup tool once, then report its result."}]
    replay = _preflight_codex_input_items(_chat_messages_to_responses_input(messages))
    assert [x["id"] for x in replay if x.get("type") == "reasoning"] == [x["id"] for x in reasoning]
    tools = [{"type": "function", "name": "test_lookup",
              "parameters": {"type": "object", "properties": {}}}]
    second = call(replay, tools=tools, tool_choice={"type": "function", "name": "test_lookup"})
    calls = [x for x in second["output"] if x["type"] == "function_call"]
    assert calls, "No synthetic tool call returned"
    messages.append({"role": "assistant", "content": "",
        "codex_reasoning_items": [x for x in second["output"] if x["type"] == "reasoning"],
        "codex_message_items": [x for x in second["output"] if x["type"] == "message"],
        "tool_calls": [{"id": x["call_id"], "type": "function", "function": {
            "name": x["name"], "arguments": x["arguments"]}} for x in calls]})
    for item in calls:
        messages.append({"role": "tool", "tool_call_id": item["call_id"], "content": "Synthetic result: OK"})
    third = call(_preflight_codex_input_items(_chat_messages_to_responses_input(messages)),
                 tools=tools, tool_choice="none")
    assert any(part.get("text") for item in third["output"] if item["type"] == "message"
               for part in item.get("content", [])), "No final text after tools"
    print("PASS: release-image adapter retained reasoning IDs; live reasoning replay, tool continuation, and final text succeeded")


if __name__ == "__main__":
    main()
