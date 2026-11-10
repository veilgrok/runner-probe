"""Remote UDP echo used for runner-to-box RTT, same pattern as the lab adapter_echo.py."""
import os
import socket
import sys
import threading

echo = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
echo.bind(("127.0.0.1", 28701))


def serve():
    while True:
        data, addr = echo.recvfrom(65535)
        echo.sendto(data, addr)


threading.Thread(target=serve, daemon=True).start()
sys.argv = ["dgram_stdio.py", "18700", "28701"]
here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dgram_stdio.py")
# On the box the script is uploaded next to this file.
if not os.path.exists(here):
    here = "/home/boxuser/veil-lab/gha/dgram_stdio.py"
exec(compile(open(here, "r", encoding="utf-8").read(), "dgram_stdio.py", "exec"))
