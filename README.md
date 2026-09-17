# Service Manager

A terminal dashboard for managing Linux systemd services. This project provides a lightweight interactive interface for browsing, monitoring, and editing service units from a TUI built with Rich.

## Features

- Browse `.service` units from `~/Services` and `/etc/systemd/system`
- View live service state and metadata in a compact dashboard
- Inspect logs and recent errors for the selected unit
- Create, remove, and link service files
- Reload systemd and perform service actions with sudo when required

## Requirements

- Linux with `systemd`
- Python 3.10+
- `rich`

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## Run

```bash
python3 service-manager.py
```

> This tool is intended for Linux systems where `systemctl` is available and may require elevated permissions for certain service-management actions.
