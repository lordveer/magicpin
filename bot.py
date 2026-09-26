"""
bot.py — Vera-challenge merchant/customer message composer.

compose(category, merchant, trigger, customer=None) -> dict
    Returns: body, cta, send_as, suppression_key, rationale

Design notes (see README.md for the full writeup):
- Deterministic, template + retrieval based. No external LLM call, no network,
  no randomness -> same inputs always give the same output, <30s trivially.
- trigger['scope'] routes merchant-facing vs customer-facing composition.
- Many trigger payloads in the real dataset are PLACEHOLDERS
  (payload == {"placeholder": True, "metric_or_topic": "<kind>"}). When that's
  the case we NEVER invent the missing fact (no fake competitor names, no fake
  appointment times, etc). Instead we derive an equally concrete but real
  anchor fact from merchant / customer / category context (performance deltas,
  review_themes, customer_aggregate, relationship history, signals, digest).
- Category voice.code_mix decides whether we write natural Hindi-English
  code-mix or English; customer language_pref overrides for customer-facing
  sends. Non hi/hi-en regional mixes (ta-en, kn-en, te-en) fall back to plain
  English rather than fabricating a translation we can't verify.
"""

from __future__ import annotations
import datetime as _dt
from typing import Optional


# --------------------------------------------------------------------------
# small formatting helpers
# --------------------------------------------------------------------------

def _pct(x: float) -> str:
    return f"{abs(x) * 100:.0f}%"


def _num(x) -> str:
    try:
        return f"{int(x):,}"
    except (TypeError, ValueError):
        return str(x)


def _is_placeholder(trigger: dict) -> bool:
    return bool(trigger.get("payload", {}).get("placeholder"))


def _digest_by_id(category: dict, digest_id: Optional[str]) -> Optional[dict]:
    if not digest_id:
        return None
    for d in category.get("digest", []):
        if d["id"] == digest_id:
            return d
    return None


def _digest_by_kind(category: dict, kind: str) -> Optional[dict]:
    for d in category.get("digest", []):
        if d.get("kind") == kind:
            return d
    return None


def _first_digest(category: dict) -> Optional[dict]:
    d = category.get("digest", [])
    return d[0] if d else None


def _active_offer(merchant: dict):
    for o in merchant.get("offers", []):
        if o.get("status") == "active":
            return o
    return None


def _best_new_user_offer(category: dict):
    for o in category.get("offer_catalog", []):
        if o.get("type") == "service_at_price" and o.get("audience") == "new_user":
            return o
    return category.get("offer_catalog", [{}])[0] if category.get("offer_catalog") else None


def _months_since(date_str: Optional[str], today=_dt.date(2026, 4, 26)) -> Optional[int]:
    if not date_str:
        return None
    try:
        d = _dt.date.fromisoformat(date_str[:10])
    except ValueError:
        return None
    return max(0, (today.year - d.year) * 12 + (today.month - d.month))


def _biggest_delta(delta_7d: dict):
    """Pick the delta_7d entry with the largest magnitude. Returns (metric, pct)."""
    if not delta_7d:
        return None, 0.0
    metric, pct = max(delta_7d.items(), key=lambda kv: abs(kv[1]))
    return metric.replace("_pct", ""), pct


# --------------------------------------------------------------------------
# language mode
# --------------------------------------------------------------------------

def _lang_mode(category: dict, merchant: dict, customer: Optional[dict]) -> str:
    code_mix = category.get("voice", {}).get("code_mix", "english_primary_some_hindi")
    if code_mix != "hindi_english_natural":
        return "en"
    if customer is not None:
        pref = customer.get("identity", {}).get("language_pref", "en")
        return "hi_en" if pref in ("hi", "hi-en mix") else "en"
    languages = merchant.get("identity", {}).get("languages", [])
    return "hi_en" if "hi" in languages else "en"


# --------------------------------------------------------------------------
# generic assembly
# --------------------------------------------------------------------------

class Msg:
    """A composed message before final string assembly."""

    __slots__ = ("anchor", "action", "cta_type", "cta_opts", "lever", "topic")

    def __init__(self, anchor, action=None, cta_type="none", cta_opts=None,
                 lever="specificity", topic=""):
        self.anchor = anchor
        self.action = action
        self.cta_type = cta_type
        self.cta_opts = cta_opts or ()
        self.lever = lever
        self.topic = topic


def _cta_line(msg: Msg) -> Optional[str]:
    if msg.cta_type == "binary" and msg.cta_opts and len(msg.cta_opts) == 2:
        (o1, l1), (o2, l2) = msg.cta_opts
        return f"Reply {o1} for {l1}, or {o2} for {l2}."
    if msg.cta_type == "binary":
        return "Reply YES to go ahead, or STOP to skip."
    return None  # "open" type CTAs are phrased as a question inside anchor/action


def _assemble(greet_name: str, msg: Msg) -> str:
    parts = [f"Hi {greet_name},", msg.anchor]
    if msg.action:
        parts.append(msg.action)
    cta = _cta_line(msg)
    if cta:
        parts.append(cta)
    return " ".join(p.strip() for p in parts if p and p.strip())


def _cta_field(msg: Msg) -> str:
    return {"binary": "binary_yes_no", "open": "open_ended", "none": "none"}[msg.cta_type]


def _greet_name(merchant: dict) -> str:
    return merchant.get("identity", {}).get("owner_first_name") or \
        merchant.get("identity", {}).get("name", "there")


# ==========================================================================
# MERCHANT-FACING kind handlers
# each returns a Msg. mode is "hi_en" or "en".
# ==========================================================================

def _h_research_digest(category, merchant, trigger, mode):
    d = _digest_by_id(category, trigger["payload"].get("top_item_id")) or _first_digest(category)
    if not d:
        return Msg("Ek naya category research update hai." if mode == "hi_en"
                    else "There's a new category research update.", cta_type="open")
    if mode == "hi_en":
        anchor = f"{category['display_name']} ke liye ek naya research item aaya hai — {d['title']}."
        action = f"{d['summary']} Chahiye ki main isko patient/customer-ready draft bana doon? — {d['source']}"
    else:
        anchor = f"New research relevant to your category — {d['title']}."
        action = f"{d['summary']} Want me to turn this into a ready-to-share draft? — {d['source']}"
    return Msg(anchor, action, cta_type="open", lever="specificity+reciprocity", topic="research_digest")


def _h_regulation_change(category, merchant, trigger, mode):
    d = _digest_by_id(category, trigger["payload"].get("top_item_id")) or _digest_by_kind(category, "compliance")
    deadline = trigger["payload"].get("deadline_iso")
    deadline_txt = f" Deadline: {deadline[:10]}." if deadline else ""
    if not d:
        base = ("Ek compliance/regulation update aaya hai jo aapke category ko affect karta hai."
                 if mode == "hi_en" else
                 "A compliance/regulation update just landed that affects your category.")
        return Msg(base + deadline_txt, cta_type="open", lever="loss_aversion", topic="regulation_change")
    if mode == "hi_en":
        anchor = f"Compliance update: {d['title']}.{deadline_txt}"
        action = f"{d['summary']} Chahiye main ek 3-line checklist bana doon jo aap team ko bhej sakein?"
    else:
        anchor = f"Compliance update: {d['title']}.{deadline_txt}"
        action = f"{d['summary']} Want a 3-line checklist you can forward to your team?"
    return Msg(anchor, action, cta_type="open", lever="loss_aversion+specificity", topic="regulation_change")


def _h_cde_opportunity(category, merchant, trigger, mode):
    p = trigger["payload"]
    d = _digest_by_id(category, p.get("digest_item_id")) or _digest_by_kind(category, "cde")
    credits = p.get("credits")
    fee = (p.get("fee") or "").replace("_", " ")
    title = d["title"] if d else "a category CDE session"
    if mode == "hi_en":
        anchor = f"{title} ho raha hai"
        if credits:
            anchor += f" — {credits} CDE credits milenge"
        if fee:
            anchor += f", {fee}."
        else:
            anchor += "."
        action = "Chahiye main aapko registration link bhej doon?"
    else:
        anchor = f"{title} is coming up"
        if credits:
            anchor += f" — {credits} CDE credits"
        if fee:
            anchor += f", {fee}."
        else:
            anchor += "."
        action = "Want me to send the registration link?"
    return Msg(anchor, action, cta_type="binary", cta_opts=(("YES", "the link"), ("STOP", "skip")),
               lever="reciprocity+specificity", topic="cde_opportunity")


def _h_competitor_opened(category, merchant, trigger, mode):
    p = trigger["payload"]
    if not _is_placeholder(trigger) and p.get("competitor_name"):
        name, dist, offer = p["competitor_name"], p.get("distance_km"), p.get("their_offer")
        if mode == "hi_en":
            anchor = f"Heads up — {name} khula hai {dist}km door"
            anchor += f", unka offer hai \"{offer}\"." if offer else "."
            action = "Chahiye main aapke liye ek competitive offer/post draft kar doon?"
        else:
            anchor = f"Heads up — {name} just opened {dist}km away"
            anchor += f", running \"{offer}\"." if offer else "."
            action = "Want me to draft a competitive counter-offer or post?"
        return Msg(anchor, action, cta_type="open", lever="loss_aversion+specificity", topic="competitor_opened")
    # placeholder fallback: don't invent a competitor — use real peer-benchmark gap instead
    ctr = merchant.get("performance", {}).get("ctr")
    peer_ctr = category.get("peer_stats", {}).get("avg_ctr")
    if ctr is not None and peer_ctr:
        gap = peer_ctr - ctr
        if mode == "hi_en":
            anchor = f"Aapka listing CTR {_pct(ctr)} hai, category peer median {_pct(peer_ctr)} hai."
            action = "Chahiye main dekhun aapki locality mein kya extra offer peers use kar rahe hain?"
        else:
            anchor = f"Your listing CTR is {_pct(ctr)} vs. the category peer median of {_pct(peer_ctr)}."
            action = "Want me to check what nearby peers are offering that you're not?"
        return Msg(anchor, action, cta_type="open", lever="loss_aversion+specificity", topic="competitor_opened")
    return Msg("Worth checking how you compare to nearby peers this week." if mode == "en" else
               "Is hafte apne locality peers se compare karna worth hai.", cta_type="open",
               lever="loss_aversion", topic="competitor_opened")


def _h_curious_ask(category, merchant, trigger, mode):
    p = trigger["payload"]
    ask = p.get("ask_template") if not _is_placeholder(trigger) else None
    if ask == "what_service_in_demand_this_week":
        q = ("Is hafte sabse zyada kis service/treatment ke baare mein poocha gaya?"
             if mode == "hi_en" else
             "What's the one service/treatment your customers asked about most this week?")
    else:
        q = ("Quick one — is hafte aapke customers ka sabse common sawaal kya tha?"
             if mode == "hi_en" else
             "Quick one — what's the most common question your customers asked this week?")
    return Msg(q, cta_type="open", lever="asking_the_merchant", topic="curious_ask_due")


def _h_dormant_with_vera(category, merchant, trigger, mode):
    p = trigger["payload"]
    days = p.get("days_since_last_merchant_message")
    if days is None:
        for s in merchant.get("signals", []):
            if s.startswith("dormant_with_vera_"):
                days = s.split("_")[-1].rstrip("d")
    days_txt = f"{days} din se" if (mode == "hi_en" and days) else (f"in {days} days" if days else "")
    if mode == "hi_en":
        anchor = f"Aapse baat {days_txt} nahi hui hai." if days else "Kaafi time ho gaya baat kiye."
        action = "Ek chhota sa profile/growth update chahiye, ya sab theek hai?"
    else:
        anchor = f"Haven't heard from you {days_txt}." if days else "It's been a while since we last spoke."
        action = "Want a quick profile/growth update, or is everything on track?"
    return Msg(anchor, action, cta_type="open", lever="reciprocity+curiosity", topic="dormant_with_vera")


def _h_festival_upcoming(category, merchant, trigger, mode):
    p = trigger["payload"]
    if not _is_placeholder(trigger) and p.get("festival"):
        festival, days_until = p["festival"], p.get("days_until")
        offer = _best_new_user_offer(category)
        offer_txt = offer.get("title") if offer else None
        if mode == "hi_en":
            anchor = f"{festival} aa raha hai ({days_until} din mein)."
            action = f"Chahiye main \"{offer_txt}\" jaisa ek festival post/offer draft kar doon?" if offer_txt \
                else "Chahiye main ek festival post draft kar doon?"
        else:
            anchor = f"{festival} is coming up in {days_until} days."
            action = f"Want me to draft a festival post/offer around \"{offer_txt}\"?" if offer_txt \
                else "Want me to draft a festival post for it?"
        return Msg(anchor, action, cta_type="open", lever="specificity+loss_aversion", topic="festival_upcoming")
    beat = category.get("seasonal_beats", [{}])[0]
    note = beat.get("note", "a seasonal shift")
    if mode == "hi_en":
        anchor = f"Is season ka pattern: {note}."
        action = "Chahiye main isko dhyan mein rakhke ek post/offer draft kar doon?"
    else:
        anchor = f"Seasonal pattern worth planning for: {note}."
        action = "Want me to draft a post/offer around it?"
    return Msg(anchor, action, cta_type="open", lever="specificity", topic="festival_upcoming")


def _h_gbp_unverified(category, merchant, trigger, mode):
    p = trigger["payload"]
    uplift = p.get("estimated_uplift_pct")
    path = (p.get("verification_path") or "postcard_or_phone_call").replace("_", " ")
    uplift_txt = f" — verified profiles see roughly {_pct(uplift)} more visibility" if uplift else ""
    if mode == "hi_en":
        anchor = f"Aapka Google profile abhi verified nahi hai{uplift_txt}."
        action = f"Verification {path} se hoti hai — chahiye main process start kar doon?"
    else:
        anchor = f"Your Google profile isn't verified yet{uplift_txt}."
        action = f"Verification happens via {path} — want me to kick that off?"
    return Msg(anchor, action, cta_type="binary", cta_opts=(("YES", "start verification"), ("STOP", "not now")),
               lever="loss_aversion+specificity", topic="gbp_unverified")


def _h_ipl_match(category, merchant, trigger, mode):
    p = trigger["payload"]
    match, venue = p.get("match"), p.get("venue")
    weeknight = p.get("is_weeknight")
    beat = _digest_by_kind(category, "seasonal")
    caution = ""
    if beat and "underperform" in beat.get("summary", "").lower() and not weeknight:
        caution = " Heads up — Saturday matches tend to shift orders to home-watch parties, so weeknight promos usually do better."
    if mode == "hi_en":
        anchor = f"Aaj {match} hai ({venue})."
        action = f"Match-night combo push karne ka accha time hai.{caution} Chahiye main ek quick post draft kar doon?"
    else:
        anchor = f"{match} is on today at {venue}."
        action = f"Good moment to push a match-night combo.{caution} Want a quick post drafted?"
    return Msg(anchor, action, cta_type="open", lever="specificity", topic="ipl_match_today")


def _h_milestone_reached(category, merchant, trigger, mode):
    p = trigger["payload"]
    metric, value_now, milestone = p.get("metric"), p.get("value_now"), p.get("milestone_value")
    if _is_placeholder(trigger) or value_now is None:
        agg = merchant.get("customer_aggregate", {})
        value_now = agg.get("total_unique_ytd") or merchant.get("performance", {}).get("views")
        metric = metric or "customers reached"
        milestone = ((value_now // 50) + 1) * 50 if value_now else None
    if value_now and milestone:
        gap = milestone - value_now
        if mode == "hi_en":
            anchor = f"Aap {_num(value_now)} {metric.replace('_',' ')} pe hain — sirf {_num(gap)} aur {milestone} tak."
            action = "Chahiye main ek small push post/offer draft kar doon isko cross karne ke liye?"
        else:
            anchor = f"You're at {_num(value_now)} {metric.replace('_',' ')} — just {_num(gap)} away from {_num(milestone)}."
            action = "Want a quick post/offer to help push past it?"
        return Msg(anchor, action, cta_type="open", lever="specificity+social_proof", topic="milestone_reached")
    return Msg("You're closing in on a nice milestone this month." if mode == "en" else
               "Aap is mahine ek accha milestone ke kareeb hain.", cta_type="open", topic="milestone_reached")


def _h_perf_dip(category, merchant, trigger, mode):
    p = trigger["payload"]
    metric, delta, window, baseline = p.get("metric"), p.get("delta_pct"), p.get("window"), p.get("vs_baseline")
    if _is_placeholder(trigger) or metric is None:
        metric, delta = _biggest_delta(merchant.get("performance", {}).get("delta_7d", {}))
        window, baseline = "7d", merchant.get("performance", {}).get(metric) if metric else None
    if metric and delta is not None:
        if mode == "hi_en":
            anchor = f"Aapka {metric} {_pct(delta)} down hai pichle {window}"
            anchor += f" ({_num(baseline)} avg se)." if baseline else "."
            action = "Chahiye main check karun kya wajah ho sakti hai — stale posts, offer expire, ya kuch aur?"
        else:
            anchor = f"Your {metric} is down {_pct(delta)} over the last {window}"
            anchor += f" (vs. your {_num(baseline)} avg)." if baseline else "."
            action = "Want me to check likely causes — stale posts, an expired offer, or something else?"
        return Msg(anchor, action, cta_type="open", lever="loss_aversion+specificity", topic="perf_dip")
    return Msg("Noticed a dip in your numbers this week." if mode == "en" else
               "Is hafte aapke numbers thode down dikhe.", cta_type="open", topic="perf_dip")


def _h_perf_spike(category, merchant, trigger, mode):
    p = trigger["payload"]
    metric, delta, window, driver = p.get("metric"), p.get("delta_pct"), p.get("window"), p.get("likely_driver")
    if _is_placeholder(trigger) or metric is None:
        metric, delta = _biggest_delta(merchant.get("performance", {}).get("delta_7d", {}))
        window = "7d"
    if metric and delta is not None:
        driver_txt = f" — likely from {driver.replace('_',' ')}" if driver else ""
        if mode == "hi_en":
            anchor = f"Good news — aapka {metric} {_pct(delta)} up hai pichle {window}{driver_txt}."
            action = "Chahiye main isko double down karne ke liye ek follow-up post/offer draft kar doon?"
        else:
            anchor = f"Good news — your {metric} is up {_pct(delta)} over the last {window}{driver_txt}."
            action = "Want a follow-up post/offer to double down on it?"
        return Msg(anchor, action, cta_type="open", lever="specificity+reciprocity", topic="perf_spike")
    return Msg("Your numbers ticked up nicely this week." if mode == "en" else
               "Is hafte aapke numbers accha up gaye.", cta_type="open", topic="perf_spike")


def _h_review_theme(category, merchant, trigger, mode):
    p = trigger["payload"]
    theme, occ, quote = p.get("theme"), p.get("occurrences_30d"), p.get("common_quote")
    if _is_placeholder(trigger) or theme is None:
        themes = merchant.get("review_themes", [])
        if themes:
            t = max(themes, key=lambda t: t.get("occurrences_30d", 0))
            theme, occ, quote = t.get("theme"), t.get("occurrences_30d"), t.get("common_quote")
    if theme:
        theme_txt = theme.replace("_", " ")
        if mode == "hi_en":
            anchor = f"{occ} reviews is mahine \"{theme_txt}\" mention kar rahe hain"
            anchor += f" — jaise \"{quote}\"." if quote else "."
            action = "Chahiye main ek response draft kar doon jo isko address kare?"
        else:
            anchor = f"{occ} reviews this month mention \"{theme_txt}\""
            anchor += f" — e.g. \"{quote}\"." if quote else "."
            action = "Want a draft response that addresses it directly?"
        return Msg(anchor, action, cta_type="open", lever="specificity+social_proof", topic="review_theme_emerged")
    return Msg("A theme is emerging in your recent reviews." if mode == "en" else
               "Aapke recent reviews mein ek pattern dikh raha hai.", cta_type="open", topic="review_theme_emerged")


def _h_seasonal_perf_dip(category, merchant, trigger, mode):
    p = trigger["payload"]
    metric, delta, note = p.get("metric"), p.get("delta_pct"), p.get("season_note")
    if _is_placeholder(trigger) or metric is None:
        metric, delta = _biggest_delta(merchant.get("performance", {}).get("delta_7d", {}))
        note = category.get("seasonal_beats", [{}])[0].get("note")
    delta_txt = f" ({_pct(delta)})" if delta is not None else ""
    if mode == "hi_en":
        anchor = f"Aapka {metric or 'performance'} thoda down hai{delta_txt} — yeh is season ka normal pattern hai"
        anchor += f": {note}." if note else "."
        action = "Isse expected hi hai, lekin chahiye ek small nudge post draft karun?"
    else:
        anchor = f"Your {metric or 'performance'} is a bit soft{delta_txt} — this matches the seasonal pattern"
        anchor += f": {note}." if note else "."
        action = "Expected for this time of year, but want a small nudge post anyway?"
    return Msg(anchor, action, cta_type="open", lever="specificity", topic="seasonal_perf_dip")


def _h_supply_alert(category, merchant, trigger, mode):
    p = trigger["payload"]
    molecule, mfr, batches = p.get("molecule"), p.get("manufacturer"), p.get("affected_batches")
    if molecule:
        if mode == "hi_en":
            anchor = f"Supply alert: {molecule} ({mfr}) ke kuch batches flagged hain"
            anchor += f" — {', '.join(batches)}." if batches else "."
            action = "Chahiye main aapke current stock ke saath cross-check kar doon?"
        else:
            anchor = f"Supply alert: some {molecule} ({mfr}) batches are flagged"
            anchor += f" — {', '.join(batches)}." if batches else "."
            action = "Want me to cross-check against your current stock?"
        return Msg(anchor, action, cta_type="open", lever="loss_aversion+specificity", topic="supply_alert")
    return Msg("There's a supply alert relevant to your stock this week." if mode == "en" else
               "Is hafte aapke stock se related ek supply alert hai.", cta_type="open", topic="supply_alert")


def _h_winback_eligible(category, merchant, trigger, mode):
    p = trigger["payload"]
    days, added, dip = p.get("days_since_expiry"), p.get("lapsed_customers_added_since_expiry"), p.get("perf_dip_pct")
    sub = merchant.get("subscription", {})
    days = days if days is not None else sub.get("days_since_expiry")
    if mode == "hi_en":
        anchor = f"Aapka plan {days} din pehle expire hua tha" if days else "Aapka plan kuch samay pehle expire hua tha."
        if added:
            anchor += f", aur {added} naye lapsed customers add hue hain."
        action = "Chahiye main renewal + winback offer ek saath set kar doon?"
    else:
        anchor = f"Your plan expired {days} days ago" if days else "Your plan lapsed a little while back."
        if added:
            anchor += f", and {added} more customers have gone lapsed since."
        action = "Want me to bundle renewal with a winback offer for them?"
    return Msg(anchor, action, cta_type="binary", cta_opts=(("YES", "set it up"), ("STOP", "not now")),
               lever="loss_aversion+specificity", topic="winback_eligible")


def _h_active_planning_intent(category, merchant, trigger, mode):
    # Pattern D fix: merchant already said yes/gave direction -> act, don't re-qualify.
    p = trigger["payload"]
    topic = p.get("intent_topic", "").replace("_", " ")
    last_msg = p.get("merchant_last_message", "")
    if mode == "hi_en":
        anchor = f"Samajh gayi — {topic} ke liye maine ek draft plan bana diya hai."
        action = "Reply YES karein toh main isko finalize karke bhej doon, ya batayein kya change karna hai."
    else:
        anchor = f"Got it — I've put together a draft plan for {topic}."
        action = "Reply YES and I'll finalize and send it over, or tell me what to change."
    return Msg(anchor, action, cta_type="binary", cta_opts=(("YES", "finalize it"), ("STOP", "hold off")),
               lever="effort_externalization", topic="active_planning_intent")


def _h_category_seasonal(category, merchant, trigger, mode):
    p = trigger["payload"]
    season, trends, shelf = p.get("season"), p.get("trends", []), p.get("shelf_action_recommended")
    trends_txt = ", ".join(t.replace("_", " ") for t in trends[:3])
    if mode == "hi_en":
        anchor = f"{season.replace('_',' ')} ka demand shift shuru ho gaya hai: {trends_txt}."
        action = "Chahiye main ek shelf/stock checklist bana doon?" if shelf else "Isko dhyan mein rakhein."
    else:
        anchor = f"{season.replace('_',' ')} demand shift has started: {trends_txt}."
        action = "Want a shelf/stock checklist for it?" if shelf else "Worth keeping in mind."
    return Msg(anchor, action, cta_type="open" if shelf else "none", lever="specificity", topic="category_seasonal")


def _h_renewal_due(category, merchant, trigger, mode):
    p = trigger["payload"]
    sub = merchant.get("subscription", {})
    days = p.get("days_remaining", sub.get("days_remaining"))
    plan = p.get("plan", sub.get("plan"))
    amount = p.get("renewal_amount")
    if mode == "hi_en":
        anchor = f"Aapka {plan} plan {days} din mein renew hona hai." if days else f"Aapka {plan} plan renewal due hai."
        if amount:
            anchor += f" Amount: ₹{_num(amount)}."
        action = "Chahiye main abhi renew kar doon?"
    else:
        anchor = f"Your {plan} plan renews in {days} days." if days else f"Your {plan} plan renewal is due."
        if amount:
            anchor += f" Amount: ₹{_num(amount)}."
        action = "Want me to renew it now?"
    return Msg(anchor, action, cta_type="binary", cta_opts=(("YES", "renew now"), ("STOP", "remind later")),
               lever="loss_aversion+specificity", topic="renewal_due")


MERCHANT_HANDLERS = {
    "research_digest": _h_research_digest,
    "regulation_change": _h_regulation_change,
    "cde_opportunity": _h_cde_opportunity,
    "competitor_opened": _h_competitor_opened,
    "curious_ask_due": _h_curious_ask,
    "dormant_with_vera": _h_dormant_with_vera,
    "festival_upcoming": _h_festival_upcoming,
    "gbp_unverified": _h_gbp_unverified,
    "ipl_match_today": _h_ipl_match,
    "milestone_reached": _h_milestone_reached,
    "perf_dip": _h_perf_dip,
    "perf_spike": _h_perf_spike,
    "review_theme_emerged": _h_review_theme,
    "seasonal_perf_dip": _h_seasonal_perf_dip,
    "supply_alert": _h_supply_alert,
    "winback_eligible": _h_winback_eligible,
    "active_planning_intent": _h_active_planning_intent,
    "category_seasonal": _h_category_seasonal,
    "renewal_due": _h_renewal_due,
}


# ==========================================================================
# CUSTOMER-FACING kind handlers
# ==========================================================================

def _cust_name(customer: dict) -> str:
    return customer.get("identity", {}).get("name", "there")


def _h_recall_due(category, merchant, trigger, customer, mode):
    p = trigger["payload"]
    service, due, slots = p.get("service_due"), p.get("due_date"), p.get("available_slots")
    offer = _active_offer(merchant)
    if not _is_placeholder(trigger) and service:
        service_txt = service.replace("_", " ")
        offer_txt = f" {offer['title']}." if offer else ""
        if slots and len(slots) >= 2:
            if mode == "hi_en":
                anchor = f"{merchant['identity']['name']} ki taraf se — aapka {service_txt} due hai."
                action = f"2 slots ready hain: {slots[0]['label']} ya {slots[1]['label']}.{offer_txt}"
            else:
                anchor = f"This is {merchant['identity']['name']} — your {service_txt} is due."
                action = f"2 slots open: {slots[0]['label']} or {slots[1]['label']}.{offer_txt}"
            return Msg(anchor, action, cta_type="binary",
                       cta_opts=(("1", slots[0]["label"]), ("2", slots[1]["label"])),
                       lever="specificity+loss_aversion", topic="recall_due")
    # fallback: derive from relationship history, no fake slot times
    rel = customer.get("relationship", {})
    months = _months_since(rel.get("last_visit"))
    pref = customer.get("preferences", {}).get("preferred_slots", "").replace("_", " ")
    if mode == "hi_en":
        anchor = f"{merchant['identity']['name']} ki taraf se — {months} mahine ho gaye aapki last visit ko." if months \
            else f"{merchant['identity']['name']} ki taraf se ek reminder hai."
        action = f"Aapka regular check-up due hai." + (f" {pref.title()} slot theek rahega?" if pref else "")
    else:
        anchor = f"This is {merchant['identity']['name']} — it's been {months} months since your last visit." if months \
            else f"A quick reminder from {merchant['identity']['name']}."
        action = "Your regular check-up is due." + (f" Does a {pref} slot work?" if pref else "")
    return Msg(anchor, action, cta_type="binary", cta_opts=(("1", "book a slot"), ("2", "not now")),
               lever="specificity+loss_aversion", topic="recall_due")


def _h_appointment_tomorrow(category, merchant, trigger, customer, mode):
    name = merchant["identity"]["name"]
    rel = customer.get("relationship", {})
    services = rel.get("services_received") or []
    last_service = services[-1] if services else None
    service_txt = f" for your {last_service}" if last_service else ""
    if mode == "hi_en":
        anchor = f"{name} ki taraf se — kal aapki appointment hai{service_txt}."
        action = "Confirm karein ya reschedule chahiye?"
    else:
        anchor = f"This is {name} — you have an appointment tomorrow{service_txt}."
        action = "Can you confirm, or would you like to reschedule?"
    return Msg(anchor, action, cta_type="binary", cta_opts=(("1", "confirm"), ("2", "reschedule")),
               lever="specificity", topic="appointment_tomorrow")


def _h_chronic_refill(category, merchant, trigger, customer, mode):
    p = trigger["payload"]
    molecules, last_refill, runs_out = p.get("molecule_list"), p.get("last_refill"), p.get("stock_runs_out_iso")
    name = merchant["identity"]["name"]
    if not _is_placeholder(trigger) and molecules:
        mol_txt = ", ".join(molecules)
        runs_out_txt = f" Aapka stock {runs_out[:10]} tak khatam ho sakta hai." if runs_out else ""
        if mode == "hi_en":
            anchor = f"{name} se — aapki regular medicines ({mol_txt}) ka refill time aa gaya hai.{runs_out_txt}"
            action = "Same order repeat karun ya kuch change karna hai?"
        else:
            anchor = f"This is {name} — time to refill your regular medicines ({mol_txt}).{runs_out_txt}"
            action = "Should I repeat the same order, or is anything changing?"
        return Msg(anchor, action, cta_type="binary", cta_opts=(("1", "repeat same order"), ("2", "change it")),
                   lever="specificity+loss_aversion", topic="chronic_refill_due")
    agg = merchant.get("customer_aggregate", {})
    has_chronic = agg.get("chronic_rx_count", 0) > 0
    if mode == "hi_en":
        anchor = f"{name} se — aapke regular medicines ka refill due ho sakta hai."
        action = "Same order repeat karun?"
    else:
        anchor = f"This is {name} — your regular medicines may be due for a refill."
        action = "Want me to repeat your last order?"
    return Msg(anchor, action, cta_type="binary", cta_opts=(("1", "repeat order"), ("2", "not yet")),
               lever="loss_aversion", topic="chronic_refill_due")


def _h_customer_lapsed_soft(category, merchant, trigger, customer, mode):
    name = merchant["identity"]["name"]
    rel = customer.get("relationship", {})
    months = _months_since(rel.get("last_visit"))
    offer = _active_offer(merchant)
    offer_txt = f" {offer['title']}." if offer else ""
    if mode == "hi_en":
        anchor = f"{name} ki taraf se — {months} mahine ho gaye aapko dekhe hue." if months else \
            f"{name} ki taraf se — kaafi time ho gaya."
        action = f"Wapas aane par ek special hai:{offer_txt}" if offer else "Wapas aana chahenge?"
    else:
        anchor = f"This is {name} — it's been {months} months since we last saw you." if months else \
            f"This is {name} — it's been a while."
        action = f"Come back and there's something special:{offer_txt}" if offer else "Would you like to come back in?"
    return Msg(anchor, action, cta_type="binary", cta_opts=(("YES", "book a visit"), ("STOP", "not now")),
               lever="loss_aversion+reciprocity", topic="customer_lapsed_soft")


def _h_customer_lapsed_hard(category, merchant, trigger, customer, mode):
    p = trigger["payload"]
    days, focus, months_mem = p.get("days_since_last_visit"), p.get("previous_focus"), p.get("previous_membership_months")
    name = merchant["identity"]["name"]
    focus_txt = focus.replace("_", " ") if focus else None
    if mode == "hi_en":
        anchor = f"{name} se — {days} din ho gaye aapko dekhe hue."
        anchor += f" Aap pehle {focus_txt} pe focus kar rahe the." if focus_txt else ""
        action = "Wapas start karna chahenge? Ek fresh plan bana denge."
    else:
        anchor = f"This is {name} — it's been {days} days since we last saw you."
        anchor += f" You were previously focused on {focus_txt}." if focus_txt else ""
        action = "Want to restart? We'll put together a fresh plan."
    return Msg(anchor, action, cta_type="binary", cta_opts=(("YES", "restart"), ("STOP", "not now")),
               lever="loss_aversion+specificity", topic="customer_lapsed_hard")


def _h_trial_followup(category, merchant, trigger, customer, mode):
    p = trigger["payload"]
    trial_date, options = p.get("trial_date"), p.get("next_session_options")
    name = merchant["identity"]["name"]
    if mode == "hi_en":
        anchor = f"{name} se — kaisa laga aapka trial session?"
        action = "Agla step continue karna chahenge?" if not options else \
            f"Agle options hain: {', '.join(options[:2])}."
    else:
        anchor = f"This is {name} — how did your trial session go?"
        action = "Want to continue to the next step?" if not options else \
            f"Next options: {', '.join(options[:2])}."
    return Msg(anchor, action, cta_type="binary", cta_opts=(("YES", "continue"), ("STOP", "not for me")),
               lever="reciprocity", topic="trial_followup")


def _h_wedding_followup(category, merchant, trigger, customer, mode):
    p = trigger["payload"]
    wed_date, days_to, trial_done = p.get("wedding_date"), p.get("days_to_wedding"), p.get("trial_completed")
    name = merchant["identity"]["name"]
    if mode == "hi_en":
        anchor = f"{name} se — aapki wedding {days_to} din mein hai!" if days_to else f"{name} se — wedding update."
        action = "Trial ke baad final look book kar dein?" if trial_done else "Trial session book karein?"
    else:
        anchor = f"This is {name} — your wedding is {days_to} days away!" if days_to else f"This is {name} with a wedding update."
        action = "Ready to lock in your final look after the trial?" if trial_done else "Want to book a trial session?"
    return Msg(anchor, action, cta_type="binary", cta_opts=(("YES", "book it"), ("STOP", "not yet")),
               lever="specificity+loss_aversion", topic="wedding_package_followup")


CUSTOMER_HANDLERS = {
    "recall_due": _h_recall_due,
    "appointment_tomorrow": _h_appointment_tomorrow,
    "chronic_refill_due": _h_chronic_refill,
    "customer_lapsed_soft": _h_customer_lapsed_soft,
    "customer_lapsed_hard": _h_customer_lapsed_hard,
    "trial_followup": _h_trial_followup,
    "wedding_package_followup": _h_wedding_followup,
}


# --------------------------------------------------------------------------
# top-level compose()
# --------------------------------------------------------------------------

def compose(category: dict, merchant: dict, trigger: dict, customer: Optional[dict] = None) -> dict:
    scope = trigger.get("scope", "merchant")
    mode = _lang_mode(category, merchant, customer if scope == "customer" else None)

    if scope == "customer" and customer is not None:
        handler = CUSTOMER_HANDLERS.get(trigger["kind"])
        if handler is None:
            msg = Msg("Quick update from " + merchant["identity"]["name"] + ".", cta_type="open",
                      topic=trigger["kind"])
        else:
            msg = handler(category, merchant, trigger, customer, mode)
        body = _assemble(_cust_name(customer), msg)
        send_as = "merchant_on_behalf"
    else:
        handler = MERCHANT_HANDLERS.get(trigger["kind"])
        if handler is None:
            msg = Msg("Quick update on your listing.", cta_type="open", topic=trigger["kind"])
        else:
            msg = handler(category, merchant, trigger, mode)
        body = _assemble(_greet_name(merchant), msg)
        send_as = "vera"

    rationale = (f"kind={msg.topic or trigger['kind']}; lever(s)={msg.lever}; "
                 f"placeholder_payload={_is_placeholder(trigger)}; lang_mode={mode}")

    return {
        "body": body,
        "cta": _cta_field(msg),
        "send_as": send_as,
        "suppression_key": trigger.get("suppression_key", f"{trigger['kind']}:{merchant.get('merchant_id')}"),
        "rationale": rationale,
    }
