#!/usr/bin/env python3
"""Banter Coach proxy: sits between the public tunnel and LM Studio.
Only exposes POST /judge and POST /reply. The system prompts live here.
Run: python3 coach_proxy.py   (env: LMS_URL, MODEL, PORT, ORIGINS)
"""
import json, os, re, time, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen

LMS = os.environ.get("LMS_URL", "http://127.0.0.1:1234/v1/chat/completions")
MODEL = os.environ.get("MODEL", "google/gemma-4-26b-a4b-qat")
PORT = int(os.environ.get("PORT", "8787"))
ORIGINS = [o for o in os.environ.get("ORIGINS", "*").split(",") if o]
RATE = int(os.environ.get("RATE", "30"))  # requests per minute per IP
MAXLEN = 400

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

hits = {}
lock = threading.Lock()

def limited(ip):
    now = time.time()
    with lock:
        h = [t for t in hits.get(ip, []) if now - t < 60]
        h.append(now)
        hits[ip] = h
        return len(h) > RATE

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
    "flags": {"type": "array", "items": {"type": "string"}}}}

def clean(s, n=MAXLEN):
    return re.sub(r"\s+", " ", str(s))[:n].strip()

SKIP = re.compile(r"^\W*(next|skip|idk|pass|no|n/?a|\?+|\.+)\W*$", re.I)

def judge(d):
    if SKIP.match(str(d.get("reply", "")).strip()) or len(clean(d.get("reply", ""))) < 3:
        return {"verdict": "Dead End", "score": 0, "flags": ["bailed"], "rewrites": [],
                "diagnosis": "You skipped the rep. If you bail on a tough prompt here, you'll freeze when it's live on your phone. Give an actual reply."}
    stage = int(d.get("stage", 1))
    thread = [clean(m.get("t", "")) and (("THEM: " if m.get("who") == "them" else "USER: ") + clean(m.get("t", "")))
              for m in d.get("thread", [])[-8:] if isinstance(m, dict)]
    prompt = ("Stage %d.\nThread so far (oldest first):\n%s\n\nJudge ONLY the user's LAST text: %s"
              % (stage, "\n".join(x for x in thread if x), clean(d.get("reply", ""))))
    if d.get("rally"):
        prompt += "\nThis is a multi-turn rally: judge it against the whole thread so far, and whether the user is sustaining the bit without retreating to safe questions."
    out = llm([{"role": "system", "content": COACH}, {"role": "user", "content": prompt}], SCHEMA)
    j = json.loads(out)
    j["rewrites"] = j["rewrites"][:2] if j["verdict"] != "Pass" else []
    return j

def reply(d):
    msgs = [{"role": "system", "content": MATCH}]
    for m in d.get("thread", [])[-8:]:
        msgs.append({"role": "assistant" if m.get("who") == "them" else "user", "content": clean(m.get("t", ""))})
    return {"text": clean(llm(msgs, None, 0.9, 150), 300)}

class H(BaseHTTPRequestHandler):
    def _cors(self):
        o = self.headers.get("Origin", "")
        ok = "*" in ORIGINS or o in ORIGINS
        self.send_header("Access-Control-Allow-Origin", o if ok and o else ORIGINS[0] if ok else "null")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self._cors()
        self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)
    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.end_headers()
    def do_GET(self):
        self._send(200, {"ok": True, "model": MODEL}) if self.path == "/health" else self._send(404, {"error": "not found"})
    def do_POST(self):
        ip = self.headers.get("CF-Connecting-IP") or self.client_address[0]
        if self.path not in ("/judge", "/reply"): return self._send(404, {"error": "not found"})
        if limited(ip): return self._send(429, {"error": "slow down"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n > 8000: return self._send(413, {"error": "too big"})
            d = json.loads(self.rfile.read(n))
            self._send(200, judge(d) if self.path == "/judge" else reply(d))
        except Exception as e:
            self._send(502, {"error": "coach offline: %s" % type(e).__name__})
    def log_message(self, *a): pass

if __name__ == "__main__":
    print("coach proxy on :%d -> %s (%s)" % (PORT, LMS, MODEL))
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
