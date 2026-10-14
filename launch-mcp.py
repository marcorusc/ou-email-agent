"""Absolute-path MCP entrypoint; independent of the host's working directory."""
import sys

from ou_email_agent.cli import entrypoint

if __name__ == '__main__':
    sys.argv = [sys.argv[0], 'mcp']
    entrypoint()
