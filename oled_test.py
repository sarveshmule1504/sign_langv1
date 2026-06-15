#!/home/raspi/Desktop/myenv/bin/python
"""Quick test for the 1.3\" 128x64 SH1106 I2C OLED."""

import sys
import time

try:
    from luma.core.interface.serial import i2c
except ModuleNotFoundError:
    print("luma.oled is not installed for this Python.")
    print("Run with the project virtualenv:")
    print("  /home/raspi/Desktop/myenv/bin/python oled_test.py")
    sys.exit(1)
from luma.core.render import canvas
from luma.oled.device import sh1106

I2C_PORT = 1
I2C_ADDRESSES = (0x3C, 0x3D)


def connect_display():
    last_error = None
    for address in I2C_ADDRESSES:
        try:
            serial = i2c(port=I2C_PORT, address=address)
            device = sh1106(serial)
            print(f"Connected on I2C bus {I2C_PORT} at 0x{address:02X}")
            return device
        except Exception as exc:
            last_error = exc
    raise last_error


def main():
    device = connect_display()

    lines = ["OLED Test OK", "SH1106 I2C", "Sign Language"]
    y = 8
    with canvas(device) as draw:
        for line in lines:
            draw.text((4, y), line, fill="white")
            y += 12

    print("Text drawn. Display stays on for 10 seconds.")
    time.sleep(10)


if __name__ == "__main__":
    main()
