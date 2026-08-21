# Pixel 3a XL (bonito) — PM660 USB-C Dual-Role Support

Experimental Linux kernel patch set enabling functional USB-C dual-role support on the Google Pixel 3a XL (`bonito`) using the Qualcomm PM660 Type-C hardware.

The work was developed and tested on postmarketOS using an SDM670 Linux 7.0.10 kernel.

## Status

**Working and hardware-tested.**

The patched kernel has successfully demonstrated:

- USB-C Type-C role detection
- USB host / OTG mode
- VBUS source control
- USB peripheral/device mode
- USB gadget enumeration
- USB networking over NCM
- USB charging while operating as a sink
- Runtime transition between host and device roles
- Clean VBUS disable after OTG device removal

Test device:

```text
Google Pixel 3a XL (bonito)
Qualcomm SDM670
PM660 PMIC
postmarketOS
Linux 7.0.10
```

Final tested kernel:

```text
7.0.10-sdm670-gaac5fb159b67
```

Patch base:

```text
06f3bc759bf5
tag: sdm670-v7.0.10
```

Final patch-series commit:

```text
aac5fb159b67d616e0aa683dd06324a5cc517126
```

---

# What Was Broken

Before these changes, the Pixel 3a XL USB-C controller was not correctly operating as a Linux dual-role Type-C port.

The work required support across several related pieces:

- Qualcomm PM660 Type-C handling
- PM660 VBUS regulator support
- Qualcomm SMB charger integration
- TCPM integration
- USB role switching
- Device-tree configuration
- VBUS state/change notification

During development, Type-C CC state changes could be detected, but TCPM could become stuck or receive incorrect VBUS notifications.

A particularly important issue was distinguishing:

```text
CC state changed
```

from:

```text
VBUS state changed
```

The PM660 exposes an aggregate Type-C change interrupt, so blindly reporting both CC and VBUS changes to TCPM caused incorrect state-machine behavior.

The final implementation tracks the actual PM660 VBUS state and only generates a TCPM VBUS notification when VBUS really changes.

The PM660 source regulator is also accounted for when the phone itself supplies VBUS.

---

# Patch Series

The complete work consists of 13 commits on top of `sdm670-v7.0.10`:

```text
aac5fb159b67 usb: typec: qcom: pm660: notify TCPM after VBUS changes
64fb5a59470c usb: typec: qcom: pm660: track VBUS changes in Type-C IRQ
3505a17f0bd3 usb: typec: qcom: pm660: only report CC changes from Type-C IRQ
b26caf853a77 power: supply: qcom_smbx: fix PM660 VBUS node lookup
f5f301628e8f usb: typec: qcom: pm660: avoid VBUS notification from set_vbus
5d6164bc79ce power: supply: qcom_smbx: define USB input suspend register
afe6d1be1b53 arm64: dts: qcom: sdm670-google: enable PM660 USB-C dual-role
655704b4bf15 dt-bindings: power: supply: qcom,pmi8998-charger: add VBUS regulator
a202d57eaeac dt-bindings: usb: qcom,pmic-typec: add PM660
fc78301d6d8f usb: typec: qcom: add PM660 Type-C port support
620f1b31c9d5 power: supply: qcom_smbx: add PM660 VBUS regulator
cc696116fc6d usb: typec: qcom: add private port backend data
f2d581bc5607 usb: typec: qcom: make PMIC port probe selectable
```

The repository contains both the individual `git format-patch` series and a combined patch.

---

# Hardware Test Results

## USB Host / OTG

A Kingston DataTraveler USB flash drive was connected through a USB-C OTG adapter.

Linux correctly transitioned the phone to:

```text
power_role: [source] sink
data_role:  [host] device
usb_role:   host

VBUS:       enabled
VBUS users: 1
```

The USB device successfully enumerated:

```text
Bus 001 Device 002:
ID 0951:1666
Kingston Technology DataTraveler 100 G3/G4/SE9 G2/50 Kyson
```

Kernel enumeration continued through USB mass-storage and SCSI:

```text
usb 1-1: new high-speed USB device
usb-storage 1-1:1.0: USB Mass Storage device detected

scsi 0:0:0:0:
Direct-Access Kingston DataTraveler 3.0

sd 0:0:0:0: [sda] 241660916 512-byte logical blocks
sda: sda1 sda2
sd 0:0:0:0: [sda] Attached SCSI removable disk
```

This verifies more than Type-C role reporting: the Pixel actually powered the external device, initialized the xHCI host controller, enumerated the flash drive, and exposed its storage device to Linux.

### PM660 state while attached

```text
130b: 00
130c: 28
130e: 91
```

After removal, VBUS was disabled and the USB host disappeared.

TCPM reported:

```text
CC1: 2 -> 0, CC2: 0 -> 0 [state SRC_READY, polarity 0, disconnected]
state change SRC_READY -> SNK_UNATTACHED
Start toggling
VBUS off
VBUS VSAFE0V
```

---

# USB Peripheral / Gadget Mode

Peripheral mode was tested by connecting the Pixel directly to a Mac.

The DWC3 USB Device Controller reached:

```text
state: configured
current_speed: high-speed
maximum_speed: high-speed
```

The USB role switch reported:

```text
usb_role: device
```

The postmarketOS configfs gadget was bound to:

```text
UDC: a600000.usb
```

The gadget uses the NCM networking function:

```text
/sys/kernel/config/usb_gadget/g1/functions/ncm.usb0
```

The resulting Linux network interface reported carrier:

```text
usb0:
carrier: 1
operstate: up
```

This demonstrates that the Pixel can operate as an active USB peripheral and that the host successfully configured the USB gadget.

## USB Networking Note

On the tested postmarketOS installation, `usb0` was created successfully but the expected IPv4 address was not automatically assigned after boot.

The USB hardware/kernel path itself was functional:

```text
UDC state: configured
USB role: device
usb0 carrier: 1
sshd: listening on 0.0.0.0:22
```

Manually assigning the expected postmarketOS gadget address immediately restored USB SSH:

```bash
sudo ip addr add 172.16.42.1/24 dev usb0
sudo ip link set usb0 up
```

Afterward the Pixel was reachable at:

```text
172.16.42.1
```

This appears to be a userspace network configuration issue rather than a failure of USB peripheral mode.

**Current known limitation:** the `172.16.42.1/24` address is not persistent across boot on the tested installation. The NCM gadget, UDC configuration, carrier, and USB role are functional; only the userspace IPv4 assignment must currently be restored manually.

---

# USB Sink / Charging Test

With the Pixel connected directly to the Mac, Type-C reported:

```text
power_role: source [sink]
data_role:  host [device]
power_operation_mode: usb_power_delivery
preferred_role: sink
```

The Qualcomm PM660 charger reported:

```text
status: Charging
online: 1
health: Good
usb_type: Unknown [SDP] DCP CDP

voltage_now: 5039062
current_now: 483471
current_max: 500000
```

Battery telemetry simultaneously reported:

```text
status: Charging
capacity: 23
voltage_now: 3773671
current_now: 178222
temp: 380
```

Repeated battery samples showed positive charging current:

```text
18:15:55  status=Charging  capacity=23  voltage_now=3762441  current_now=84472
18:16:00  status=Charging  capacity=23  voltage_now=3773671  current_now=179199
18:16:05  status=Charging  capacity=23  voltage_now=3770254  current_now=140624
18:16:10  status=Charging  capacity=23  voltage_now=3758047  current_now=62988
18:16:15  status=Charging  capacity=23  voltage_now=3761220  current_now=82519
18:16:20  status=Charging  capacity=23  voltage_now=3774404  current_now=178222
18:16:25  status=Charging  capacity=23  voltage_now=3762441  current_now=84960
18:16:30  status=Charging  capacity=23  voltage_now=3764150  current_now=107421
```

This confirms that the phone can operate as a USB sink and receive power rather than only operating as an OTG source.

---

# Quick Test Summary

| Test | Result |
|---|---|
| PM660 Type-C driver loads | ✅ |
| TCPM integration | ✅ |
| Dual-role Type-C port exposed | ✅ |
| CC attach/detach detection | ✅ |
| USB host role | ✅ |
| USB peripheral role | ✅ |
| VBUS source enable | ✅ |
| VBUS source disable | ✅ |
| xHCI host startup | ✅ |
| USB mass-storage enumeration | ✅ |
| DWC3 gadget mode | ✅ |
| ConfigFS NCM gadget | ✅ |
| USB gadget reaches `configured` | ✅ |
| `usb0` carrier | ✅ |
| USB SSH after assigning `usb0` IP | ✅ |
| USB sink / charging | ✅ |
| Automatic `usb0` IPv4 assignment | ⚠️ Userspace configuration issue |

---

# Applying the Patch Series

Start from the tested kernel base:

```bash
git checkout 06f3bc759bf5
```

Apply the complete series:

```bash
git am /path/to/pixel3axl/patches/*.patch
```

The resulting tree should end at the equivalent of:

```text
aac5fb159b67
```

when using the original commit series.

Alternatively, the combined source diff can be applied with:

```bash
git apply bonito-pm660-usbc-final.patch
```

Using the `git format-patch` series is recommended because it preserves the individual commits, authorship, and development history.

---

# Standalone Boot Image Patcher

The repository also includes a standalone helper:

```text
tools/patch_bonito_usb.py
```

Its purpose is to take an existing postmarketOS Android-format boot image and replace its kernel payload with the tested kernel and Bonito DTB while preserving the original ramdisk and Android boot-header parameters.

The tested Bonito boot image uses Android boot header version 0 and does **not** expose the DTB as a separate boot-image component. The DTB is appended directly to the compressed kernel payload. The patcher therefore constructs:

```text
Image.gz + sdm670-google-bonito-sdc.dtb
```

and uses that combined payload as the boot image kernel.

Example:

```bash
python3 tools/patch_bonito_usb.py \
  --input /tmp/boot-original.img \
  --kernel /tmp/bonito-pm660-usbc-final-7.0.10-sdm670-gaac5fb159b67/Image.gz \
  --dtb /tmp/bonito-pm660-usbc-final-7.0.10-sdm670-gaac5fb159b67/sdm670-google-bonito-sdc.dtb \
  --output /tmp/bonito-pm660-usbc-test.img \
  --keep-workdir
```

The validated build produced:

```text
output:
  /tmp/bonito-pm660-usbc-test.img

size:
  24.6 MiB

sha256:
  15610aeab00bafeb6095f6aa023cdd68e847730df343f25eb77de37f843fc2d6
```

The repacked image was unpacked again and the DTB at the end of the kernel payload was independently verified.

```text
Image.gz size:       12311453 bytes
DTB size:              102754 bytes
combined kernel:     12414207 bytes
```

The SHA-256 of the final 102754 bytes of the combined kernel exactly matched the supplied DTB:

```text
02ca934bd01a6e65de02ed315e2ca6805a8546ac4e44033ecb056ec68f4e4950
```

This gives a reproducible check that the intended Bonito DTB was actually embedded in the generated boot image.

The patcher does **not** flash the phone.

---

# Testing a Generated Image

Always RAM-boot a newly generated image before permanently flashing it.

On the Mac, reboot the Pixel into the bootloader and confirm Fastboot sees it:

```bash
cd ~/platform-tools
./fastboot devices
```

Then boot the generated image without modifying the installed boot partition:

```bash
./fastboot boot /path/to/bonito-pm660-usbc-test.img
```

After postmarketOS starts, verify the kernel:

```bash
uname -r
```

Expected for the validated build:

```text
7.0.10-sdm670-gaac5fb159b67
```

Verify the required modules:

```bash
lsmod | grep -E 'qcom_pmic_tcpm|tcpm|qcom_smbx'
```

Expected modules include:

```text
qcom_pmic_tcpm
tcpm
qcom_smbx
```

A matching `qcom_pmic_tcpm` module should report the same kernel release in `vermagic`:

```bash
modinfo qcom_pmic_tcpm | grep -E '^(filename|name|vermagic):'
```

Also check for obvious boot/module/gadget failures:

```bash
sudo dmesg | grep -E \
'Module .* not found|FATAL|pmOS-rd.*Could not find|usb_gadget' |
tail -50
```

The final test boot produced no matching errors.

## Device / peripheral test

With the phone connected to a computer:

```bash
cat /sys/class/typec/port0/power_role
cat /sys/class/typec/port0/data_role
cat /sys/class/usb_role/a600000.usb-role-switch/role

sudo cat /sys/kernel/config/usb_gadget/g1/UDC

UDC=/sys/class/udc/a600000.usb
for f in state current_speed maximum_speed; do
    [ -f "$UDC/$f" ] && echo "$f: $(cat "$UDC/$f")"
done

ip -br addr show usb0
cat /sys/class/net/usb0/carrier
cat /sys/class/net/usb0/operstate
```

The validated device produced:

```text
power_role: source [sink]
data_role:  host [device]
usb_role:   device

UDC: a600000.usb

state: configured
current_speed: high-speed
maximum_speed: high-speed

usb0: UP 172.16.42.1/24
carrier: 1
operstate: up
```

Note that `172.16.42.1/24` is **not currently persistent on the tested installation**. If `usb0` exists and has carrier but has no IPv4 address, restore USB SSH with:

```bash
sudo ip addr add 172.16.42.1/24 dev usb0
sudo ip link set usb0 up
```

USB SSH then works at:

```text
172.16.42.1
```

The missing automatic IPv4 assignment is a postmarketOS/userspace network configuration issue; it is separate from the kernel USB-C role-switching patch.

## Host / OTG test

Attach an OTG adapter and USB device, then run:

```bash
cat /sys/class/typec/port0/power_role
cat /sys/class/typec/port0/data_role
cat /sys/class/usb_role/a600000.usb-role-switch/role

VBUS="$(for d in /sys/class/regulator/regulator.*; do
    [ -f "$d/name" ] || continue
    [ "$(cat "$d/name")" = "pm660-vbus" ] && echo "$d"
done)"

echo "VBUS=$VBUS"
cat "$VBUS/state"
cat "$VBUS/num_users"

lsusb
```

The validated Kingston test produced:

```text
power_role: [source] sink
data_role:  [host] device
usb_role:   host

VBUS: enabled
VBUS users: 1

0951:1666 Kingston Technology DataTraveler
```

After unplugging the OTG device:

```bash
sleep 2
cat /sys/class/usb_role/a600000.usb-role-switch/role
cat "$VBUS/state"
cat "$VBUS/num_users"
```

The validated result was:

```text
usb_role: device
VBUS: disabled
VBUS users: 0
```

---

# Installing the Tested Image Permanently

Only do this after the exact generated image has successfully passed the RAM-boot tests above.

First enter Fastboot and determine the currently active slot:

```bash
./fastboot devices
./fastboot getvar current-slot
```

Back up or retain a known-good original boot image before flashing.

If Fastboot reports slot `b`, flash only `boot_b`:

```bash
./fastboot flash boot_b /path/to/bonito-pm660-usbc-test.img
./fastboot set_active b
./fastboot reboot
```

If Fastboot reports slot `a`, use `boot_a` instead:

```bash
./fastboot flash boot_a /path/to/bonito-pm660-usbc-test.img
./fastboot set_active a
./fastboot reboot
```

Do not flash both slots merely for symmetry. Preserve a known-good boot image and a working Fastboot recovery path.

After the permanent boot, repeat the kernel, module, peripheral, charging, host/VBUS, and detach tests documented above.


---

# Verification

After booting the patched kernel:

```bash
uname -r

lsmod | grep -E 'qcom_pmic_tcpm|tcpm|qcom_smbx'

cat /sys/class/typec/port0/power_role
cat /sys/class/typec/port0/data_role
cat /sys/class/typec/port0/port_type

cat /sys/class/usb_role/a600000.usb-role-switch/role
```

Locate the PM660 VBUS regulator:

```bash
for d in /sys/class/regulator/regulator.*; do
    [ -f "$d/name" ] || continue

    if [ "$(cat "$d/name")" = "pm660-vbus" ]; then
        echo "$d"
        cat "$d/state"
        cat "$d/num_users"
    fi
done
```

When an OTG device is connected, expected behavior is:

```text
power_role: [source] sink
data_role:  [host] device
usb_role:   host

pm660-vbus:
state: enabled
num_users: 1
```

The attached USB device should also appear in:

```bash
lsusb
```

After removal:

```text
VBUS: disabled
VBUS users: 0
```

---

# PM660 Debugging

The PM660 Type-C status registers used during development can be inspected through regmap debugfs:

```bash
sudo grep -Ei \
'^(130b|130c|130e):' \
/sys/kernel/debug/regmap/0-00/registers
```

TCPM debugging is available at:

```text
/sys/kernel/debug/usb/tcpm-c440000.spmi:pmic@0:typec@1300/log
```

For example:

```bash
sudo cat \
/sys/kernel/debug/usb/tcpm-c440000.spmi:pmic@0:typec@1300/log
```

These interfaces were heavily used to compare physical cable/device state against TCPM's interpretation of the port.

---

# Important Notes

This is currently an experimental patch set tested on a real Google Pixel 3a XL (`bonito`).

It has not been validated across every SDM670/PM660 device or every USB-C peripheral.

The patches modify low-level USB-C, charging, VBUS, TCPM, and device-tree behavior. Anyone testing them should have a working recovery path and understand how to recover/boot their device if the kernel fails.

The tested device used an unlocked bootloader and the development images were initially validated using `fastboot boot` before being installed permanently.

---

# Why This Repository Does Not Contain the Linux Kernel

This repository intentionally contains the **patches rather than a complete Linux kernel checkout**.

The tested base is identified by commit:

```text
06f3bc759bf5
```

and the changes can be reproduced by applying the included patch series.

This keeps the repository small, makes the actual changes easy to review, and avoids duplicating millions of objects from the upstream kernel repository.

---

# Documentation

A more detailed engineering validation report is available under:

```text
docs/
```

It contains the development history, debugging process, implementation details, and captured hardware test results.

---

# Hardware

Tested on:

**Google Pixel 3a XL (`bonito`)**

The closely related Pixel 3a (`sargo`) has not been validated by this testing and should not be assumed to behave identically without additional hardware testing.