#!/bin/sh
# Run the MCP server with its own virtualenv.
# The image PYTHONPATH points at the web app's dependencies; leaving that set
# would let those packages shadow the MCP server's own copies.
export PYTHONPATH=/app
exec /opt/mcp/bin/python -m mcp_server "$@"
