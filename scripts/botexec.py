#!/usr/bin/env python3
"""Dev helper: run python inside a bot's agent. botexec.py Void1 'print(m.player_position())'"""
import socket, sys
s = socket.socket(socket.AF_UNIX); s.connect(f"{__import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__)))}/run/{sys.argv[1]}.sock")
s.sendall((sys.argv[2] if len(sys.argv) > 2 else sys.stdin.read()).encode()); s.shutdown(socket.SHUT_WR)
out = b""
while (d := s.recv(65536)): out += d
print(out.decode(), end="")
