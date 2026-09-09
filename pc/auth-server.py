#!/usr/bin/env python3
"""Atoll NMOS IS-10 Authorization Server (AMWA IS-10 / BCP-003-02).

An OAuth 2.0 authorization server for the NMOS control plane: it issues RS256-signed JWT bearer
tokens carrying the NMOS private claims (`x-nmos-<api>` access rights), publishes its public key as a
JWKS and its metadata per RFC 8414, and advertises itself over DNS-SD (`_nmos-auth._tcp`) so NMOS
nodes discover it. NMOS resource servers (here: Program Out's IS-05 API) validate the token against
this key before honouring a request.

Grant: `client_credentials` (service-to-service; no interactive login) -- the natural fit for a rig
of headless senders/receivers. Token issuance is demo-open (any client_id); the point demonstrated
is the token lifecycle + resource-server validation, not hardening the AS.

Env: AUTH_PORT, NMOS_ADVERTISE_HOST, ISLAND_PC_IP, ATOLL_RUN.
"""
import http.server, socketserver, json, os, time, base64, subprocess, threading, hashlib, sys
from urllib.parse import urlparse, parse_qs
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

HERE = os.path.dirname(os.path.abspath(__file__))
NEED = ["AUTH_PORT", "NMOS_ADVERTISE_HOST", "ISLAND_PC_IP", "ATOLL_RUN"]
raw = subprocess.check_output(["bash", "-c", f'source "{HERE}/atoll.conf"; ' + "".join(f'echo "{k}=${{{k}}}";' for k in NEED)], text=True)
CFG = dict(l.split("=", 1) for l in raw.strip().splitlines() if "=" in l)
PORT = int(CFG.get("AUTH_PORT") or 8106)
HOST = (CFG.get("NMOS_ADVERTISE_HOST") or "").strip() or (CFG.get("ISLAND_PC_IP") or "localhost").strip()
RUN = (CFG.get("ATOLL_RUN") or os.path.expanduser("~/atoll-run")).strip()
ISSUER = f"http://{HOST}:{PORT}"
KEYFILE = os.path.join(RUN, "auth-key.pem")
SCOPES = ["connection", "node", "query", "registration", "events", "channelmapping"]

# ---- RSA key (persisted so tokens survive restarts) ----
os.makedirs(RUN, exist_ok=True)
if os.path.exists(KEYFILE):
    with open(KEYFILE, "rb") as f:
        PRIV = serialization.load_pem_private_key(f.read(), password=None)
else:
    PRIV = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with open(KEYFILE, "wb") as f:
        f.write(PRIV.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                   serialization.NoEncryption()))
PRIV_PEM = PRIV.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
PUB = PRIV.public_key()
_pn = PUB.public_numbers()
def _b64u(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
def _int_b64u(x): return _b64u(x.to_bytes((x.bit_length() + 7) // 8, "big"))
KID = hashlib.sha256(PUB.public_bytes(serialization.Encoding.DER,
      serialization.PublicFormat.SubjectPublicKeyInfo)).hexdigest()[:16]
JWK = {"kty": "RSA", "use": "sig", "alg": "RS256", "kid": KID, "n": _int_b64u(_pn.n), "e": _int_b64u(_pn.e)}
JWKS = {"keys": [JWK]}

METADATA = {
    "issuer": ISSUER,
    "authorization_endpoint": ISSUER + "/authorize",
    "token_endpoint": ISSUER + "/token",
    "jwks_uri": ISSUER + "/jwks",
    "registration_endpoint": ISSUER + "/register",
    "grant_types_supported": ["client_credentials", "authorization_code"],
    "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post", "none"],
    "scopes_supported": SCOPES,
    "response_types_supported": ["code", "token"],
    "code_challenge_methods_supported": ["S256"],
}

def issue_token(client_id, scope):
    now = int(time.time())
    scopes = [s for s in (scope or "").split() if s in SCOPES] or SCOPES
    claims = {
        "iss": ISSUER, "sub": client_id or "atoll-client", "aud": ISSUER,
        "client_id": client_id or "atoll-client",
        "iat": now, "nbf": now, "exp": now + 3600, "scope": " ".join(scopes),
    }
    for api in scopes:                       # BCP-003-02 private claims: per-API access rights
        claims[f"x-nmos-{api}"] = {"read": ["*"], "write": ["*"]}
    return jwt.encode(claims, PRIV_PEM, algorithm="RS256", headers={"kid": KID}), now + 3600


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, obj, ctype="application/json"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_GET(self):
        p = urlparse(self.path).path.rstrip("/") or "/"
        if p in ("/.well-known/oauth-authorization-server", "/.well-known/openid-configuration"):
            return self._send(200, METADATA)
        if p == "/jwks" or p == "/certs":
            return self._send(200, JWKS)
        if p in ("/", "/status"):
            return self._send(200, {"service": "atoll IS-10 authorization server", "issuer": ISSUER,
                                    "kid": KID, "scopes": SCOPES,
                                    "endpoints": {"token": "/token", "jwks": "/jwks",
                                                  "metadata": "/.well-known/oauth-authorization-server",
                                                  "register": "/register"}})
        return self._send(404, {"error": "not_found"})

    def do_POST(self):
        p = urlparse(self.path).path.rstrip("/")
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode("utf-8", "replace") if n else ""
        form = {k: v[0] for k, v in parse_qs(body).items()}
        if not form and body:
            try: form = json.loads(body)
            except Exception: form = {}
        if p == "/token":
            grant = form.get("grant_type", "client_credentials")
            if grant not in ("client_credentials", "authorization_code"):
                return self._send(400, {"error": "unsupported_grant_type"})
            tok, exp = issue_token(form.get("client_id"), form.get("scope", ""))
            return self._send(200, {"access_token": tok, "token_type": "Bearer",
                                    "expires_in": 3600, "scope": form.get("scope") or " ".join(SCOPES)})
        if p == "/register":                 # RFC 7591 dynamic client registration (demo-simplified)
            cid = "atoll-" + hashlib.sha256(os.urandom(8)).hexdigest()[:12]
            return self._send(201, {"client_id": cid, "client_secret": _b64u(os.urandom(24)),
                                    "client_id_issued_at": int(time.time()),
                                    "grant_types": ["client_credentials"],
                                    "token_endpoint_auth_method": "none",
                                    "scope": " ".join(SCOPES),
                                    "client_name": form.get("client_name", cid)})
        return self._send(404, {"error": "not_found"})


class Threaded(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True; allow_reuse_address = True

def advertise():
    """DNS-SD _nmos-auth._tcp so NMOS nodes discover the AS (BCP-003-02). avahi-publish-service blocks."""
    try:
        subprocess.Popen(["avahi-publish-service", "atoll-auth", "_nmos-auth._tcp", str(PORT),
                          "api_ver=v1.0", "api_proto=http", "api_auth=false",
                          "pri=100", "oauth_mode=client_credentials"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"  DNS-SD advertise failed: {e}", flush=True)

if __name__ == "__main__":
    advertise()
    print(f"auth-server: IS-10 AS on {ISSUER}  (kid={KID}, token/jwks/.well-known)", flush=True)
    Threaded(("0.0.0.0", PORT), H).serve_forever()
