# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The one list of conversations across every channel: the inbox.

Every place that shows "who wrote, when, and does somebody wait for me" reads the rows built
here: the agent's `inbox` tool, `vaf inbox list`, the `/api/inbox` routes behind the
Posteingang window, and the per-channel windows' own lists. The predicates are pure functions
over the store's grouped overview, so the four surfaces cannot disagree about what "unread",
"waits for you" or "done" means, and a test can pin each rule without a database.

Five sources, one row shape: the messenger chats of the channel message store (WhatsApp,
Telegram, Discord, groups included), mail threads (v2 store), and A2A rooms. The person's
own state per messenger chat lives in `channel_message_store.chat_marks`; mail keeps IMAP's
Seen flag as its read marker and rooms their cursor, both take the done mark from the same
table. Listing is store-only and never waits on a bridge: a GET must answer at once, and a
chat the store never saw has nothing to say about unread or waiting anyway.

Facade: CONSIDERED AND LEFT OFF. Every consumer is first-party (a tool, a command, a route,
a window); the public facade is a versioned promise, and the first embedder who asks for a
cross-channel inbox is the measurement that earns an export.
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from vaf.core.channel_message_store import OWNER_SENDER

CHANNELS: Tuple[str, ...] = ("whatsapp", "telegram", "discord", "mail", "room")
MESSENGERS: Tuple[str, ...] = ("whatsapp", "telegram", "discord")
VIEWS: Tuple[str, ...] = ("all", "waits", "unread", "agent")

WAITS_UNANSWERED = "unanswered"
WAITS_OWNER_ASKED = "owner_asked"
WAITS_INVITATION = "invitation"

# Whether an inbound message asks for an answer is decided from its text alone, no model
# (see reply_expectation): a score from 0 to 1, and the configured threshold turns it into
# "waits for you". 0.6 is the point where a plain greeting or statement still waits and a
# short thank-you, goodbye or acknowledgement does not.
WAITS_THRESHOLD_DEFAULT = 0.6

# The question marks of every script a chat may arrive in: ASCII, fullwidth CJK, Arabic,
# Armenian, and the two emoji marks (the voice agent's is_question reads the same set for a
# spoken reply; it is not imported here because that module carries the whole voice stack).
_QUESTION_MARKS = "?？؟՞❓❔"

# What is stripped before anything is read: a link's own "?" is not a question, and a
# question quoted from somebody else asks nothing of the reader.
_URL_RE = re.compile(r"https?://\S+|www\.\S+|\b[\w-]+(?:\.[\w-]+)*\.(?:de|com|net|org|io|at|ch|eu|info|app)(?:/\S*)?")
_QUOTED_RE = re.compile(r"\"[^\"\n]{1,200}\"|„[^“”\n]{1,200}[“”]|“[^”\n]{1,200}”|»[^«\n]{1,200}«")
# A greeting opens a chat; it is skipped so that what follows it decides.
_GREETING_RE = re.compile(
    r"^(?:hallo|hallöchen|hi|hey|huhu|moin moin|moin|servus|grüß dich|grüß gott|grüezi|guten morgen|guten tag|"
    r"guten abend|mahlzeit|hello|good morning|good afternoon|good evening|dear|liebe|lieber|sehr geehrte|"
    r"sehr geehrter)(?:\s+(?:frau|herr|mr|mrs|ms|dr))?(?=\s|$)", re.IGNORECASE)
# The addressee after a greeting ("Hallo Max", "Dear Mrs Smith") is told by its capital letter.
_ADDRESSEE_RE = re.compile(r"^(?:[A-ZÄÖÜ][^\s]*\s*){1,2}")
# Chat fillers in front of a clause, skipped before the question-opener test.
_FILLER_RE = re.compile(r"^(?:(?:und|aber|also|oder|ja|jaa|ok|okay|danke|dankeschön|hallo|hi|hey|so|oh|ach|na|dann|hm+|äh+|well|yes|no|thanks)\s+)+")
# Idioms that carry a question word without asking: "wie besprochen" is a statement.
_IDIOM_RE = re.compile(r"\bwie (?:besprochen|gesagt|gewünscht|abgemacht|vereinbart|geplant|immer|üblich|erwartet|versprochen|angekündigt)\b")

# A message that is, or begins with, a thank-you, a goodbye, an acknowledgement or a
# deferral closes an exchange rather than opening one. The list is German and English
# plus the thanks and goodbyes of the languages a German chat commonly borrows.
_CLOSERS = (
    # thanks
    "danke", "dankeschön", "danke schön", "danke dir", "danke sehr", "danke euch", "danke ihnen", "vielen dank",
    "vielen lieben dank", "lieben dank", "besten dank", "herzlichen dank", "tausend dank", "recht herzlichen dank",
    "merci", "thanks", "thank you", "thank u", "thx", "ty", "cheers", "much appreciated", "appreciated",
    "teşekkürler", "teşekkür ederim", "sağ ol", "gracias", "muchas gracias", "merci beaucoup", "grazie",
    "ありがとう", "ありがとうございます", "ありがとうございました", "どうも", "谢谢", "谢谢你", "多谢", "感谢",
    # you're welcome
    "bitte", "bitte schön", "bitteschön", "bitte gerne", "bitte gern", "gern geschehen", "gerne geschehen",
    "nichts zu danken", "kein problem", "kein ding", "kein thema", "keine ursache", "youre welcome", "gern", "gerne",
    "you are welcome", "anytime", "my pleasure", "no problem", "no worries", "np", "de nada", "de rien", "rica ederim",
    # acknowledgements
    "ok", "okay", "okey", "oki", "k", "kk", "alles klar", "alles gut", "alles gute", "passt", "passt so", "passt schon",
    "perfekt", "super", "top", "prima", "gut", "sehr gut", "in ordnung", "verstanden", "verstehe", "verstehe ich",
    "klingt gut", "klingt super", "klingt prima", "hört sich gut an", "sounds great", "sounds like a plan", "machs gut",
    "mach es gut", "sehr gerne", "notiert", "ist notiert", "läuft", "aber gerne", "aber gern", "aber klar", "aber sicher",
    "aber natürlich", "aber nein", "but of course", "have a great weekend", "have a nice weekend", "have a good weekend",
    "have a great week", "have a great day", "have a nice evening", "schönen urlaub", "schöne ferien",
    "leider nein", "leider nicht", "bestätigt", "bin angekommen", "gut angekommen", "bin zuhause", "bin zu hause",
    "sehen wir uns", "machen wir so", "machen wir es so", "komm gut heim", "komm gut nach hause", "kommt gut heim",
    "kommen sie gut", "gute fahrt", "gute heimfahrt",
    "genau", "stimmt", "richtig", "klar", "geht klar", "mach ich", "mache ich", "machen wir", "wird gemacht",
    "erledigt", "abgemacht", "einverstanden", "wunderbar", "toll", "cool", "nice", "fine", "sure", "great",
    "perfect", "awesome", "got it", "gotcha", "noted", "understood", "alright", "all right", "all good", "sounds good",
    "will do", "on it", "done", "agreed", "deal", "yes", "yep", "yeah", "yup", "no", "nope", "ja", "jaa", "jo",
    "jep", "jup", "jap", "nein", "nö", "ne", "nee", "tamam", "vale", "daccord", "はい", "了解", "好的", "好",
    # goodbyes and wishes
    "bis später", "bis dann", "bis morgen", "bis bald", "bis gleich", "bis nachher", "bis heute abend",
    "bis montag", "bis dienstag", "bis mittwoch", "bis donnerstag", "bis freitag", "bis samstag", "bis sonntag",
    "bis nächste woche", "bis zum wochenende", "tschüss", "tschüß", "tschau", "ciao", "ade", "baba",
    "schönen tag", "schönen tag noch", "schönen abend", "schönen abend noch", "schönen feierabend",
    "schönes wochenende", "schönen sonntag", "gute nacht", "schlaf gut", "gute besserung", "gute reise",
    "guten flug", "viel erfolg", "viel spaß", "dir auch", "euch auch", "ihnen auch", "gleichfalls", "ebenso",
    "lg", "vg", "mfg", "liebe grüße", "viele grüße", "beste grüße", "schöne grüße", "herzliche grüße",
    "freundliche grüße", "mit freundlichen grüßen", "grüße", "gruß", "bye", "goodbye", "see you", "see ya", "see u",
    "later", "take care", "good night", "have a nice day", "have a good one", "talk soon", "safe travels",
    "good luck", "you too", "same to you", "best regards", "kind regards", "regards", "best", "görüşürüz",
    "hasta luego", "adiós", "au revoir", "bonne journée", "à bientôt", "さようなら", "また", "再见", "拜拜",
    # reactions and deferrals
    "haha", "hahaha", "hehe", "lol", "freu mich", "freue mich", "ich bin dann mal weg", "muss los", "muss jetzt los",
    "melde mich", "meld mich", "ich melde mich", "melden uns", "sag dir bescheid", "sage dir bescheid",
    "geb dir bescheid", "gebe dir bescheid", "geben bescheid", "bin gleich zurück", "bin gleich wieder da",
    "get back to you", "ill let you know", "will let you know", "keep you posted",
)
# Closers that count only when the message IS one of them (or begins with one): inside a
# longer message these words are ordinary words ("Donnerstag passt", "alles gut bei dir?").
_START_ONLY = frozenset((
    "passt", "passt so", "passt schon", "gut", "sehr gut", "klar", "genau", "stimmt", "richtig", "super", "top",
    "prima", "perfekt", "toll", "cool", "nice", "fine", "sure", "great", "perfect", "awesome", "done", "later",
    "best", "yes", "no", "ja", "nein", "ne", "nee", "jo", "ok", "okay", "k", "kk", "alles klar", "bestätigt",
    "verstehe", "erledigt", "deal", "bitte", "ade", "baba", "gern", "gerne", "また", "好", "はい",
    "bis später", "bis dann", "bis morgen", "bis bald", "bis gleich", "bis nachher", "bis heute abend", "bis montag",
    "bis dienstag", "bis mittwoch", "bis donnerstag", "bis freitag", "bis samstag", "bis sonntag", "bis nächste woche",
    "bis zum wochenende",
))
# A goodbye with a day in it ("bis Freitag") is a deadline inside a message ("das muss bis
# Freitag fertig sein") and a goodbye only at its end.
_BIS_END_RE = re.compile(
    r"\bbis (?:später|dann|morgen|bald|gleich|nachher|heute abend|montag|dienstag|mittwoch|donnerstag|freitag|samstag|"
    r"sonntag|nächste woche|zum wochenende)(?: dann)?$")
# Explicit "nothing to answer" markers, anywhere in the message.
_NO_REPLY_RE = re.compile(
    r"\b(?:no action (?:needed|required)|no need to (?:reply|respond|answer)|no reply (?:needed|necessary)|"
    r"just fyi|fyi|for your information|just so you know|just letting you know|nur zur info|zur info|zur kenntnis|"
    r"kein handlungsbedarf|keine antwort nötig|keine antwort erforderlich|musst nicht antworten|"
    r"brauchst nicht(?: zu)? antworten|nur damit du es weißt|nur damit ihr es wisst)\b")
# A deferral is the answer ("ich sag dir morgen Bescheid", "kann ich dir morgen sagen"): the
# other side will write again, nothing waits.
_DEFERRAL_RE = re.compile(
    r"\b(?:(?:sag|sage|geb|gebe|gib) (?:dir|euch|ihnen) (?:\w+ ){0,3}bescheid|"
    r"sag ich (?:dir|euch|ihnen) (?:\w+ ){0,3}(?:bescheid|später|morgen|heute|nachher|dann|nächste woche|am \w+)|"
    r"ich melde? mich|melde? mich (?:später|morgen|dann|wieder|nachher|heute|gleich|bei dir|bei euch|sobald|wenn|wegen|nochmal|noch mal)|"
    r"melden uns|kann ich (?:dir|euch|ihnen) (?:\w+ ){0,3}sagen|get back to you|let you know|keep you posted)\b", re.IGNORECASE)
# A short confirmation ("10 Uhr passt", "Termin bestätigt", "Montag geht") closes a proposal.
_CONFIRM_RE = re.compile(
    r"^(?:\w+ ){1,6}(?:passt(?: (?:mir|uns|bei mir))?(?: gut| super| perfekt| prima)?|geht(?: klar| auch| gut| bei mir)?|ist ok|ist okay|"
    r"ist in ordnung|bestätigt|klingt gut|ist gut|works(?: for me)?|is fine|is ok|is okay|is good|suits me)$")
# An automatic reply, a confirmation, a list footer: text nobody wrote for this reader.
_AUTO_TEXT_RE = re.compile(
    r"out of (?:the )?office|abwesenheitsnotiz|automatic(?:al)? reply|automatische antwort|"
    r"auto ?reply|this is an automated|automatically generated|automatisch generiert|automatisch erstellt|"
    r"do not reply to this|bitte nicht auf diese|not monitored|wird nicht gelesen|you are receiving this|"
    r"sie erhalten diese|du erhältst diese|unsubscribe here|unsubscribe from this|to unsubscribe|click here to unsubscribe|"
    r"hier abbestellen|hier abmelden|newsletter abbestellen|vom newsletter abmelden|"
    r"(?:your|ihr|dein) (?:verification|security|one time|login|access) code|(?:ihr|dein) (?:bestätigungscode|sicherheitscode|anmeldecode)|"
    r"code lautet|is your (?:\w+ )?code|your code is|reset your password|password reset (?:link|request)|link zum zurücksetzen|"
    r"(?:your|ihre|deine) (?:order|bestellung) (?:has|was|is|ist|wurde|wird|nr|no|number)|order confirmation|bestellbestätigung|"
    r"has been shipped|wurde versandt|ist unterwegs|is on its way|sendungsnummer|tracking number|transaction id|"
    r"transaktionsnummer|ihr paket|your (?:package|parcel)|confirm your e ?mail|bestätigen sie ihre e ?mail|by clicking|"
    r"klicken sie (?:hier|auf)|thank you for (?:registering|signing up|your (?:order|purchase))|"
    r"(?:vielen )?dank für (?:ihre|deine) (?:bestellung|registrierung|anmeldung)|"
    r"your account has|ihr konto wurde|view (?:this |it )?(?:e ?mail )?in (?:your )?browser|im browser (?:ansehen|anzeigen|öffnen)|"
    r"no longer wish to receive|manage (?:your )?(?:e ?mail )?preferences|this e ?mail was sent to|diese e ?mail wurde an|"
    r"all rights reserved|alle rechte vorbehalten")
# A salutation to a class of people opens a mass mail ("Dear DeepSeek API user,", "Liebe
# Kundin, lieber Kunde,"): at the very start, the class noun right before the comma, and
# never a title (Herr Leser is a person). Read on the text with its punctuation.
_MASS_SALUTATION_RE = re.compile(
    r"^\s*(?:dear|hello|hi|hallo|liebe[rs]?|sehr geehrte[rs]?|guten tag)\s+(?:(?:valued|esteemed|liebe[rs]?|geschätzte[rs]?)\s+)?"
    r"(?:(?!(?:herr|frau|mr|mrs|ms|dr)\b)[\w-]+\s+){0,2}"
    r"(?:user|customer|member|subscriber|client|developer|guest|reader|shopper|traveller|traveler|patron|kundin|kunde|kundinnen|kunden|"
    r"kund[:*_]?innen|nutzerin|nutzer|nutzerinnen|nutzer[:*_]?innen|mitglied|mitglieder|community|abonnentin|abonnent|abonnenten|"
    r"leserin|leser|leser[:*_]?innen)s?\s*[,!:.\n]", re.IGNORECASE)
# Question openers at the start of a clause (after a greeting or a filler): the shape a
# question takes when the writer skipped the question mark.
_QUESTION_OPENER_RE = re.compile(
    r"^(?:wann|wie|wo|was|wer|wen|wem|warum|wieso|weshalb|welche[rsnm]?|wieviel|wie viel|wie viele|wohin|woher|"
    r"when|what|where|how|why|who|whom|which|oder|und du|und ihr|und sie|und dir|und selbst|und bei dir|und bei euch|und ihnen|und bei ihnen|"
    r"kannst du|könntest du|könnten wir|können wir|können sie|könnten sie|könnt ihr|würdest du|würden sie|"
    r"willst du|wollt ihr|wollen wir|soll ich|sollen wir|kann ich|darf ich|dürfen wir|kommst du|kommt ihr|"
    r"hast du|habt ihr|haben sie|gibt es|gibts|ist das|ist es|ist er|ist sie|geht das|geht es|gehts|passt das|passt dir|"
    r"passt es|passt euch|passt ihnen|bist du|seid ihr|sind sie|magst du|brauchst du|hättest du|hätten sie|"
    r"do you|did you|does it|does that|is it|is there|is that|are you|are there|will you|would you|could you|"
    r"can you|can i|shall i|should i|should we|have you|has it|any chance|are we|were you|was it|"
    r"[a-zäöüß]+st du|[a-zäöüß]+t ihr|[a-zäöüß]+en (?:sie|wir))\b")
# Requests and offers anywhere in the message.
_ANY_CUE_RE = re.compile(
    r"(?<![\w])(?:bitte|please|pls|plz|let me know|lass es mich wissen|melde dich|meldet euch|sag mir|sagt mir|"
    r"sag bescheid|sagt bescheid|gib bescheid|gebt bescheid|schick mir|schickt mir|schick uns|schickst du|sende mir|"
    r"send me|send us|send it|eine frage|ne frage|kurze frage|quick question|one question|brauchen?(?! (?:nichts|keine|kein|nix)\b)|"
    r"bräuchten?|ich hätte gern|ich hätte gerne|hätte lieber|würde gern|würde gerne|könnte|könnten wir|"
    r"was meinst du|was meint ihr|what do you think|if you could|if you can|if you have|wäre es möglich|"
    r"is it possible|vorschlag:|termin\?|könntest|könntet|würdest|würdet|"
    # an objection needs something before it, and "aber gerne" or "danke, aber nein" is an answer
    r"(?<=\w )(?:aber|but|however|allerdings|jedoch)(?! (?:nein|no|gerne|gern|klar|sicher|natürlich|ja|of course|sure|danke|thanks|nicht nötig|kein problem)\b)|"
    r"eine sache noch|noch eine sache|noch etwas|noch was|one more thing|funktioniert nicht|geht (?:\w+ ){0,3}nicht|passt (?:\w+ ){0,3}nicht|klappt nicht|"
    r"doesnt work|does not work|not working|(?:das|ein|the|a) problem|problems?(?: ist| mit| bei| with)|"
    r"(?:passt|geht|klappt|ok|okay|alles gut|alles klar|in ordnung)(?: \w+){0,3} (?:bei|für) (?:dir|euch|ihnen|dich)$)(?![\w])")
_WORD_RE = re.compile(r"[^\W\d_]+")
_REPEAT_RE = re.compile(r"(.)\1{2,}")
_CLOSER_START_RE = re.compile(r"^(?:" + "|".join(re.escape(c) for c in sorted(_CLOSERS, key=len, reverse=True)) + r")(?:\s|$)")
_CLOSER_ANY_RE = re.compile(r"(?<![\w])(?:" + "|".join(re.escape(c) for c in sorted((c for c in _CLOSERS if c not in _START_ONLY), key=len, reverse=True)) + r")(?![\w])")
_CLOSER_SET = frozenset(_CLOSERS)


def _normalize(text: str) -> str:
    """Words only, case kept: apostrophes are joined ("geht's" reads as "gehts"), punctuation
    and emoji become spaces, letter runs of three or more shrink to one ("dankeee" reads as
    "danke"), whitespace collapses."""
    norm = re.sub(r"[^\w\s]", " ", text.replace("'", "").replace("\u2019", ""))
    norm = _REPEAT_RE.sub(r"\1", norm)
    return re.sub(r"\s+", " ", norm).strip()


def _clause_core(cased: str) -> str:
    """A normalized clause without its greeting and addressee, lowercased: "Hallo Max, danke
    dir" reads as "danke dir". Without a greeting the clause is returned as it is; a bare
    greeting comes back empty."""
    rest = _GREETING_RE.sub("", cased, count=1).strip()
    if rest != cased.strip():
        rest = _ADDRESSEE_RE.sub("", rest, count=1).strip()
    return rest.lower()


def _message_core(body: str) -> str:
    """The message without its greeting and addressee, normalized and lowercased: "Hallo
    Max, danke dir" and "Hallo Anna! Ja, ich komme" read as "danke dir" and "ja ich komme".
    The addressee is read in the head before the first punctuation only, so the capital
    of the next sentence is not taken for a second name. Without a greeting the whole
    message comes back; a bare greeting comes back empty."""
    m = re.match(r"\s*([^,;:.!?\n]*)", body)
    head, rest = m.group(1), body[m.end():]
    head_norm = _normalize(head)
    stripped = _GREETING_RE.sub("", head_norm, count=1).strip()
    if stripped == head_norm.strip():
        return _normalize(body).lower()
    stripped = _ADDRESSEE_RE.sub("", stripped, count=1).strip()
    return (stripped + " " + _normalize(rest)).strip().lower()


def _opens_a_question(body: str) -> bool:
    """A clause that starts (after a greeting, an addressee or a filler) like a question or
    a request."""
    for clause in re.split(r"[,;:.!\n]+", body):
        c = _IDIOM_RE.sub(" ", _clause_core(_normalize(clause))).strip()
        if c.endswith(" oder"):
            return True
        # "ja und du", "ok oder nein": each filler is dropped in turn, and what follows it
        # is tested as an opener ("und du", "oder") before the next one goes.
        parts = c.split(" ")
        for i in range(min(len(parts), 5)):
            rest = " ".join(parts[i:])
            opener = _QUESTION_OPENER_RE.match(rest)
            if _FILLER_RE.match(parts[i] + " "):
                # "ja und du": the filler itself may open the question that follows it.
                if opener:
                    return True
                continue
            # A clause that starts with a closer is not a question ("dann sehen wir uns
            # morgen", "kommen Sie gut heim"), whatever its verb-pronoun shape; the longer
            # reading wins where the two overlap ("passt dir Donnerstag" asks).
            closer = _CLOSER_START_RE.match(rest)
            if opener and not (closer and closer.end() >= opener.end() + 1):
                return True
            break
    return False


def reply_expectation(text: str) -> float:
    """How much an inbound message asks for an answer, 0 to 1, from the text alone.

    No model. Links and quoted speech are stripped first; an automatic reply, a
    confirmation or a list footer scores 0. Then: a question mark in any script (+0.4) or
    a question opener, a request or an objection (+0.2) raise the score; a message that
    is or begins with a thank-you, goodbye, acknowledgement or deferral (after a greeting)
    lowers it (-0.5, or -0.2 when a request follows it); a closer inside a message of up to
    twelve words lowers it a little (-0.3); an explicit "nothing to answer" marker lowers it
    (-0.5); a deferral ("ich sag dir morgen Bescheid": the other side will write again)
    counts as a closer (-0.5, -0.2 when a request follows it), and a chat filler ("ja", "ok",
    "danke") in front of a question or a request is no closer at all; a short confirmation
    ("10 Uhr passt", "Freitag geht bei mir") and a goodbye with a day in it at the very end
    count as a closer inside; emoji and digits are not words, and an emoji-only message
    counts as none (-0.4); length nudges it up (+0.1 above four words, +0.15 above twelve).
    A plain
    greeting or statement lands at 0.6: it opened the exchange and waits; "danke", "bis
    später", "ok" or a lone thumbs-up land near 0. A question mark outweighs a closer, so
    "ok?" asks. The threshold that turns the number into "waits for you" is
    `inbox_waits_threshold`."""
    raw = str(text or "").strip()
    if not raw:
        return 0.0
    body = _QUOTED_RE.sub(" ", _URL_RE.sub(" ", raw))
    cased = _normalize(body)
    norm = cased.lower()
    # A mass mail's salutation settles it; a template word settles it only without a question
    # mark, because a person asking about their order or their code is a person.
    if _MASS_SALUTATION_RE.match(body):
        return 0.0
    asks = any(m in body for m in _QUESTION_MARKS)
    if not asks and _AUTO_TEXT_RE.search(norm):
        return 0.0
    words = _WORD_RE.findall(norm)
    n = len(words)
    # The closer test reads past a greeting and an addressee ("Hallo Max, danke dir"); a bare
    # greeting is its own message.
    core = _message_core(body) or norm
    exact_closer = core in _CLOSER_SET
    no_reply = bool(_NO_REPLY_RE.search(norm))
    deferred = bool(_DEFERRAL_RE.search(norm))
    # The request test reads past the deferral itself ("kann ich dir morgen sagen" is not a
    # "kann ich" question) and past the idioms ("wie besprochen").
    plain = _IDIOM_RE.sub(" ", _DEFERRAL_RE.sub(" ", norm))
    cued = (not exact_closer) and (not no_reply) and (_opens_a_question(_DEFERRAL_RE.sub(" ", body)) or bool(_ANY_CUE_RE.search(plain)))
    score = 0.6
    if asks:
        score += 0.4
    if cued:
        score += 0.2
    if n == 0:
        score -= 0.4
    elif n > 12:
        score += 0.15
    elif n > 4:
        score += 0.1
    # A closer with a question mark is a question ("ok?", "passt Donnerstag?"): the mark wins.
    # A closer followed by a request ("danke, schick mir bitte die Adresse") is only the polite
    # opening: it lowers the score a little, not by half.
    if not asks:
        if no_reply:
            score -= 0.5
        elif exact_closer or deferred or _CLOSER_START_RE.match(core):
            # "ja und du", "ok, schick mir die Adresse": a chat filler in front of a question
            # or a request is not a closer at all.
            if not (cued and _FILLER_RE.match(core + " ")):
                score -= 0.2 if cued else 0.5
        elif n <= 12 and not cued and (_CLOSER_ANY_RE.search(norm) or _CONFIRM_RE.match(core) or _BIS_END_RE.search(norm)):
            score -= 0.3
    return max(0.0, min(1.0, round(score, 3)))


# A sender that reads no answer: the address's local part or its display name says so
# (strong tokens anywhere, role names only as the whole local part, so "info@" and "Max
# Info" stay people), or the sync filed the mail under a non-primary Gmail category.
_AUTOMATED_TOKEN_RE = re.compile(
    r"no[-_.]?reply|do[-_.]?not[-_.]?reply|dont[-_.]?reply|notifications?|newsletters?|mailer[-_.]?daemon|postmaster|"
    r"auto[-_.]?reply|autoreply|unsubscribe|bounce[-_.]?handler")
_AUTOMATED_NAME_RE = re.compile(
    r"\b(?:no[-_. ]?reply|do[-_. ]?not[-_. ]?reply|dont[-_. ]?reply|mailer[-_. ]?daemon|postmaster|auto[-_. ]?reply|"
    r"notifications?|newsletters?|unsubscribe)\b")
_AUTOMATED_LOCALS = frozenset((
    "news", "alerts", "alert", "status", "updates", "update", "digest", "bounce", "bounces", "automated", "robot",
    "marketing", "promo", "promotions", "system", "daemon", "notify", "mailer", "noreply", "nobody",
))
_AUTOMATED_CATEGORIES = frozenset(("promotions", "social", "updates", "forums", "newsletter", "newsletters"))


# The categories that make a mail thread bulk mail: the provider's tabs and the labels the
# mail client offers, plus the words a person or a sender rule may file a thread under.
# "junkemail" is the legacy Outlook spelling the phishing scorer in mail_utils accepts too.
_BULK_CATEGORIES = frozenset(_AUTOMATED_CATEGORIES | {"spam", "junk", "junkemail", "marketing", "notifications", "ads", "advertising"})


def is_bulk_mail(thread: Dict[str, Any]) -> bool:
    """Whether a mail thread is bulk mail, which the inbox hides unless asked: it sits in
    the Junk folder (the provider's or the person's own placement, which outranks any tab
    stamp: Gmail stamps every message outside INBOX primary), its newest message's category
    is a bulk one (the provider's tab, the person's own label in the mail client, or a sender
    rule), or, with no category at all, its sender reads no answer (`is_automated_sender`:
    no-reply, notifications, newsletters). A thread filed under primary or under a label of
    the person's own is never bulk, whatever its sender: the person's or the provider's word
    wins over the heuristic."""
    if str(thread.get("newest_special_use") or "").lower() == "\\junk":
        return True
    category = str(thread.get("category") or "").strip().lower()
    if category == "primary":
        return False
    if category:
        return category in _BULK_CATEGORIES
    return is_automated_sender(str(thread.get("from_addr") or ""))


def is_automated_sender(from_addr: str, category: Optional[str] = None) -> bool:
    """Whether a mail's sender is something that reads no answer: a no-reply, do-not-reply
    or notification address, a newsletter, a mailer daemon, a status page or an alert
    feed, by the local part of the address (the strong tokens anywhere in it, the role
    names such as status or alerts only as the whole local part) or a strong token in its
    display name ("Do Not Reply", "GitHub Notifications"), or a mail the sync filed under a
    non-primary Gmail category (promotions, social, updates, forums). "info@", "support@",
    "Max Info" and "Status Meier" are people."""
    if (category or "").strip().lower() in _AUTOMATED_CATEGORIES:
        return True
    from email.utils import parseaddr
    name, addr = parseaddr(str(from_addr or "").strip())
    if not addr and "@" not in (from_addr or ""):
        name = str(from_addr or "")
    local = addr.split("@", 1)[0].strip().lower()
    if local and (local in _AUTOMATED_LOCALS or _AUTOMATED_TOKEN_RE.search(local)):
        return True
    return bool(_AUTOMATED_NAME_RE.search(name.lower()))


def waits_threshold() -> float:
    """The configured threshold (`inbox_waits_threshold`, clamped to 0..1)."""
    try:
        from vaf.core.config import Config
        value = float(Config.get("inbox_waits_threshold", WAITS_THRESHOLD_DEFAULT))
    except Exception:
        value = WAITS_THRESHOLD_DEFAULT
    return max(0.0, min(1.0, value))


def expects_answer(text: str, threshold: Optional[float] = None) -> bool:
    """Whether the newest inbound message waits for an answer, by the score and the threshold."""
    t = waits_threshold() if threshold is None else threshold
    return reply_expectation(text) >= t

_CHANNEL_NAMES = {"whatsapp": "WhatsApp", "telegram": "Telegram", "discord": "Discord", "mail": "Mail", "room": "Room"}


# -- pure rules --------------------------------------------------------------------------------

def reply_window_until(agent_ts: Optional[float], in_within_ts: Optional[float],
                       window_seconds: float) -> Optional[float]:
    """The bridge's reply-window rule, on the overview's two inputs: the agent's own message
    opens the window, and a reply the contact sent inside it extends it. No agent message,
    no window; the person's own sends never open one (they are not `agent_ts`)."""
    if not window_seconds or window_seconds <= 0 or agent_ts is None:
        return None
    until = float(agent_ts) + float(window_seconds)
    if in_within_ts is not None and float(in_within_ts) > float(agent_ts):
        until = max(until, float(in_within_ts) + float(window_seconds))
    return until


# The group shape of each messenger as a LIKE pattern, for the store's channel-wide read
# (a test pins that it agrees with `is_group`).
_GROUP_LIKE = {"whatsapp": "%@g.us", "telegram": "-%"}


def is_group(channel: str, chat_id: str) -> bool:
    cid = str(chat_id or "")
    if channel == "whatsapp":
        return cid.endswith("@g.us")
    if channel == "telegram":
        return cid.startswith("-")
    return channel == "room"


def chat_state(row: Dict[str, Any], *, now: Optional[float] = None,
               waits_threshold_value: Optional[float] = None) -> Dict[str, Any]:
    """The person's view of one messenger chat, from one overview row.

    unread: inbound rows after the seen marker (the overview counted them).
    answered_by_agent: the newest row is the agent's own send.
    done: marked done and nothing newer arrived, or the newest row is the person's own reply.
    waits: not done, not opened since, and either the agent asked the person about this
    chat and neither the person nor the agent has written since, or the newest row is the
    other side's, nobody answered, and its text asks for an answer (`reply_expectation` at
    or above the threshold: a "danke" or a "bis später" waits for nobody). Opening the chat
    (the seen mark) takes it off "waits": the person read it and decides for themselves
    whether to answer; the agent's reply lifts "waits" too and does not close the row, the
    person may still want to see what was said in their name.
    `waits_threshold_value` defaults to the configured `inbox_waits_threshold`."""
    last_ts = float(row.get("last_ts") or 0.0)
    last_direction = row.get("last_direction") or ""
    last_sender = row.get("last_sender") or ""
    done_ts = row.get("done_ts")
    owner_asked_ts = row.get("owner_asked_ts")
    last_agent_ts = row.get("last_agent_ts")
    last_owner_ts = row.get("last_owner_ts")
    newest_is_owner = last_direction == "out" and last_sender == OWNER_SENDER
    newest_is_agent = last_direction == "out" and last_sender != OWNER_SENDER
    done = newest_is_owner or (done_ts is not None and float(done_ts) >= last_ts)
    floor = max(float(done_ts or 0.0), float(last_owner_ts or 0.0), float(last_agent_ts or 0.0),
                float(row.get("seen_ts") or 0.0))
    owner_asked_pending = owner_asked_ts is not None and float(owner_asked_ts) > floor
    unread = int(row.get("unread") or 0)
    waits_reason = ""
    if not done:
        if owner_asked_pending:
            waits_reason = WAITS_OWNER_ASKED
        elif unread > 0 and last_direction == "in" and expects_answer(row.get("last_body") or "", waits_threshold_value):
            waits_reason = WAITS_UNANSWERED
    preview_from = "you" if newest_is_owner else ("agent" if newest_is_agent else "them")
    return {
        "unread": unread,
        "waits": bool(waits_reason),
        "waits_reason": waits_reason,
        "answered_by_agent": newest_is_agent,
        "done": done,
        "preview_from": preview_from,
    }


def chat_mode(channel: str, chat_id: str, *, owners: Set[str], contacts: Set[str], relays: Set[str],
              reply_window_until_ts: Optional[float], now: float, needs_assign: bool = False) -> str:
    """Which lane answers in this chat, the words the channel windows already use."""
    if channel == "discord":
        return "admin"
    if needs_assign:
        return "needs_assign"
    cid = str(chat_id or "")
    if cid in owners:
        return "owner"
    if channel == "telegram" and cid in relays:
        return "relay"
    if cid in contacts:
        return "contact"
    if channel == "whatsapp" and reply_window_until_ts is not None and reply_window_until_ts > now:
        return "conversation"
    return "readonly"


def mail_thread_state(thread: Dict[str, Any], mark: Optional[Dict[str, Any]], *,
                      waits_threshold_value: Optional[float] = None) -> Dict[str, Any]:
    """A mail thread's state: unread is IMAP's count, the last word was ours when the newest
    message sits in the Sent folder, and the thread waits when it is not done, still unread
    (opening it marks its messages read, and a read thread is the person's to answer or
    not), the last word was the correspondent's, nobody marked it answered, the sender is
    somebody who reads answers (`is_automated_sender`: no-reply, notifications,
    newsletters, status pages and non-primary Gmail categories never wait), and its text
    asks for an answer (the newest message's snippet through `reply_expectation`, as a chat
    message would be)."""
    newest_in_sent = str(thread.get("newest_special_use") or "").lower() == "\\sent"
    # The newest message alone: an older reply in the thread says nothing about the mail
    # that arrived after it.
    answered = bool(thread.get("newest_answered_at"))
    last_ts = float(thread.get("last_date_ts") or 0.0)
    done_ts = (mark or {}).get("done_ts")
    done = newest_in_sent or (done_ts is not None and float(done_ts) >= last_ts)
    unread = int(thread.get("unread_count") or 0)
    waits = ((not done) and (not answered) and unread > 0
             and not is_automated_sender(thread.get("from_addr") or "", thread.get("category"))
             and expects_answer(thread.get("snippet") or thread.get("subject") or "", waits_threshold_value))
    return {
        "unread": unread,
        "waits": waits,
        "waits_reason": WAITS_UNANSWERED if waits else "",
        "answered_by_agent": answered,
        "done": done,
        "preview_from": "you" if newest_in_sent else "them",
    }


def room_state(room: Dict[str, Any], mark: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """A room's state: unread is the person's own reading position; an invitation waits for
    an answer; a room with unread frames waits until the person looked, or marked it done."""
    last_ts = float(room.get("last_ts") or 0.0)
    done_ts = (mark or {}).get("done_ts")
    done = done_ts is not None and float(done_ts) >= last_ts and not room.get("invited")
    unread = int(room.get("unread") or 0)
    if room.get("invited"):
        reason = WAITS_INVITATION
    elif not done and unread > 0:
        reason = WAITS_UNANSWERED
    else:
        reason = ""
    last = room.get("last") or {}
    return {
        "unread": unread,
        "waits": bool(reason),
        "waits_reason": reason,
        "answered_by_agent": False,
        "done": done,
        "preview_from": "you" if last.get("mine") else str(last.get("sender") or "them"),
    }


# -- identity ---------------------------------------------------------------------------------

def _row_username(channel: str, username: Optional[str]) -> str:
    from vaf.core.contacts_store import message_channel_username
    return message_channel_username(channel, username)


def _local_admin(username: Optional[str], user_scope_id: Optional[str]) -> bool:
    from vaf.core.contacts_store import is_local_admin_caller
    return is_local_admin_caller(username, user_scope_id)


def _display_name(channel: str, chat_id: str, chat_name: str, username: Optional[str],
                  user_scope_id: Optional[str]) -> str:
    name = (chat_name or "").strip()
    if name:
        return name
    if channel == "whatsapp" and str(chat_id).startswith("+"):
        try:
            from vaf.core.contacts_store import get_contact_name_by_phone
            found = get_contact_name_by_phone(chat_id, username, user_scope_id=user_scope_id)
            if found and found.strip():
                return found.strip()
        except Exception:
            pass
    return str(chat_id or "")


def _session_id(channel: str, chat_id: str, username: Optional[str]) -> str:
    if channel == "whatsapp":
        from vaf.core.messaging_connections import whatsapp_session_id
        return whatsapp_session_id(username, chat_id, fallback="")
    return f"{channel}_{chat_id}"


def _lid_needs_assign(chat_id: str, lid_map: Dict[str, Any]) -> bool:
    """A WhatsApp chat keyed by a LID that `whatsapp_config.lid_to_e164` (resolved once per
    listing by the caller) does not map to a number yet."""
    cid = str(chat_id or "")
    if "@lid" not in cid:
        return False
    return not str(lid_map.get(cid) or "").strip()


# -- lanes ------------------------------------------------------------------------------------

def _messenger_rows(username: Optional[str], user_scope_id: Optional[str], channels: Iterable[str],
                    *, now: float) -> List[Dict[str, Any]]:
    from vaf.core.channel_message_store import chat_overview, store_exists
    from vaf.core.messaging_connections import owner_endpoints, reply_window_hours
    rows: List[Dict[str, Any]] = []
    window = reply_window_hours() * 3600.0
    threshold = waits_threshold()
    # The WhatsApp window's compose rule: the person writes where the agent does not answer,
    # which is every chat once the channel switch is off.
    lid_map: Dict[str, Any] = {}
    try:
        from vaf.core.config import Config
        wc = Config.get("whatsapp_config") or {}
        whatsapp_off = isinstance(wc, dict) and wc.get("inbound_to_agent", True) is False
        lid_map = dict((wc.get("lid_to_e164") or {}) if isinstance(wc, dict) else {})
    except Exception:
        whatsapp_off = False
    for channel in channels:
        if channel not in MESSENGERS:
            continue
        if channel == "discord" and not _local_admin(username, user_scope_id):
            continue
        row_user = _row_username(channel, username)
        scope = user_scope_id if channel != "discord" else None
        if not store_exists(row_user, scope):
            continue
        try:
            overview = chat_overview(row_user, user_scope_id=scope, channel=channel, limit=500,
                                     reply_window_seconds=window if channel == "whatsapp" else None)
        except Exception:
            continue
        owners = owner_endpoints(channel, username, user_scope_id)
        relays = owner_endpoints(channel, username, user_scope_id, relay=True) if channel == "telegram" else set()
        try:
            from vaf.core.contacts_store import front_office_endpoints
            contacts = set(front_office_endpoints(username, user_scope_id, channel) or ())
        except Exception:
            contacts = set()
        for o in overview:
            chat_id = str(o.get("chat_id") or "")
            state = chat_state(o, now=now, waits_threshold_value=threshold)
            until = reply_window_until(o.get("last_agent_ts"), o.get("last_in_within_ts"), window) \
                if channel == "whatsapp" else None
            needs_assign = channel == "whatsapp" and _lid_needs_assign(chat_id, lid_map)
            mode = chat_mode(channel, chat_id, owners=owners, contacts=contacts, relays=relays,
                             reply_window_until_ts=until, now=now, needs_assign=needs_assign)
            rows.append({
                "key": f"{channel}:{chat_id}",
                "channel": channel,
                "id": chat_id,
                "name": _display_name(channel, chat_id, o.get("chat_name") or "", username, user_scope_id),
                "preview": o.get("last_body") or "",
                "preview_from": state["preview_from"],
                "last_ts": float(o.get("last_ts") or 0.0),
                "message_count": int(o.get("message_count") or 0),
                "unread": state["unread"],
                "waits": state["waits"],
                "waits_reason": state["waits_reason"],
                "answered_by_agent": state["answered_by_agent"],
                "done": state["done"],
                "is_group": is_group(channel, chat_id),
                "mode": mode,
                "reply_window_until": until,
                "can_compose": channel == "whatsapp" and not needs_assign and (mode == "readonly" or whatsapp_off),
                "session_id": _session_id(channel, chat_id, username),
                "jump": {"channel": channel, "chat_id": chat_id},
            })
    return rows


def _mail_rows(username: Optional[str], user_scope_id: Optional[str], *, limit: int,
               account_id: Optional[str] = None, folder: Optional[str] = None,
               svc: Any = None, include_bulk: bool = False) -> List[Dict[str, Any]]:
    from vaf.tools.mail_utils import mail_v2_active
    if not mail_v2_active(username or "", user_scope_id) or not user_scope_id:
        return []
    from vaf.mail.store import MailStore
    if not MailStore.exists(user_scope_id):
        return []
    from vaf.core.channel_message_store import chat_marks
    from vaf.mail.service import MailService
    svc = svc or MailService(user_scope_id)
    marks = chat_marks(username or "", user_scope_id, channel="mail")
    threshold = waits_threshold()
    rows: List[Dict[str, Any]] = []
    # The store hands out 200 threads a page. With bulk mail hidden the lane pages on
    # until it holds `limit` primary threads (or the store runs dry, at most five pages),
    # so a real conversation behind a wall of newsletters still reaches the inbox.
    want = min(max(int(limit), 1), 200)
    primary = 0
    offset = 0
    seen: Set[str] = set()
    for _page in range(5):
        batch = svc.list_threads(account_id=account_id or None, folder=folder or None, limit=200, offset=offset)
        for t in batch:
            # A sync between two pages shifts the order by one: the thread that closed the
            # last page opens the next, and is listed once.
            if str(t.get("thread_id")) in seen:
                continue
            seen.add(str(t.get("thread_id")))
            # Bulk mail (promotions, social, newsletters, notifications, junk) is not inbox
            # material unless asked for: the row says so, the listing drops it after the
            # stored count, and the bulk read skips it.
            bulk = is_bulk_mail(t)
            if not bulk:
                primary += 1
            thread_id = str(t.get("thread_id"))
            state = mail_thread_state(t, marks.get(("mail", thread_id)), waits_threshold_value=threshold)
            rows.append({
                "key": f"mail:{thread_id}",
                "channel": "mail",
                "id": thread_id,
                "name": (t.get("from_addr") or t.get("subject") or "").strip() or thread_id,
                "subject": t.get("subject") or "",
                "preview": (t.get("snippet") or t.get("subject") or "")[:160],
                "preview_from": state["preview_from"],
                "last_ts": float(t.get("last_date_ts") or 0.0),
                "message_count": int(t.get("message_count") or 0),
                "unread": state["unread"],
                "waits": state["waits"],
                "waits_reason": state["waits_reason"],
                "answered_by_agent": state["answered_by_agent"],
                "done": state["done"],
                "is_group": False,
                "mode": "mail",
                "bulk": bulk,
                "reply_window_until": None,
                "can_compose": False,
                "session_id": "",
                "jump": {"channel": "mail", "thread_id": thread_id, "account_id": t.get("acct"),
                         "folder": t.get("newest_folder"), "message_id": t.get("newest_message_id"),
                         "provider_message_id": t.get("newest_gm_msgid") or "", "message_pk": t.get("newest_pk")},
            })
        if len(batch) < 200 or (primary if not include_bulk else len(rows)) >= want:
            break
        offset += 200
    return rows


def _room_lane(username: Optional[str], user_scope_id: Optional[str]) -> List[Dict[str, Any]]:
    from vaf.core.channel_message_store import chat_marks
    from vaf.core.session import _room_rows
    marks = chat_marks(username or "", user_scope_id, channel="room")
    rows: List[Dict[str, Any]] = []
    for r in _room_rows(user_scope_id):
        room_id = str(r.get("room_id") or "")
        state = room_state(r, marks.get(("room", room_id)))
        last = r.get("last") or {}
        rows.append({
            "key": f"room:{room_id}",
            "channel": "room",
            "id": room_id,
            "name": r.get("name") or room_id,
            "preview": str(last.get("text") or ""),
            "preview_from": state["preview_from"],
            "last_ts": float(r.get("last_ts") or 0.0),
            "message_count": int(r.get("message_count") or 0),
            "unread": state["unread"],
            "waits": state["waits"],
            "waits_reason": state["waits_reason"],
            "answered_by_agent": False,
            "done": state["done"],
            "is_group": True,
            "mode": "room",
            "reply_window_until": None,
            "can_compose": False,
            "session_id": "",
            "members": int(r.get("members") or 0),
            "invited": bool(r.get("invited")),
            "jump": {"channel": "room", "room_id": room_id},
        })
    return rows


# -- the list ---------------------------------------------------------------------------------

def search_hits(username: Optional[str], user_scope_id: Optional[str], query: str,
                channels: Iterable[str]) -> Set[str]:
    """Row keys whose stored messages match the query, in every lane the caller asked for."""
    q = (query or "").strip()
    if not q:
        return set()
    hits: Set[str] = set()
    wanted = set(channels)
    if wanted & set(MESSENGERS):
        from vaf.core.channel_message_store import search_messages, store_exists
        for channel in MESSENGERS:
            if channel not in wanted:
                continue
            if channel == "discord" and not _local_admin(username, user_scope_id):
                continue
            row_user = _row_username(channel, username)
            scope = user_scope_id if channel != "discord" else None
            if not store_exists(row_user, scope):
                continue
            try:
                for m in search_messages(row_user, q, limit=100, user_scope_id=scope, channel=channel):
                    hits.add(f"{channel}:{m.get('chat_id')}")
            except Exception:
                continue
    if "mail" in wanted and user_scope_id:
        try:
            from vaf.tools.mail_utils import mail_v2_active
            from vaf.mail.store import MailStore
            if mail_v2_active(username or "", user_scope_id) and MailStore.exists(user_scope_id):
                from vaf.mail.service import MailService
                for m in MailService(user_scope_id).search(q, limit=100):
                    if m.get("thread_id") is not None:
                        hits.add(f"mail:{m['thread_id']}")
        except Exception:
            pass
    return hits


def list_conversations(username: Optional[str], user_scope_id: Optional[str], *,
                       channels: Optional[Iterable[str]] = None, view: str = "all",
                       include_groups: bool = True, include_done: bool = False, query: str = "",
                       limit: int = 200, now: Optional[float] = None,
                       mail_account_id: Optional[str] = None, mail_folder: Optional[str] = None,
                       include_bulk: bool = False) -> Dict[str, Any]:
    """Every conversation of this person, newest first, with the counts the rail shows.
    `include_bulk` lists the mail lane's bulk mail too (`is_bulk_mail`); off, the lane holds
    primary mail only and `stored_per_channel` still counts everything.

    `channels` narrows the lanes (default all five); `view` is one of VIEWS; the group and
    done toggles apply before the counts, the view after them, so the rail's numbers describe
    what the toggles allow. `query` keeps rows whose name or preview contain it, or whose
    stored messages match (`search_hits`). `mail_account_id` and `mail_folder` narrow the mail
    lane at the source, before the counts and the limit, so a narrowed listing never loses a
    matching thread to the cut."""
    now = float(now if now is not None else time.time())
    wanted = tuple(c for c in (channels or CHANNELS) if c in CHANNELS) or CHANNELS
    view = view if view in VIEWS else "all"
    rows: List[Dict[str, Any]] = []
    rows.extend(_messenger_rows(username, user_scope_id, wanted, now=now))
    if "mail" in wanted:
        try:
            # The lane's own cap, not the caller's row limit: the counts (and the summary,
            # which asks for one row) cover the newest 200 threads the toggle allows (with
            # bulk mail hidden the lane pages on until it holds 200 primary threads, at most
            # five pages). Every row read comes back flagged; the listing drops the bulk ones
            # after the stored count.
            rows.extend(_mail_rows(username, user_scope_id, limit=200,
                                   account_id=mail_account_id, folder=mail_folder, include_bulk=include_bulk))
        except Exception:
            pass
    if "room" in wanted:
        try:
            rows.extend(_room_lane(username, user_scope_id))
        except Exception:
            pass
    # What each lane holds before any toggle, filter or cut: the one number that can say
    # "nothing is stored here" without lying about a view or a query that hid the rows.
    stored_per_channel = {c: sum(1 for r in rows if r["channel"] == c) for c in CHANNELS}
    bulk_hidden = 0
    if not include_bulk:
        bulk_hidden = sum(1 for r in rows if r.get("bulk"))
        rows = [r for r in rows if not r.get("bulk")]
    if not include_groups:
        rows = [r for r in rows if not r["is_group"]]
    if not include_done:
        rows = [r for r in rows if not r["done"]]
    q = (query or "").strip().lower()
    if q:
        hits = search_hits(username, user_scope_id, q, wanted)
        rows = [r for r in rows
                if q in (r["name"] or "").lower() or q in (r["preview"] or "").lower()
                or q in (r.get("subject") or "").lower() or r["key"] in hits]
    rows.sort(key=lambda r: r["last_ts"], reverse=True)
    counts = {
        "all": len(rows),
        "waits": sum(1 for r in rows if r["waits"]),
        "unread": sum(r["unread"] for r in rows),
        "agent": sum(1 for r in rows if r["answered_by_agent"]),
        "per_channel": {c: sum(1 for r in rows if r["channel"] == c) for c in CHANNELS},
        "waits_per_channel": {c: sum(1 for r in rows if r["channel"] == c and r["waits"]) for c in CHANNELS},
        "unread_per_channel": {c: sum(r["unread"] for r in rows if r["channel"] == c) for c in CHANNELS},
        # Invitations wait for a decision, not for reading: a window subtracts them from what
        # "mark all as read" can clear.
        "invitations": sum(1 for r in rows if r["waits_reason"] == WAITS_INVITATION),
        # The bulk mail the listing dropped, so a surface can say the list is not all there is.
        "bulk_hidden": bulk_hidden,
        "stored_per_channel": stored_per_channel,
    }
    if view == "waits":
        rows = [r for r in rows if r["waits"]]
    elif view == "unread":
        rows = [r for r in rows if r["unread"] > 0]
    elif view == "agent":
        rows = [r for r in rows if r["answered_by_agent"]]
    return {"rows": rows[: max(int(limit), 1)], "counts": counts, "channels": list(wanted)}


# -- marks and history ------------------------------------------------------------------------

def mark_conversation(username: Optional[str], user_scope_id: Optional[str], channel: str, chat_id: str,
                      *, seen: bool = False, done: Optional[bool] = None) -> Dict[str, Any]:
    """The person opened a conversation (`seen`) or marked it done / not done (`done`).

    Messenger chats write `chat_marks`; a mail thread's seen goes to every unread message of
    the thread (IMAP's flag stays the read marker, the mail window's own rule moved
    server-side) and its done to `chat_marks`; a room's seen moves the person's cursor to the
    newest frame (`Room.mark_read`, as the room view does), its done goes to `chat_marks`."""
    from vaf.core.channel_message_store import mark_done, mark_seen
    channel = (channel or "").strip().lower()
    chat_id = str(chat_id or "").strip()
    if channel not in CHANNELS or not chat_id:
        raise ValueError("unknown conversation")
    out: Dict[str, Any] = {"channel": channel, "id": chat_id}
    if channel == "discord" and not _local_admin(username, user_scope_id):
        raise ValueError("Discord marks belong to the local admin")
    if channel in MESSENGERS:
        row_user = _row_username(channel, username)
        scope = user_scope_id if channel != "discord" else None
        if seen:
            out["seen_ts"] = mark_seen(row_user, channel, chat_id, user_scope_id=scope)
        if done is not None:
            mark_done(row_user, channel, chat_id, user_scope_id=scope, done=bool(done))
            out["done"] = bool(done)
        return out
    if channel == "mail":
        if seen and user_scope_id:
            from vaf.mail.service import MailService
            _read_mail_thread(MailService(user_scope_id), int(chat_id))
            out["seen"] = True
        if done is not None:
            mark_done(username or "", "mail", chat_id, user_scope_id=user_scope_id, done=bool(done))
            out["done"] = bool(done)
        return out
    # A room that is not the person's answers as unknown, for seen and done alike.
    from vaf.core.session import _room_rows
    mine = {str(r.get("room_id") or ""): r for r in _room_rows(user_scope_id)}
    if chat_id not in mine:
        raise ValueError("unknown room")
    if seen:
        # Reading a room in the inbox is reading it: the person's cursor moves as the room
        # view moves it, and the sidebar hears of real movement. An invitation is read by
        # answering it: nothing moves.
        if mine[chat_id].get("invited"):
            out["seen"] = False
        else:
            try:
                out["seen"] = _read_room(user_scope_id, chat_id)
            except Exception as e:
                raise ValueError("unknown room") from e
            if out["seen"]:
                try:
                    from vaf.core.web_interface import notify_rooms_changed
                    notify_rooms_changed(user_scope_id)
                except Exception:
                    pass
    if done is not None:
        mark_done(username or "", "room", chat_id, user_scope_id=user_scope_id, done=bool(done))
        out["done"] = bool(done)
    return out


def _read_mail_thread(svc: Any, thread_id: int) -> int:
    """Every unseen message of the thread is marked read through the service (the local flag
    first, one flags op per message for the writeback, as the mail window does). Returns how
    many messages were marked."""
    n = 0
    for m in svc.thread_messages(int(thread_id)):
        if "\\Seen" not in (m.get("flags") or []):
            svc.mark_read(int(m["id"]), True)
            n += 1
    return n


def _read_room(user_scope_id: Optional[str], room_id: str) -> bool:
    """The person's cursor of one room moves to its newest frame (`Room.mark_read`, the rule
    the room view applies when it is shown). Returns whether it moved."""
    from vaf.core.a2a.room import Room, derive_peer_id, participant_key
    room = Room.open(room_id)
    return bool(room.mark_read(derive_peer_id(participant_key("cli", user_scope_id), room.room_id)))


def _read_rooms(user_scope_id: Optional[str]) -> int:
    """Every room with something to read (the rows the sidebar and the inbox list, unread
    and not an invitation: an invitation is a decision, not a message) is read through
    `_read_room`. Returns how many moved and announces `rooms_changed` once when any did."""
    from vaf.core.session import _room_rows
    moved = 0
    for r in _room_rows(user_scope_id):
        if int(r.get("unread") or 0) <= 0 or r.get("invited"):
            continue
        try:
            if _read_room(user_scope_id, str(r.get("room_id") or "")):
                moved += 1
        except Exception:
            continue
    if moved:
        try:
            from vaf.core.web_interface import notify_rooms_changed
            notify_rooms_changed(user_scope_id)
        except Exception:
            pass
    return moved


def mark_all_seen(username: Optional[str], user_scope_id: Optional[str], *,
                  channels: Optional[Iterable[str]] = None, include_groups: bool = True,
                  include_bulk: bool = False, now: Optional[float] = None) -> Dict[str, int]:
    """"Mark all as read": every conversation of the named channels (default all) counts as
    read at once; group chats and rooms only when `include_groups` is true, bulk mail only
    when `include_bulk` is true (they are the conversations the two toggles show).

    Messenger chats go through the store's `mark_channel_seen` (one transaction over the
    whole channel, one announce, a marker never moves backwards, no cap); the mail lane's
    unread threads through the same per-thread seen as a single row (the lane's reach:
    the newest 200 threads the bulk toggle allows, paged; one service for the call); every unread room through the
    person's cursor (`Room.mark_read`, as opening it would; an invitation stays). The done
    and owner-asked marks are left alone (a read marker newer than the agent's question
    lifts that reason by itself). Returns how many conversations were read per channel; a
    messenger or mail lane without a store, and Discord for anybody but the local admin,
    are absent; rooms are present whenever they were wanted."""
    wanted = [c for c in (list(channels) if channels else list(CHANNELS)) if c in CHANNELS]
    at = float(now) if now is not None else time.time()
    moved: Dict[str, int] = {}
    from vaf.core.channel_message_store import mark_channel_seen, store_exists
    for channel in wanted:
        if channel not in MESSENGERS:
            continue
        if channel == "discord" and not _local_admin(username, user_scope_id):
            continue
        row_user = _row_username(channel, username)
        scope = user_scope_id if channel != "discord" else None
        if not store_exists(row_user, scope):
            continue
        moved[channel] = mark_channel_seen(row_user, channel, user_scope_id=scope, ts=at,
                                           exclude_like=None if include_groups else _GROUP_LIKE.get(channel))
    if "mail" in wanted and user_scope_id:
        # The same gate as the listing: a glance must never materialise an empty mail store.
        from vaf.mail.store import MailStore
        from vaf.tools.mail_utils import mail_v2_active
        if mail_v2_active(username or "", user_scope_id) and MailStore.exists(user_scope_id):
            n = 0
            try:
                from vaf.mail.service import MailService
                svc = MailService(user_scope_id)
                for row in _mail_rows(username, user_scope_id, limit=200, svc=svc, include_bulk=include_bulk):
                    if int(row.get("unread") or 0) <= 0 or (not include_bulk and row.get("bulk")):
                        continue
                    try:
                        if _read_mail_thread(svc, int(row["id"])):
                            n += 1
                    except Exception:
                        continue
            except Exception:
                pass
            moved["mail"] = n
    if "room" in wanted and include_groups:
        moved["room"] = _read_rooms(user_scope_id)
    return moved


def _stamp(ts: Optional[float]) -> Optional[str]:
    if not ts:
        return None
    from datetime import datetime
    return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")


def conversation_history(username: Optional[str], user_scope_id: Optional[str], channel: str, chat_id: str,
                         limit: int = 200) -> List[Dict[str, Any]]:
    """One conversation, oldest first, in the shape the channel windows' pane reads:
    role (`user` = the other side, `assistant` = what left on our behalf), content, timestamp,
    content_type, and `sender` (`them`, `agent`, `you`, or a room member's label)."""
    channel = (channel or "").strip().lower()
    chat_id = str(chat_id or "").strip()
    limit = min(max(int(limit or 1), 1), 200)
    if channel in MESSENGERS:
        from vaf.core.channel_message_store import get_chat_messages, store_exists
        if channel == "discord" and not _local_admin(username, user_scope_id):
            return []
        row_user = _row_username(channel, username)
        scope = user_scope_id if channel != "discord" else None
        if not store_exists(row_user, scope):
            return []
        rows = get_chat_messages(row_user, chat_id, limit=limit, user_scope_id=scope, channel=channel)
        out = []
        for r in sorted(rows, key=lambda r: float(r.get("ts") or 0)):
            outbound = (r.get("direction") or "in") == "out"
            sender = "you" if (outbound and r.get("sender_jid") == OWNER_SENDER) else ("agent" if outbound else "them")
            out.append({"role": "assistant" if outbound else "user", "content": (r.get("body") or "")[:2000],
                        "timestamp": _stamp(r.get("ts")), "content_type": r.get("content_type") or "text",
                        "sender": sender})
        return out
    if channel == "mail":
        if not user_scope_id:
            return []
        from vaf.mail.service import MailService
        out = []
        for m in MailService(user_scope_id).thread_messages(int(chat_id))[-limit:]:
            mine = str(m.get("folder_special_use") or "").lower() == "\\sent"
            text = (m.get("snippet") or m.get("subject") or "")[:2000]
            out.append({"role": "assistant" if mine else "user", "content": text,
                        "timestamp": _stamp(m.get("date_ts") or m.get("internaldate_ts")),
                        "content_type": "mail", "sender": "you" if mine else (m.get("from_addr") or "them")})
        return out
    if channel == "room":
        from vaf.core.a2a.room import Room, derive_peer_id, participant_key
        room = Room.open(chat_id)
        human = derive_peer_id(participant_key("cli", user_scope_id), chat_id)
        out = []
        for line in room.transcript()[-limit:]:
            mine = line.get("sender") == human or line.get("peer") == human
            out.append({"role": "assistant" if mine else "user", "content": str(line.get("text") or "")[:2000],
                        "timestamp": _stamp(line.get("ts")), "content_type": str(line.get("kind") or "text"),
                        "sender": "you" if mine else str(line.get("label") or line.get("sender") or "them")})
        return out
    return []


def channel_label(channel: str) -> str:
    return _CHANNEL_NAMES.get(channel, channel.title() if channel else "")
