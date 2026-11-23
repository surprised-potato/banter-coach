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
LLM_FLAGS = ["agreeing", "interviewing", "advice", "too literal", "thirsty", "try-hard", "pun only", "clunky", "bailed", "hesitant"]
CODE_FLAGS = ["borrowed", "repetitive", "stale", "off-technique"]   # set by code, never by the model
FLAGS = LLM_FLAGS + CODE_FLAGS
NEED = 3  # passes per stage before its rally

# ---------------- technique library ----------------
# stage = first stage where the technique is drilled; examples use neutral texts
TECHNIQUES = [
 {"id": "mock_judgment", "name": "Mock judgment", "stage": 1, "how": "Pretend to disapprove of a harmless habit or choice.",
  "ex": ["THEM: i put ketchup on eggs → \"I'm going to need a minute to process this betrayal\""]},
 {"id": "exaggeration", "name": "Absurd exaggeration", "stage": 1, "how": "Blow a small detail up to a ridiculous scale.",
  "ex": ["THEM: i walked 12k steps today → \"So you basically hiked to another country. Do you need a medal or a nap?\""]},
 {"id": "accusation", "name": "Playful accusation", "stage": 1, "how": "Accuse them of being a silly 'type' based on what they said.",
  "ex": ["THEM: i alphabetize my spices → \"You're definitely the friend who makes a spreadsheet for group trips\""]},
 {"id": "misinterpret", "name": "Misinterpret on purpose", "stage": 1, "how": "Deliberately read their text the wrong (funnier) way.",
  "ex": ["THEM: i'm so tired i could sleep for days → \"Is that an invitation to a nap competition? Because I train for this\""]},
 {"id": "role_reversal", "name": "Role reversal", "stage": 1, "how": "Act as if they are the one chasing or trying to impress you.",
  "ex": ["THEM: i make a mean lasagna → \"Wow, trying to win me over with pasta already? Bold strategy\""]},
 {"id": "agree_amplify", "name": "Agree and amplify", "stage": 1, "how": "Accept their tease or self-roast and push it even further.",
  "ex": ["THEM: you seem like trouble → \"Trouble is underselling it. I've been banned from two bowling alleys\""]},
 {"id": "push_pull", "name": "Push-pull", "stage": 1, "how": "A compliment and a tease in the same line.",
  "ex": ["THEM: i'm great at karaoke → \"Okay that's cute. I bet you pick a ballad and make everyone hold lighters though\""]},
 {"id": "cold_read", "name": "Cold read", "stage": 1, "how": "Confidently guess something specific about them from a tiny clue.",
  "ex": ["THEM: i have 400 unread emails → \"Let me guess, you also have 37 tabs open and one of them is playing music you can't find\""]},
 {"id": "callback", "name": "Callback", "stage": 1, "how": "Bring back an earlier detail from the thread in a new way.",
  "ex": ["(earlier: they burn toast) THEM: i'm cooking tonight → \"Should I put the fire department on standby again?\""]},
 {"id": "nickname", "name": "Nickname", "stage": 1, "how": "Give them a playful title based on what they said.",
  "ex": ["THEM: i've rewatched friends 9 times → \"Noted. Hi, Professor of Central Perk\""]},
 {"id": "deadpan", "name": "Deadpan understatement", "stage": 1, "how": "React to something big as if it's tiny, or tiny as if it's huge, with a straight face.",
  "ex": ["THEM: my cat knocked over my christmas tree → \"Sounds like he has notes on your decorating\""]},
 {"id": "co_conspirator", "name": "Co-conspirator", "stage": 1, "how": "Frame the two of you as a team against the world.",
  "ex": ["THEM: my roommate ate my leftovers → \"We need a plan. I'm thinking a fake leftover box full of salad\""]},
 {"id": "hypothetical_us", "name": "Hypothetical us", "stage": 2, "how": "Drop both of you into a tiny, specific future scene.",
  "ex": ["THEM: i always get lost on road trips → \"So on our road trip you're banned from the map. You're on snacks and playlist\""]},
 {"id": "compat_test", "name": "Compatibility test", "stage": 2, "how": "Playfully make them pass a silly test before you approve of them.",
  "ex": ["THEM: i love horror movies → \"Okay but do you watch with the lights off or are you a pillow-shield person? This decides a lot\""]},
 {"id": "invite", "name": "Low-pressure invite", "stage": 3, "how": "A specific plan (place, rough time) that is easy to say yes to, tied to the banter.",
  "ex": ["THEM: i've been craving dumplings all week → \"There's a dumpling spot on 5th that will end your search. Thursday after work?\""]},
]
TECH_IDS = [t["id"] for t in TECHNIQUES]
TECH = {t["id"]: t for t in TECHNIQUES}
# overused templates: banned from coach output, flagged in player replies
STALE = ["cry for help", "careful, you're making", "careful youre making", "making the * look bad", "making the * think twice", "blink twice",
         "hostage situation", "reconsidering our friendship", "calling the police", "call the cops", "speedrun", "quake in their boots", "red flag"]
STALE_RE = re.compile(r"^\W*careful\b|" + "|".join(re.escape(s).replace(r"\*", r"\w+(?: \w+)?") for s in STALE), re.I)

COACH = """You are a blunt, uncompromising Flirting & Banter Coach training a user to turn friendly texts into playful, flirtatious banter. No participation trophies: if a reply is flat, safe, try-hard, needy, overly logical, a literal answer, an interview question, unsolicited advice, or a dead-end agreement, say so and explain why it would flop in a real text thread. Only call a line a Pass when it is genuinely sharp.
Flirting is a dial, not a switch: low-pressure playful tension, teasing, exaggeration, mock judgment, role reversal, misdirection, co-conspirator framing. Never pickup lines, forced jokes, premature heavy compliments, asking for photos, or inviting yourself over (Dead End: thirsty, stranger-danger). Always respect the other person's comfort and consent; a good line is one the recipient would enjoy.
Verdicts: "Pass" (sharp, calibrated, gives them something to bounce off), "Weak" (right instinct, flat or clunky or hedged), "Dead End" (agrees, interviews, advises, bails, thirsty, or kills the tension).
Stage goals: 1 Pivot = stop agreeing/advising/interviewing, tease or exaggerate. 2 Frame = create 'us' energy with hypothetical scenarios, playful compatibility tests, specific vibe-based compliments. 3 Close = turn banter into a confident, low-pressure invitation to meet, not hesitant or over-eager.
Calibration: be strict but fair. Any technique from the library below that stays low-pressure and gives the recipient something to bounce off is a PASS (score 75-95). Do not mark a Pass down for being a mild stereotype if it is playful and warm. Weak = 35-60, Dead End = 0-30. Do not hedge to the middle.
Technique library (id: how):
%s
Rewrites must be fresh lines written for this exact text, not stock phrases.
Examples (each shows a different move):
THEM: "just surviving on iced coffee and spreadsheets lol" / USER: "Try not to ice the spreadsheets" -> Weak 40, technique none (dad-joke pun, nothing to bounce off).
THEM: same / USER: "You're one of those people who color-codes their grocery list, aren't you" -> Pass 85, accusation.
THEM: "going to rot on my couch and rewatch The Office" / USER: "Respect. Professional couch athlete, Office division" -> Pass 80, nickname.
THEM: "kitchen looks like a crime scene from homemade pasta" / USER: "send pics and i might come over and investigate" -> Dead End 15, none (thirsty, invites self over).
THEM: same / USER: "Whoa, you sure i wont be accused as an accomplice if the cops see it?" -> Pass 85, co_conspirator (keeps their bit alive, low-pressure).
THEM: "my legs are jelly after the gym" / USER: "So you're basically a wobbly flamingo with a gym membership" -> Pass 85, exaggeration.
THEM: "debating if cereal counts as a real lunch" / USER: "Serves better as dinner honestly" -> Weak 40, none (agrees, coworker-in-Slack energy).
THEM: same / USER: "What kind of cereal?" -> Dead End 10, none (interview question).
THEM: "you seem like trouble" / USER: "Trouble is underselling it. I've been banned from two bowling alleys" -> Pass 85, agree_amplify.
THEM: "haha yeah" / USER: "That 'haha yeah' had real 'watching something better than this convo' energy" -> Pass 80, cold_read.
THEM: "I'm weirdly competitive at board games" / USER: "Cute. I'll still destroy you at Monopoly though" -> Pass 85, push_pull.
THEM: "craving a really good taco and nobody knows where to go" / USER: "There's a place on 5th that will ruin all other tacos for you. Thursday after work?" -> Pass 88, invite.
THEM: same / USER: "maybe we could get tacos sometime if you want, no pressure haha" -> Weak 45, invite (hesitant, over-qualified).
Fields: "technique" = the library id the user's reply mainly uses, or "none". "rewrites" = up to 2 sharper alternatives (empty if Pass), each using a DIFFERENT technique from the user's and from each other, written fresh for this exact text.
Respond ONLY with JSON: {"verdict": "Pass|Weak|Dead End", "score": 0-100, "technique": "id or none", "diagnosis": "2-3 sentences on how the recipient would actually react, naming weak words", "rewrites": [{"technique": "id", "line": "..."}], "flags": [any of %s]}""" % (
    "\n".join("- %s: %s" % (t["id"], t["how"]) for t in TECHNIQUES), ", ".join('"%s"' % f for f in LLM_FLAGS))

MATCH = """You play the person the user is texting, in a casual dating-app text thread. Reply in 1-2 short, natural lowercase-ish sentences, matching the energy of the user's last text: if it was playful, play back and escalate slightly; if it was flat or interview-like, reply flat and short. Never be hostile. Output ONLY the text message, nothing else."""

GEN = """You write ONE realistic incoming text message from a dating-app match, for a banter training game.
Stage %d (%s). The text should naturally tempt the player into this mistake: %s.
Text type: %s. Example of this type: %s
Make it casual and specific, 3-25 words, lowercase-ish like a real text, with a concrete detail (food, show, pet, chore, plan...) unless the type is a dry reply.
It must fit the stage. Example for this stage: %s
Do not repeat or closely paraphrase any of these: %s
Also write "context": ONE short sentence in second person setting the scene from the player's side (how long you've been texting, what the player just said or asked), e.g. "You matched yesterday and asked what they're up to tonight." It must make the text make sense.
Respond ONLY with JSON {"context": "...", "text": "..."}"""
STAGE_NAMES = {1: "The Pivot: they mention something mundane", 2: "The Frame: they share a preference or plan the player could build a shared scenario around", 3: "The Close: banter has peaked and the player should suggest meeting up"}
GEN_EX = {1: '{"context": "You matched yesterday and asked how their day is going.", "text": "just survived a 3 hour meeting that could have been an email, send snacks"}',
          2: '{"context": "A few days of good banter. You asked about their ideal weekend.", "text": "honestly a farmers market, way too many pastries, then a nap. i take naps very seriously"}',
          3: '{"context": "You have texted daily for a week and the banter has been great, but you have not met.", "text": "ok this new ramen place opened near me and i refuse to go alone like a loser"}'}
TEXT_TYPES = {
    "update": ("a mundane update about their day", '{"context": "You asked how their day is going.", "text": "just survived a 3 hour meeting that could have been an email"}'),
    "tease": ("they tease the player first about something the player said", '{"context": "You told them you can\'t cook anything but toast.", "text": "wow so i\'m guessing our first date is at a toaster museum"}'),
    "dry": ("a dry, low-effort reply; the context must say exactly what the player had sent", '{"context": "You sent a long joke about your neighbour\'s rooster waking you up at 4am.", "text": "haha nice"}'),
    "question": ("they ask the player a normal getting-to-know-you question", '{"context": "You matched yesterday and have been chatting about weekend plans.", "text": "so what do you actually do for work"}'),
    "compliment": ("a light compliment to the player", '{"context": "You sent a photo of the bread you baked.", "text": "ok that bread looks better than anything from my local bakery"}'),
    "test": ("a mild playful test of the player", '{"context": "You said they have great taste in music.", "text": "you probably say that to everyone lol"}'),
}
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
    if "technique" not in [r[1] for r in db.execute("PRAGMA table_info(attempts)")]:
        db.execute("ALTER TABLE attempts ADD COLUMN technique TEXT")
        db.execute("ALTER TABLE attempts ADD COLUMN challenge TEXT")
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
  "required": ["verdict", "score", "technique", "diagnosis", "rewrites", "flags"],
  "properties": {"verdict": {"type": "string", "enum": ["Pass", "Weak", "Dead End"]},
    "score": {"type": "integer"}, "technique": {"type": "string", "enum": TECH_IDS + ["none"]}, "diagnosis": {"type": "string"},
    "rewrites": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["technique", "line"],
        "properties": {"technique": {"type": "string", "enum": TECH_IDS}, "line": {"type": "string"}}}},
    "flags": {"type": "array", "items": {"type": "string", "enum": LLM_FLAGS}}}}
GSCHEMA = {"type": "object", "additionalProperties": False, "required": ["context", "text"], "properties": {"context": {"type": "string"}, "text": {"type": "string"}}}

def clean(s, n=MAXLEN):
    return re.sub(r"\s+", " ", str(s))[:n].strip()

SKIP = re.compile(r"^\W*(next|skip|idk|pass|no|n/?a|\?+|\.+)\W*$", re.I)

norm = lambda x: re.sub(r"[^a-z0-9 ]", "", x.lower()).strip()

def same_move(a, b):
    """True if two replies share a skeleton: same 3-word opener, or very similar overall."""
    a, b = norm(a), norm(b)
    wa, wb = a.split(), b.split()
    tri = lambda w: {" ".join(w[i:i + 3]) for i in range(len(w) - 2)}
    return ((len(wa) >= 3 and wa[:3] == wb[:3]) or bool(tri(wa) & tri(wb))   # same opener or same 3-word skeleton
            or difflib.SequenceMatcher(None, a, b).ratio() >= 0.6)

def cap(j, flag, note):
    if j["verdict"] == "Pass":
        j.update(verdict="Weak", score=min(j["score"], 55))
        note = "Capped at Weak: " + note + " (On its own the line works:)"
    j["diagnosis"] = note + " " + j["diagnosis"]
    j["flags"] = sorted(set(j["flags"]) | {flag})

def judge_llm(d, recent=None):
    reply = clean(d.get("reply", ""))
    if SKIP.match(reply) or len(reply) < 3:
        return {"verdict": "Dead End", "score": 0, "flags": ["bailed"], "rewrites": [], "technique": "none",
                "diagnosis": "You skipped the rep. If you bail on a tough prompt here, you'll freeze when it's live on your phone. Give an actual reply."}
    if recent is None:
        recent = [clean(x, 300) for x in (d.get("recent") or [])[:6] if isinstance(x, str)]
    challenge = d.get("challenge") if d.get("challenge") in TECH else None
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
    if challenge:
        t = TECH[challenge]
        prompt += "\nTECHNIQUE CHALLENGE: the user must use %s (%s). If the reply does not clearly use it, the verdict is at most Weak and the diagnosis must say so." % (t["name"], t["how"])
    j = json.loads(llm([{"role": "system", "content": COACH}, {"role": "user", "content": prompt}], SCHEMA))
    j["score"] = max(0, min(100, int(j["score"])))
    j["technique"] = j.get("technique") if j.get("technique") in TECH else "none"
    rw = [] if j["verdict"] == "Pass" else [r for r in j["rewrites"] if r.get("technique") in TECH and not STALE_RE.search(r.get("line", ""))]
    seen_t, j["rewrites"] = {j["technique"]}, []
    for r in rw:  # different technique from the user's and from each other
        if r["technique"] not in seen_t and len(j["rewrites"]) < 2:
            seen_t.add(r["technique"]); j["rewrites"].append({"technique": TECH[r["technique"]]["name"], "line": clean(r["line"], 200)})
    j["flags"] = sorted(set(f for f in j["flags"] if f in LLM_FLAGS))
    # code-enforced caps (the model is unreliable at these)
    if shown and max(difflib.SequenceMatcher(None, norm(reply), norm(x)).ratio() for x in shown) >= 0.7:
        cap(j, "borrowed", "That's my line, not yours. Borrowing the coach's example won't help when it's live on your phone. Find your own angle.")
        j["rewrites"] = []
    elif sum(same_move(reply, r) for r in recent) >= 2:
        cap(j, "repetitive", "You've sent this same move twice already. Real banter needs range: switch techniques.")
    if STALE_RE.search(reply):
        j["flags"] = sorted(set(j["flags"]) | {"stale"})
        j["diagnosis"] += " Also: that phrasing is a worn-out template. It works once, then it reads like a script."
    if challenge and j["technique"] != challenge:
        cap(j, "off-technique", "The challenge was %s, and this isn't it." % TECH[challenge]["name"])
    return j

def reply_llm(d):
    ctx = clean(d.get("context", ""), 300)
    msgs = [{"role": "system", "content": MATCH + ("\nSituation (from the user's side): " + ctx if ctx else "")}]
    for m in d.get("thread", [])[-8:]:
        msgs.append({"role": "assistant" if m.get("who") == "them" else "user", "content": clean(m.get("t", ""))})
    return {"text": clean(llm(msgs, None, 0.9, 150), 300)}

HELP = """You are the banter coach giving a hint BEFORE the player replies. Stage %d goal: %s
Give: "tip": one blunt sentence on what to avoid and what angle to take for THIS text.
"examples": exactly one example reply for EACH of these techniques, in this order:
%s
Each "line" is short, natural texting style, specific to THIS message (reuse its concrete details), playful, low-pressure, never thirsty. The three lines must not share an opener or structure.
Never start a line with "Careful". Never use these overused templates: "cry for help", "careful, you're making...", "blink twice", "hostage situation", "reconsidering our friendship", "calling the police", "speedrun", "red flag".
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
    picks = pick_techniques(stage, d.get("used") or [], d.get("exclude") or [], 3)
    sysmsg = HELP % (stage, STAGE_GOALS[stage], "\n".join("%d. %s: %s" % (i + 1, TECH[t]["name"], TECH[t]["how"]) for i, t in enumerate(picks)))
    for _ in range(2):
        j = json.loads(llm([{"role": "system", "content": sysmsg}, {"role": "user", "content": prompt}], HSCHEMA, 0.9, 500))
        ex = [{"id": t, "technique": TECH[t]["name"], "how": TECH[t]["how"], "line": clean(e["line"], 200)}
              for t, e in zip(picks, j["examples"]) if not STALE_RE.search(e.get("line", ""))]
        if len(ex) >= 2: break
    return {"tip": clean(j["tip"], 300), "examples": ex}

def pick_techniques(stage, used, exclude, k):
    """Random techniques for this stage, favouring ones the player rarely uses and skipping the last help set."""
    pool = [t["id"] for t in TECHNIQUES if t["stage"] <= stage and t["id"] not in exclude]
    if stage == 3 and "invite" not in exclude: pool += ["invite"] * 2      # the close always leans on invites
    counts = {t: sum(1 for u in used if u == t) for t in TECH_IDS}
    picks = []
    while pool and len(picks) < k:
        w = [1.0 / (1 + 2 * counts.get(t, 0)) for t in pool]
        r, acc = secrets.randbelow(10_000) / 10_000 * sum(w), 0.0
        for t, wt in zip(pool, w):
            acc += wt
            if acc >= r: break
        picks.append(t); pool = [x for x in pool if x != t]
    return picks

recent_gen = []  # global recent generated texts, to avoid repeats
def generate(d, uid):
    stage = int(d.get("stage", 1)); stage = stage if stage in (1, 2, 3) else 1
    trap = d.get("trap") if d.get("trap") in TRAPS else ("hesitant" if stage == 3 else "interviewing")
    tt = d.get("type") if d.get("type") in TEXT_TYPES else secrets.choice(list(TEXT_TYPES))
    seen = list(recent_gen[-15:])
    if uid:
        seen += [r["them_text"] for r in q("SELECT them_text FROM attempts WHERE user_id=? AND source='gen' ORDER BY ts DESC LIMIT 15", (uid,))]
    for _ in range(3):
        g = json.loads(llm([{"role": "user", "content": GEN % (stage, STAGE_NAMES[stage], TRAPS[trap], TEXT_TYPES[tt][0], TEXT_TYPES[tt][1], GEN_EX[stage], json.dumps(seen[-25:]))}], GSCHEMA, 1.0, 300))
        t, cx = clean(g["text"], 220), clean(g["context"], 200)
        if 3 <= len(t) <= 200 and t not in seen:
            recent_gen.append(t); del recent_gen[:-50]
            return {"text": t, "context": cx, "trap": trap, "type": tt, "key": "gen:" + hashlib.sha1(t.encode()).hexdigest()[:10]}
    raise ValueError("generation failed")

# ---------------- handlers ----------------
def user_from(h):
    a = h.headers.get("Authorization", "")
    if not a.startswith("Bearer "): return None
    r = q("SELECT u.id, u.name FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=?", (a[7:].strip(),), one=True)
    return dict(r) if r else None

def history(uid, limit):
    rows = q("SELECT ts, stage, scenario_key, source, them_text, reply, verdict, score, flags, diagnosis, rally_id, technique, challenge FROM attempts WHERE user_id=? ORDER BY ts DESC LIMIT ?", (uid, limit))
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
        if u.path == "/techniques": return self._send(200, {"techniques": TECHNIQUES})
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
            stage = int(d.get("stage", 1))
            if user:
                prog = get_progress(user["id"])
                if not unlocked(prog, stage): return self._send(403, {"error": "stage locked"})
                recent = [r["reply"] for r in q("SELECT reply FROM attempts WHERE user_id=? ORDER BY ts DESC LIMIT 6", (user["id"],))]
                j = judge_llm(d, recent)
            else:
                j = judge_llm(d)
            if user:
                key = clean(d.get("key", ""), 40)
                them = next((clean(m.get("t", "")) for m in reversed(d.get("thread", [])) if isinstance(m, dict) and m.get("who") == "them"), "")
                rid = clean(d.get("rally_id", ""), 40) or None
                ch = d.get("challenge") if d.get("challenge") in TECH else None
                q("INSERT INTO attempts(user_id, ts, stage, scenario_key, source, them_text, reply, verdict, score, flags, diagnosis, rally_id, technique, challenge) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (user["id"], time.time(), stage, key, "gen" if key.startswith("gen:") else "core", them, clean(d.get("reply", "")),
                   j["verdict"], j["score"], json.dumps(j["flags"]), j["diagnosis"], rid, j["technique"], ch), write=True)
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
