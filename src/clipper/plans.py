"""Plans shown on the pricing page.

PLACEHOLDERS: the prices, minutes and the split of features between plans are a
starting point, not a decision. Change them here; the page, the API and the
usage meter all read this file. Nothing is charged (payments are not connected)
and the monthly minutes are counted but not enforced, so today every account
can use everything.
"""
from __future__ import annotations

from datetime import date

CURRENCY = "USD"

PLANS = [
    {"id": "free", "name": "Free", "tagline": "Free forever", "monthly": 0, "yearly": 0, "minutes": 60,
     "features": ["60 minutes of video a month", "AI clipping and auto-polish", "Animated captions", "Reframe to vertical",
                  "Export up to 1080p"]},
    {"id": "starter", "name": "Starter", "tagline": "For individual creators", "monthly": 15, "yearly": 15, "minutes": 150,
     "yearly_note": "Starter is billed monthly only",
     "features": ["150 minutes of video a month", "Everything in Free, plus:", "The editor: cut by editing the transcript",
                  "Filler and pause removal", "Punch-in zooms", "Tell the AI editor what to change", "1 brand template"]},
    {"id": "pro", "name": "Pro", "tagline": "For professional creators, marketers and teams", "monthly": 29, "yearly": 14.5,
     "minutes": 300, "popular": True,
     "features": ["300 minutes of video a month", "Everything in Starter, plus:", "Title cards, lower thirds and callouts",
                  "Your own b-roll, music and sound effects", "Transitions", "Every shape: 9:16, 1:1, 4:5, 16:9",
                  "Copy the style of a reference short", "Command line for batch runs", "4 brand templates",
                  {"text": "Social scheduler", "soon": True}, {"text": "Export to Premiere Pro and DaVinci Resolve", "soon": True},
                  {"text": "Speech enhance and dubbing", "soon": True}, {"text": "Team workspace with 2 seats", "soon": True}]},
    {"id": "business", "name": "Business", "tagline": "For organisations that need a tailored set-up", "monthly": None,
     "yearly": None, "minutes": None,
     "features": ["Everything in Pro, plus:", "Minutes and seats sized to your team", "Your own templates and fonts built in",
                  "Help setting up batch runs and automation", "Priority support"]},
]

# The comparison table: one row per feature, one value per plan in the order above.
# True is a tick, False a dash, "soon" a Soon tag, any other text is shown as it is.
COMPARE = [
    ("AI clipping", [
        ("Minutes of video a month", ["60", "150", "300", "Custom"]),
        ("Clip finder", [True, True, True, True]),
        ("Choose how many clips and how long", [True, True, True, True]),
        ("Auto-polish", [True, True, True, True]),
        ("Tell the AI editor what to change", [False, True, True, True]),
    ]),
    ("Captions", [
        ("Animated caption templates", [True, True, True, True]),
        ("Change size, position and colours", [False, True, True, True]),
        ("Copy captions from a reference short", [False, False, True, True]),
    ]),
    ("Reframe", [
        ("Shapes", ["9:16", "9:16", "9:16, 1:1, 4:5, 16:9", "9:16, 1:1, 4:5, 16:9"]),
        ("Speaker kept in frame", [True, True, True, True]),
    ]),
    ("Editing", [
        ("Cut by editing the transcript", [False, True, True, True]),
        ("Filler and pause removal", [False, True, True, True]),
        ("Punch-in zooms", [False, True, True, True]),
        ("Title cards, lower thirds, callouts", [False, False, True, True]),
        ("Your own b-roll", [False, False, True, True]),
        ("Music with ducking, sound effects", [False, False, True, True]),
        ("Transitions", [False, False, True, True]),
        ("Speech enhance", [False, False, "soon", "soon"]),
        ("Dubbing", [False, False, "soon", "soon"]),
    ]),
    ("Branding", [
        ("Brand templates", [False, "1", "4", "Custom"]),
        ("Reference styles", [False, False, True, True]),
    ]),
    ("Export and publish", [
        ("MP4 up to 1080p", [True, True, True, True]),
        ("Export to Premiere Pro and DaVinci Resolve", [False, False, "soon", "soon"]),
        ("Social scheduler", [False, False, "soon", "soon"]),
    ]),
    ("Automation", [
        ("Command line", [False, False, True, True]),
    ]),
    ("Team", [
        ("Team workspace", [False, False, "soon", "soon"]),
        ("Seats", ["1", "1", "2", "Custom"]),
    ]),
]


# A dated offer. It shows by itself from `starts` to `ends` (both days included) and disappears after.
# PLACEHOLDER like the prices: the percentage, the dates and the plans are yours to set. Set PROMO = None for no offer.
PROMO = {
    "id": "black-friday-2026",
    "name": "Black Friday",
    "percent": 40,
    "plans": ["starter", "pro"],
    "starts": "2026-11-23",     # Black Friday is 27 November 2026; the offer runs the week of it
    "ends": "2026-11-30",       # through Cyber Monday
}


def plan(plan_id: str | None) -> dict:
    return next((p for p in PLANS if p["id"] == plan_id), PLANS[0])


def active_promo(today: date | None = None) -> dict | None:
    """The offer, if today falls inside its dates."""
    if not PROMO:
        return None
    today = today or date.today()
    starts, ends = date.fromisoformat(PROMO["starts"]), date.fromisoformat(PROMO["ends"])
    if not (starts <= today <= ends):
        return None
    names = [p["name"] for p in PLANS if p["id"] in PROMO["plans"]]
    return {**PROMO, "ends_label": f"{ends.day} {ends.strftime('%B')}", "days_left": (ends - today).days,
            "headline": f"{PROMO['name']}: {PROMO['percent']}% off {' and '.join(names)}"}


def _discounted(price: float | None, percent: float) -> float | None:
    return None if price is None else round(price * (100 - percent) / 100, 2)


def payload(today: date | None = None) -> dict:
    promo = active_promo(today)
    plans = []
    for p in PLANS:
        offer = promo and p["id"] in promo["plans"] and p["monthly"]
        plans.append({**p, "promo_monthly": _discounted(p["monthly"], promo["percent"]) if offer else None,
                      "promo_yearly": _discounted(p["yearly"], promo["percent"]) if offer else None})
    return {"currency": CURRENCY, "plans": plans, "promo": promo,
            "compare": [{"group": g, "rows": [{"label": label, "values": v} for label, v in rows]} for g, rows in COMPARE]}
