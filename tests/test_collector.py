"""Tests for collector parsers"""
import json
import tempfile

from kg_memory_mcp.collector.claude_code import parse_claude_code_session
from kg_memory_mcp.collector.codex import parse_codex_session
from kg_memory_mcp.collector.gemini import parse_gemini_session


def test_parse_claude_code_session():
    records = [
        {"type": "user", "timestamp": "2026-01-01T00:00:00Z", "cwd": "/tmp/project",
         "message": {"role": "user", "content": "Hello"}},
        {"type": "assistant", "timestamp": "2026-01-01T00:01:00Z",
         "message": {"role": "assistant", "content": "Hi there!", "model": "claude-sonnet-4-5-20250929"}},
    ]

    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
        f.flush()

        result = parse_claude_code_session(f.name)

    assert result is not None
    assert result["agent"] == "claude-code"
    assert result["project_dir"] == "/tmp/project"
    assert result["model"] == "claude-sonnet-4-5-20250929"
    assert len(result["messages"]) == 2


def test_parse_claude_code_empty():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write("")
        f.flush()
        result = parse_claude_code_session(f.name)
    assert result is None


def test_parse_codex_session():
    records = [
        {"type": "session_meta", "timestamp": "2026-01-01T00:00:00Z",
         "payload": {"id": "ses_123", "cwd": "/tmp"}},
        {"type": "response_item", "timestamp": "2026-01-01T00:01:00Z",
         "payload": {"role": "user", "content": [{"type": "input_text", "text": "Hello"}]}},
        {"type": "response_item", "timestamp": "2026-01-01T00:02:00Z",
         "payload": {"role": "assistant", "content": [{"type": "output_text", "text": "Hi!"}]}},
    ]

    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
        f.flush()
        result = parse_codex_session(f.name)

    assert result is not None
    assert result["agent"] == "codex"
    assert result["native_session_id"] == "ses_123"
    assert len(result["messages"]) == 2


def test_parse_gemini_session():
    data = {
        "sessionId": "session-abc",
        "projectHash": "deadbeef",
        "messages": [
            {"type": "user", "timestamp": "2026-01-01T00:00:00Z", "content": "Hello"},
            {"type": "gemini", "timestamp": "2026-01-01T00:01:00Z", "content": "Hi!"},
        ],
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(data, f)
        f.flush()
        result = parse_gemini_session(f.name)

    assert result is not None
    assert result["agent"] == "gemini-cli"
    assert result["native_session_id"] == "session-abc"
    assert len(result["messages"]) == 2
    assert result["messages"][1]["role"] == "assistant"
