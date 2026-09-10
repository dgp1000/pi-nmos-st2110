import json, urllib.request, base64, time
AS="http://localhost:8106"
def get(p):
    with urllib.request.urlopen(AS+p, timeout=5) as r: return r.status, json.loads(r.read())
def post(p, data):
    body="&".join(f"{k}={v}" for k,v in data.items()).encode()
    req=urllib.request.Request(AS+p, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r: return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())
def b64u(s):
    s+="="*(-len(s)%4); return json.loads(base64.urlsafe_b64decode(s))
res=[]
def chk(name, ok, detail=""):
    res.append((("PASS" if ok else "FAIL"), name, detail))

# RFC 8414 metadata
st,md=get("/.well-known/oauth-authorization-server")
chk("RFC 8414 metadata endpoint 200", st==200, f"HTTP {st}")
for f in ("issuer","token_endpoint","jwks_uri","response_types_supported","grant_types_supported","token_endpoint_auth_methods_supported"):
    chk(f"metadata has '{f}'", f in md, str(md.get(f))[:60])
chk("metadata advertises client_credentials", "client_credentials" in (md.get("grant_types_supported") or []))
chk("BCP-003-02 scopes advertised", "connection" in (md.get("scopes_supported") or []), ",".join(md.get("scopes_supported") or [])[:60])

# JWKS
st,jwks=get("/jwks")
chk("JWKS endpoint 200", st==200)
keys=jwks.get("keys") or []
k=keys[0] if keys else {}
chk("JWKS has an RSA sig key", k.get("kty")=="RSA" and k.get("use")=="sig", f"alg={k.get('alg')} kid={k.get('kid')}")
chk("JWKS key alg RS256", k.get("alg")=="RS256")
chk("JWKS key has kid", bool(k.get("kid")))

# token via client_credentials
st,tok=post("/token", {"grant_type":"client_credentials","client_id":"conformance","scope":"connection"})
chk("token endpoint 200 (client_credentials)", st==200, f"HTTP {st}")
chk("token_type Bearer", (tok.get("token_type") or "").lower()=="bearer")
chk("expires_in present", "expires_in" in tok, str(tok.get("expires_in")))
jwt=tok.get("access_token","")
parts=jwt.split(".")
chk("access_token is a 3-part JWT", len(parts)==3)
hdr=b64u(parts[0]) if len(parts)==3 else {}
claims=b64u(parts[1]) if len(parts)==3 else {}
chk("JWT header alg RS256", hdr.get("alg")=="RS256", str(hdr))
chk("JWT header kid matches JWKS", hdr.get("kid")==k.get("kid"), f"{hdr.get('kid')} vs {k.get('kid')}")
for c in ("iss","sub","exp","iat","scope"):
    chk(f"JWT claim '{c}'", c in claims, str(claims.get(c))[:40])
chk("JWT iss == metadata issuer", claims.get("iss")==md.get("issuer"), claims.get("iss"))
chk("BCP-003-02 private claim x-nmos-connection", "x-nmos-connection" in claims, str(claims.get("x-nmos-connection"))[:60])
xc=claims.get("x-nmos-connection") or {}
chk("x-nmos-connection has read/write", "read" in xc and "write" in xc, str(xc))
chk("JWT exp in the future (~1h)", (claims.get("exp",0)-time.time())>3000, f"{int(claims.get('exp',0)-time.time())}s")

# RS256 signature verifies against JWKS
try:
    import jwt as pyjwt
    key=pyjwt.PyJWKClient(AS+"/jwks").get_signing_key_from_jwt(jwt).key
    dec=pyjwt.decode(jwt, key, algorithms=["RS256"], options={"verify_aud":False})
    chk("RS256 signature verifies against JWKS", True, "verified")
except Exception as e:
    chk("RS256 signature verifies against JWKS", False, str(e)[:60])

# unsupported grant rejected
st,err=post("/token", {"grant_type":"password"})
chk("unsupported grant -> 400 + error", st==400 and "error" in err, f"HTTP {st} {err.get('error')}")

# dynamic client registration (RFC 7591)
st,reg=post("/register", {"client_name":"conformance"})
chk("RFC 7591 /register issues client_id", st in (200,201) and "client_id" in reg, f"HTTP {st}")

p=sum(1 for r in res if r[0]=="PASS"); f=sum(1 for r in res if r[0]=="FAIL")
print(f"\n=== AS conformance: {p} PASS / {f} FAIL ===")
for state,name,detail in res:
    print(f"  [{state}] {name}" + (f"  -- {detail}" if detail else ""))
