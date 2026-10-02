#!/usr/bin/env python3
"""Which panic a board that reboots is forgiven, and how its boots are told
apart (ADR 0114).

The T-Deck boot test lets the board reboot, as silicon does after a panic (ADR
0099). That is safe only while these helpers keep the one panic it may reboot
from apart from the FPU regression it exists to catch (ADR 0013), so they are
checked here, in milliseconds, on the text the firmware prints.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.support import (  # noqa: E402
    Run, by_boot, last_boot, panics, unexplained_resets, unforgiven,
)

BANNER = "ESP-ROM:esp32s3-20210327\nBuild:Mar 27 2021\nrst:{cause}\n"
ROM = BANNER + "SPIWP:0xee\nentry 0x403c98d4\n"
POWER_ON = ROM.format(cause="0x1 (POWERON),boot:0x4 (SPI_FLASH_BOOT)")
SW_RESET = ROM.format(cause="0xc (RTC_SW_CPU_RST),boot:0x4 (SPI_FLASH_BOOT)")
# A reset that landed again while the ROM printed its banner.
SW_RESET_CUT_SHORT = BANNER.format(cause="0xc (RTC_SW_CPU_RST),boot:0x4 (SPI_FLASH_BOOT)")

# As the vendor firmware prints them, trailing spaces and all.
IPC1 = ("Guru Meditation Error: Core  1 panic'ed (Unhandled debug exception). \n"
        "Debug exception reason: Stack canary watchpoint triggered (ipc1) \n"
        "Core  1 register dump:\nPC      : 0x40382773  PS      : 0x00060036\n"
        "\nRebooting...\n")
COPROCESSOR = ("Guru Meditation Error: Core  0 panic'ed (Coprocessor exception). \n"
               "\nCore  0 register dump:\nEXCCAUSE: 0x00000004\n\nRebooting...\n")
LOOP_TASK = IPC1.replace("(ipc1)", "(loopTask)")
IPC0 = IPC1.replace("Core  1", "Core  0").replace("(ipc1)", "(ipc0)")
# What 250620 prints after the canary, before it resets: its core dump faults.
CORE_DUMP_FAULT = (
    "Guru Meditation Error: Core  1 panic'ed (LoadProhibited). Exception was unhandled.\n"
    "\nCore  1 register dump:\nEXCCAUSE: 0x0000001c  EXCVADDR: 0x80382139\n"
    "\nRe-entered core dump! Exception happened during core dump!\nRebooting...\n")
IPC1_WITH_ITS_CORE_DUMP = IPC1.replace("Rebooting...\n", "") + CORE_DUMP_FAULT


def test_the_ipc1_canary_is_forgiven() -> None:
    assert len(panics(IPC1)) == 1
    assert unforgiven(IPC1) == []


def test_the_fpu_regression_is_not() -> None:
    """ADR 0013's panic, which the boot test exists to catch."""
    assert unforgiven(COPROCESSOR) == panics(COPROCESSOR) != []


def test_a_canary_on_another_stack_is_not() -> None:
    """The canary is forgiven for the one stack ADR 0099 measured, not for any
    task that overflows."""
    assert unforgiven(LOOP_TASK) != []
    assert unforgiven(IPC0) != []


def test_a_panic_cut_off_before_its_reason_is_not() -> None:
    """A run stopped mid-panic leaves no reason to forgive it by."""
    assert unforgiven(IPC1.split("\n", 1)[0]) != []


def test_the_canarys_own_core_dump_failing_is_part_of_it() -> None:
    """Seen under load: the panic handler faults writing the canary's core dump
    and panics again in the same boot. That boot ended for the canary."""
    assert len(panics(IPC1_WITH_ITS_CORE_DUMP)) == 2
    assert unforgiven(POWER_ON + IPC1_WITH_ITS_CORE_DUMP + SW_RESET) == []


def test_a_boot_is_judged_by_its_first_panic() -> None:
    """The second panic in a boot is forgiven only behind the canary, never in
    front of it."""
    uart = POWER_ON + CORE_DUMP_FAULT + IPC1 + SW_RESET
    assert unforgiven(uart) == panics(CORE_DUMP_FAULT)


def test_every_boot_is_judged() -> None:
    uart = POWER_ON + IPC1 + SW_RESET + COPROCESSOR
    assert unforgiven(uart) == panics(COPROCESSOR)


def test_a_reboot_from_the_canary_is_accounted_for() -> None:
    uart = POWER_ON + IPC1_WITH_ITS_CORE_DUMP + SW_RESET + "ADC_CALI\n"
    assert unexplained_resets(uart) == []


def test_a_reset_with_no_panic_is_not() -> None:
    """A watchdog, an abort, a reset with nothing printed: before the board
    could reboot, any of them ended the run."""
    uart = POWER_ON + "Start Radio success!\n" + SW_RESET + "ADC_CALI\n"
    assert unexplained_resets(uart) == [1]
    uart = POWER_ON + COPROCESSOR + SW_RESET
    assert unexplained_resets(uart) == [1]


def test_a_reset_delivered_twice_is_not_one_reboot() -> None:
    """Seen under load before ADR 0123: the canary's reset, a banner, and at
    once another. It was QEMU letting the stalled core 0 run its ROM, and it is
    fixed, so the boot of nothing but the banner is a reset to account for."""
    uart = (POWER_ON + IPC1_WITH_ITS_CORE_DUMP + SW_RESET_CUT_SHORT + SW_RESET
            + "ADC_CALI\n")
    assert len(by_boot(uart)) == 3
    assert unexplained_resets(uart) == [2]
    assert unexplained_resets(SW_RESET_CUT_SHORT + SW_RESET) == [1]


def test_a_run_is_split_at_each_rom_banner() -> None:
    uart = POWER_ON + "Start Radio success!\n" + IPC1 + SW_RESET + "ADC_CALI\n"
    boots = by_boot(uart)
    assert len(boots) == 2 and "".join(boots) == uart
    assert IPC1 in boots[0]
    assert last_boot(uart).startswith("rst:0xc")
    assert "Start Radio" not in last_boot(uart) and "ADC_CALI" in last_boot(uart)
    assert Run(outdir=Path("."), uart=uart).boots == 2


def test_text_with_no_banner_is_one_boot() -> None:
    """A log the ROM never wrote to is all one boot, not none."""
    assert by_boot("Start Radio success!\n") == ["Start Radio success!\n"]
    assert last_boot("") == ""
