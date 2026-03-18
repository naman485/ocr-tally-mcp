# Tally Prime Remote Tunnel Setup Guide (Port 9000)

Expose Tally Prime's HTTP server remotely using either **Cloudflare Tunnel** or **ngrok** — both free, no domain required.

---

# Part 1: Cloudflare Tunnel

## Quick Tunnel (Temporary — URL Changes on Restart)

### Step 1: Install cloudflared

Open **PowerShell as Administrator** on the Tally machine:

```powershell
winget install cloudflare.cloudflared
```

Or download manually from `https://github.com/cloudflare/cloudflared/releases/latest` — grab `cloudflared-windows-amd64.msi`.

### Step 2: Verify

```powershell
cloudflared --version
```

### Step 3: Start Quick Tunnel

```powershell
cloudflared tunnel --url http://localhost:9000
```

Output:

```
+--------------------------------------------------------------------------------------------+
|  Your quick Tunnel has been created! Visit it at (it may take some time to be reachable):  |
|  https://something-random-words.trycloudflare.com                                          |
+--------------------------------------------------------------------------------------------+
```

### Step 4: Test

From your development machine:

```bash
curl https://something-random-words.trycloudflare.com
```

### Limitations

- No signup needed
- URL changes every restart
- Closing terminal kills the tunnel

---

## Persistent Tunnel (Fixed URL, Survives Restarts)

### Step 1: Create Free Cloudflare Account

Sign up at `https://dash.cloudflare.com`

### Step 2: Login

```powershell
cloudflared login
```

Browser opens — authorize your account.

### Step 3: Create Named Tunnel

```powershell
cloudflared tunnel create tally-server
```

Note the **Tunnel ID** printed (e.g., `a1b2c3d4-e5f6-7890-abcd-ef1234567890`).

Your fixed URL: `https://<tunnel-id>.cfargotunnel.com`

### Step 4: Create Config File

Create `C:\Users\<YourUser>\.cloudflared\config.yml`:

```yaml
tunnel: tally-server
credentials-file: C:\Users\<YourUser>\.cloudflared\<tunnel-id>.json

ingress:
  - service: http://localhost:9000
```

Replace `<YourUser>` and `<tunnel-id>` with actual values.

### Step 5: Test

```powershell
cloudflared tunnel run tally-server
```

From your dev machine:

```bash
curl https://<tunnel-id>.cfargotunnel.com
```

### Step 6: Install as Windows Service

```powershell
cloudflared service install
net start cloudflared
```

Auto-starts on boot, runs in background.

### Manage Service

```powershell
sc query cloudflared          # check status
net stop cloudflared          # stop
net start cloudflared         # start
cloudflared service uninstall # remove
cloudflared tunnel delete tally-server  # delete tunnel
```

---

# Part 2: ngrok

## Quick Tunnel (Temporary — URL Changes on Restart)

### Step 1: Install ngrok

```powershell
winget install ngrok.ngrok
```

### Step 2: Create Free Account & Get Auth Token

Sign up at `https://ngrok.com` — copy auth token from dashboard.

```powershell
ngrok config add-authtoken YOUR_AUTH_TOKEN_HERE
```

### Step 3: Start Tunnel

```powershell
ngrok http 9000
```

Output shows a URL like `https://abc123.ngrok-free.app`.

### Limitations

- URL changes every restart
- Closing terminal kills the tunnel

---

## Persistent Tunnel (Fixed URL, Survives Restarts)

### Step 1: Get a Free Static Domain

Go to `https://dashboard.ngrok.com/domains` and click **"New Domain"**.

You get a fixed domain like: `your-name-randomly.ngrok-free.app`

ngrok gives **1 free static domain** per account.

### Step 2: Run with Fixed Domain

```powershell
ngrok http 9000 --domain your-name-randomly.ngrok-free.app
```

### Step 3: Install as Windows Service (Auto-Start on Reboot)

Download **NSSM** from `https://nssm.cc/download`, then:

```powershell
nssm install ngrok-tally "C:\path\to\ngrok.exe" "http 9000 --domain your-name-randomly.ngrok-free.app"
nssm start ngrok-tally
```

### Manage Service

```powershell
nssm status ngrok-tally   # check status
nssm stop ngrok-tally     # stop
nssm start ngrok-tally    # start
nssm remove ngrok-tally   # remove
```

---

# After Setup (Both Options)

## Update .env

```env
TALLY_URL=https://your-tunnel-url-here
```

## Verify Tally is Responding

```bash
curl https://your-tunnel-url-here
```

Should return an XML response from Tally.

---

# Comparison

| Feature | Cloudflare | ngrok |
|---------|-----------|-------|
| Free quick tunnel | Yes (no signup) | Yes (signup needed) |
| Free fixed URL | Yes (via named tunnel) | Yes (1 free static domain) |
| Auto-start service | Built-in `cloudflared service install` | Needs NSSM |
| HTTPS | Automatic | Automatic |
| Speed | Fast | Fast |
| Domain needed | No | No |

---

# Troubleshooting

| Issue | Solution |
|-------|----------|
| Tool not found after install | Restart PowerShell |
| Tunnel connects but no Tally response | Ensure Tally is running with a company open |
| Connection refused on 9000 | Check Tally Client/Server config is enabled |
| Service won't start | Run tunnel manually first to check for errors |

**Note:** Tally Prime must be running with a company loaded for port 9000 to respond.
