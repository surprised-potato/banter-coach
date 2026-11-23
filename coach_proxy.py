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
CODE_FLAGS = ["borrowed", "repetitive", "stale", "off-technique", "rule-broken"]   # set by code, never by the model
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
Principles (from research on teasing, charisma and conversation):
- A tease needs a play signal (exaggeration, mock-drama, "lol", an obviously absurd claim) and must target something harmless; without the signal it reads as criticism -> "clunky".
- Turn toward their bid ("yes, and"): keep THEIR premise or metaphor and add to it. Switching to logistics, solutions or advice is turning away -> "too literal" / "advice".
- Power + warmth: commit to the line. Hedges and self-talk ("I'm not sure if", "I don't know", "maybe", talking about your own reaction instead of them) read as low power -> "hesitant" or "clunky".
- Humor signals interest when it is about THEM (their choices, their vibe), not about the words; a pun alone shows cleverness, not interest -> "pun only".
- Questions: dry interview questions are bad ("interviewing"), but a PLAYFUL follow-up that proves you read their text and has a tease built in can Pass (e.g. "Is it a negotiation or a hostile takeover?").
- One idea per text. Two stacked ideas or a long sentence -> "clunky".
Name the principle in the diagnosis when it explains the miss (e.g. "you turned away from their bit").
Calibration: be strict but fair. Any technique from the library below that stays low-pressure and gives the recipient something to bounce off is a PASS (score 75-95). Do not mark a Pass down for being a mild stereotype if it is playful and warm. Dead End = 0-30. Use the FULL Weak band 31-74 by how close the reply came: about 35 = barely a joke or mostly off-target, about 50 = right instinct but flat or hedged, about 65-70 = almost there, one fix away. Do not default to 45.
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
THEM: "negotiating with my cat for control of my laptop" / USER: "You should just buy a new laptop, you aren't gonna win this one" -> Weak 35, none (turns away from their bit into advice).
THEM: same / USER: "Is it a negotiation or a hostile takeover? Be honest" -> Pass 82, accusation (playful follow-up that stays in their premise).
THEM: "deep dive of old sitcoms and a massive bowl of popcorn" / USER: "I dont know, im not prepared for that level of cozy" -> Weak 50, none (right instinct, but hedged and about you).
THEM: same / USER: "Old sitcoms AND popcorn? You're one blanket away from retirement" -> Pass 80, exaggeration.
THEM: "spent the morning looking for my missing left sock" / USER: "Uh oh better call the sock authorities" -> Weak 40, none (pun-ish, about the word not them).
THEM: same / USER: "A missing left sock? You're giving 'loses things on purpose for the drama' energy" -> Weak 68, cold_read (almost: good angle, a bit long).
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
# research-based drill templates ("inspired by", not proof)
PRINCIPLES = {
    "mark_tease": {"name": "Mark the tease", "source": "Keltner et al. 2001, 'Just Teasing' (Psychological Bulletin)",
                   "idea": "A tease needs a play signal (exaggeration, mock-drama, obvious absurdity) and a harmless target, or it reads as criticism."},
    "no_hedges": {"name": "No hedges", "source": "Cabane 2012, The Charisma Myth (power + warmth)",
                  "idea": "Commit to the line. Hedging and talking about your own reaction reads as low power."},
    "yes_and": {"name": "Yes, and their bit", "source": "Gottman's bids for connection; improv 'yes, and'",
                "idea": "Turn toward their bid: keep their premise or metaphor and add one escalation, instead of switching to logistics or advice."},
    "about_them": {"name": "About them, not the word", "source": "Li et al. 2009, humor as an interest indicator",
                   "idea": "Humor signals interest when it's about the person (their choices, their vibe), not wordplay."},
    "playful_followup": {"name": "Playful follow-up", "source": "Huang et al. 2017, 'It Doesn't Hurt to Ask' (JPSP)",
                         "idea": "Follow-up questions that prove you listened increase liking; make it playful, not an interview."},
    "push_pull": {"name": "Push-pull, kindly", "source": "Whitchurch, Wilson & Gilbert 2011 (Psychological Science)",
                  "idea": "Mix warmth and a tease in one line. Keep it kind: no negging, no games about real interest."},
    "one_idea": {"name": "One idea, short", "source": "Coach rule from your own stats",
                 "idea": "One frame per text, stated briefly. Long or double-idea texts get skimmed."},
}
FLAG_PRINCIPLE = {"clunky": "one_idea", "try-hard": "mark_tease", "too literal": "yes_and", "advice": "yes_and", "agreeing": "yes_and",
                  "pun only": "about_them", "hesitant": "no_hedges", "bailed": "no_hedges", "interviewing": "playful_followup",
                  "thirsty": "push_pull", "repetitive": "about_them", "stale": "about_them", "borrowed": "no_hedges", "off-technique": "about_them"}

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
    cols = [r[1] for r in db.execute("PRAGMA table_info(attempts)")]
    if "technique" not in cols:
        db.execute("ALTER TABLE attempts ADD COLUMN technique TEXT")
        db.execute("ALTER TABLE attempts ADD COLUMN challenge TEXT")
    if "drill_run" not in cols:
        db.execute("ALTER TABLE attempts ADD COLUMN drill_run TEXT")
    db.execute("""CREATE TABLE IF NOT EXISTS drills(id TEXT PRIMARY KEY, user_id INTEGER, created REAL, spec TEXT,
        saved INTEGER DEFAULT 0, runs INTEGER DEFAULT 0, best INTEGER DEFAULT 0, cleared INTEGER DEFAULT 0)""")
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
    if d.get("_rule"):
        prompt += "\nDRILL RULE the user is practising: %s (principle: %s). Judge the reply normally, and say in the diagnosis whether it applied the principle." % (
            d["_rule"]["text"], d.get("_principle", ""))
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
    rule = d.get("_rule")
    if rule:
        broken = rule_broken(reply, rule)
        if not broken and rule.get("technique") and j["technique"] != rule["technique"]:
            broken = "use %s" % TECH[rule["technique"]]["name"]
        if broken:
            cap(j, "rule-broken", "Drill rule broken (%s)." % broken)
    return j

def word_re(w):
    return re.compile(r"(?<![\w'])" + re.escape(w.lower()) + r"(?![\w'])")

def rule_broken(reply, rule):
    """Code-checkable drill rules. Returns a short reason or None."""
    r = reply.replace("’", "'").lower()
    n = len(reply.split())
    if rule.get("max_words") and n > rule["max_words"]:
        return "max %d words, you used %d" % (rule["max_words"], n)
    for w in rule.get("ban_words") or []:
        if word_re(w.replace("’", "'")).search(r):
            return "no \"%s\"" % w
    if rule.get("no_question") and "?" in reply:
        return "no questions"
    return None

def clean_rule(r):
    """Sanitize a rule object coming from the model or a guest client."""
    if not isinstance(r, dict): return None
    mw = r.get("max_words")
    mw = int(mw) if isinstance(mw, (int, float)) and 5 <= int(mw) <= 30 else 0
    stop = {"if", "so", "just", "really", "the", "a", "an", "i", "is", "it", "to", "and", "or", "you", "pun", "wordplay"}
    bw = [clean(w, 20) for w in (r.get("ban_words") or []) if isinstance(w, str) and clean(w, 20) and clean(w, 20).lower() not in stop][:6]
    t = r.get("technique") if r.get("technique") in TECH else None
    return {"text": clean(r.get("text", ""), 200), "max_words": mw, "ban_words": bw, "no_question": bool(r.get("no_question")), "technique": t}

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

# ---------------- custom drills ----------------
DRILL = """You design ONE short practice drill for a banter-training game, targeted at this player's worst habit.
Player profile:
%s
Research-based principles you can build the drill on (pick the ONE that best fixes the habit):
%s
Design:
- "title": 2-5 words, catchy.
- "habit": the habit flag it targets.
- "principle": the principle id.
- "why": 2 sentences to the player. Quote 1-2 of THEIR actual weak replies word for word (from the profile) and, if they have one, contrast with one of their passes. Blunt but encouraging.
- "rule": a constraint they must follow on every reply. "text" is the rule in one sentence. Make it checkable where possible: "max_words" (8-20, or 0 for none), "ban_words" (up to 5 words or short phrases that actually appear in THEIR weak replies and cause the habit, e.g. "should", "I'm not sure", "maybe"; never common words like "if", "just", "really"; [] for none), "no_question" (true/false), "technique" ("none" unless the principle is literally about one technique). Use at least one checkable field. The rule must agree with the principle (e.g. a "Playful follow-up" drill must allow questions).
- "texts": exactly 5 NEW incoming texts. "text" is the message THE MATCH sends to the player (3-25 words, lowercase-ish, concrete detail); the player will write the reply. "context" is one second-person sentence about the situation BEFORE that message (e.g. "You asked how their Monday is going."); it must not contain the match's message. Example: {"context": "You told them you were baking bread tonight.", "text": "bold of you to assume i won't judge the crust"}. They must bait the target habit, fit Stage %d, vary in type (update, they tease you, dry reply, question to you, compliment), and not repeat anything in the profile.
%s
Respond ONLY with JSON."""
DSCHEMA = {"type": "object", "additionalProperties": False, "required": ["title", "habit", "principle", "why", "rule", "texts"], "properties": {
    "title": {"type": "string"}, "habit": {"type": "string", "enum": FLAGS}, "principle": {"type": "string", "enum": list(PRINCIPLES)},
    "why": {"type": "string"},
    "rule": {"type": "object", "additionalProperties": False, "required": ["text", "max_words", "ban_words", "no_question", "technique"], "properties": {
        "text": {"type": "string"}, "max_words": {"type": "integer"}, "ban_words": {"type": "array", "items": {"type": "string"}},
        "no_question": {"type": "boolean"}, "technique": {"type": "string", "enum": TECH_IDS + ["none"]}}},
    "texts": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["context", "text"],
        "properties": {"context": {"type": "string"}, "text": {"type": "string"}}}}}}

def habit_profile(atts):
    """atts: newest first, dicts with reply, verdict, score, flags, technique."""
    atts = [a for a in atts if a.get("reply")][:60]
    fc = {}
    for a in atts:
        for f in a.get("flags") or []: fc[f] = fc.get(f, 0) + 1
    passes = [a for a in atts if a.get("verdict") == "Pass"]
    weak = [a for a in atts if a.get("verdict") != "Pass" and a.get("flags")]
    top = sorted(fc.items(), key=lambda x: -x[1])
    lines = ["%d replies, %d passes (%d%%), average score %d." % (len(atts), len(passes), 100 * len(passes) // max(1, len(atts)),
             sum(a.get("score") or 0 for a in atts) // max(1, len(atts))),
             "Habit flags (count): " + (", ".join("%s %d" % kv for kv in top) or "none yet")]
    lines += ['PASS: "%s"' % clean(a["reply"], 140) for a in passes[:4]]
    lines += ['WEAK (%s): "%s"' % (", ".join(a["flags"]), clean(a["reply"], 140)) for a in weak[:8]]
    return "\n".join(lines), top, weak

def suggest_drill(atts, stage, focus=None, avoid=()):
    prof, top, weak = habit_profile(atts)
    habit = focus if focus in FLAGS else (top[0][0] if top else "clunky")
    pri = "\n".join("- %s: %s (%s)" % (k, v["idea"], v["source"]) for k, v in PRINCIPLES.items())
    extra = "Target habit: %s (suggested principle: %s)." % (habit, FLAG_PRINCIPLE.get(habit, "one_idea"))
    if avoid: extra += " Do not reuse these drill titles: %s." % json.dumps(list(avoid)[:10])
    for _ in range(2):
        s = json.loads(llm([{"role": "system", "content": DRILL % (prof, pri, stage, extra)}, {"role": "user", "content": "Design the drill."}], DSCHEMA, 0.9, 1200))
        texts = [{"context": clean(t["context"], 200), "text": clean(t["text"], 220)} for t in s["texts"] if 3 <= len(clean(t.get("text", ""), 220))][:5]
        rule = clean_rule(s["rule"])
        if rule:
            rule["technique"] = "push_pull" if s["principle"] == "push_pull" else None   # forced techniques made drills too narrow
            if s["principle"] == "playful_followup": rule["no_question"] = False
        if len(texts) >= 4 and rule and rule["text"]: break
    else:
        raise ValueError("drill generation failed")
    why = clean(s["why"], 500)
    def quoted(r):
        m = difflib.SequenceMatcher(None, why.lower(), r.lower()).find_longest_match(0, len(why), 0, len(r))
        return m.size >= min(18, len(r))
    if weak and not any(quoted(clean(a["reply"], 140)) for a in atts if a.get("reply")):  # make sure it quotes them
        why += ' For example you wrote: "%s".' % clean(weak[0]["reply"], 120)
    if not (rule["max_words"] or rule["ban_words"] or rule["no_question"] or rule["technique"]):
        rule["max_words"] = 15
    return {"title": clean(s["title"], 60), "habit": s["habit"] if s["habit"] in FLAGS else habit, "principle": s["principle"],
            "why": why, "rule": rule, "texts": texts, "pass_need": max(1, len(texts) - 1), "stage": stage}

def drill_out(row):
    s = json.loads(row["spec"])
    s.update(id=row["id"], saved=bool(row["saved"]), runs=row["runs"], best=row["best"], cleared=bool(row["cleared"]),
             principle_info=PRINCIPLES.get(s.get("principle"), {}))
    s.pop("runs_seen", None)
    return s

def drill_get(uid, did):
    return q("SELECT * FROM drills WHERE id=? AND user_id=?", (did, uid), one=True)

def drill_api(p, d, user):
    if p == "/drill/suggest":
        if user:
            atts = history(user["id"], 60)
            prog = get_progress(user["id"])
            avoid = [json.loads(r["spec"]).get("title") for r in q("SELECT spec FROM drills WHERE user_id=? ORDER BY created DESC LIMIT 10", (user["id"],))]
        else:
            atts = [{"reply": clean(a.get("reply", ""), 300), "verdict": a.get("verdict"), "score": a.get("score") if isinstance(a.get("score"), int) else 0,
                     "flags": [f for f in (a.get("flags") or []) if f in FLAGS]} for a in (d.get("attempts") or [])[:60] if isinstance(a, dict)]
            prog, avoid = None, d.get("avoid") or []
        stage = max([n for n in (1, 2, 3) if not prog or unlocked(prog, n)])
        spec = suggest_drill(atts, stage, d.get("focus"), avoid)
        did = secrets.token_hex(6)
        if user:
            q("DELETE FROM drills WHERE user_id=? AND saved=0 AND created<?", (user["id"], time.time() - 86400), write=True)
            q("INSERT INTO drills(id, user_id, created, spec) VALUES(?,?,?,?)", (did, user["id"], time.time(), json.dumps(spec)), write=True)
            return 200, drill_out(drill_get(user["id"], did))
        spec.update(id="g" + did, saved=False, runs=0, best=0, cleared=False, principle_info=PRINCIPLES.get(spec["principle"], {}))
        return 200, spec
    if not user: return 401, {"error": "log in to save drills"}
    row = drill_get(user["id"], clean(d.get("id", ""), 20))
    if not row: return 404, {"error": "drill not found"}
    if p == "/drill/save":
        q("UPDATE drills SET saved=1 WHERE id=?", (row["id"],), write=True)
        return 200, drill_out(drill_get(user["id"], row["id"]))
    if p == "/drill/remove":
        q("DELETE FROM drills WHERE id=?", (row["id"],), write=True)
        return 200, {"ok": True}
    if p == "/drill/result":
        run = clean(d.get("run", ""), 40); s = json.loads(row["spec"])
        rows = q("SELECT verdict FROM attempts WHERE user_id=? AND drill_run=?", (user["id"], run))
        ps = sum(r["verdict"] == "Pass" for r in rows)
        seen = s.setdefault("runs_seen", [])
        if run and run not in seen and rows:
            seen.append(run); del seen[:-50]
            cleared = row["cleared"] or ps >= s.get("pass_need", 4)
            q("UPDATE drills SET runs=runs+1, best=MAX(best,?), cleared=?, spec=? WHERE id=?", (ps, int(bool(cleared)), json.dumps(s), row["id"]), write=True)
        out = drill_out(drill_get(user["id"], row["id"]))
        out["run_passes"] = ps
        return 200, out
    return 404, {"error": "not found"}

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
    rows = q("SELECT ts, stage, scenario_key, source, them_text, reply, verdict, score, flags, diagnosis, rally_id, technique, challenge, drill_run FROM attempts WHERE user_id=? ORDER BY ts DESC LIMIT ?", (uid, limit))
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
        if u.path in ("/me", "/history", "/drills") and not user: return self._send(401, {"error": "log in again"})
        if u.path == "/drills":
            return self._send(200, {"drills": [drill_out(r) for r in q("SELECT * FROM drills WHERE user_id=? AND saved=1 ORDER BY created DESC", (user["id"],))]})
        if u.path == "/me":
            return self._send(200, {"name": user["name"], "progress": get_progress(user["id"]), "attempts": history(user["id"], 200)})
        if u.path == "/history":
            n = int((parse_qs(u.query).get("limit") or ["50"])[0])
            return self._send(200, {"attempts": history(user["id"], max(1, min(n, 500)))})
        self._send(404, {"error": "not found"})
    def do_POST(self):
        p = self.path
        if p not in ("/judge", "/reply", "/generate", "/help", "/signup", "/login", "/logout", "/rally_result",
                     "/drill/suggest", "/drill/save", "/drill/remove", "/drill/result"):
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
            if p in ("/drill/save", "/drill/remove", "/drill/result"): return self._send(*drill_api(p, d, user))
            if limited(hits, self._ip(), RATE): return self._send(429, {"error": "slow down"})
            if p == "/drill/suggest": return self._send(*drill_api(p, d, user))
            if p == "/reply": return self._send(200, reply_llm(d))
            if p == "/help": return self._send(200, help_llm(d))
            if p == "/generate": return self._send(200, generate(d, user and user["id"]))
            stage = int(d.get("stage", 1))
            key = clean(d.get("key", ""), 40)
            if key.startswith("drill:"):   # drill rule: from the DB for profiles, from the client for guests
                row = user and drill_get(user["id"], key.split(":")[1])
                spec = json.loads(row["spec"]) if row else ({"rule": d.get("drill_rule"), "principle": d.get("drill_principle")} if not user else None)
                if not spec: return self._send(404, {"error": "drill not found"})
                d["_rule"] = clean_rule(spec.get("rule"))
                d["_principle"] = PRINCIPLES.get(spec.get("principle"), {}).get("idea", "")
            if user:
                prog = get_progress(user["id"])
                if not unlocked(prog, stage): return self._send(403, {"error": "stage locked"})
                recent = [r["reply"] for r in q("SELECT reply FROM attempts WHERE user_id=? ORDER BY ts DESC LIMIT 6", (user["id"],))]
                j = judge_llm(d, recent)
            else:
                j = judge_llm(d)
            if user:
                them = next((clean(m.get("t", "")) for m in reversed(d.get("thread", [])) if isinstance(m, dict) and m.get("who") == "them"), "")
                rid = clean(d.get("rally_id", ""), 40) or None
                ch = d.get("challenge") if d.get("challenge") in TECH else None
                run = clean(d.get("drill_run", ""), 40) or None
                src = "gen" if key.startswith("gen:") else "drill" if key.startswith("drill:") else "core"
                q("INSERT INTO attempts(user_id, ts, stage, scenario_key, source, them_text, reply, verdict, score, flags, diagnosis, rally_id, technique, challenge, drill_run) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (user["id"], time.time(), stage, key, src, them, clean(d.get("reply", "")),
                   j["verdict"], j["score"], json.dumps(j["flags"]), j["diagnosis"], rid, j["technique"], ch, run), write=True)
                apply_attempt(prog, stage, None if (rid or src == "drill") else key, j["verdict"])
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
