"""
core/imap_extractor.py — SynthTel Standalone Inbox Extractor v14
=================================================================
Extracts From-addresses from any email inbox.

Login cascade (auto-selects based on domain):
  Microsoft (O365/Outlook/Hotmail/any corp tenant):
    1. ROPC silent — tries 6 app client IDs × 3 authorities
    2. Device code — browser popup at microsoft.com/devicelogin
       (works with MFA, ADFS, Conditional Access)
    Then reads via Microsoft Graph API.

  IMAP (Gmail, Yahoo, AOL, iCloud, GoDaddy, Zoho, Fastmail, etc.):
    1. Direct IMAP SSL (port 993)
    2. Auth fail + provider needs app-pw → error with instructions
    3. STARTTLS (port 143) fallback
    4. Alternate hostnames: imap.domain, mail.domain
    5. GoDaddy: explicit imap.secureserver.net probe

Called from server.py POST /api/extract-inbox
"""
import re, ssl, imaplib, socket, subprocess, sys, time, logging, email as _email
from email.header import decode_header as _decode_hdr
from datetime import datetime
from typing import Optional
from collections import Counter

log = logging.getLogger(__name__)

def _ensure(pkg, pip_name=None):
    try: __import__(pkg)
    except ImportError:
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", pip_name or pkg,
                 "-q", "--break-system-packages", "--disable-pip-version-check"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
        except Exception: pass

for _pkg in [("msal","msal"),("requests","requests"),("dns.resolver","dnspython")]:
    try: _ensure(*_pkg)
    except Exception: pass

try: import msal as _msal; _HAS_MSAL = True
except ImportError: _HAS_MSAL = False
try: import requests as _req; _HAS_REQUESTS = True
except ImportError: _HAS_REQUESTS = False
try: import dns.resolver as _dns; _HAS_DNS = True
except ImportError: _HAS_DNS = False

GRAPH = "https://graph.microsoft.com/v1.0"
MS_DOMAINS = frozenset({"outlook.com","hotmail.com","hotmail.co.uk","hotmail.fr","live.com",
    "live.ca","live.co.uk","live.fr","live.com.au","msn.com","passport.com"})
MS_SCOPES = ["https://graph.microsoft.com/Mail.Read","https://graph.microsoft.com/Mail.ReadBasic"]
MS_APPS = [
    ("Microsoft Office",       "d3590ed6-52b3-4102-aeff-aad2292ab01c"),
    ("Microsoft Teams",        "1fec8e78-bce4-4aaf-ab1b-5451cc387264"),
    ("Outlook Mobile",         "27922004-5251-4030-b22d-91ecd9a37ea4"),
    ("Office Portal",          "89bee1f7-5e6e-4d8a-9f3d-ecd601259da7"),
    ("Azure CLI",              "04b07795-8ddb-461a-bbee-02f9e1bf7b46"),
    ("Microsoft Authenticator","4813382a-8fa7-425e-ab75-3b753aab3abb"),
]

PROVIDERS = {
    "gmail.com":      ("Gmail",     "imap.gmail.com",       993, True, "https://myaccount.google.com/apppasswords","Google Account → Security → App Passwords"),
    "googlemail.com": ("Gmail",     "imap.gmail.com",       993, True, "https://myaccount.google.com/apppasswords","Same as Gmail"),
    "yahoo.com":      ("Yahoo",     "imap.mail.yahoo.com",  993, True, "https://login.yahoo.com/account/security","Yahoo → Account Security → Generate App Password → Other App"),
    "ymail.com":      ("Yahoo",     "imap.mail.yahoo.com",  993, True, "https://login.yahoo.com/account/security","Same as Yahoo"),
    "yahoo.co.uk":    ("Yahoo UK",  "imap.mail.yahoo.com",  993, True, "https://login.yahoo.com/account/security","Same as Yahoo"),
    "yahoo.co.jp":    ("Yahoo JP",  "imap.mail.yahoo.com",  993, True, "https://login.yahoo.com/account/security","Same as Yahoo"),
    "yahoo.com.au":   ("Yahoo AU",  "imap.mail.yahoo.com",  993, True, "https://login.yahoo.com/account/security","Same as Yahoo"),
    "aol.com":        ("AOL",       "imap.aol.com",         993, True, "https://login.aol.com/account/security","AOL → Account Security → Generate App Password"),
    "icloud.com":     ("iCloud",    "imap.mail.me.com",     993, True, "https://appleid.apple.com","Apple ID → Sign-In & Security → App-Specific Passwords → + → name it 'Mail'"),
    "me.com":         ("iCloud",    "imap.mail.me.com",     993, True, "https://appleid.apple.com","Same as iCloud"),
    "mac.com":        ("iCloud",    "imap.mail.me.com",     993, True, "https://appleid.apple.com","Same as iCloud"),
    "zoho.com":       ("Zoho",      "imap.zoho.com",        993, False,"",""),
    "zohomail.com":   ("Zoho",      "imap.zoho.com",        993, False,"",""),
    "zoho.eu":        ("Zoho EU",   "imap.zoho.eu",         993, False,"",""),
    "fastmail.com":   ("Fastmail",  "imap.fastmail.com",    993, True, "https://app.fastmail.com/settings/security/devicekeys/new","Fastmail → Settings → Security → App Passwords"),
    "fastmail.fm":    ("Fastmail",  "imap.fastmail.com",    993, True, "https://app.fastmail.com/settings/security/devicekeys/new","Same"),
    "gmx.com":        ("GMX",       "imap.gmx.com",         993, False,"",""),
    "gmx.net":        ("GMX",       "imap.gmx.net",         993, False,"",""),
    "gmx.de":         ("GMX DE",    "imap.gmx.net",         993, False,"",""),
    "web.de":         ("Web.de",    "imap.web.de",          993, False,"",""),
    "protonmail.com": ("ProtonMail","127.0.0.1",            1143,False, "https://account.proton.me/settings#import-export","Requires ProtonMail Bridge running locally"),
    "proton.me":      ("ProtonMail","127.0.0.1",            1143,False, "https://account.proton.me/settings#import-export","Same"),
    "pm.me":          ("ProtonMail","127.0.0.1",            1143,False, "https://account.proton.me/settings#import-export","Same"),
    "outlook.com":    ("Outlook",   "outlook.office365.com",993, False,"",""),
    "hotmail.com":    ("Hotmail",   "outlook.office365.com",993, False,"",""),
    "live.com":       ("Live",      "outlook.office365.com",993, False,"",""),
    "hotmail.co.uk":  ("Hotmail UK","outlook.office365.com",993, False,"",""),
    "secureserver.net":("GoDaddy",  "imap.secureserver.net",993, False,"",""),
}

MX_HINTS = {
    "protection.outlook":"ms","mail.protection.outlook":"ms","outlook.com":"ms",
    "pphosted.com":"ms","microsoft":"ms","messagelabs.com":"ms","mimecast":"ms",
    "barracuda":"ms","eo.outlook.com":"ms",
    "aspmx.l.google":"gmail.com","googlemail.com":"gmail.com","google.com":"gmail.com",
    "yahoodns.net":"yahoo.com","yahoo.com":"yahoo.com",
    "icloud.com":"icloud.com","fastmail":"fastmail.com","zoho.com":"zoho.com",
    "secureserver.net":"secureserver.net","1and1.com":"secureserver.net","ionos.com":"secureserver.net",
}

_GENERIC = [
    r'^noreply',r'^no-reply',r'^no\.reply',r'^donotreply',r'^do-not-reply',
    r'^postmaster',r'^mailer-daemon',r'^bounced?@',r'^daemon@',
    r'^notifications?@',r'^notify@',r'^alerts?@',r'^newsletter',r'^news@',
    r'^updates?@',r'^digest@',r'^automated',r'^auto-',r'^system@',
    r'^service@',r'^feedback@',r'^survey',r'^billing@',r'^receipt',
    r'^invoice@',r'^calendar-notification',r'^noreply-',
    r'^microsoftexchange',r'^microsoft365',r'^msonlineservicesteam',r'^microsoft-noreply',
    r'@.+\.(mailchimp|sendgrid|amazonses|constantcontact|hubspot|salesforce|marketo|mandrillapp|mailgun|campaign-archive|createsend|klaviyo|brevo|sendinblue|mailerlite)\.com$',
]
_generic_re = [re.compile(p, re.I) for p in _GENERIC]
def _is_generic(a): return any(r.search(a) for r in _generic_re)

def _mx(domain):
    if _HAS_DNS:
        try: return [str(r.exchange).lower().rstrip(".") for r in _dns.resolve(domain,"MX")]
        except: pass
    try:
        r = subprocess.run(["nslookup","-type=MX",domain],capture_output=True,text=True,timeout=8)
        return [l.split("=")[-1].strip().rstrip(".") for l in r.stdout.splitlines() if "mail exchanger" in l.lower()]
    except: return []

def _o365_autodiscover(domain):
    if not _HAS_REQUESTS: return False
    for url in [
        f"https://outlook.office365.com/autodiscover/autodiscover.json/v1.0/{domain}?Protocol=Rest",
        f"https://autodiscover.{domain}/autodiscover/autodiscover.json/v1.0/{domain}?Protocol=Rest",
    ]:
        try:
            r = _req.get(url,timeout=5,allow_redirects=False)
            if r.status_code in (200,301,302): return True
        except: continue
    return False

def detect_provider(addr):
    domain = addr.split("@")[-1].lower().strip()
    def _ms(name): return {"type":"ms","name":name,"domain":domain,
        "host":"outlook.office365.com","port":993,"needs_app_pw":False,
        "app_pw_url":"","app_pw_hint":"","is_godaddy":False,"is_google":False}
    def _imap(name,host,port,app_pw=False,url="",hint="",is_gd=False,is_g=False):
        return {"type":"imap","name":name,"domain":domain,"host":host,"port":port,
            "needs_app_pw":app_pw,"app_pw_url":url,"app_pw_hint":hint,"is_godaddy":is_gd,"is_google":is_g}
    if domain in MS_DOMAINS: return _ms(f"Microsoft ({domain})")
    if domain in PROVIDERS:
        n,h,p,app_pw,url,hint = PROVIDERS[domain]
        return _imap(n,h,p,app_pw,url,hint,is_gd="secureserver" in h,is_g="gmail" in h)
    mx = _mx(domain)
    if mx:
        mx_str = " ".join(mx)
        if "secureserver.net" in mx_str:
            return _imap(f"GoDaddy ({domain})","imap.secureserver.net",993,is_gd=True)
        for hint_key,prov_key in MX_HINTS.items():
            if hint_key in mx_str:
                if prov_key=="ms": return _ms(f"Office 365 ({domain})")
                if prov_key in PROVIDERS:
                    n,h,p,app_pw,url,hint = PROVIDERS[prov_key]
                    return _imap(f"{n} ({domain})",h,p,app_pw,url,hint,is_gd="secureserver" in h,is_g="gmail" in h)
    if _o365_autodiscover(domain): return _ms(f"Office 365 ({domain})")
    try:
        s=socket.create_connection(("imap.secureserver.net",993),timeout=4); s.close()
        return _imap(f"GoDaddy ({domain})","imap.secureserver.net",993,is_gd=True)
    except: pass
    for candidate in [f"imap.{domain}",f"mail.{domain}",domain]:
        try:
            s=socket.create_connection((candidate,993),timeout=4); s.close()
            return _imap(f"IMAP ({domain})",candidate,993,is_gd="secureserver" in candidate)
        except: continue
    return {"type":"unknown","name":f"Unknown ({domain})","domain":domain,
        "host":f"imap.{domain}","port":993,"needs_app_pw":False,
        "app_pw_url":"","app_pw_hint":"","is_godaddy":False,"is_google":False}

def ropc_login(email_addr, password):
    if not _HAS_MSAL: return None,"msal not installed"
    domain = email_addr.split("@")[-1].lower()
    authorities = (["https://login.microsoftonline.com/consumers","https://login.microsoftonline.com/common"]
        if domain in MS_DOMAINS else
        ["https://login.microsoftonline.com/organizations",f"https://login.microsoftonline.com/{domain}","https://login.microsoftonline.com/common"])
    scopes = ["https://graph.microsoft.com/.default"]
    last_err = ""
    for auth in authorities:
        for app_name,cid in MS_APPS:
            try:
                app = _msal.PublicClientApplication(cid,authority=auth)
                res = app.acquire_token_by_username_password(username=email_addr,password=password,scopes=scopes)
                if "access_token" in res:
                    h={"Authorization":f"Bearer {res['access_token']}"}
                    t=_req.get(f"{GRAPH}/me/mailFolders/Inbox?$select=totalItemCount",headers=h,timeout=10)
                    if t.status_code==200: return res["access_token"],None
                    last_err=f"{app_name}: no mail scope"; continue
                e=res.get("error_description",res.get("error",""))
                if "AADSTS50126" in e: return None,"wrong_password"
                if "AADSTS50034" in e: return None,"Account not found."
                if "AADSTS50053" in e: return None,"Account locked."
                if "AADSTS50057" in e: return None,"Account disabled."
                if "AADSTS50055" in e: return None,"Password expired."
                if any(x in e for x in ["AADSTS50076","AADSTS50079","AADSTS50158","AADSTS7000112"]): return None,"mfa_required"
                last_err=f"{app_name}: {e[:100]}"
            except Exception as exc:
                s=str(exc)
                if any(x in s for x in ["no element","XML","parsing","parse"]): return None,"federated"
                last_err=f"{app_name}: {s[:80]}"
    return None,f"login_failed: {last_err}"

def start_device_code(email_addr, state):
    if not _HAS_MSAL: return None
    domain=email_addr.split("@")[-1].lower()
    auth=("https://login.microsoftonline.com/consumers" if domain in MS_DOMAINS
          else "https://login.microsoftonline.com/organizations")
    for app_name,cid in MS_APPS[:4]:
        try:
            app=_msal.PublicClientApplication(cid,authority=auth)
            flow=app.initiate_device_flow(scopes=MS_SCOPES)
            if "user_code" not in flow: continue
            state["_device_flow"]=flow; state["_device_app"]=app; state["_device_email"]=email_addr
            return {"url":"https://microsoft.com/devicelogin","code":flow["user_code"],"app":app_name,"ok":True}
        except: continue
    return None

def poll_device_code(state):
    flow=state.get("_device_flow"); app=state.get("_device_app")
    if not flow or not app: return {"ok":False,"error":"No pending device login"}
    res=app.acquire_token_by_device_flow(flow)
    if "access_token" in res:
        tok=res["access_token"]
        h={"Authorization":f"Bearer {tok}"}
        t=_req.get(f"{GRAPH}/me/mailFolders/Inbox?$select=totalItemCount",headers=h,timeout=10)
        if t.ok:
            state["_device_flow"]=None; state["_device_app"]=None
            return {"ok":True,"token":tok,"count":t.json().get("totalItemCount",0)}
    err=res.get("error","")
    if err=="authorization_pending": return {"ok":False,"waiting":True}
    return {"ok":False,"error":res.get("error_description",err)[:120]}

def imap_login(prov, email_addr, password):
    host=prov.get("host",""); port=int(prov.get("port",993)); domain=prov.get("domain","")
    is_godaddy=prov.get("is_godaddy",False)
    ctx=ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE

    def _try_ssl(h,p):
        try:
            m=(imaplib.IMAP4(h,p) if p==1143 else imaplib.IMAP4_SSL(h,p,ssl_context=ctx))
            m.login(email_addr,password); return m,""
        except imaplib.IMAP4.error as exc: return None,str(exc)
        except ssl.SSLError as exc: return None,f"SSL: {exc}"
        except OSError as exc: return None,f"Connect: {exc}"
        except Exception as exc: return None,str(exc)

    def _try_starttls(h,p=143):
        try:
            m=imaplib.IMAP4(h,p); m.starttls(ssl_context=ctx); m.login(email_addr,password); return m,""
        except Exception as exc: return None,str(exc)

    def _is_auth(e):
        return any(x in e.lower() for x in ["authentication","credentials","auth","login failed",
            "authenticationfailed","[auth]","too many login","invalid password","wrong password","invalid credentials"])

    conn,err=_try_ssl(host,port)
    if conn: return conn,""
    if _is_auth(err):
        if prov.get("needs_app_pw"): return None,"needs_app_pw"
        return None,f"auth_failed: {err}"
    conn2,err2=_try_starttls(host)
    if conn2: return conn2,""
    if is_godaddy:
        for gd in ["imap.secureserver.net","imap.1and1.com"]:
            c,_=_try_ssl(gd,993)
            if c: return c,""
    for alt in [f"imap.{domain}",f"mail.{domain}"]:
        if alt==host: continue
        c,_=_try_ssl(alt,993)
        if c: return c,""
        c2,_=_try_starttls(alt)
        if c2: return c2,""
    return None,f"connection_failed: {err2 or err}"

def _parse_from(raw):
    parts=_decode_hdr(raw or ""); s=""
    for p,c in parts:
        s+=(p.decode(c or "utf-8",errors="replace") if isinstance(p,bytes) else p)
    m=re.search(r'<([^>]+@[^>]+)>',s)
    addr=m.group(1).strip().lower() if m else None
    if not addr:
        m2=re.search(r'[\w.+%-]+@[\w.-]+\.\w+',s)
        addr=m2.group(0).strip().lower() if m2 else None
    name=re.sub(r'<[^>]+>','',s).strip().strip('"').strip("'").strip()
    return addr,name

def _build_result(results, total_scanned, prov, server):
    seen={}
    for r in results:
        a=r["addr"]
        if a not in seen or r.get("date","")>seen[a].get("date",""): seen[a]=r
    uniq=sorted(seen.values(),key=lambda x:x.get("date",""),reverse=True)
    dom_ctr={}
    for r in results:
        dom=r["addr"].split("@")[-1] if "@" in r["addr"] else "?"
        dom_ctr[dom]=dom_ctr.get(dom,0)+1
    dom_sorted=dict(sorted(dom_ctr.items(),key=lambda x:-x[1])[:30])
    return {"ok":True,"status":"success","provider":prov.get("name",""),
        "provider_type":prov.get("type",""),"total_scanned":total_scanned,
        "total_inbox":total_scanned,"unique":len(uniq),"results":uniq,"domains":dom_sorted,"server":server}

def extract_inbox_simple(email_addr, password, token=None, limit=1000,
                          filter_generic=True, use_proxy=None, device_state=None):
    prov=detect_provider(email_addr)
    ptype=prov["type"]

    if ptype in ("ms","unknown") or token:
        ms_token=token
        if not ms_token:
            ms_token,err=ropc_login(email_addr,password)
            if not ms_token:
                if err=="wrong_password": return {"ok":False,"error":"Wrong password.","hint":"Double-check your Microsoft password."}
                if err=="mfa_required": return {"ok":False,"error":"MFA required","hint":"Use browser login (device code) to authenticate.","needs_device_code":True}
                if err=="federated": return {"ok":False,"error":"Federated login","hint":"This account uses ADFS/SSO. Use browser login.","needs_device_code":True}
                if ptype!="unknown": return {"ok":False,"error":err,"hint":"Try browser login if password login is blocked."}
        if ms_token:
            return _extract_graph(ms_token,prov,limit,filter_generic)

    conn,err=imap_login(prov,email_addr,password)
    if not conn and ptype=="unknown":
        for probe_host in [f"imap.{prov['domain']}",f"mail.{prov['domain']}"]:
            probe_prov=dict(prov,host=probe_host)
            conn,_=imap_login(probe_prov,email_addr,password)
            if conn: prov=probe_prov; break

    if not conn:
        if "needs_app_pw" in err:
            return {"ok":False,"error":"App Password required",
                "hint":prov.get("app_pw_hint") or "This provider requires an App Password, not your regular password.",
                "needs_app_pw":True,"app_pw_url":prov.get("app_pw_url",""),"app_pw_hint":prov.get("app_pw_hint","")}
        return {"ok":False,"error":err or "Could not connect",
            "hint":"Check that IMAP is enabled in your email settings. For Gmail/Yahoo/iCloud, use an App Password."}

    return _extract_imap(conn,prov,limit,filter_generic)

def _extract_graph(token, prov, limit, filter_generic):
    h={"Authorization":f"Bearer {token}"}
    results=[]; done=0; cap=limit or 999999
    url=f"{GRAPH}/me/mailFolders/Inbox/messages"
    params={"$select":"from,receivedDateTime,subject,hasAttachments","$orderby":"receivedDateTime desc","$top":100}
    while url and done<cap:
        try:
            r=_req.get(url,headers=h,params=params,timeout=30)
            if not r.ok: break
            data=r.json(); msgs=data.get("value",[])
            if not msgs: break
            for m in msgs:
                if done>=cap: break
                done+=1
                frm=(m.get("from") or {}).get("emailAddress") or {}
                addr=(frm.get("address") or "").strip().lower()
                name=(frm.get("name") or "").strip()
                if not addr: continue
                if filter_generic and _is_generic(addr): continue
                dt=(m.get("receivedDateTime") or "")[:19].replace("T"," ")
                subj=(m.get("subject") or "")[:80]
                results.append({"addr":addr,"name":name,"date":dt,"subject":subj})
            url=data.get("@odata.nextLink"); params={}
        except Exception: break
    return _build_result(results,done,prov,"Graph")

def _extract_imap(conn, prov, limit, filter_generic):
    try:
        st,d=conn.select("INBOX",readonly=True)
        if st!="OK": return {"ok":False,"error":"Could not open INBOX"}
        total_inbox=int(d[0])
        st,msgs=conn.search(None,"ALL")
        if st!="OK": return {"ok":False,"error":"IMAP search failed"}
        ids=msgs[0].split()
        if limit and limit<len(ids): ids=ids[-limit:]
        results=[]; CHUNK=50
        for i in range(0,len(ids),CHUNK):
            chunk=ids[i:i+CHUNK]; id_str=b",".join(chunk).decode()
            try:
                st2,data=conn.fetch(id_str,"(BODY.PEEK[HEADER.FIELDS (FROM DATE SUBJECT)] BODYSTRUCTURE)")
                if st2!="OK" or not data: continue
            except Exception: continue
            for part in data:
                if isinstance(part,tuple):
                    raw=part[1] if isinstance(part[1],bytes) else b""
                    try:
                        msg=_email.message_from_bytes(raw)
                        addr,name=_parse_from(msg.get("From",""))
                        if not addr: continue
                        if filter_generic and _is_generic(addr): continue
                        results.append({"addr":addr,"name":name or "","date":(msg.get("Date") or "")[:30],"subject":(msg.get("Subject") or "")[:80]})
                    except Exception: pass
        try: conn.close(); conn.logout()
        except Exception: pass
        return _build_result(results,total_inbox,prov,f"{prov.get('host','')}:{prov.get('port',993)}")
    except Exception as exc:
        return {"ok":False,"error":str(exc)[:200]}


# ── Backwards-compat alias ──────────────────────────────────────────
# core/server.py imports this name; the canonical implementation is
# extract_inbox_simple above.
extract_from_inbox = extract_inbox_simple


# ═══════════════════════════════════════════════════════════════════
# LETTER RIPPER  —  pull full HTML bodies out of a mailbox
# ═══════════════════════════════════════════════════════════════════
# Two endpoints' worth of helpers:
#   list_letters()  → metadata (subject/from/date/size) for the most
#                     recent N messages (or matching a search query).
#   fetch_letter()  → full HTML body (and plain fallback) for one msg.
#   parse_eml_raw() → take a pasted .eml / RFC822 string and pull HTML
#                     out without ever touching IMAP.
# All three return {ok, ..., error?, hint?} in the same shape as the
# existing extract_inbox_simple() helper.

def _decode_header_value(raw) -> str:
    if not raw:
        return ""
    try:
        parts = _decode_hdr(raw)
    except Exception:
        return str(raw)
    out = ""
    for p, c in parts:
        if isinstance(p, bytes):
            try: out += p.decode(c or "utf-8", errors="replace")
            except Exception: out += p.decode("utf-8", errors="replace")
        else:
            out += p
    return out


def _walk_for_html(msg):
    """Return (html_str, plain_str) — best-effort extraction.
    Prefers the highest-quality text/html part, falls back to text/plain.
    """
    html_part  = None
    plain_part = None
    try:
        if msg.is_multipart():
            for part in msg.walk():
                ctype = (part.get_content_type() or "").lower()
                disp  = (part.get("Content-Disposition") or "").lower()
                if "attachment" in disp:
                    continue
                if ctype == "text/html" and html_part is None:
                    html_part = part
                elif ctype == "text/plain" and plain_part is None:
                    plain_part = part
        else:
            ctype = (msg.get_content_type() or "").lower()
            if ctype == "text/html":
                html_part = msg
            elif ctype == "text/plain":
                plain_part = msg
    except Exception:
        pass

    def _decode(part):
        if part is None:
            return ""
        try:
            payload = part.get_payload(decode=True)
            if payload is None:
                return ""
            charset = part.get_content_charset() or "utf-8"
            try: return payload.decode(charset, errors="replace")
            except (LookupError, UnicodeDecodeError):
                return payload.decode("utf-8", errors="replace")
        except Exception:
            return ""

    return _decode(html_part), _decode(plain_part)


def list_letters(email_addr, password, token=None, limit=50,
                 search="", folder="INBOX"):
    """List recent messages with metadata for selection in the UI.

    Returns:
        {ok: True, letters: [{id, from, fromName, subject, date, size}], total}
        OR
        {ok: False, error, hint?, needs_app_pw?, needs_device_code?}
    """
    prov  = detect_provider(email_addr)
    ptype = prov["type"]
    limit = max(1, min(int(limit or 50), 200))

    # Microsoft Graph path (OAuth) — used when we have a token already
    # or when the provider is MS-flavoured.
    if (ptype in ("ms", "unknown") and token) or (ptype == "ms" and token):
        try:
            import urllib.parse as _up
            h = {"Authorization": f"Bearer {token}"}
            qs = {"$select": "id,from,subject,receivedDateTime,bodyPreview",
                  "$orderby": "receivedDateTime desc",
                  "$top":     limit}
            if search:
                qs["$search"] = f'"{search}"'
            url = f"{GRAPH}/me/mailFolders/Inbox/messages?" + _up.urlencode(qs)
            r = _req.get(url, headers=h, timeout=30)
            if not r.ok:
                return {"ok": False, "error": f"Graph {r.status_code}: {r.text[:200]}"}
            data = r.json()
            msgs = data.get("value", [])
            letters = []
            for m in msgs:
                frm = (m.get("from") or {}).get("emailAddress") or {}
                letters.append({
                    "id":       m.get("id"),
                    "from":     (frm.get("address") or "").lower(),
                    "fromName": frm.get("name") or "",
                    "subject":  m.get("subject") or "",
                    "date":     (m.get("receivedDateTime") or "")[:19].replace("T", " "),
                    "snippet":  (m.get("bodyPreview") or "")[:140],
                    "source":   "graph",
                })
            return {"ok": True, "letters": letters, "total": len(letters), "source": "graph"}
        except Exception as e:
            return {"ok": False, "error": f"Graph error: {e}"}

    # IMAP path
    conn, err = imap_login(prov, email_addr, password)
    if not conn and ptype == "unknown":
        for probe_host in [f"imap.{prov['domain']}", f"mail.{prov['domain']}"]:
            probe_prov = dict(prov, host=probe_host)
            conn, _ = imap_login(probe_prov, email_addr, password)
            if conn:
                prov = probe_prov; break
    if not conn:
        if "needs_app_pw" in (err or ""):
            return {"ok": False, "error": "App Password required",
                    "hint": prov.get("app_pw_hint") or "Use an App Password.",
                    "needs_app_pw": True,
                    "app_pw_url":  prov.get("app_pw_url", ""),
                    "app_pw_hint": prov.get("app_pw_hint", "")}
        return {"ok": False, "error": err or "Could not connect",
                "hint": "Check IMAP is enabled.  For Gmail/Yahoo/iCloud use an App Password."}

    try:
        st, _ = conn.select(folder, readonly=True)
        if st != "OK":
            return {"ok": False, "error": f"Could not open {folder}"}
        if search:
            # IMAP SEARCH on multiple fields — combine with OR.
            crit = f'(OR OR SUBJECT "{search}" FROM "{search}" BODY "{search}")'
            st, msgs = conn.search(None, crit)
        else:
            st, msgs = conn.search(None, "ALL")
        if st != "OK":
            return {"ok": False, "error": "IMAP search failed"}
        ids = msgs[0].split()
        if limit and limit < len(ids):
            ids = ids[-limit:]   # most recent N
        ids = list(reversed(ids))   # newest first
        letters = []
        if not ids:
            try: conn.close(); conn.logout()
            except Exception: pass
            return {"ok": True, "letters": [], "total": 0}
        id_str = b",".join(ids).decode()
        try:
            st2, data = conn.fetch(id_str,
                "(BODY.PEEK[HEADER.FIELDS (FROM DATE SUBJECT)] RFC822.SIZE)")
        except Exception as e:
            try: conn.close(); conn.logout()
            except Exception: pass
            return {"ok": False, "error": f"IMAP fetch failed: {e}"}
        # Pair UID + headers — IMAP returns interleaved tuples.
        i_iter = iter(ids)
        for part in (data or []):
            if not isinstance(part, tuple):
                continue
            try:
                seq_id = next(i_iter).decode()
            except StopIteration:
                seq_id = ""
            raw = part[1] if isinstance(part[1], bytes) else b""
            try:
                msg = _email.message_from_bytes(raw)
            except Exception:
                continue
            addr, name = _parse_from(msg.get("From", ""))
            letters.append({
                "id":       seq_id,
                "from":     addr or "",
                "fromName": name or "",
                "subject":  _decode_header_value(msg.get("Subject", ""))[:200],
                "date":     (msg.get("Date") or "")[:30],
                "snippet":  "",
                "source":   "imap",
            })
        try: conn.close(); conn.logout()
        except Exception: pass
        return {"ok": True, "letters": letters, "total": len(letters), "source": "imap"}
    except Exception as e:
        try: conn.close(); conn.logout()
        except Exception: pass
        return {"ok": False, "error": str(e)[:200]}


def fetch_letter(email_addr, password, token=None, msg_id="", folder="INBOX"):
    """Fetch one message and return its HTML body + headers.

    Returns:
        {ok: True, html, plain, subject, from, fromName, date, size}
        OR
        {ok: False, error, hint?}
    """
    if not msg_id:
        return {"ok": False, "error": "msg_id required"}
    prov  = detect_provider(email_addr)
    ptype = prov["type"]

    if (ptype in ("ms", "unknown") and token) or (ptype == "ms" and token):
        try:
            h = {"Authorization": f"Bearer {token}"}
            url = f"{GRAPH}/me/messages/{msg_id}?$select=subject,from,receivedDateTime,body,bodyPreview"
            r = _req.get(url, headers=h, timeout=30)
            if not r.ok:
                return {"ok": False, "error": f"Graph {r.status_code}: {r.text[:200]}"}
            m = r.json()
            body  = m.get("body") or {}
            ctype = (body.get("contentType") or "").lower()
            content = body.get("content") or ""
            html  = content if ctype == "html" else ""
            plain = content if ctype != "html" else (m.get("bodyPreview") or "")
            frm   = (m.get("from") or {}).get("emailAddress") or {}
            return {
                "ok":       True,
                "html":     html,
                "plain":    plain,
                "subject":  m.get("subject") or "",
                "from":     (frm.get("address") or "").lower(),
                "fromName": frm.get("name") or "",
                "date":     (m.get("receivedDateTime") or "")[:19].replace("T", " "),
                "size":     len(content),
            }
        except Exception as e:
            return {"ok": False, "error": f"Graph error: {e}"}

    conn, err = imap_login(prov, email_addr, password)
    if not conn:
        return {"ok": False, "error": err or "IMAP login failed"}
    try:
        st, _ = conn.select(folder, readonly=True)
        if st != "OK":
            return {"ok": False, "error": f"Could not open {folder}"}
        st2, data = conn.fetch(msg_id.encode() if isinstance(msg_id, str) else msg_id,
                                "(RFC822)")
        if st2 != "OK" or not data:
            return {"ok": False, "error": "Message not found"}
        raw = b""
        for part in data:
            if isinstance(part, tuple) and isinstance(part[1], bytes):
                raw = part[1]; break
        try: conn.close(); conn.logout()
        except Exception: pass
        if not raw:
            return {"ok": False, "error": "Empty message body"}
        return parse_eml_raw(raw)
    except Exception as e:
        try: conn.close(); conn.logout()
        except Exception: pass
        return {"ok": False, "error": str(e)[:200]}


def parse_eml_raw(raw):
    """Parse a raw RFC822 / .eml byte-string or text and extract HTML."""
    if isinstance(raw, str):
        raw_bytes = raw.encode("utf-8", errors="replace")
    else:
        raw_bytes = raw
    try:
        msg = _email.message_from_bytes(raw_bytes)
    except Exception as e:
        return {"ok": False, "error": f"Could not parse RFC822: {e}"}
    html, plain = _walk_for_html(msg)
    addr, name  = _parse_from(msg.get("From", ""))
    subject     = _decode_header_value(msg.get("Subject", ""))
    return {
        "ok":       True,
        "html":     html or "",
        "plain":    plain or "",
        "subject":  subject or "",
        "from":     addr or "",
        "fromName": name or "",
        "date":     (msg.get("Date") or "")[:30],
        "size":     len(raw_bytes),
    }
