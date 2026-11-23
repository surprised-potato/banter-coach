#!/usr/bin/env python3
"""Banter Coach server: sits between the public tunnel and LM Studio.
Judges replies, plays the match in rallies, generates fresh texts, and keeps
name+PIN profiles with progress and attempt history in SQLite (banter.db).
Run: python3 coach_proxy.py   (env: LMS_URL, MODEL, PORT, ORIGINS, DB)
"""
import difflib, hashlib, hmac, json, os, re, secrets, sqlite3, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen

HERE = os.path.dirname(os.path.abspath(__file__))
LMS = os.environ.get("LMS_URL", "http://127.0.0.1:1234/v1/chat/completions")
MODEL = os.environ.get("MODEL", "google/gemma-4-26b-a4b-qat")
PORT = int(os.environ.get("PORT", "8787"))
ORIGINS = [o for o in os.environ.get("ORIGINS", "*").split(",") if o]
RATE = int(os.environ.get("RATE", "30"))      # LLM requests / minute / IP
AUTH_RATE = 5                                 # signup+login / minute / IP
DB_PATH = os.environ.get("DB", os.path.join(HERE, "banter.db"))
MAXLEN = 400
FLAGS = ["agreeing", "interviewing", "advice", "too literal", "thirsty", "try-hard", "pun only", "clunky", "bailed", "borrowed"]
NEED = 3  # passes per stage before its rally

COACH = """You are a blunt, uncompromising Flirting & Banter Coach training a user to turn friendly texts into playful, flirtatious banter. No participation trophies: if a reply is flat, safe, try-hard, needy, overly logical, a literal answer, an interview question, unsolicited advice, or a dead-end agreement, say so and explain why it would flop in a real text thread. Only call a line a Pass when it is genuinely sharp.
Flirting is a dial, not a switch: low-pressure playful tension, teasing, exaggeration, mock judgment, role reversal, misdirection, co-conspirator framing. Never pickup lines, forced jokes, premature heavy compliments, asking for photos, or inviting yourself over (Dead End: thirsty, stranger-danger). Always respect the other person's comfort and consent; a good line is one the recipient would enjoy.
Verdicts: "Pass" (sharp, calibrated, gives them something to bounce off), "Weak" (right instinct, flat or clunky or hedged), "Dead End" (agrees, interviews, advises, bails, thirsty, or kills the tension).
Stage goals: 1 Pivot = stop agreeing/advising/interviewing, tease or exaggerate. 2 Frame = create 'us' energy with hypothetical scenarios, playful compatibility tests, specific vibe-based compliments. 3 Close = turn banter into a confident, low-pressure invitation to meet, not hesitant or over-eager.
Calibration: be strict but fair. Absurd exaggeration, playful accusation, mock judgment, or co-conspirator framing that stays low-pressure and gives the recipient something to bounce off is a PASS (score 75-95). Do not mark a Pass down for being a mild stereotype or cliche if it is playful and warm. Weak = 35-60, Dead End = 0-30. Do not hedge to the middle.
Examples (stage 1):
THEM: "just surviving on iced coffee and spreadsheets lol" / USER: "Try not to ice the spreadsheets" -> Weak 40 (dad-joke pun, nothing to bounce off).
THEM: same / USER: "Iced coffee? Youre making the adderall users quake in their boots" -> Pass 85 (bold absurd exaggeration, playful).
THEM: "rot on my couch and rewatch The Office" / USER: "Careful, youre making the party people think twice about their lifestyle choices" -> Pass 80 (playful flip of their lazy night into a flex).
THEM: "kitchen looks like a crime scene from homemade pasta" / USER: "send pics and i might come over and investigate" -> Dead End 15 (thirsty, invites self over, stranger-danger).
THEM: same / USER: "Whoa, you sure i wont be accused as an accomplice if the cops see it?" -> Pass 85 (keeps the bit, co-conspirator framing, low-pressure).
THEM: "debating if cereal counts as a real lunch" / USER: "Serves better as dinner honestly" -> Weak 40 (agrees, coworker-in-Slack energy).
THEM: same / USER: "What kind of cereal?" -> Dead End 10 (interview question).
Respond ONLY with JSON: {"verdict": "Pass|Weak|Dead End", "score": 0-100, "diagnosis": "2-3 sentences on how the recipient would actually react, naming weak words", "rewrites": [up to 2 sharper alternatives, empty if Pass], "flags": [any of "agreeing","interviewing","advice","too literal","thirsty","try-hard","pun only","clunky","bailed"]}"""

MATCH = """You play the person the user is texting, in a casual dating-app text thread. Reply in 1-2 short, natural lowercase-ish sentences, matching the energy of the user's last text: if it was playful, play back and escalate slightly; if it was flat or interview-like, reply flat and short. Never be hostile. Output ONLY the text message, nothing else."""

GEN = """You write ONE realistic incoming text message from a dating-app match, for a banter training game.
Stage %d (%s). The text should naturally tempt the player into this mistake: %s.
Make it casual, specific and a bit funny, 6-25 words, lowercase-ish like a real text, with a concrete detail (food, show, pet, chore, plan...).
It must fit the stage. Example for this stage: %s
Do not repeat or closely paraphrase any of these: %s
Also write "context": ONE short sentence in second person setting the scene from the player's side (how long you've been texting, what the player just said or asked), e.g. "You matched yesterday and asked what they're up to tonight." It must make the text make sense.
Respond ONLY with JSON {"context": "...", "text": "..."}"""
STAGE_NAMES = {1: "The Pivot: they mention something mundane", 2: "The Frame: they share a preference or plan the player could build a shared scenario around", 3: "The Close: banter has peaked and the player should suggest meeting up"}
GEN_EX = {1: '{"context": "You matched yesterday and asked how their day is going.", "text": "just survived a 3 hour meeting that could have been an email, send snacks"}',
          2: '{"context": "A few days of good banter. You asked about their ideal weekend.", "text": "honestly a farmers market, way too many pastries, then a nap. i take naps very seriously"}',
          3: '{"context": "You have texted daily for a week and the banter has been great, but you have not met.", "text": "ok this new ramen place opened near me and i refuse to go alone like a loser"}'}
TRAPS = {"interviewing": "asking a boring follow-up question", "advice": "giving practical advice", "agreeing": "just agreeing or saying same",
         "thirsty": "escalating too fast or inviting themselves over", "too literal": "answering literally", "try-hard": "a forced joke or pickup line",
         "pun only": "a weak pun", "clunky": "an overcomplicated joke", "bailed": "freezing and not replying", "hesitant": "a hesitant, over-qualified invitation"}

# ---------------- db ----------------
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
dbl = threading.Lock()
with dbl:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT UNIQUE COLLATE NOCASE, salt BLOB, pin_hash BLOB, created REAL);
    CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user_id INTEGER, created REAL);
    CREATE TABLE IF NOT EXISTS attempts(id INTEGER PRIMARY KEY, user_id INTEGER, ts REAL, stage INTEGER, scenario_key TEXT, source TEXT,
        them_text TEXT, reply TEXT, verdict TEXT, score INTEGER, flags TEXT, diagnosis TEXT, rally_id TEXT);
    CREATE INDEX IF NOT EXISTS att_user ON attempts(user_id, ts);
    CREATE TABLE IF NOT EXISTS progress(user_id INTEGER PRIMARY KEY, json TEXT);
    """)
    db.commit()

def q(sql, args=(), one=False, write=False):
    with dbl:
        cur = db.execute(sql, args)
        if write:
            db.commit(); return cur.lastrowid
        rows = cur.fetchall()
    return (rows[0] if rows else None) if one else rows

def new_progress():
    return {"xp": 0, "streak": 0, "best": 0, "tries": 0, "passes": 0, "passed": {"1": [], "2": [], "3": []}, "rally": {}}

def get_progress(uid):
    r = q("SELECT json FROM progress WHERE user_id=?", (uid,), one=True)
    return json.loads(r["json"]) if r else new_progress()

def set_progress(uid, p):
    q("INSERT OR REPLACE INTO progress(user_id, json) VALUES(?,?)", (uid, json.dumps(p)), write=True)

def unlocked(p, n):
    return n == 1 or bool(p["rally"].get(str(n - 1)))

def apply_attempt(p, stage, key, verdict):
    """Same rules as the client's guest mode."""
    p["tries"] += 1
    if verdict == "Pass":
        p["passes"] += 1; p["streak"] += 1; p["best"] = max(p["best"], p["streak"])
        p["xp"] += 10 + min(p["streak"], 5) * 2
        lst = p["passed"].setdefault(str(stage), [])
        if key and key not in lst: lst.append(key)
    else:
        p["streak"] = 0; p["xp"] += 2

def hash_pin(pin, salt):
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, 200_000)

# ---------------- rate limits ----------------
hits, ahits, bad = {}, {}, {}
rl = threading.Lock()
def limited(store, key, n, window=60):
    now = time.time()
    with rl:
        h = [t for t in store.get(key, []) if now - t < window]
        h.append(now); store[key] = h
        return len(h) > n

# ---------------- llm ----------------
def llm(messages, schema=None, temp=0.2, max_tokens=700):
    body = {"model": MODEL, "messages": messages, "temperature": temp, "max_tokens": max_tokens, "reasoning_effort": "none"}
    if schema:
        body["response_format"] = {"type": "json_schema", "json_schema": {"name": "r", "strict": True, "schema": schema}}
    req = Request(LMS, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urlopen(req, timeout=120) as r:
        return json.load(r)["choices"][0]["message"]["content"]

SCHEMA = {"type": "object", "additionalProperties": False,
  "required": ["verdict", "score", "diagnosis", "rewrites", "flags"],
  "properties": {"verdict": {"type": "string", "enum": ["Pass", "Weak", "Dead End"]},
    "score": {"type": "integer"}, "diagnosis": {"type": "string"},
    "rewrites": {"type": "array", "items": {"type": "string"}},
    "flags": {"type": "array", "items": {"type": "string", "enum": FLAGS}}}}
GSCHEMA = {"type": "object", "additionalProperties": False, "required": ["context", "text"], "properties": {"context": {"type": "string"}, "text": {"type": "string"}}}

def clean(s, n=MAXLEN):
    return re.sub(r"\s+", " ", str(s))[:n].strip()

SKIP = re.compile(r"^\W*(next|skip|idk|pass|no|n/?a|\?+|\.+)\W*$", re.I)

def judge_llm(d):
    reply = clean(d.get("reply", ""))
    if SKIP.match(reply) or len(reply) < 3:
        return {"verdict": "Dead End", "score": 0, "flags": ["bailed"], "rewrites": [],
                "diagnosis": "You skipped the rep. If you bail on a tough prompt here, you'll freeze when it's live on your phone. Give an actual reply."}
    stage = int(d.get("stage", 1))
    thread = [("THEM: " if m.get("who") == "them" else "USER: ") + clean(m.get("t", ""))
              for m in d.get("thread", [])[-8:] if isinstance(m, dict) and clean(m.get("t", ""))]
    ctx = clean(d.get("context", ""), 300)
    prompt = (("Situation: %s\n" % ctx if ctx else "") + "Stage %d.\nThread so far (oldest first):\n%s\n\nJudge ONLY the user's LAST text: %s" % (stage, "\n".join(thread), reply))
    shown = [clean(x, 200) for x in (d.get("help_shown") or [])[:6] if isinstance(x, str)]
    if shown:
        prompt += "\nThe user was shown these coach example lines before replying: %s. If their reply copies or only lightly rewords one, the verdict is at most Weak: tell them to stop borrowing your lines and find their own angle." % json.dumps(shown)
    if d.get("rally"):
        prompt += "\nThis is a multi-turn rally: judge it against the whole thread so far, and whether the user is sustaining the bit without retreating to safe questions."
    j = json.loads(llm([{"role": "system", "content": COACH}, {"role": "user", "content": prompt}], SCHEMA))
    j["score"] = max(0, min(100, int(j["score"])))
    j["rewrites"] = [clean(r, 200) for r in j["rewrites"][:2]] if j["verdict"] != "Pass" else []
    j["flags"] = sorted(set(f for f in j["flags"] if f in FLAGS))
    norm = lambda x: re.sub(r"[^a-z0-9 ]", "", x.lower())
    if shown and max(difflib.SequenceMatcher(None, norm(reply), norm(x)).ratio() for x in shown) >= 0.7:
        if j["verdict"] == "Pass":
            j.update(verdict="Weak", score=min(j["score"], 55), rewrites=[])
        j["diagnosis"] = "That's my line, not yours. Borrowing the coach's example won't help when it's live on your phone. Find your own angle on the same idea. " + j["diagnosis"]
        j["flags"] = sorted(set(j["flags"]) | {"borrowed"})
    return j

def reply_llm(d):
    ctx = clean(d.get("context", ""), 300)
    msgs = [{"role": "system", "content": MATCH + ("\nSituation (from the user's side): " + ctx if ctx else "")}]
    for m in d.get("thread", [])[-8:]:
        msgs.append({"role": "assistant" if m.get("who") == "them" else "user", "content": clean(m.get("t", ""))})
    return {"text": clean(llm(msgs, None, 0.9, 150), 300)}

HELP = """You are the banter coach giving a hint BEFORE the player replies. Stage %d goal: %s
Give: "tip": one blunt sentence on what to avoid and what angle to take for THIS text.
"examples": 2-3 sharp example replies, each with "technique" (2-4 words, e.g. "Mock judgment", "Absurd exaggeration", "Co-conspirator framing", "Hypothetical us", "Low-pressure invite") and "line" (the reply text, short, natural texting style). They must fit the situation and the last message, stay playful and low-pressure, and never be thirsty.
Respond ONLY with JSON."""
STAGE_GOALS = {1: "stop agreeing, advising or interviewing; tease, exaggerate, mock-judge, flip the script",
               2: "create 'us' energy: hypothetical scenarios with both of you, playful compatibility tests, specific vibe-based compliments",
               3: "turn the banter into a confident, specific, low-pressure invitation to meet"}
HSCHEMA = {"type": "object", "additionalProperties": False, "required": ["tip", "examples"], "properties": {
    "tip": {"type": "string"},
    "examples": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["technique", "line"],
        "properties": {"technique": {"type": "string"}, "line": {"type": "string"}}}}}}

def help_llm(d):
    stage = int(d.get("stage", 1)); stage = stage if stage in (1, 2, 3) else 1
    ctx = clean(d.get("context", ""), 300)
    thread = [("THEM: " if m.get("who") == "them" else "USER: ") + clean(m.get("t", ""))
              for m in d.get("thread", [])[-8:] if isinstance(m, dict) and clean(m.get("t", ""))]
    prompt = ("Situation: %s\n" % ctx if ctx else "") + "Thread (oldest first):\n" + "\n".join(thread) + "\nGive the hint for the user's next reply."
    j = json.loads(llm([{"role": "system", "content": HELP % (stage, STAGE_GOALS[stage])}, {"role": "user", "content": prompt}], HSCHEMA, 0.8, 500))
    return {"tip": clean(j["tip"], 300), "examples": [{"technique": clean(e["technique"], 40), "line": clean(e["line"], 200)} for e in j["examples"][:3]]}

recent_gen = []  # global recent generated texts, to avoid repeats
def generate(d, uid):
    stage = int(d.get("stage", 1)); stage = stage if stage in (1, 2, 3) else 1
    trap = d.get("trap") if d.get("trap") in TRAPS else ("hesitant" if stage == 3 else "interviewing")
    seen = list(recent_gen[-15:])
    if uid:
        seen += [r["them_text"] for r in q("SELECT them_text FROM attempts WHERE user_id=? AND source='gen' ORDER BY ts DESC LIMIT 15", (uid,))]
    for _ in range(3):
        g = json.loads(llm([{"role": "user", "content": GEN % (stage, STAGE_NAMES[stage], TRAPS[trap], GEN_EX[stage], json.dumps(seen[-25:]))}], GSCHEMA, 1.0, 300))
        t, cx = clean(g["text"], 220), clean(g["context"], 200)
        if 8 <= len(t) <= 200 and t not in seen:
            recent_gen.append(t); del recent_gen[:-50]
            return {"text": t, "context": cx, "trap": trap, "key": "gen:" + hashlib.sha1(t.encode()).hexdigest()[:10]}
    raise ValueError("generation failed")

# ---------------- handlers ----------------
def user_from(h):
    a = h.headers.get("Authorization", "")
    if not a.startswith("Bearer "): return None
    r = q("SELECT u.id, u.name FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=?", (a[7:].strip(),), one=True)
    return dict(r) if r else None

def history(uid, limit):
    rows = q("SELECT ts, stage, scenario_key, source, them_text, reply, verdict, score, flags, diagnosis, rally_id FROM attempts WHERE user_id=? ORDER BY ts DESC LIMIT ?", (uid, limit))
    return [dict(r, flags=json.loads(r["flags"] or "[]")) for r in rows]

def signup_login(d, ip, signup):
    if limited(ahits, ip, AUTH_RATE): return 429, {"error": "too many tries, wait a minute"}
    name = clean(d.get("name", ""), 24); pin = str(d.get("pin", ""))
    if not re.fullmatch(r"[A-Za-z0-9_ .-]{2,24}", name): return 400, {"error": "name: 2-24 letters, numbers, spaces"}
    if not re.fullmatch(r"\d{4,6}", pin): return 400, {"error": "PIN must be 4-6 digits"}
    k = name.lower()
    if signup:
        salt = secrets.token_bytes(16)
        try:
            uid = q("INSERT INTO users(name, salt, pin_hash, created) VALUES(?,?,?,?)", (name, salt, hash_pin(pin, salt), time.time()), write=True)
        except sqlite3.IntegrityError:
            return 409, {"error": "name taken"}
    else:
        with rl:
            b = [t for t in bad.get(k, []) if time.time() - t < 600]; bad[k] = b
        if len(b) >= 10: return 429, {"error": "too many wrong PINs, try again in 10 minutes"}
        r = q("SELECT id, name, salt, pin_hash FROM users WHERE name=?", (name,), one=True)
        if not r or not hmac.compare_digest(hash_pin(pin, r["salt"]), r["pin_hash"]):
            with rl: bad.setdefault(k, []).append(time.time())
            return 401, {"error": "wrong name or PIN"}
        uid, name = r["id"], r["name"]
    tok = secrets.token_urlsafe(32)
    q("INSERT INTO sessions(token, user_id, created) VALUES(?,?,?)", (tok, uid, time.time()), write=True)
    return 200, {"token": tok, "name": name, "progress": get_progress(uid)}

class H(BaseHTTPRequestHandler):
    def _cors(self):
        o = self.headers.get("Origin", "")
        ok = "*" in ORIGINS or o in ORIGINS
        self.send_header("Access-Control-Allow-Origin", (o or "*") if ok else "null")
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self._cors()
        self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)
    def _ip(self):
        return self.headers.get("CF-Connecting-IP") or self.client_address[0]
    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.end_headers()
    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/health": return self._send(200, {"ok": True, "model": MODEL})
        user = user_from(self)
        if u.path in ("/me", "/history") and not user: return self._send(401, {"error": "log in again"})
        if u.path == "/me":
            return self._send(200, {"name": user["name"], "progress": get_progress(user["id"]), "attempts": history(user["id"], 200)})
        if u.path == "/history":
            n = int((parse_qs(u.query).get("limit") or ["50"])[0])
            return self._send(200, {"attempts": history(user["id"], max(1, min(n, 500)))})
        self._send(404, {"error": "not found"})
    def do_POST(self):
        p = self.path
        if p not in ("/judge", "/reply", "/generate", "/help", "/signup", "/login", "/logout", "/rally_result"):
            return self._send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n > 8000: return self._send(413, {"error": "too big"})
            d = json.loads(self.rfile.read(n) or b"{}")
            if p in ("/signup", "/login"): return self._send(*signup_login(d, self._ip(), p == "/signup"))
            user = user_from(self)
            if p == "/logout":
                q("DELETE FROM sessions WHERE token=?", (self.headers.get("Authorization", "")[7:],), write=True)
                return self._send(200, {"ok": True})
            if p == "/rally_result": return self._send(*rally_result(d, user))
            if limited(hits, self._ip(), RATE): return self._send(429, {"error": "slow down"})
            if p == "/reply": return self._send(200, reply_llm(d))
            if p == "/help": return self._send(200, help_llm(d))
            if p == "/generate": return self._send(200, generate(d, user and user["id"]))
            j = judge_llm(d)
            if user:
                stage = int(d.get("stage", 1)); key = clean(d.get("key", ""), 40)
                prog = get_progress(user["id"])
                if not unlocked(prog, stage): return self._send(403, {"error": "stage locked"})
                them = next((clean(m.get("t", "")) for m in reversed(d.get("thread", [])) if isinstance(m, dict) and m.get("who") == "them"), "")
                rid = clean(d.get("rally_id", ""), 40) or None
                q("INSERT INTO attempts(user_id, ts, stage, scenario_key, source, them_text, reply, verdict, score, flags, diagnosis, rally_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                  (user["id"], time.time(), stage, key, "gen" if key.startswith("gen:") else "core", them, clean(d.get("reply", "")),
                   j["verdict"], j["score"], json.dumps(j["flags"]), j["diagnosis"], rid), write=True)
                apply_attempt(prog, stage, None if rid else key, j["verdict"])
                set_progress(user["id"], prog)
                j["progress"] = prog
            self._send(200, j)
        except Exception as e:
            self._send(502, {"error": "coach offline: %s" % type(e).__name__})
    def log_message(self, *a): pass

def rally_score(verdicts):
    ps = sum(v == "Pass" for v in verdicts); fs = len(verdicts) - ps
    win = ps >= 3 or (len(verdicts) >= 4 and fs < 2 and ps >= 2)
    return ps, fs, win

def rally_result(d, user):
    if not user: return 401, {"error": "log in to save rallies"}
    stage = int(d.get("stage", 1)); rid = clean(d.get("rally_id", ""), 40)
    prog = get_progress(user["id"])
    rows = q("SELECT verdict FROM attempts WHERE user_id=? AND rally_id=? ORDER BY ts", (user["id"], rid))
    ps, fs, win = rally_score([r["verdict"] for r in rows][:4])
    if win and len(prog["passed"].get(str(stage), [])) >= NEED and not prog["rally"].get(str(stage)):
        prog["rally"][str(stage)] = True; prog["xp"] += 40; set_progress(user["id"], prog)
    return 200, {"win": win and bool(prog["rally"].get(str(stage))), "passes": ps, "fails": fs, "progress": prog}

if __name__ == "__main__":
    print("coach server on :%d -> %s (%s), db %s" % (PORT, LMS, MODEL, DB_PATH))
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
