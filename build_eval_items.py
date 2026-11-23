#!/usr/bin/env python3
"""One-off: build the pool of replies the player rates to calibrate the judge.
Sources: the player's own past replies, LLM-written candidates spread across quality,
and the 8 replies from the original Gemini coaching chat. Safe to re-run (dedupes).
Run: python3 build_eval_items.py [user_id]
"""
import json, random, sys
import coach_proxy as c

UID = int(sys.argv[1]) if len(sys.argv) > 1 else 1
random.seed(7)

def add(them, context, reply, source, intended, owner=None):
    c.q("INSERT OR IGNORE INTO eval_items(them, context, reply, source, intended, owner) VALUES(?,?,?,?,?,?)",
        (c.clean(them, 300), c.clean(context or "", 300), c.clean(reply, 300), source, intended, owner), write=True)

# 1. the 8 replies from the Gemini chat (Gemini's verdicts kept as "intended", for reference only)
GEMINI = [
 ("Honestly just surviving on iced coffee and spreadsheets at this point lol", "Try not to ice the spreadsheets", "Weak"),
 ("Honestly just surviving on iced coffee and spreadsheets at this point lol", "Iced coffee this late? Are you asking for help?", "Weak"),
 ("Honestly just surviving on iced coffee and spreadsheets at this point lol", "Iced coffee? Youre making the aderall users quake in their boots", "Pass"),
 ("Nothing crazy, probably just going to rot on my couch and rewatch The Office for the millionth time", "Careful, youre making the party people think twice about their lifestyle choices", "Pass"),
 ("Attempted homemade pasta from scratch and honestly my kitchen looks like a crime scene right now", "Im sort of a crime detective, send pics and i might come over and investigate", "Dead End"),
 ("Attempted homemade pasta from scratch and honestly my kitchen looks like a crime scene right now", "Whoa, you sure i wont be accused as an accomplice if the cops see it?", "Pass"),
 ("Woke up at noon and currently debating if cereal counts as a real lunch", "Serves better as dinner honestly", "Weak"),
 ("Woke up at noon and currently debating if cereal counts as a real lunch", "I know you like to sleep but not role model level", "Weak"),
]
for them, reply, v in GEMINI:
    add(them, "", reply, "gemini", v)

# 2. the player's own replies: all passes plus a spread of misses (deduped)
rows = c.q("SELECT them_text, reply, verdict FROM attempts WHERE user_id=? AND reply<>'' ORDER BY ts", (UID,))
seen, own = set(), []
for r in rows:
    k = r["reply"].strip().lower()
    if k in seen or len(k) < 4: continue
    seen.add(k); own.append(dict(r))
passes = [r for r in own if r["verdict"] == "Pass"]
misses = [r for r in own if r["verdict"] != "Pass"]
random.shuffle(misses)
for r in passes + misses[:max(0, 30 - len(passes))]:
    add(r["them_text"], "", r["reply"], "own", r["verdict"], UID)   # private to that player

# 3. LLM candidates across the quality range
TEXTS = [
 ("You matched two days ago and asked how their week is going.", "Honestly just surviving on iced coffee and spreadsheets at this point lol"),
 ("You've been texting casually for a day and asked what they're up to tonight.", "Nothing crazy, probably just going to rot on my couch and rewatch The Office for the millionth time"),
 ("You matched yesterday and texted \"how's your day going?\" an hour ago.", "Just got back from the gym, my legs are jelly"),
 ("Their profile is full of photos of their golden retriever. You asked how lunch was.", "My dog just stole my sandwich off the counter and he is not sorry"),
 ("You replied to their message within a minute. They tease you first.", "wow so you're one of those people who replies instantly huh"),
 ("You sent a long story about your neighbour's rooster waking you up at 4am.", "haha nice"),
 ("You mentioned your friends have a game night this weekend.", "I'm weirdly competitive at board games, fair warning"),
 ("You sent them a song from your road-trip playlist. They compliment you.", "ok your taste in music is actually elite, i'm impressed"),
 ("Earlier you joked that you're a self-appointed taco expert.", "I've been craving a really good taco all day and nobody here knows where to go"),
 ("You told them their profile was the funniest one you've seen this week.", "you probably say that to everyone lol"),
]
KINDS = [("sharp: specific, playful, clearly funny", "Pass"), ("decent but a bit safe or long", "Weak"),
         ("generic stock line that could answer anything", "Weak"), ("literal, advice, interview question, or too forward", "Dead End")]
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["replies"], "properties": {"replies": {"type": "array", "items": {
    "type": "object", "additionalProperties": False, "required": ["kind", "reply"], "properties": {"kind": {"type": "integer"}, "reply": {"type": "string"}}}}}}
for ctx, them in TEXTS:
    prompt = ("Write 4 different replies a person might text back, one of each kind, for rating practice. Make them realistic, like a real person typed them.\n"
              "Situation: %s\nTHEM: \"%s\"\nKinds:\n%s\nRespond ONLY with JSON {\"replies\": [{\"kind\": 1, \"reply\": \"...\"}, ...]}"
              % (ctx, them, "\n".join("%d. %s" % (i + 1, k[0]) for i, k in enumerate(KINDS))))
    out = json.loads(c.llm([{"role": "user", "content": prompt}], SCHEMA, 0.9, 500))
    for r in out["replies"][:4]:
        k = r["kind"] if 1 <= r["kind"] <= 4 else 2
        add(them, ctx, r["reply"], "llm", KINDS[k - 1][1])

n = c.q("SELECT source, intended, COUNT(*) n FROM eval_items GROUP BY source, intended")
print("pool:", [(r["source"], r["intended"], r["n"]) for r in n])
print("total:", c.q("SELECT COUNT(*) n FROM eval_items", one=True)["n"])
