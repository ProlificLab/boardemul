"""The SIMCom A7682E LTE modem on the T-Deck Max's second UART (ADR 0105).

An AT-command modem: a command is a line ending in a carriage return, echoed
back while echo is on (the modem's default), and answered with a result code
framed in CR LF. The factory demo powers it with the expander's PWRKEY line and
then asks "AT" until it hears OK -- five tries, a second each -- and gives up
with "[A7682E] Init Fail" if it never does. Then it sends AT+CLIP=1.

Only commands seen from a firmware are answered, and each is answered the way
the AT command set defines it. Anything else is answered ERROR and logged as
unmodelled, so the next command to model is read off a run rather than
guessed. There is no SIM card: that is a scenario input this model does not
take yet.
"""

from __future__ import annotations

import logging

from emu.serial_link import SerialDevice

log = logging.getLogger("modem")

OK = b"\r\nOK\r\n"
ERROR = b"\r\nERROR\r\n"


class A7682E(SerialDevice):
    def __init__(self) -> None:
        self.echo = True
        self.clip = False
        self._line = b""
        self.commands: list[str] = []
        self.unmodelled: list[str] = []

    def receive(self, data: bytes) -> bytes:
        answer = b""
        for byte in data:
            if byte == 0x0D:                       # the command is complete
                line, self._line = self._line, b""
                answer += (line + b"\r" if self.echo else b"") + self._command(line)
            elif byte == 0x0A:
                continue                           # a LF after the CR is not part of it
            else:
                self._line += bytes([byte])
        return answer

    def _command(self, raw: bytes) -> bytes:
        text = raw.decode("ascii", "replace").strip()
        if not text:
            return b""
        self.commands.append(text)
        upper = text.upper()
        if upper == "AT":
            return OK
        if upper in ("ATE0", "ATE1", "ATE"):
            self.echo = upper == "ATE1"
            return OK
        if upper.startswith("AT+CLIP="):
            value = upper.split("=", 1)[1]
            if value in ("0", "1"):
                self.clip = value == "1"
                return OK
            return ERROR
        self.unmodelled.append(text)
        log.info("modem: asked for %s, no model", text)
        return ERROR

    def summary(self) -> str:
        asked = len(self.commands)
        missing = ", ".join(sorted(set(self.unmodelled))) or "nothing"
        return f"A7682E: {asked} command(s), unmodelled: {missing}"
