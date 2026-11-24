# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""Board descriptions (ADR 0184): read strictly, and the described T-Deck is
the hand-written one, part by part."""

from pathlib import Path

import pytest

from emu import described
from emu.described import DescriptionError, parse

TDECK = Path(described.__file__).parent / "boards" / "tdeck.toml"

def refused(text: str) -> str:
    with pytest.raises(DescriptionError) as caught:
        parse(text, where="b.toml")
    return str(caught.value)


def test_the_tdeck_description_reads():
    desc = described.load(TDECK)
    assert desc.name == "T-Deck (described)"
    assert [p.part for p in desc.spi] == ["st7789", "sx1262", "sdcard"]
    assert desc.spi[1].pins == {"cs": 9, "busy": 13, "reset": 17, "irq": 45}
    assert [(p.part, p.address) for p in desc.i2c[0]] == [
        ("gt911", 0x5D), ("tdeck-keyboard", 0x55), ("es7210", 0x40)]
    assert desc.i2c[1] == []
    assert desc.uart[1].part == "l76k"
    assert desc.screen().part == "st7789"
    assert desc.source.startswith("# SPDX")


def test_a_minimal_board_is_a_name():
    desc = parse('name = "bare"')
    assert desc.parts() == [] and desc.screen() is None


@pytest.mark.parametrize("text, says", [
    ('chip = "esp32s3"', "`name` is required"),
    ('name = "x"\nchip = "esp32c3"', "esp32s3 only"),
    ('name = "x"\ncolour = 1', "'colour'"),
    ('name = "x"\n[[spi2.parts]]\npart = "ili9341"\ncs = 1', "not in the catalogue"),
    ('name = "x"\n[[spi2.parts]]\npart = "sdcard"', "needs `cs`"),
    ('name = "x"\n[[spi2.parts]]\npart = "sdcard"\ncs = 49', "from 0 to 48"),
    ('name = "x"\n[[spi2.parts]]\npart = "sdcard"\ncs = 4\nbusy = 5', "'busy'"),
    ('name = "x"\n[[spi2.parts]]\npart = "sdcard"\ncs = 4\n'
     '[[spi2.parts]]\npart = "sx1262"\ncs = 4', "GPIO 4 is both"),
    ('name = "x"\n[[spi2.parts]]\npart = "st7789"\ncs = 4', "needs spi2's `dc`"),
    ('name = "x"\n[[i2c0.parts]]\npart = "sdcard"\ncs = 4', "goes on spi2, not i2c"),
    ('name = "x"\n[[i2c1.parts]]\npart = "gt911"\n'
     '[[i2c1.parts]]\npart = "es7210"\naddress = 0x5d', "both at 0x5d"),
    ('name = "x"\n[[i2c0.parts]]\npart = "es7210"\naddress = 0x80', "7-bit"),
    ('name = "x"\n[[spi2.parts]]\npart = "sdcard"\ncs = 4\nsize_mb = 0', "positive int"),
    ('name = "x"\n[uart1]\npart = "gt911"', "goes on i2c, not uart"),
    ('name = "x"\n' + "".join(f'[[spi2.parts]]\npart = "sdcard"\ncs = {n}\n'
                              for n in range(4)), "3 selects"),
    ('name = [', "not TOML"),
])
def test_a_wrong_description_is_refused_and_says_why(text, says):
    message = refused(text)
    assert message.startswith("b.toml: ")
    assert says in message


def _options(spec: str) -> tuple[str, frozenset[str]]:
    kind, _, rest = spec.partition(",")
    return kind, frozenset(o for o in rest.split(",") if not o.startswith(
        ("socket=", "report=", "frames=")))


def test_the_described_tdeck_builds_the_hand_written_one(tmp_path):
    from emu import board_tdeck

    made = board_tdeck.build(tmp_path / "hand")
    desc = described.load(TDECK)
    ours = described.module(desc).build(tmp_path / "described")
    try:
        assert sorted(map(_options, ours.devices)) == sorted(map(_options, made.devices))
        assert {s: type(d) for s, d in ours.spi.devices.items()} == \
            {s: type(d) for s, d in made.spi.devices.items()}
        for bus in ("i2c0", "i2c1"):
            assert {a: type(d) for a, d in getattr(ours, bus).devices.items()} == \
                {a: type(d) for a, d in getattr(made, bus).devices.items()}
        assert [(r.name, type(r)) for r in ours.regions] == \
            [(r.name, type(r)) for r in made.regions]
        assert ours.qemu_args()[-2:] == made.qemu_args()[-2:]
        assert ours.not_wired() == []
    finally:
        for board in (made, ours):
            for link in (board.gpio, board.spi, board.i2c0, board.i2c1,
                         board.reports, board.clock, board.usb_otg):
                stop = getattr(link, "stop", None) or getattr(link, "close", None)
                if stop:
                    try:
                        stop()
                    except Exception:
                        pass


TDECK_MAX = TDECK.with_name("tdeck-max.toml")


def _serials(board) -> list[str]:
    args = board.qemu_args()
    return [Path(args[i + 1].removeprefix("unix:")).name
            for i, a in enumerate(args) if a == "-serial"]


def test_the_described_max_has_the_hand_written_ones_parts(tmp_path):
    """Its parts, selects, addresses and UARTs are emu/board.py's; its chip
    is the T-Deck's, as every described board's (ADR 0184, decision 3)."""
    from emu import board as max_board

    made = max_board.build(tmp_path / "hand")
    ours = described.module(described.load(TDECK_MAX)).build(tmp_path / "described")
    try:
        spi = dict(map(_options, made.devices))["esp-spi-master"]
        assert dict(map(_options, ours.devices))["esp-spi-master"] == spi
        assert {s: type(d) for s, d in ours.spi.devices.items()} == \
            {s: type(d) for s, d in made.spi.devices.items()}
        for bus in ("i2c0", "i2c1"):
            assert {a: type(d) for a, d in getattr(ours, bus).devices.items()} == \
                {a: type(d) for a, d in getattr(made, bus).devices.items()}
        assert _serials(ours) == _serials(made) == ["modem.sock", "gps.sock"]
        assert ours.held_high == [max_board.EPD_BUSY_PIN]
    finally:
        for board in (made, ours):
            for link in (board.gpio, board.spi, board.i2c0, board.i2c1, board.gps,
                         board.modem, getattr(board, "reports", None), board.clock,
                         board.usb_otg):
                stop = getattr(link, "stop", None) or getattr(link, "close", None)
                if stop:
                    try:
                        stop()
                    except Exception:
                        pass


def test_a_board_has_one_satellite_receiver():
    assert "two satellite receivers" in refused(
        'name = "x"\n[uart1]\npart = "l76k"\n[uart2]\npart = "ublox-m10"')


def test_a_device_keeps_its_description(tmp_path, monkeypatch):
    """The folder holds the text as board.toml and the card the description
    wires, at its size (ADR 0184, decision 6)."""
    from emu import device as device_folder, run

    def build(firmware, outdir, mac, overlays):
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "flash.bin").write_bytes(b"f")
        (outdir / "efuse.bin").write_bytes(b"e")
        return outdir / "flash.bin", outdir / "efuse.bin"

    monkeypatch.setattr(run, "build_images", build)
    firmware = tmp_path / "fw.bin"
    firmware.write_bytes(b"\xe9" + bytes(31))
    text = ('name = "logger"\n[spi2]\n[[spi2.parts]]\npart = "sdcard"\n'
            'cs = 10\nsize_mb = 8\n')
    device = device_folder.create(tmp_path / "devices", "l1", firmware,
                                  description=parse(text))
    assert device.board == "logger"
    assert device.board_file.read_text() == text
    assert device_folder.card_bytes(device) == 8 * 1024 * 1024
    assert len(device.sdcard.read_bytes()) == 8 * 1024 * 1024
    loaded = device_folder.load(device.root)
    assert loaded.description().spi[0].options == {"size_mb": 8}

    plain = device_folder.create(tmp_path / "devices", "p1", firmware, board="tdeck")
    assert plain.description() is None

    # A clone is the same board; and a device goes as any device goes, its
    # description with it.
    twin = device_folder.clone(loaded, "l2")
    assert twin.description().name == "logger"
    device_folder.remove(loaded)
    assert not device.root.exists()


def test_the_library_sends_a_description_by_its_path(tmp_path):
    from boardemul.lab import LabError, _board

    path = tmp_path / "mine.toml"
    path.write_text('name = "mine"')
    assert _board(path) == {"board_description": 'name = "mine"'}
    assert _board(str(path)) == {"board_description": 'name = "mine"'}
    assert _board("tdeck") == {"board": "tdeck"}
    with pytest.raises(LabError):
        _board(tmp_path / "absent.toml")
