"""
Embedded SFTP server for Five9 call recording ingestion.

Five9 VCC can export call recordings via FTP or SFTP.  This module runs an
asyncssh-based SFTP server inside the VoiceIntel container so Five9 can
deposit .wav/.mp3 files directly — no intermediate storage, no polling of
a remote server.

Incoming files land in  <STORAGE_DIR>/sftp_incoming/  where the APScheduler
job in sftp_watcher.py picks them up every 30 seconds and routes them through
the existing transcription + NLP pipeline.

Configuration (environment variables)
──────────────────────────────────────
SFTP_ENABLED         Set to "true" to start the server (default: false).
SFTP_PORT            Port to listen on (default: 2222).
SFTP_USERNAME        Username Five9 authenticates with.
SFTP_PASSWORD        Password for username/password auth.  If omitted, only
                     key-based auth is accepted (requires SFTP_AUTHORIZED_KEYS).
SFTP_AUTHORIZED_KEYS OpenSSH public key line(s) for key-based auth (newline-
                     separated).  If omitted, only password auth is used.
SFTP_HOST_KEY        PEM-encoded RSA/ECDSA private key for the server identity.
                     If unset, a 2048-bit RSA key is auto-generated and saved
                     to <STORAGE_DIR>/sftp_host_key (persisted across restarts).

Five9 chroot notes
──────────────────
The SFTP session is chrooted to sftp_incoming/, so Five9 can write anywhere
under that tree.  The default Five9 file-name pattern creates nested dirs:
  recordings/<owner>/<created_date>/<phone> by <agent> @ <time>_<module>.wav
All subdirectories are created on the fly as Five9 uploads.
"""

import asyncio
import logging
import os
import threading

logger = logging.getLogger(__name__)

_thread = None
_loop = None


# ──────────────────────────────────────────────────────────────────────────────
# Host-key helpers
# ──────────────────────────────────────────────────────────────────────────────

def _load_or_generate_host_key(storage_dir: str):
    """Return a persistent asyncssh private key, generating one if needed."""
    import asyncssh

    key_path = os.path.join(storage_dir, "sftp_host_key")
    if os.path.exists(key_path):
        try:
            key = asyncssh.read_private_key(key_path)
            logger.info("SFTP: loaded host key from %s", key_path)
            return key
        except Exception as exc:
            logger.warning("SFTP: could not read %s (%s) — regenerating", key_path, exc)

    key = asyncssh.generate_private_key("ssh-rsa", key_size=2048)
    key.write_private_key(key_path)
    os.chmod(key_path, 0o600)
    logger.info("SFTP: generated new RSA host key → %s", key_path)
    return key


# ──────────────────────────────────────────────────────────────────────────────
# SFTP server implementation
# ──────────────────────────────────────────────────────────────────────────────

def _build_server_factory(incoming_dir: str, username: str,
                           password: str | None, authorized_keys: str | None):
    """Return (ssh_server_factory, sftp_server_factory) for asyncssh."""
    import asyncssh

    class _SFTPHandler(asyncssh.SFTPServer):
        """Chrooted SFTP handler — Five9 can only write inside incoming_dir."""

        def __init__(self, conn):
            super().__init__(conn, chroot=incoming_dir)

    class _SSHHandler(asyncssh.SSHServer):
        def connection_made(self, conn):
            peer = conn.get_extra_info("peername", ("?", 0))
            logger.info("SFTP: connection from %s:%s", peer[0], peer[1])

        def connection_lost(self, exc):
            if exc:
                logger.warning("SFTP: connection closed with error: %s", exc)

        def begin_auth(self, attempted_user: str) -> bool:
            # Always return True — asyncssh interprets False as "allow without
            # credentials", which is the opposite of what we want for unknown
            # users. Wrong usernames will fail all auth methods naturally.
            if attempted_user != username:
                logger.warning("SFTP: unknown user %r — will fail all auth methods", attempted_user)
            return True  # always require authentication

        # ── Password auth ──────────────────────────────────────────────────
        def password_auth_supported(self) -> bool:
            return bool(password)

        def validate_password(self, user: str, pw: str) -> bool:
            import hmac as _hmac
            if not password:
                return False
            ok = _hmac.compare_digest(pw, password)
            if not ok:
                logger.warning("SFTP: password auth failed for %r", user)
            else:
                logger.info("SFTP: %r authenticated (password)", user)
            return ok

        # ── Public-key auth ────────────────────────────────────────────────
        def public_key_auth_supported(self) -> bool:
            return bool(authorized_keys)

        def validate_public_key(self, user: str, key) -> bool:
            if not authorized_keys:
                return False
            import asyncssh as _asyncssh
            try:
                for line in authorized_keys.splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    allowed = _asyncssh.import_authorized_key(line)
                    if key == allowed:
                        logger.info("SFTP: %r authenticated (public key)", user)
                        return True
            except Exception as exc:
                logger.warning("SFTP: public-key validation error: %s", exc)
            return False

    def ssh_factory():
        return _SSHHandler()

    return ssh_factory, _SFTPHandler


async def _start_async_server(host_key, incoming_dir: str, port: int,
                               username: str, password: str | None,
                               authorized_keys: str | None):
    import asyncssh
    ssh_factory, sftp_factory = _build_server_factory(
        incoming_dir, username, password, authorized_keys
    )
    server = await asyncssh.create_server(
        ssh_factory,
        host="",          # bind all interfaces
        port=port,
        server_host_keys=[host_key],
        sftp_factory=sftp_factory,
        allow_scp=False,
    )
    logger.info("SFTP: server listening on port %d (user=%r)", port, username)
    return server


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def start_sftp_server(app) -> None:
    """
    Start the embedded SFTP server in a daemon background thread.
    Called from create_app() when SFTP_ENABLED=true.
    No-op if disabled or misconfigured (errors are logged, not raised).
    """
    global _thread, _loop

    enabled = os.environ.get("SFTP_ENABLED", "false").lower() in ("true", "1", "yes")
    if not enabled:
        return

    try:
        import asyncssh  # noqa: F401
    except ImportError:
        logger.error(
            "SFTP_ENABLED=true but asyncssh is not installed. "
            "Add 'asyncssh' to requirements.txt and rebuild."
        )
        return

    port = int(os.environ.get("SFTP_PORT", "2222"))
    username = os.environ.get("SFTP_USERNAME", "").strip()
    password = os.environ.get("SFTP_PASSWORD", "").strip() or None
    authorized_keys = os.environ.get("SFTP_AUTHORIZED_KEYS", "").strip() or None

    if not username:
        logger.error("SFTP_ENABLED=true but SFTP_USERNAME is not configured — server not started")
        return
    if not password and not authorized_keys:
        logger.error(
            "SFTP_ENABLED=true but neither SFTP_PASSWORD nor SFTP_AUTHORIZED_KEYS "
            "is set — server not started"
        )
        return

    storage_dir = app.config["STORAGE_DIR"]
    incoming_dir = os.path.join(storage_dir, "sftp_incoming")
    os.makedirs(incoming_dir, exist_ok=True)

    # Resolve host key
    pem_env = os.environ.get("SFTP_HOST_KEY", "").strip()
    if pem_env:
        import asyncssh
        try:
            host_key = asyncssh.import_private_key(pem_env)
            logger.info("SFTP: using host key from SFTP_HOST_KEY env var")
        except Exception as exc:
            logger.error("SFTP: could not parse SFTP_HOST_KEY: %s — server not started", exc)
            return
    else:
        host_key = _load_or_generate_host_key(storage_dir)

    def _thread_main():
        global _loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _loop = loop
        try:
            loop.run_until_complete(
                _start_async_server(host_key, incoming_dir, port,
                                    username, password, authorized_keys)
            )
            loop.run_forever()
        except Exception as exc:
            logger.error("SFTP: server thread crashed: %s", exc, exc_info=True)
        finally:
            loop.close()

    _thread = threading.Thread(target=_thread_main, name="sftp-server", daemon=True)
    _thread.start()
    logger.info("SFTP: server thread started (port=%d, user=%r)", port, username)
