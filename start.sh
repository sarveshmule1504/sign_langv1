#!/bin/bash
# Kill any existing processes on port 5000 to avoid address in use error
fuser -k 5000/tcp > /dev/null 2>&1

# Wait 15 seconds to allow network interfaces and the USB camera to initialize fully on boot
sleep 15

# Setup environment variables needed for Audio and Display in Cron
export XDG_RUNTIME_DIR=/run/user/$(id -u)
export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus
export DISPLAY=:0
export WAYLAND_DISPLAY=wayland-0

# Navigate to the project directory
cd /home/raspi/Desktop/signlang

# Ensure I2C OLED dependencies are present
/home/raspi/Desktop/myenv/bin/pip install -q luma.oled 2>/dev/null

# Run the app using the virtual environment's Python and log all outputs for debugging
/home/raspi/Desktop/myenv/bin/python app.py > /home/raspi/Desktop/signlang/cron_log.log 2>&1

