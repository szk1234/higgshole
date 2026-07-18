# Deployment

HiggsHole runs anywhere Python 3.12 and ffmpeg run. A systemd unit is provided
for boot-time startup on Linux; nothing in the architecture depends on systemd.

Every path below is an example. Substitute your own; nothing machine-specific
is committed to this repository.

## 1. Install

```bash
sudo git clone https://github.com/higgshole/higgshole.git /opt/higgshole
cd /opt/higgshole
uv sync --no-dev
```

`uv sync` creates `/opt/higgshole/.venv`, which is what the unit's `ExecStart`
refers to.

## 2. Create the service account

An unprivileged system account with no login shell and no home directory:

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin higgshole
```

## 3. Create the data and state directories

The media root and the database live in different places on purpose: the media
tree is expected to be exported over a file-sharing protocol, and a database
reachable by remote clients risks corruption through incompatible locking. The
state directory also holds the API keys.

```bash
sudo install -d -o higgshole -g higgshole -m 0755 /var/lib/higgshole/media
sudo install -d -o higgshole -g higgshole -m 0750 /var/lib/higgshole/state
```

These two paths are the only ones the unit lists in `ReadWritePaths`. If you
move either, update the drop-in described in step 6 — `ProtectSystem=strict`
makes everything else read-only, so a mismatch shows up as a permission error
rather than a silent write elsewhere.

## 4. Write the environment file

```bash
sudo install -d -m 0755 /etc/higgshole
sudo tee /etc/higgshole/higgshole.env >/dev/null <<'EOF'
HIGGSHOLE_OPENROUTER_API_KEY=your-openrouter-key-here
HIGGSHOLE_DAILY_CAP_USD=10.00
HIGGSHOLE_MAX_JOB_COST_USD=2.00
EOF
sudo chown root:higgshole /etc/higgshole/higgshole.env
sudo chmod 640 /etc/higgshole/higgshole.env
```

The file must not be world-readable. `chmod 600` with `chown higgshole:` works
equally well; the point is that no other account can read the key.

A local daily cap is only the second line of defence. **Also set a credit limit
on the OpenRouter key itself.** That limit is enforced provider-side and is the
only guard a bug in this application cannot defeat.

`EnvironmentFile=` in the unit carries a leading `-`, so the service still
starts if this file is absent. It will then have no key and every generation
will fail at the provider with an authentication error — which is the intended
behaviour, not a silent success.

## 5. Install the unit

The shipped unit carries two placeholders, `@USER@` and `@INSTALL_DIR@`:

```bash
sed -e 's|@USER@|higgshole|g' \
    -e 's|@INSTALL_DIR@|/opt/higgshole|g' \
    /opt/higgshole/deploy/higgshole.service.example \
    | sudo tee /etc/systemd/system/higgshole.service >/dev/null

sudo systemctl daemon-reload
sudo systemctl enable --now higgshole.service
sudo systemctl status higgshole.service
```

Confirm it answers:

```bash
curl -s http://127.0.0.1:8077/api/budget
```

## 6. Local overrides

Do not edit `/etc/systemd/system/higgshole.service` directly — a later
reinstall would discard your changes without saying so. Use a drop-in:

```bash
sudo systemctl edit higgshole.service
```

That opens `/etc/systemd/system/higgshole.service.d/override.conf`. To move the
media root, for example, both the environment variable and the writable-path
allowance must change together:

```ini
[Service]
Environment=HIGGSHOLE_MEDIA_ROOT=/srv/media/higgshole
ReadWritePaths=
ReadWritePaths=/srv/media/higgshole /var/lib/higgshole/state
```

The empty `ReadWritePaths=` line resets the list inherited from the unit;
without it the two lists are merged and the old path stays writable.

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl restart higgshole.service
```

## 7. Exposing it beyond the loopback interface

There is **no authentication**, by design. The service binds `127.0.0.1` unless
you change `HIGGSHOLE_BIND_HOST`. Changing it exposes every generation control
and the whole media library to anyone who can reach the port. Do it only on a
network you trust, and consider a reverse proxy that adds authentication.

## 8. Backups

`rescan` can rebuild the generation index from the sidecar files in the media
tree. It **cannot** rebuild `spend_ledger` or `settings` — the local spend
record and the stored API keys exist nowhere else. Back up
`/var/lib/higgshole/state` as well as the media root.

## 9. Logs

```bash
journalctl -u higgshole.service -f
```
