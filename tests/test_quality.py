"""Tests for quality module"""
from kg_memory_mcp.quality import contains_sensitive, filter_sensitive


def test_contains_sensitive_api_key():
    assert contains_sensitive("my api_key=sk-1234567890abcdefghij")
    assert contains_sensitive("secret_key: m0-abcdefghijklmnopqrstuvwxyz")


def test_contains_sensitive_password():
    assert contains_sensitive("password=mySecret123")
    assert contains_sensitive("PASSWORD: hunter2")


def test_contains_sensitive_bearer():
    assert contains_sensitive("Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abcdefg")


def test_contains_sensitive_github():
    assert contains_sensitive("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefgh")


def test_not_sensitive():
    assert not contains_sensitive("This is a normal observation")
    assert not contains_sensitive("The API endpoint is /v1/users")
    assert not contains_sensitive("Use password hashing with bcrypt")


def test_filter_sensitive():
    observations = [
        "Normal observation",
        "api_key=sk-1234567890abcdefghij",
        "Another normal observation",
    ]
    result = filter_sensitive(observations)
    assert len(result) == 2
    assert "Normal observation" in result
    assert "Another normal observation" in result
