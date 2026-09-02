# Hardware record (fill at build time — plan §02, R-1)

- Nighthawk model/firmware: ______ (3-antenna classic line; record RP-SMA? per-band radio toggle?)
  - base DHCP reservation: ______   OFDMA enabled: [ ]   bridges on 5 GHz only (INV-4): [ ]
- Base tablet: model ______, distro/kernel ______, USB-C GbE adapter chipset ______
- Edge bridges: Pi 3 A+ serials + /boot/convoy.toml node_id map
- BT dongles: model + `lsusb` VID:PID per unit (revision drift check; EDUP EP-B3536 = safe RTL8761BU)
- Headset qualification cards: docs/headsets/<model>.md (H-01/H-07): mic outside a call?
  codec (btmon: CVSD/mSBC)? 10 call cycles + 20 power cycles? ownership vs rider's phone?
- Bridge CPU: `bridge.main --verbose` tick busy % at silero / energy (DR-001 revisit: >30 %)

Software stack per bridge (see docs/runbook.md §2.2): RPi OS Lite 64 Bookworm,
PipeWire with `bluez5.roles = [hfp_ag]` + mSBC, bt-agent NoInputNoOutput,
`dtoverlay=disable-bt` (INV-3), overlay FS read-only root (INV-11).
