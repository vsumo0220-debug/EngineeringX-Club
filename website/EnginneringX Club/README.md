# EngineeringX Club

## Run the website and project submission service

The project gallery, image uploads, and moderation require the Python server; opening `index.html` directly does not provide those features. Python 3.10 or later and Pillow are required.

In PowerShell, from this folder, set a private administrator password and start the service:

```powershell
python -m pip install -r .\requirements.txt
$env:ENGINEERINGX_ADMIN_PASSWORD = "replace-this-with-a-unique-password-of-at-least-16-characters"
python .\server.py
```

Open <http://127.0.0.1:8080>. The password is only read from the server environment and is never stored in the database or page. The administrator panel is under **Projects Members Made → Administrator moderation**. The club logo is served from `assets/engineeringx-club-logo.jpg`.

Project records are stored in SQLite and uploaded images are stored as files in `..\..\data` by default. To choose another persistent directory before starting the server:

```powershell
$env:ENGINEERINGX_DATA_DIR = "D:\EngineeringXData"
```

Set `ENGINEERINGX_PORT` to change the port. The server binds to `127.0.0.1` by default. To make it reachable from other devices on a trusted local network, set `ENGINEERINGX_HOST` to `0.0.0.0` and configure the host firewall accordingly.

For a public deployment, run this service behind an HTTPS reverse proxy, use a unique secret administrator password, and mount a persistent disk for the SQLite database and `uploads` directory. Back up both together. Do not deploy on an ephemeral filesystem: uploaded images and project records must survive redeployments. The `Secure` administrator cookie attribute is enabled when the trusted proxy sets `X-Forwarded-Proto: https`.

The server validates required fields, category, platform-specific optional links, decodes and verifies uploaded JPEG/PNG/WebP images (5 MB and 16-megapixel limits), and stores each submission as `pending`. Only authenticated administrator API actions can approve, reject, or delete entries; public gallery and image endpoints expose approved projects only.

## Run tests

```powershell
python -m unittest discover -s .\tests -v
```
