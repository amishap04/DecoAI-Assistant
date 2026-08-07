"""Shared connection settings for session_server.py and qnn_runner.py.

Kept in one place so the client and server can never drift apart on
host/port/authkey (they previously duplicated these constants separately).
"""

SERVER_HOST = "127.0.0.1"
SERVER_PORT = 50002
SERVER_AUTHKEY = b"sd21-ort-server"
