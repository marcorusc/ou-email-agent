"""Request the same background indexing job used by MCP and the monitor schedule."""
import json
from ou_email_agent.config import load_config
from ou_email_agent.indexing import start_indexing

if __name__ == '__main__':
    print(json.dumps(start_indexing(load_config()), indent=2))
