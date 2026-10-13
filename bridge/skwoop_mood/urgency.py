"""urgency(text) -> bool: a deterministic, rule-based alarm check for one English line. Stdlib only.

The bridge forces alert / [crisply] whenever this returns True, so the rules lean toward firing: a missed alarm
costs more than a false one. Rules are mostly an alarm cue plus an active state, grouped by category:
- STRONG: imperatives ("get out", "call 911", "take cover"), medical emergencies (collapse, breathing, chest
  pain, stroke signs, bleeding, allergic swelling, falls, overdose), time pressure, "?!". They fire on the
  whole line and are never guarded.
- BOOST: "right now", "immediately", "ASAP", "urgent". They never fire alone; with another cue they are reported
  and keep a resolution word from cancelling it.
- DESCRIPTIVE: smoke, fire, gas, CO, alarms, leaks, water through ceilings and walls, rising water, freezers and
  fridges, tipped or smoking appliances, sirens, tornadoes, downed power lines, doors, intruders, glass
  breaking, children and pets in danger. These are checked per clause, and a clause is skipped when it also
  carries a guard: resolved or test, product talk, cooking or candles, fiction or remote news. A freezer reading
  above 10 F or a fridge reading above 45 F counts as warm.
- Line-level checks: a person collapsing or choking (figurative uses such as "collapsed on the couch" or
  "choking up" do not count); leaving in an emergency ("we have to clear out" plus fire, flood, police...);
  a missing person named by first name; a security word at night. Food words never guard an appliance
  failure, and routine event-log lines ("door opened at 3:12 pm by Sam") are guarded unless they also say
  "still open", "forced" and the like.
- v0.2.1 line-level checks: fainting, passing out or unresponsive; a weapon with a person, or a panic or emergency
  alarm with an intrusion; a child or teen who is gone, unreachable or not home.
- A line whose only hits are descriptive is dropped when it also says it is resolved ("all clear",
  "false alarm") and has no boost word.
English only. Context is not read: a line that is urgent only because of its situation returns False.
"""

from __future__ import annotations

import re
from typing import Final

from skwoop_mood.urgency_clf import default_clf, urgency_probability

_QUOTES: Final = str.maketrans({"\u2019": "'", "\u2018": "'", "\u02bc": "'", "\u2032": "'"})
_NUM: Final = r"(?:a|an|one|two|three|four|five|ten|fifteen|twenty|thirty|\d{1,2})"
_DEVICE: Final = (
    r"(?:baby monitor|smoke (?:alarm|detector)|co (?:alarm|detector|monitor)|fire alarm|carbon monoxide "
    r"(?:alarm|detector)|security (?:camera|system)|alarm system|camera|sensor|detector|monitor|sump pump)"
)


def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


# Whole-line rules; never guarded.
STRONG: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("get out", _rx(r"\b(?:get|go) (?:out|outside)\b(?! (?:the|some|a|an|your|my|our|those|these)\b)")),
    ("everyone out", _rx(r"\b(?:everyone|everybody|all of you) (?:out|outside)\b")),
    ("call 911", _rx(r"\b(?:call|dial|ring) (?:9-?1-?1|an ambulance|the fire department|the police)\b|\b911\b")),
    ("leave now", _rx(r"\bleave (?:the house|now|immediately|right now|right away|the building)\b")),
    (
        "evacuate",
        _rx(
            r"\bevacuat\w*\b(?! (?:drill|plan|planning|route|routes|map|practice|kit|bag|zone map)\b)|"
            r"\b(?:take|seek) (?:shelter|cover)\b|\bshelter in place\b"
        ),
    ),
    (
        "go to shelter",
        _rx(
            r"\b(?:get|go|head|run|hurry|move)\b[^.;!?]{0,20}\b(?:to|into|down to|in) (?:the )?(?:basement|storm "
            r"cellar|cellar|safe room|storm shelter|shelter|interior (?:room|closet|bathroom|hallway)|lowest floor)\b"
        ),
    ),
    ("don't open the door", _rx(r"\bdon'?t (?:open|answer) the door\b")),
    ("shut the valve", _rx(r"\bshut (?:off )?the (?:main |water |gas )?(?:valve|main|water|gas)\b")),
    ("opening word", _rx(r"^\s*(?:quick|help|stop|hurry)\s*[,!]|^\s*stop\b.*!")),
    ("interrobang", _rx(r"\?!|!\?")),
    ("distress interjection", _rx(r"\b(?:oh no|ugh|argh|oh god|oh my god)\b.*!")),
    ("turn it off!", _rx(r"^(?=.*!)(?=.*\b(?:turn|shut|switch)\b[^.;!?]{0,25}\boff\b)")),
    (
        "medical",
        _rx(
            r"\b(?:unconscious|unresponsive|not breathing|isn'?t breathing|stopped breathing|can'?t "
            r"breathe|trouble breathing|seizure|overdos\w*|having a (?:heart attack|stroke)|bleeding badly|"
            r"can'?t get up|fell (?:down|off|and)|passed out|fainted)\b|\bheart attack\b(?! of)"
            r"(?<!was a heart attack)(?<!is a heart attack)"
        ),
    ),
    (
        "won't wake",
        _rx(
            r"\b(?:won'?t|will not|can'?t|isn'?t|is not|not) (?:wake|waking) (?:up|him|her|them)\b|"
            r"\bcan'?t (?:wake|rouse) (?:him|her|them|mom|dad|grandma|grandpa|the baby)\b"
        ),
    ),
    (
        "chest pain",
        _rx(
            r"\bchest (?:pain|pains|hurts|is hurting|feels tight|is tight|tightness|pressure)\b|"
            r"\b(?:clutching|grabbing|holding|gripping) (?:his|her|my|their) chest\b|"
            r"\b(?:arm|face|side|leg) (?:is |went |is going |feels )?numb\b|"
            r"\bnumb(?:ness)? (?:in|down) (?:his|her|my|their) (?:arm|face|left|right|side)\b"
        ),
    ),
    (
        "stroke signs",
        _rx(
            r"\b(?:face|mouth|smile|eyelid)\b[^.;!?]{0,10}\bdroop\w*|\bdrooping on one side\b|\bslurr(?:ed|ing)\b|"
            r"\bcan'?t (?:lift|raise|move) (?:his|her|my|their) (?:arm|leg|hand)\b|\bstroke (?:symptoms|signs)\b"
        ),
    ),
    (
        "severe bleeding",
        _rx(
            r"\b(?:won'?t|will not|can'?t|doesn'?t|does not|isn'?t|is not) stop(?:ping)? (?:the )?bleeding\b|"
            r"\bbleeding (?:heavily|a lot|everywhere|through|so much|won'?t stop)\b|"
            r"\b(?:a lot|so much|lots) of blood\b|\bblood everywhere\b|\bgushing blood\b"
        ),
    ),
    (
        "allergic reaction",
        _rx(
            r"\b(?:lips?|tongue|throat|face|eyes?)\b[^.;!?]{0,8}\b(?:is |are )?(?:swelling|swollen|closing)\b|"
            r"\bswelling up\b|\banaphyla\w*\b|\b(?:needs?|use|get|grab) (?:the|her|his|my) epi-?pen\b|"
            r"\bhaving an allergic reaction\b|\bhives all over\b"
        ),
    ),
    (
        "bad fall",
        _rx(
            r"\bfell (?:in|on|down|off|from|out of) (?:the |a |his |her |my )?(?:stairs|steps|shower|tub|bathtub|"
            r"bathroom|ice|ladder|roof|bed|porch|deck)\b|\b(?:has|have) fallen\b(?! asleep| behind| off the)|"
            r"\btook a (?:bad |hard )?fall\b"
        ),
    ),
    (
        "too many pills",
        _rx(
            r"\btook (?:way )?too many\b|\btoo many (?:of )?(?:his |her |my |their |the )?(?:pills|tablets|"
            r"gummies|meds|medicines?)\b|\b(?:took|swallowed|ate) (?:a|the) (?:whole )?bottle of\b"
        ),
    ),
    ("ambulance", _rx(r"\b(?:need|get|call|send) (?:an |the )?ambulance\b")),
    (
        "swallowed",
        _rx(r"\bswallowed\b.*\b(?:pods?|pills?|bleach|batter(?:y|ies)|chemicals?|detergent|medicine|poison)\b"),
    ),
    ("late", _rx(r"\b(?:i'?m|we'?re|i am|you'?re|going to be|gonna be|running) late\b|\balmost missed\b")),
    ("deadline", _rx(rf"\b(?:starts?|leaves?|closes?|departs?|boards?|begins?|ends?) in {_NUM} minutes?\b")),
    ("ride is here", _rx(r"\b(?:taxi|cab|ride|uber|lyft|driver) is (?:already )?(?:here|outside)\b")),
    (
        "card fraud",
        _rx(
            r"\b(?:card|account)\b[^.;!?]{0,30}\b(?:used|charged|accessed)\b[^.;!?]{0,25}\b(?:another|unknown|"
            r"unrecognized|foreign|overseas|suspicious)\b|\bfraud\w*\b|\bsuspicious (?:charge|login|activity|"
            r"sign-?in)\b"
        ),
    ),
    ("heads up", _rx(r"\bheads up\b")),
    (
        "almost dead battery",
        _rx(r"\bbattery\b[^.;!?]{0,25}(?<!\d)(?:[0-9]|10|one|two|three|four|five) ?(?:percent|%)"),
    ),
)

# Boost words: reported alongside another rule, never enough on their own ("it's 72 degrees right now").
# A boost also keeps a resolution word ("all good") from cancelling a descriptive hit.
BOOST: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("right now", _rx(r"\bright (?:now|away)\b|\bnow!|\basap\b|\bthis instant\b")),
    ("immediately", _rx(r"\bimmediately\b(?! (?:after|before|following)\b)")),
    ("urgent", _rx(r"\b(?:urgent|urgently|emergency)\b")),
)

_BOOST_NAMES: Final = frozenset(name for name, _ in BOOST)
# Rule names that come from line-level checks in rule_reasons, not from the pattern tables.
LINE_CHECK_NAMES: Final = frozenset(
    {
        "collapse or choking",
        "leave in an emergency",
        "night + security",
        "missing person",
        "fainted or unresponsive",
        "armed or alarmed intruder",
        "missing child",
        "missing relative",
        "missing, reported by another adult",
        "forced entry",
        "call for help",
    }
)

# Per-clause rules; skipped in a clause that also matches a guard.
DESCRIPTIVE: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    (
        "smoke",
        _rx(
            r"\bsmoke\b(?!-free| free| alarm| detector)|\bsmoky\b|\bsmoking\b(?= (?:outlet|plug|pan|pot|oven|"
            r"stove|wire))"
        ),
    ),
    (
        "fire",
        _rx(
            r"\bthere'?s a fire\b|\bfires? (?:in|on|at|upstairs|downstairs|near|inside)\b|\bon fire\b|"
            r"\bflames?\b|\bwildfire\b|\bsparks?\b|\bburning smell\b|\bsmell(?:s|ing)? (?:of )?(?:smoke|"
            r"burning)\b|\bsomething'?s burning\b"
        ),
    ),
    ("gas", _rx(r"\bgas (?:leak|smell)\b|\bsmell(?:s|ing)? (?:of )?gas\b|\bgas is leaking\b")),
    ("carbon monoxide", _rx(r"\bcarbon monoxide\b|\bco (?:level|levels|reading)\b|\b\d+ ?ppm\b")),
    (
        "alarm going off",
        _rx(
            r"\b(?:alarm|alarms|detector|siren|sensor|monitor)\b(?! clock)[^.,;!?]{0,30}\b(?:going off|"
            r"went off|goes off|is sounding|sounding|beeping|chirping|blaring|tripped|triggered|spiked)\b"
        ),
    ),
    (
        "detected",
        _rx(r"\b(?:motion|movement|leak|smoke|water|intruder|glass break|gas|flood) (?:was |has been )?detected\b"),
    ),
    (
        "device fault",
        _rx(
            rf"\b{_DEVICE}\b[^.;!?]{{0,40}}\b(?:lost (?:its |the )?(?:signal|connection|power)|went offline|"
            rf"is offline|offline|disconnected|stopped(?: working)?|failed|dying|critically low|battery is low|"
            rf"low battery|chirping)\b"
        ),
    ),
    ("battery critical", _rx(r"\bbattery\b[^.;!?]{0,25}\b(?:critically low|critical|dying|almost dead|nearly dead)\b")),
    (
        "water moving",
        _rx(
            r"\bwater(?:'s| is| are)?\b[^.;!?]{0,30}\b(?:rising|pouring|spraying|gushing|coming in|"
            r"coming through|everywhere|leaking|dripping through)\b|\b(?:flooding|flooded|overflowing)\b|"
            r"\binches of water\b|\b(?:pipe|heater|tank|hose|main) (?:has |just )?(?:burst|split)\b|"
            r"\b(?:sensor|floor|carpet|ceiling|basement)\b[^.;!?]{0,20}\bis (?:wet|soaked)\b|"
            r"\bwater (?:is )?(?:on|all over) the (?:floor|ground)\b|\bwater all over\b"
        ),
    ),
    (
        "water through ceiling or wall",
        _rx(
            r"\b(?:water|it)\b[^.;!?]{0,25}\b(?:coming|dripping|pouring|running|seeping|leaking|spraying|"
            r"trickling|streaming)\b[^.;!?]{0,12}\b(?:from|through|down|out of|in)\b[^.;!?]{0,25}\b(?:ceiling|"
            r"walls?|light|fixture|outlet|vent|drywall)\b|\b(?:ceiling|drywall)\b[^.;!?]{0,25}\b(?:dripping|"
            r"bulging|sagging|leaking|caving|collapsing|coming down|is wet|soaked)\b|"
            r"\b(?:burst|broken|split) (?:pipe|water line|supply line)\b|\btaking on water\b"
        ),
    ),
    (
        "water spreading",
        _rx(
            r"\b(?:water|rain|drips?|dripping)\b[^.;!?]{0,40}\b(?:running|trickling|seeping|creeping|spreading|"
            r"pooling|spilling|dripping|falling|leaking|coming in|coming through|coming out)\b[^.;!?]{0,50}"
            r"\b(?:walls?|ceiling|floors?|carpet|rugs?|counters?|cabinets?|stairs|light|fixture|outlet|vent|windows?|"
            r"baseboards?|hallway|closet|hardwood|furnace|attic|basement)\b|"
            r"\b(?:toilet|tub|bathtub|sink|washer|washing machine|dishwasher)\b[^.;!?]{0,15}\boverflowed\b"
        ),
    ),
    (
        "active leak",
        _rx(
            r"\bleaks? (?:was |were |has been )?(?:detected|found)\b|"
            r"\bleak(?:s|ing)? (?:under|behind|from|coming from|near|onto|into|through|in the (?:ceiling|wall|"
            r"basement|roof|attic))\b|\b(?:is|are|it's|keeps|started|still) leaking\b|"
            r"\bleaking (?:from|under|onto|into|through|everywhere)\b"
        ),
    ),
    (
        "leak sensor alarm",
        _rx(
            r"\b(?:leak|water|flood) (?:sensor|detector)s?\b[^.;!?]{0,40}\b(?:alert|alerting|alarm|alarming|"
            r"triggered|tripped|detected|detects|wet|went off|going off)\b"
        ),
    ),
    (
        "rising water",
        _rx(
            r"\b(?:river|creek|stream|lake|pond|ditch|water|floodwater|waters)\b[^.;!?]{0,40}\b(?:rising|"
            r"climbing|over the (?:road|bank|banks|dock|driveway|bridge)|overflowing|spreading|backing up)\b|"
            r"\b(?:road|street|driveway|yard|basement|garage|bridge|drain)\b[^.;!?]{0,25}\b(?:under water|"
            r"underwater|flooded|flooding|washed out|backing up)\b|\bunder ?water\b|\bover the road\b"
        ),
    ),
    (
        "freezer warm",
        _rx(
            r"\b(?:freezer|fridge|refrigerator)\b[^.;!?]{0,40}\b(?:warm|warming|rising|climbing|thawing|"
            r"melting|off for|died|quit|stopped|stopped working|isn'?t cold|not cold|isn'?t cooling|not cooling|"
            r"went out|conked out|broke down|spoil\w*|smells? off|going bad|gone bad|stopped running|is dead|"
            r"been dead|thawed|melted|(?:lid|door) (?:was |is |has been )?(?:left )?open)\b|"
            r"\b(?:freezer|fridge|refrigerator) (?:temperature|temp) (?:is )?(?:up|high|too high)\b"
        ),
    ),
    (
        "tipped over",
        _rx(
            r"\b(?:space heater|heater|lamp|fryer|air fryer|iron|halogen)\b[^.;!?]{0,25}\b(?:tipped|fell|knocked|"
            r"toppled) (?:over|onto|on)\b"
        ),
    ),
    (
        "appliance smoking",
        _rx(
            r"\b(?:dryer|washer|outlet|plug|charger|cord|toaster|microwave|heater|furnace|motor|battery|laptop|"
            r"fan|dishwasher|extension cord|power strip)\b[^.;!?]{0,20}\b(?:smoking|sparking|melting|smold\w*|"
            r"arcing)\b|\bsmells? like (?:burning|smoke|something burning|burnt plastic|melting plastic)\b"
        ),
    ),
    (
        "sirens",
        _rx(
            r"\b(?:tornado|warning|outdoor|storm|air raid|emergency|civil defense|town) sirens?\b(?! test)|"
            r"\bsirens? (?:are |is )?(?:going|going off|sounding|wailing|blaring|howling|just (?:started|went off)|"
            r"started)\b"
        ),
    ),
    (
        "tornado",
        _rx(
            r"\bfunnel cloud\b|\btornado\b[^.;!?]{0,20}\b(?:on the ground|touched down|spotted|coming|headed|"
            r"heading|close|near|approaching|warning)\b|\btouchdown confirmed\b"
        ),
    ),
    (
        "downed power line",
        _rx(
            r"\b(?:power|electric|electrical|utility) lines?\b[^.;!?]{0,30}\b(?:down|fell|snapped|sparking|arcing|"
            r"on the (?:ground|road|car|driveway|house|fence)|across)\b|\b(?:brought|knocked|took|came|pulled) down "
            r"(?:a |the )?(?:power|electric|utility) lines?\b|\b(?:downed|fallen) (?:power |electric )?lines?\b|"
            r"\blive wires?\b|\bwires? (?:is |are )?(?:down and )?(?:sparking|arcing)\b"
        ),
    ),
    (
        "door open",
        _rx(
            r"\b(?:door|garage|gate|window|windows)\b[^.;!?]{0,30}\b(?:unlocked|wide open|left open|"
            r"standing open|still open|been open|is open|was open|opened|propped open)\b|\bunlocked\b"
        ),
    ),
    (
        "intruder",
        _rx(
            r"\b(?:someone|somebody|stranger|intruder|prowler|a man|a woman|a person|"
            r"people)\b[^.;!?]{0,40}\b(?:door|doors|window|windows|gate|yard|driveway|porch|garage|outside|"
            r"bell|handle|knob|trying|jiggling|rattling|prying|pounding|knocking|banging|ringing|walking|lurking|"
            r"climbing|breaking)\b"
        ),
    ),
    (
        "break-in",
        _rx(
            r"\b(?:intruder|prowler|break-?in|breaking in|broke into|(?:someone|somebody|they|he|"
            r"she) broke in|burglar|burglary)\b|\b(?:glass|window|windows)\b(?! break (?:sensor|detector))[^.;!?]{0,15}"
            r"\b(?:broke|shatter\w*|smash\w*|broken in|breaking)\b|\b(?:shatter\w*|smash\w*|breaking) "
            r"(?:glass|window|windows)\b"
        ),
    ),
    ("vehicle lurking", _rx(r"\b(?:car|van|truck|vehicle)\b[^.;!?]{0,20}\b(?:idling|lurking|circling|loitering)\b")),
    ("weather warning", _rx(r"\bwarnings?\b|\b(?:tornado|storm|flood|hurricane) watch\b|\bflash flood\w*\b")),
    (
        "appliance left on",
        _rx(
            r"\b(?:oven|stove|burner|iron|space heater|grill|curling iron|straightener|"
            r"hot plate)\b[^.;!?]{0,25}\b(?:has been on|been on for|still on|left on)\b|\bboiling over\b"
        ),
    ),
    (
        "pet or child out",
        _rx(
            r"\b(?:dog|cat|puppy|kitten|toddler|kid|baby|child|son|daughter|boy|girl|\w+-year-old|grandma|"
            r"grandpa)\b[^.;!?]{0,30}\b(?:got out|is loose|got loose|escaped|is missing|went missing|wandered off|"
            r"ran off|is lost|got lost|slipped out|bolted|never came home|hasn'?t come home|didn'?t come home|"
            r"isn'?t home yet|not home yet)\b(?! of the (?:bath|tub|car|crate)\b)|"
            r"\bcan'?t find (?:my|our|the) (?:son|daughter|kid|child|toddler|baby|boy|girl|dog|cat|puppy)\b|"
            r"(?<!nearly )(?<!almost )\blost (?:my|our) (?:son|daughter|kid|child|toddler|little one)\b"
        ),
    ),
    (
        "pet or child near road",
        _rx(
            r"\b(?:dog|cat|puppy|kitten|toddler|kid|child|son|daughter|\w+-year-old)\b[^.;!?]{0,40}"
            r"\b(?:toward|towards|into|onto|across|in|near|in the middle of) the (?:road|street|highway|traffic)\b"
        ),
    ),
    (
        "child near water",
        _rx(
            r"^(?=.*\b(?:toddler|baby|kid|child|little one|\w+-year-old|son|daughter)\b)"
            r"(?=.*\b(?:pool|pond|lake|hot tub|river|creek|canal)\b)"
            r"(?=.*\b(?:alone|by (?:himself|herself|themselves)|unattended|edge of|fell in|no one (?:is )?watching|"
            r"nobody (?:is )?watching|wandered)\b)"
        ),
    ),
)


def _warm_reading(clause: str) -> bool:
    """A freezer above 10 F or a fridge above 45 F, read from a number ("the freezer is at 28 degrees")."""
    m = _READING.search(clause)
    if m is None:
        return False

    value = -int(m.group("deg")) if m.group("neg") else int(m.group("deg"))

    return value > (10 if m.group("box") == "freezer" else 45)


_READING: Final = _rx(
    r"\b(?P<box>freezer|fridge|refrigerator)\b[^.;!?]{0,30}?\b(?:at|reads?|reading|is|hit|hits|up to|to)\s+"
    r"(?P<neg>minus |-)?(?P<deg>\d{1,3}) ?(?:degrees|deg|°)"
)


# A night-time hint plus a security noun anywhere in the line.
_NIGHT: Final = _rx(
    r"\b(?:1[0-2]|[1-4])(?::\d\d)? ?a\.?m\b|\b(?:one|two|three|four) (?:a\.?m\b|in the morning)|"
    r"\b\d{1,2}(?::\d\d)? in the morning\b|\bmidnight\b|\btonight\b|\bin the middle of the night\b"
)
_SECURITY: Final = _rx(
    r"\b(?:door|gate|garage|window|motion|camera|someone|somebody|stranger|yard|driveway|unlocked|open)\b"
)

# Product talk ("the detector arrived"); a missing-person check ignores it ("she never arrived").
_PRODUCT: Final = _rx(
    r"\b(?:arrived|on sale|came with|ordered|delivered|for sale|bought|installed|set it up|stocked|batteries)\b"
)
# "The storm never showed up" is resolved; "he never showed up at school" is a missing child (v0.2.2).
_NEVER_SHOWED: Final = _rx(r"\bnever showed up\b")
_GUARDS: Final[tuple[re.Pattern[str], ...]] = (
    _rx(
        r"\b(?:fixed|repaired|replaced|resolved|passed|lifted|cancell?ed|expired|is over(?! the)|was over(?! the)|"
        r"went fine|false alarm|just a test|a test|drill|all clear|all good|is fine|are fine|"
        r"everything'?s fine|no issues|turned out)\b"
    ),
    _NEVER_SHOWED,
    _rx(r"\bno (?:leaks?|smoke|fire|gas|water|damage|sign|signs|issues?|problems?)\b|\bnothing (?:wrong|to worry)\b"),
    _PRODUCT,
    _rx(
        r"^(?!.*\b(?:still|left|wide|forced|unknown|unrecognized|stranger|kicked|pried|broken|alarm)\b)"
        r"(?:.*\b(?:opened|unlocked|closed|locked|disarmed)\b(?: by (?:code|keypad|app|key|[a-z]+))?"
        r"[^.;!?]{0,25}\bat \d{1,2}:\d\d\b|.*\b(?:opened|unlocked) \d+ times\b)"
    ),
    # v0.2.2: a lock event that names who or what did it ("unlocked 4:47pm with Sam's code").
    _rx(
        r"^(?!.*\b(?:still|left|wide|forced|unknown|unrecognized|stranger|kicked|pried|broken|alarm)\b)"
        r".*\b(?:opened|unlocked|closed|locked|disarmed)\b[^.;!?]{0,40}\b(?:with|by|using|via) (?:the )?"
        r"(?:[a-z']+ ){0,3}(?:code|keypad|key ?fob|fob|phone|app|remote|key)\b"
    ),
    _rx(
        r"\b(?:movie|show|tv|book|novel|story|chapter|episode|game|song|album)\b|\b(?:news (?:says|said|"
        r"reports|reported)|on the news|states? away|miles away|in another (?:city|town|country))\b"
    ),
)
# Food and smoke-making words guard most rules, but never an appliance failure ("the freezer quit, the meat
# is thawing").
_FOOD_GUARD: Final = _rx(
    r"\b(?:brisket|salmon|ribs|meat|bbq|barbecue|smoker|candles?|incense|cigar\w*|vape|mouth|tongue|"
    r"chili|hot sauce|spicy|fireplace|fire pit|campfire)\b"
)
_IGNORES_FOOD_GUARD: Final = frozenset({"freezer warm", "tipped over", "appliance smoking"})

# A person collapsing or choking; figurative uses ("collapsed on the couch", "choking up") do not count.
_COLLAPSE: Final = _rx(r"\b(?:collapsed|choking)\b")
_FIGURATIVE_COLLAPSE: Final = _rx(
    r"\b(?:i|we) (?:\w+ )?collapsed (?:on|onto|into|in) (?:the |my |a |our )?(?:couch|sofa|bed|chair|recliner|"
    r"hammock|heap|pile)\b|\bcollapsed (?:with|from) laughter\b|\bcollapsed laughing\b|"
    r"\b(?:cake|souffle|bread|dough|meringue|loaf|tent|fort|defen[cs]e|offense|team|lead|markets?|stocks?|"
    r"prices|deal|talks|negotiations|plans?|economy|government|coalition|empire|league|company|campaign|"
    r"ratings|bubble)s? (?:\w+ )?collapsed\b|"
    r"\bchok(?:ing|ed) (?:up|back)\b|"
    r"\b(?:vines?|weeds?|plants?|ivy|roots?|algae|traffic|smog)\b[^.;!?]{0,15}\bchok(?:ing|es?)\b"
)

# Leaving in an emergency: an evacuation-shaped phrase plus an emergency word somewhere in the line.
_LEAVE: Final = _rx(
    r"\bclear (?:out|the (?:building|house|area))\b|\b(?:have|has|had|need|needs|got) to (?:leave|go|get out)\b|"
    r"\btold (?:us|everyone|everybody|people|residents) to (?:leave|go|get out)\b|"
    r"\b(?:telling|ordering) (?:us|everyone|everybody|people|residents) to (?:leave|go|get out)\b|"
    r"\bleave the (?:area|house|home|building|apartment|neighborhood|block|premises)\b|\bmust leave\b"
)
_EMERGENCY: Final = _rx(
    r"\b(?:fires?|smoke|smoky|flood\w*|gas|fumes|chemical|spill|burning|wildfire|levee|dam|police|sheriff|"
    r"deputy|deputies|firefighters?|fire department|authorities|officials|within the hour)\b"
)
# A missing person named only by first name (checked on the whole line, which may span clauses).
_MISSING_PERSON: Final = _rx(
    r"\bcan'?t find (?!(?:my|our|the|a|an|your|his|her|their|it|them|this|that|anything|anyone|one|time|"
    r"work|out|parking|a spot|any\w*)\b)[a-z]+ anywhere\b|\b[a-z]+ (?:is|went) missing[,.;]? (?:she|he)\b|"
    r"\b(?:nobody|no one) knows where (?:he|she) (?:is|went)\b"
)
_DRILL: Final = _rx(r"\b(?:drill|practice|plan|a test|just a test)\b")

# v0.2.1 line-level checks (weak rules: the classifier must agree unless the trainer puts them in the floor).
_FAINTED: Final = _rx(
    r"\b(?:fainted|fainting|blacked out|lost consciousness|losing consciousness|keeled over|unresponsive|"
    r"not responding|isn'?t responding|won'?t respond|out cold|slumped over|went limp|gone limp|"
    r"won'?t wake up|can'?t (?:wake|rouse) (?:him|her|them))\b|"
    r"\bpass(?:ed|ing|es)? out\b(?! (?:the|free|flyers?|samples?|candy|worksheets?|papers?|wristbands?|tickets?)\b)"
)
_WEAPON: Final = _rx(
    r"\b(?:knife|knives|gun|guns|pistol|handgun|shotgun|rifle|machete|hatchet|axe|crowbar|tire iron|pry bar|"
    r"baseball bat|bat|pipe|hammer)\b"
)
_STRANGER: Final = _rx(
    r"\b(?:man|men|guy|guys|someone|somebody|stranger|intruder|person|people|he|they|my ex)\b[^.;!?]{0,40}"
    r"\b(?:with|has|have|had|holding|waving|carrying|pointing|swinging|using|is|'s|are)\b"
)
_FORCING: Final = _rx(
    r"\b(?:jimmy(?:ing)?|jimmied|pry(?:ing)?|pried|forc(?:e|es|ed|ing)|kick(?:s|ed|ing)?|ram(?:s|med|ming)|"
    r"shov(?:e|es|ed|ing)|slamm(?:ed|ing) into|wrench(?:es|ed|ing)?|cut(?:ting)?|pop(?:ping)?|rattl(?:e|es|ed|ing)|"
    r"break(?:ing)? (?:down|open))\b[^.;!?]{0,30}\b(?:door|doors|window|windows|lock|slider|screen|gate|frame|"
    r"knob|handle|garage)\b|\b(?:door|window|lock)\b[^.;!?]{0,15}\b(?:kicked in|forced|pried)\b"
)
_MASKED: Final = _rx(
    r"\b(?:masked|masks on|in (?:a )?(?:ski )?masks?|wearing a (?:ski )?mask|balaclava|face covered)\b"
)
_ENTRY_POINT: Final = _rx(r"\b(?:door|doors|window|windows|porch|yard|house|knob|lock|slider|garage)\b")
_PERSON_HERE: Final = _rx(
    r"\b(?:someone|someone'?s|somebody|man|men|guy|guys|stranger|person|people|they'?re|they|he'?s|he|two|"
    r"masked)\b"
)
_PANIC_ALARM: Final = _rx(r"\b(?:panic|emergency) (?:button|alarm|code)\b|\bhit the alarm\b")
_INTRUSION: Final = _rx(
    r"\b(?:broke in|breaking in|break-in|broken into|forced (?:the|our|a) door|kicked in|someone|somebody|"
    r"stranger|intruder|footsteps)\b"
)
_CHILD: Final = _rx(
    r"\b(?:son|daughter|kid|kids|child|children|toddler|teen|teenager|twins|niece|nephew|boy|girl|baby|"
    r"grandson|granddaughter|grandkids?|grandchild(?:ren)?|little (?:brother|sister)|"
    r"\d{1,2} year old|\w+-year-old)\b"
)
# v0.2.2: adults whose absence is an emergency in the same shapes ("dad went hiking ... hasn't called").
_RELATIVE: Final = _rx(
    r"\b(?:husband|wife|mom|mother|dad|father|grandma|grandpa|grandmother|grandfather|brother|sister|aunt|"
    r"uncle|partner)\b"
)
_DEMENTIA: Final = _rx(r"\b(?:dementia|alzheimer'?s|memory (?:problems|loss)|confused)\b")
# Another adult reporting the absence ("the coach says she never arrived").
_REPORTER: Final = _rx(
    r"\b(?:school|front office|teacher|coach|counselor|neighbor|nanny|babysitter|sitter|daycare|staff|"
    r"manager|bus driver|(?:friend'?s|his|her) (?:mom|dad|mother|father|parents)|instructor|leader)\b"
)
# A call for help said as a plea (v0.2.3, broadened in v0.2.4). The line is first cleaned: misspelt help and
# please ("hlep", "halp", "pleese", "plz") become help and please, and stage notes ("(whispering)", "*crying*")
# are dropped but count as a strong cue.
_FUZZY_HELP: Final = _rx(r"\b(?:h+e+l+p+|hlep|halp+|hepl|hel+o?p)(?:(me)\b|\b)")
_FUZZY_PLEASE: Final = _rx(r"\b(?:ple+a*[sz]e*|pl[sz]+|plis|pweese)\b")
_STAGE_NOTE: Final = _rx(
    r"[\[(*]\s*(?:whisper\w*|crying|cries|sobbing|sobs|quietly|softly|scared|panick\w*|screaming|shaking|"
    r"terrified|in tears|breathless|gasping|shouting|yelling)\b[^\])*]{0,20}[\])*]|^\s*whisper(?:s|ing|ed)?\s*:"
)
_PLEA_GATE: Final = _rx(r"\b(?:h+e+l+p+|hlep|halp+|hepl|helpme)\b|\bcome\b|\bget (?:over )?here\b")

_ADDRESS: Final = (
    r"(?:hey|skwoop|oh god|oh no|omg|mommy|mommie|mom|mum|mama|momma|ma|daddy|dad|papa|nana|grandma|grandpa|"
    r"granny|gran|honey|babe|guys|someone|somebody|anyone|anybody|please)"
)
_PLEA_TAIL: Final = (
    r"(?:me|us|him|her|them|please|help|now|quick|quickly|hurry|fast|asap|someone|somebody|anyone|anybody|"
    r"mommy|mom|mama|daddy|dad|skwoop)"
)
# The whole line is the call: address words, then help, then me/us/please/now or more help.
_HELP_ALONE: Final = _rx(rf"^(?:{_ADDRESS}[\s,!.]+)*help(?:[\s,!.]+{_PLEA_TAIL})*[\s!.]*$")
# Shapes of a call inside a longer line.
_HELP_CALL: Final = _rx(
    rf"(?:^|[.!?,;]\s*)(?:{_ADDRESS}[\s,]+)*help\b|\b(?:some|any)(?:body|one),? (?:please )?help\b|"
    r"\bplease\b[^.;!?]{0,12}\bhelp\b|\byou help me\b|"
    r"\b(?:i|we) (?:really |urgently )?need (?:some |your |urgent )?help\b|\b(?:come|send|get|go get) help\b|"
    r"\bneed help (?:now|right now|asap|please|quick)\b|"
    r"\b(?:screaming|yelling|shouting|crying|calling) for help\b"
)
# "Come quick" and "somebody come" are pleas without the word help.
_COME_PLEA: Final = _rx(
    r"\b(?:some|any)(?:body|one),? (?:please )?come\b(?! (?:get|see|look|watch|try|eat|play|over for)\b)|"
    r"\bcome (?:here |over )?(?:quick|quickly|fast|right now|asap)\b|\bhurry,? (?:come|get here|get over here)\b|"
    r"\bplease,? come (?:quick|quickly|fast|now|here now)\b|\bget (?:over )?here (?:now|quick|fast|right now)\b"
)
# Any "come" is a call when a strong cue rides along ("please come, i hurt").
_COME_ANY: Final = _rx(
    rf"(?:^|[.!?,;]\s*)(?:{_ADDRESS}[\s,]+)*(?:please,? )?come\b|\b(?:you|someone|somebody|anyone|anybody) "
    r"(?:please )?come\b|\bplease,? come\b"
)
# Good news and food: "come quick, the baby is walking", "come quick, dinner's ready".
_COME_FUN: Final = _rx(
    r"\b(?:look|see|watch|ready|dinner|lunch|breakfast|food|show|surprise|cute|funny|trick|rainbow|sunset|"
    r"sunrise|cake|cookies?|pizza|starting|started|movie|tv|snow|snowing|walking|talking|puppy|kitten|fireworks|"
    r"eclipse|amazing|beautiful|guess what|news|birthday|presents?|eat|eating|ice cream|melting|treats?)\b"
)
# Time pressure makes a help call a plea, but it is part of "come quick" itself, so a come plea needs distress.
_HELP_HURRY: Final = _rx(r"\b(?:quick|quickly|hurry|urgent|urgently|asap|right now|right away|immediately)\b")
_HELP_DISTRESS: Final = _rx(
    r"\b(?:emergency|911|"
    r"(?:screaming|yelling|shouting|crying|calling) for help|hurts?|hurt|hurting|in (?:so much |a lot of )?pain|"
    r"bleeding|blood|scared|afraid|terrified|frightened|panicking|dying|injured|trapped|"
    r"(?:i'?m|i am|we'?re|we are|he'?s|she'?s|they'?re|got) stuck(?! on)|"
    r"fell(?! (?:asleep|for|behind|apart|in love|through))|fallen|ow+|ouch|"
    r"(?:can'?t|cannot|no can|can not) (?:move|get up|breathe|stand|walk|feel)|"
    r"(?:not|won'?t|doesn'?t|isn'?t|no) (?:wake|waking|wake up|breathe|breathing|moving|respond\w*))\b|"
    r"(?:^|[.!?,;]\s*)(?:is|it is|its|it'?s) (?:very |so |really )?bad\b"
)
_HELP_CUE: Final = _rx(r"\b(?:please|now|fast|someone|somebody|anyone|anybody|i need help|we need help)\b")
# Not a plea: help with a task, joking, charity appeals, help as a feature or menu, the song or a play request,
# offers and thanks, idioms, and everyday complaints.
_HELP_NOT_PLEA: Final = _rx(
    r"\bhelp (?:(?:me|us|him|her|them|my|our|his|her|the) (?:[a-z]+ )?)?(?:with|on|find|finding|pick|choose|"
    r"decide|make|cook|plan|write|fix|set|figure|learn|understand|remember|study|clean|move|carry|wrap|bake|"
    r"build|spell|translate|do|organize|look|search|name|think|practice|prepare|order|book|buy|shop|sort|pack|"
    r"get ready|come up|pick out|work out|read|draft|force|open|unlock|hang|install|assemble|change|load)\b|"
    r"\bhelp (?:with|on|for|to|[a-z]+ing)\b|"
    r"\b(?:lol|lmao|haha|jk|just kidding|kidding)\b|"
    r"\bhelp us help\b|\b(?:donat(?:e)|donation|donations|charity|fundrais\w*|volunteer\w*|sponsor|your gift)\b|"
    r"\b(?:play|playing|played|song|songs|beatles|album|track|lyrics|listen|listening|soundtrack|movie|film|"
    r"episode|playlist|spotify|sing|singing|karaoke)\b|"
    r"\bhelp (?:menu|page|section|screen|center|centre|desk|line|button|command|file|docs?|article|text|"
    r"option|options|mode|tab|key|feature|wanted|settings|topics|tips|guide)\b|\bwhat (?:does|is|can) (?:the )?"
    r"help\b|\bthe help\b|\bhelp yourself\b|\bsay help\b|\b(?:can|could|may|shall|should) (?:i|we) help\b|"
    r"\bhow (?:can|do|could|may) (?:i|we) help\b|\b(?:do|does|did) (?:you|he|she|they|anyone|anybody) need "
    r"(?:any )?help\b|\b(?:thanks|thank you|appreciate)\b[^.;!?]{0,20}\bhelp\b|\bhelp you\b|"
    r"\b(?:can'?t|cannot|couldn'?t) help\b|\bso help me\b|\b(?:bored|boring|hungry|starving|mondays?|homework)\b"
)
# Negated or resolved: "no need to get help", "I'm fine now", "never mind".
_HELP_RESOLVED: Final = _rx(
    r"\b(?:no need (?:to|for)|(?:don'?t|doesn'?t|didn'?t|do not|does not) need|not anymore|no longer|anymore|"
    r"never ?mind|nvm|no help needed|don'?t (?:send|get|call)|without (?:any )?help|got it now|figured it out|"
    r"nothing to worry|all good now|all clear|false alarm|just a test|a test|drill|turned out|resolved|"
    r"(?:i'?m|i am|we'?re|we are|he'?s|she'?s|it'?s|all|everything'?s?) "
    r"(?:fine|ok|okay|good|alright|all right|safe) now)\b"
)
_HELP_GAME: Final = _rx(
    r"\b(?:game|games|gaming|minecraft|fortnite|roblox|zelda|mario|level|boss|pretend|pretending|role ?play\w*|"
    r"hide and seek|my character|npc|quest|the scene|script|rehears\w*|my lines|the play|acting|in the story|"
    r"novel|halloween|haunted house|escape room)\b"
)
# A question ("can you help me?", "help?") needs a strong cue to count as a plea.
_HELP_QUESTION: Final = _rx(r"\b(?:can|could|would|will) (?:you|someone|somebody|anyone|anybody)\b|\bhelp\b[^.!;]*\?")
# A short line that opens with help, then a person or a break ("help the baby", "mommy help, spider").
_HELP_OPENS: Final = _rx(
    rf"^(?:{_ADDRESS}[\s,!.]+)*help(?:[\s!.]*$|[,!.]|\s+(?:me|us|him|her|them|my|the|our|{_PLEA_TAIL})\b)"
)
_SHORT_PLEA_WORDS: Final = 4
# "Come quick" without a strong cue is a plea only when it is the whole line ("someone come, please!"); a longer
# line ("come quick, dinner's on the table") is left to the classifier.
_COME_ALONE: Final = _rx(
    rf"^(?:{_ADDRESS}[\s,!.]+)*(?:hurry[\s,!.]+)?(?:come|get)(?: over)?(?: here)?(?:[\s,!.]+{_PLEA_TAIL}|"
    r"[\s,!.]+right now)*[\s!.]*$"
)
# "help me" then any other word is a task ("help me pck a dress", with typos), unless a strong cue rides along.
_PLEA_FOLLOW: Final = (
    r"(?:please|now|quick|quickly|fast|hurry|asap|right|someone|somebody|anyone|anybody|i|im|i'?m|it|its|it'?s|"
    r"there|theres|there'?s|he|hes|he'?s|she|shes|she'?s|they|we|my|mom|mommy|mama|dad|daddy|papa|nana|grandma|"
    r"grandpa|up|out|oh|god|is|no|this|skwoop|guys|help|me|us)"
)
_HELP_TASK_ANY: Final = _rx(rf"\bhelp (?:me|us|him|her|them) (?!{_PLEA_FOLLOW}\b)[a-z]")


def _plea_text(line: str) -> tuple[str, bool]:
    """The line with stage notes dropped and misspelt help/please fixed, and whether a stage note was there."""
    noted = bool(_STAGE_NOTE.search(line))
    text = _STAGE_NOTE.sub(" ", line)
    text = _FUZZY_HELP.sub(lambda m: "help me" if m.group(1) else "help", text)

    return " ".join(_FUZZY_PLEASE.sub("please", text).split()), noted


def _help_excluded(text: str) -> bool:
    return bool(_HELP_NOT_PLEA.search(text) or _HELP_RESOLVED.search(text) or _HELP_GAME.search(text))


def _opening_help_excluded(line: str) -> bool:
    """A line opening with "help," that is a task, the song, a game or resolved ("help, never mind, I'm fine now").
    The "opening word" rule ignores it (v0.2.4)."""
    text, _ = _plea_text(line)

    return text.startswith("help") and _help_excluded(text)


def _help_plea(line: str) -> bool:
    """A call for help said as a plea (v0.2.4). See the patterns above for what is and is not one."""
    text, noted = _plea_text(line)
    if _help_excluded(text):
        return False

    distress = noted or bool(_HELP_DISTRESS.search(text))
    strong = distress or bool(_HELP_HURRY.search(text))
    if _HELP_QUESTION.search(text) and not strong:
        return False
    if not strong and _HELP_TASK_ANY.search(text):
        return False
    if _HELP_ALONE.search(text):
        return True
    words = len(text.split())
    if _HELP_OPENS.search(text) and words <= _SHORT_PLEA_WORDS:
        return True
    if distress and (_COME_ANY.search(text) or _COME_PLEA.search(text)) and not _COME_FUN.search(text):
        return True
    if _COME_PLEA.search(text) and _COME_ALONE.search(text):
        return True
    if not _HELP_CALL.search(text):
        return False

    return strong or bool(_HELP_CUE.search(text))


# Long-past events ("someone pried our window last year") are not alarms.
_LONG_AGO: Final = _rx(r"\b(?:last (?:year|month|summer|winter|spring|fall)|years ago|months ago)\b")
_GONE: Final = _rx(
    r"\b(?:never came (?:home|back)|didn'?t come (?:home|back)|hasn'?t (?:come|been) (?:home|back)|"
    r"isn'?t (?:home|back)|still isn'?t home|not (?:home|back) yet|(?:has|have) been gone|(?:was|is|are) gone|"
    r"can'?t find|cannot find|can'?t reach|(?:nobody|no one) can reach|(?:nobody|no one) has heard|"
    r"hasn'?t checked in|(?:isn'?t|not) answering|won'?t pick up|straight to voicemail|bed (?:is|was) empty|"
    r"bed wasn'?t slept in|last seen|never got on|never (?:showed|turned) up|was never in|out all night|"
    r"snuck out|separated from|"
    # v0.2.2
    r"never (?:arrived|got there|made it|came in|checked in)|didn'?t (?:make it|get off|get on)|"
    r"hasn'?t (?:shown up|called|come back)|nowhere to be (?:found|seen)|nowhere in the|isn'?t anywhere|"
    r"no sign of|(?:isn'?t|wasn'?t|not) in (?:his|her|their) (?:room|bed|crib)|bed hasn'?t been slept in|"
    r"never made it|room is empty|wandered (?:off|away)|walked out|slipped out|bolted|split up|got separated|"
    r"lost sight of|let go of my hand|\w+ hours? late|(?:was|is) due (?:home|back)|not picking up|"
    r"(?:nobody|no one) (?:has seen|knows where)|hasn'?t seen (?:him|her|them)|can'?t locate|marked absent|"
    r"(?:isn'?t|wasn'?t) (?:there|at (?:school|practice|drop-off|the \w+)))\b"
)
# A missing-person line that says the person is accounted for, planned or in a game.
_ACCOUNTED_FOR: Final = _rx(
    r"\b(?:found (?:him|her|them)|found safe|(?:is|are|was|were|got) (?:home|back)(?! (?:yet|by))|turned up|"
    r"(?:he|she|they) (?:texted|called|messaged)|texted (?:from|that)|called from|checked in (?:at|like)|"
    r"on (?:his|her|their) way|safe now|walked (?:him|her|them) home|the whole time|i can see (?:him|her|them)|"
    r"sleepover|sleeping (?:over|at)|staying (?:the night|overnight|at)|spending the night|like we (?:agreed|"
    r"arranged)|planned|hide and seek|lol)\b"
)

_RESOLVED: Final = _rx(
    r"\b(?:all clear|all good|everything(?:'s| is) fine|false alarm|just a test|"
    r"nothing to worry about|no need to worry)\b"
)
_CLAUSE_SPLIT: Final = re.compile(r"[.;!?]+(?:\s|$)|,\s*|\s+but\s+")


def parse_text(text: str | None) -> str | None:
    """Boundary: None, non-strings and blank lines become None; otherwise lower case with straight quotes."""
    if not isinstance(text, str):
        return None

    clean = " ".join(text.translate(_QUOTES).lower().split())

    return clean or None


def rule_reasons(text: str | None) -> tuple[str, ...]:
    """Names of the rules (v0.1.2 plus the three v0.2.1 line checks) that fire; empty when none fires."""
    line = parse_text(text)
    if line is None:
        return ()

    boost = tuple(name for name, rx in BOOST if rx.search(line))
    strong = tuple(name for name, rx in STRONG if rx.search(line))
    if "opening word" in strong and _opening_help_excluded(line):
        strong = tuple(name for name in strong if name != "opening word")
    if _COLLAPSE.search(line) and not _FIGURATIVE_COLLAPSE.search(line):
        strong += ("collapse or choking",)
    if _LEAVE.search(line) and _EMERGENCY.search(line) and not _DRILL.search(line):
        strong += ("leave in an emergency",)
    if _PLEA_GATE.search(line) and _help_plea(line) and not _LONG_AGO.search(line):
        strong += ("call for help",)
    if strong:
        return strong + boost

    found: list[str] = []
    if _NIGHT.search(line) and _SECURITY.search(line):
        found.append("night + security")
    line_guards = [g for g in _GUARDS if g.search(line)]
    unguarded = not line_guards
    if unguarded and _MISSING_PERSON.search(line):
        found.append("missing person")
    if unguarded and _FAINTED.search(line):
        found.append("fainted or unresponsive")
    if unguarded and (
        (_WEAPON.search(line) and _STRANGER.search(line)) or (_PANIC_ALARM.search(line) and _INTRUSION.search(line))
    ):
        found.append("armed or alarmed intruder")
    missing_ok = all(g is _NEVER_SHOWED or g is _PRODUCT for g in line_guards)
    recent = not _LONG_AGO.search(line)
    if missing_ok and recent and not _ACCOUNTED_FOR.search(line) and _GONE.search(line):
        if _CHILD.search(line):
            found.append("missing child")
        elif _RELATIVE.search(line) or _DEMENTIA.search(line):
            found.append("missing relative")
        elif _REPORTER.search(line):
            found.append("missing, reported by another adult")
    if (
        unguarded
        and recent
        and _PERSON_HERE.search(line)
        and (_FORCING.search(line) or (_MASKED.search(line) and _ENTRY_POINT.search(line)))
    ):
        found.append("forced entry")

    # A clause is a substring of the line, so a rule that misses the whole line misses every clause. Checking the
    # line first skips most per-clause work (same result; v0.2.2 latency).
    on_line = [(name, rx) for name, rx in DESCRIPTIVE if rx.search(line)]
    reading = bool(_READING.search(line))
    for clause in _CLAUSE_SPLIT.split(line) if on_line or reading else ():
        if not clause or any(g.search(clause) for g in _GUARDS):
            continue

        food = bool(_FOOD_GUARD.search(clause))
        found += [
            name
            for name, rx in on_line
            if rx.search(clause) and name not in found and (not food or name in _IGNORES_FOOD_GUARD)
        ]
        if reading and _warm_reading(clause) and "freezer warm" not in found:
            found.append("freezer warm")

    if not found or (_RESOLVED.search(line) and not boost):
        return ()

    return (*found, *boost)


def urgency_rules(text: str | None) -> bool:
    """Every rule on its own, without the classifier (v0.1.2 plus the v0.2.1 line checks)."""
    return bool(rule_reasons(text))


def urgency_reasons(text: str | None) -> tuple[str, ...]:
    """Why the line is urgent; empty when it is not.

    - A floor rule (high precision, stored with the classifier) fires on its own: its rule names are returned.
    - Any other rule fires only when the classifier agrees (p >= threshold_weak): its rule names are returned.
    - With no rule, ("classifier",) when p >= threshold.
    Boost words ("right now", "urgent") never count as a rule here; they only ride along.
    """
    reasons = rule_reasons(text)
    clf = default_clf()
    core = [r for r in reasons if r not in _BOOST_NAMES]
    if any(r in clf.floor_rules for r in core):
        return reasons

    p = urgency_probability(text, clf)
    if core and p >= clf.threshold_weak:
        return reasons

    return ("classifier",) if p >= clf.threshold else ()


def urgency(text: str | None) -> bool:
    """True when the line reads as an alarm or urgent request (v1.4 round 2: floor rules, classifier-confirmed
    rules, or the classifier alone).

    The bridge then forces alert / [crisply].
    """
    return bool(urgency_reasons(text))
