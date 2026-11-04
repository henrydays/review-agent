#!/usr/bin/env python3
"""A tiny OpenAI-compatible server that answers review prompts with canned JSON.

For testing the plumbing around a model with no key and no cost: the container, the provider
configuration, the stages. It says nothing about review quality.

  fake_model_server.py [--port 8099] [--host 0.0.0.0]
"""
import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def answer(prompt: str) -> str:
    if "Your job is to find out whether the claim is false" in prompt:
        ids = re.findall(r'<claim id="(c\d+)"', prompt)
        return json.dumps({"verdicts": [{"id": i, "reasoning": "Checked against the code shown.", "verdict": "confirmed"} for i in ids]})
    if "Group those together" in prompt:
        return json.dumps({"groups": []})
    if "Reply with the word PONG" in prompt:
        return "PONG"
    match = re.search(r"=== (\S+) \(", prompt)
    line = re.search(r"^\s+(\d+) \+ ", prompt, re.M)
    if not match or not line:
        return json.dumps({"findings": []})
    focus = re.search(r"Focus of this pass: (.+)", prompt)
    return json.dumps({"findings": [{"file": match.group(1), "start_line": int(line.group(1)), "end_line": int(line.group(1)), "severity": "medium",
                                     "category": "correctness", "confidence": 0.7,
                                     "message": f"Canned finding from the fake model for the pass named {focus.group(1) if focus else 'unknown'}, so this exercises the pipeline around the model and nothing more."}]})


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"data":[{"id":"fake","object":"model"}],"object":"list"}')

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        messages = body.get("messages") or []
        parts = []
        for message in messages:
            content = message.get("content")
            parts.append(content if isinstance(content, str) else " ".join(p.get("text", "") for p in content or [] if isinstance(p, dict)))
        text = answer("\n".join(parts))
        base = {"id": "fake-1", "created": 0, "model": body.get("model", "fake")}
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunks = [{"object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}]},
                      {"object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}}]
            for chunk in chunks:
                self.wfile.write(f"data: {json.dumps({**base, **chunk})}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({**base, "object": "chat.completion", "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                                     "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}}).encode())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
