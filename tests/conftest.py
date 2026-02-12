"""Test fixtures for kg-memory-mcp"""
import os

# Use test database if available
os.environ.setdefault("KG_DB_NAME", "knowledge_base_test")
os.environ.setdefault("KG_DB_USER", "postgres")
