"""
conversation_handlers.py — multi-turn tiebreaker (brief §7.4 / open challenges 1,2,5).

respond(state, merchant_message) -> dict with keys: body, cta, send_as, suppression_key, rationale

state is a plain dict the caller maintains across turns:
{
    "merchant_name": str,
    "sent_messages": [str, ...],     # every body we've sent so far, in order
    "merchant_messages": [str, ...], # every inbound merchant message so far, in order
    "unanswered_nudges": int,        # how many of our nudges got no real reply
    "topic": str,                    # what we're currently pitching (for context)
}
"""

from __future__ import annotations
import re
from typing import Optional

# --------------------------------------------------------------------------
# Auto-reply detection (open challenge #1)
# --------------------------------------------------------------------------

_AUTO_REPLY_PATTERNS = [
    r"thank you for (contacting|reaching out)",
    r"we (will|shall) get back to you",
    r"aapki jaankari.*shukriya",
    r"team tak pahuncha",
    r"i am an? automated (assistant|reply|message)",
    r"main (ek )?automated assistant",
    r"currently (unavailable|away|closed)",
    r"business hours",
]
_AUTO_REPLY_RE = re.compile("|".join(_AUTO_REPLY_PATTERNS), re.IGNORECASE)


def is_auto_reply(message: str, prior_merchant_messages: list[str]) -> bool:
    """Heuristic: matches a canned-reply pattern, OR is a verbatim repeat of an
    earlier merchant message (3+ times = definitely a canned auto-reply, per
    the brief's own hint)."""
    if _AUTO_REPLY_RE.search(message):
        return True
    repeat_count = sum(1 for m in prior_merchant_messages if m.strip() == message.strip())
    return repeat_count >= 2  # this would be the 3rd verbatim occurrence


# --------------------------------------------------------------------------
# Intent detection (open challenge #2 / brief pattern D)
# --------------------------------------------------------------------------

_HEDGE_RE = re.compile(
    r"\b(maybe|later|not sure|let me think|abhi nahi|shayad|dekhte hain)\b",
    re.IGNORECASE,
)

_AFFIRMATIVE_INTENT_RE = re.compile(
    r"\b(yes|yeah|go ahead|let'?s do it|haan(?! nahi)|kar do|chalo|"
    r"i want to (join|do|start)|mujhe.*(karna|judrna|join)\b)",
    re.IGNORECASE,
)

_NOT_INTERESTED_RE = re.compile(
    r"\b(not interested|no thanks|stop|nahi chahiye|band karo|unsubscribe|"
    r"don'?t (message|contact) me)\b",
    re.IGNORECASE,
)

_QUESTION_RE = re.compile(r"\?\s*$")


def detect_signal(message: str) -> str:
    """Returns one of: 'affirmative_intent', 'not_interested', 'question', 'neutral'."""
    if _NOT_INTERESTED_RE.search(message):
        return "not_interested"
    if _HEDGE_RE.search(message):
        return "neutral"
    if _AFFIRMATIVE_INTENT_RE.search(message):
        return "affirmative_intent"
    if _QUESTION_RE.search(message):
        return "question"
    return "neutral"


# --------------------------------------------------------------------------
# respond()
# --------------------------------------------------------------------------

def respond(state: dict, merchant_message: str) -> dict:
    name = state.get("merchant_name", "there")
    prior = state.get("merchant_messages", [])
    topic = state.get("topic", "this")

    # 1. Auto-reply -> try exactly once more, then stop wasting turns.
    if is_auto_reply(merchant_message, prior):
        already_retried = any(
            "quick look khud" in b or "quick look yourself" in b.lower()
            for b in state.get("sent_messages", [])
        )
        if already_retried:
            body = (f"Koi baat nahi, samajh gayi. Main directly owner/manager se connect kar lungi. "
                     f"Best wishes {name}! 🙂")
            return _out(body, "none", "vera", state, rationale="auto_reply_second_time -> graceful exit")
        body = ("Samajh gayi, yeh auto-reply lag raha hai. Ek quick look khud le lenge? "
                "2 minute ka kaam hai — chalega?")
        return _out(body, "binary_yes_no", "vera", state, rationale="auto_reply_first_time -> one retry")

    signal = detect_signal(merchant_message)

    # 2. Explicit "not interested" -> exit gracefully, no more pitching.
    if signal == "not_interested":
        body = f"Samajh gayi, {name}. Koi zabardasti nahi — jab bhi zaroorat ho, bata dijiyega. All the best!"
        return _out(body, "none", "vera", state, rationale="not_interested -> graceful exit, no further pitch")

    # 3. Affirmative intent -> ACT immediately, don't re-qualify (fixes Pattern D).
    if signal == "affirmative_intent":
        body = f"Great — let's get {topic} moving. I'll draft it now and share it here in a moment."
        return _out(body, "none", "vera", state, rationale="affirmative_intent -> route straight to action, no re-qualifying question")

    # 4. A genuine question -> acknowledge + answer placeholder (caller should
    #    fill in the real answer from MerchantContext before sending).
    if signal == "question":
        body = f"Good question — let me pull the exact numbers for you on that."
        return _out(body, "open_ended", "vera", state, rationale="question -> acknowledge and answer directly")

    # 5. Neutral engaged reply -> continue the thread.
    unanswered = state.get("unanswered_nudges", 0)
    if unanswered >= 3:
        body = f"No pressure, {name} — I'll check back another time. 🙂"
        return _out(body, "none", "vera", state, rationale="3_unanswered_nudges -> graceful exit per open challenge #5")

    body = "Got it — want me to go ahead with that, or is there something you'd tweak first?"
    return _out(body, "binary_yes_no", "vera", state, rationale="neutral_reply -> keep thread moving, single binary ask")


def _out(body, cta, send_as, state, rationale):
    state.setdefault("sent_messages", []).append(body)
    return {
        "body": body,
        "cta": cta,
        "send_as": send_as,
        "suppression_key": state.get("suppression_key", "conversation:" + state.get("merchant_name", "unknown")),
        "rationale": rationale,
    }
