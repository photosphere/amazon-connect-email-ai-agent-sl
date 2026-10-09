"""Tools the agent can call: knowledge base + simulated connected systems.

The booking engine / PMS and the Example Rewards CRM are not reachable in the
PoC, so they are simulated here on top of DynamoDB with the POC Testing Data
(hotels.json, seed_data.json). Response shapes for reservations follow the
Example Hotel Open API ``/api/v1/partner/reservation/detail`` example, so swapping
in the real API only changes ``_load_reservation`` / ``_save_reservation``.

SOP rules that must never depend on the model are enforced HERE, not in the
prompt: occupancy limits, minimum stay, fully-booked dates, identity check
before reading/changing a booking, LOYALTY 3-criteria verification with a 3-attempt
lockout, risk levels, Advance Purchase non-amendable, cancellation penalties,
Cash & Points minimum 500 points at 15 points = 1 USD.

Every tool receives ``ctx`` (the mutable case state + email metadata) and
returns a JSON-serialisable dict. ``ctx["evidence"]`` collects tool outputs for
the grounding judge and ``ctx["actions"]`` the system changes made.
"""
import random
import re
from datetime import date, datetime, timedelta, timezone

import kb
from common import load_json, now_utc, store

HOTELS = load_json("hotels.json")
SEED = load_json("seed_data.json")
POLICY = HOTELS["common_policy"]
HOTEL_TZ = timezone(timedelta(hours=8))  # all four PoC hotels are UTC+8
NON_CANCELLABLE_CHANNELS = {"GDS", "OTA", "GROUP"}
LOW_RISK_LOYALTY = {"CHECK_PROFILE", "NAME_CORRECTION", "REDEMPTION_OWN_NAME", "DUPLICATE_MERGE", "UNSUBSCRIBE"}
VERIFICATION_FIELDS = ["date_of_birth", "mobile", "email", "postal_address", "company", "last_stay",
                       "source_of_enrolment", "passport"]
POINTS_PER_USD = 15
MIN_REDEEM_POINTS = 500


# ---------------------------------------------------------------- helpers

def _hotel(code):
    code = (code or "").upper().strip()
    aliases = {"LAKESIDE": "HGH1", "FUTIAN": "SZX1", "SEAVIEW": "PEN1", "BEACHFRONT": "PEN2"}
    code = aliases.get(code, code)
    hotel = HOTELS["hotels"].get(code)
    if not hotel:
        raise ToolError(f"Unknown hotel_code '{code}'. PoC hotels: HGH1, SZX1, PEN1, PEN2.")
    return hotel


class ToolError(Exception):
    pass


def _d(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise ToolError(f"Date '{value}' must be YYYY-MM-DD") from exc


def _money(x):
    return round(x + 1e-9, 2)


def _norm(s):
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", str(s or "").lower())


def _match_room(hotel, requested):
    if not requested:
        return None, None
    rooms = hotel["rooms"]
    if requested in rooms:
        return requested, rooms[requested]
    req_tokens = set(re.findall(r"[a-z]+", requested.lower())) - {"room", "the", "wing"}
    best, best_score = None, 0.0
    for name in rooms:
        tokens = set(re.findall(r"[a-z]+", name.lower())) - {"room", "the", "wing"}
        if not tokens:
            continue
        score = len(req_tokens & tokens) / len(req_tokens | tokens)
        if score > best_score:
            best, best_score = name, score
    return (best, rooms[best]) if best_score >= 0.5 else (None, None)


def _occupancy(room):
    """Split guests by the 12-years-and-above-is-adult rule."""
    ages = room.get("children_ages") or []
    unknown = [a for a in ages if a is None or a == ""]
    adults = int(room.get("adults") or 0) + sum(1 for a in ages if a not in (None, "") and int(a) >= POLICY["adult_age_from"])
    children = sum(1 for a in ages if a not in (None, "") and int(a) < POLICY["adult_age_from"])
    return adults, children, len(unknown)


def _fits(adults, children, patterns):
    for p in patterns:
        m = re.match(r"(\d+)A(?:(\d+)C)?", p)
        if m and adults <= int(m.group(1)) and children <= int(m.group(2) or 0):
            return True
    return False


def _price(hotel, plan, nights, adults, foreign_passport):
    rate = hotel["rates"][plan]
    extra = max(0, adults - 2) * hotel["third_occupancy_supplement"]
    base = rate + extra
    tax = hotel["tax"]
    after_sc = base * (1 + tax["service_charge_pct"] / 100)
    taxed = after_sc * (1 + tax["vat_pct"] / 100)
    fees = sum(f["amount"] for f in tax["per_room_night_fees"])
    tourism = tax["tourism_tax_foreign_nett"] if foreign_passport else 0
    per_night = taxed + fees + tourism
    return {"base_rate_per_night": base, "rate_display": f"{hotel['currency']} {rate}++",
            "third_occupancy_supplement_per_night": extra, "per_night_inclusive": _money(per_night),
            "total_inclusive": _money(per_night * nights), "tax_note": tax["note"],
            "tourism_tax_included": bool(tourism)}


def _evidence(ctx, name, payload):
    ctx.setdefault("evidence", []).append({"tool": name, "result": payload})
    return payload


# ---------------------------------------------------------------- knowledge base

def search_knowledge_base(ctx, query, hotel_code=None, top_k=6):
    results = kb.search(query, hotel_code=hotel_code or ctx.get("hotel_code"), top_k=min(int(top_k or 6), 8))
    ctx.setdefault("kb_chunks_seen", []).extend(r["chunk_id"] for r in results)
    return _evidence(ctx, "search_knowledge_base", {"query": query, "results": results,
                     "note": "Answer only from these passages. If they do not contain the answer, report it as a knowledge gap."})


def get_hotel_policy(ctx, hotel_code):
    hotel = _hotel(hotel_code)
    out = {k: hotel[k] for k in ("hotel_code", "name_en", "name_zh", "currency", "availability", "rates",
                                 "third_occupancy_supplement", "tax", "airport_transfer", "check_in", "check_out")}
    out["rate_plans"] = {"FLEX": "Flexible Rate", "AP": "Advance Purchase Rate"}
    out["room_catalog_max_occupancy"] = {k: v["max"] for k, v in hotel["rooms"].items()}
    out["policies"] = POLICY
    return _evidence(ctx, "get_hotel_policy", out)


# ---------------------------------------------------------------- availability & booking

def check_availability(ctx, hotel_code, check_in, check_out, rooms, rate_plan=None, foreign_passport=True):
    hotel = _hotel(hotel_code)
    ci, co = _d(check_in), _d(check_out)
    nights = (co - ci).days
    if nights <= 0:
        raise ToolError("check_out must be after check_in")
    today = now_utc().astimezone(HOTEL_TZ).date()
    if ci < today:
        return _evidence(ctx, "check_availability", {"status": "PAST_DATE", "check_in": str(ci), "today": str(today),
                         "message": "Check-in date is in the past. Ask the guest to confirm the travel dates."})
    av = hotel["availability"]
    window_from, window_to = _d(av["from"]), _d(av["to"])
    stay_nights = [ci + timedelta(days=i) for i in range(nights)]
    out = {"hotel": hotel["name_en"], "check_in": str(ci), "check_out": str(co), "nights": nights}
    if any(n < window_from or n > window_to for n in stay_nights):
        out.update(status="NO_INVENTORY_DATA",
                   message=f"The PoC inventory only covers {av['from']} to {av['to']}. Availability for these dates cannot be confirmed - do not quote or book; pass to a human agent.")
        return _evidence(ctx, "check_availability", out)
    booked = sorted({str(n) for n in stay_nights if str(n) in av["fully_booked"]})
    if booked:
        out.update(status="FULLY_BOOKED", fully_booked_nights=booked,
                   message="The hotel is fully booked on these nights. Offer alternative dates outside the fully booked period.")
        return _evidence(ctx, "check_availability", out)
    rule = av.get("min_stay_rule")
    if rule and any(_d(rule["from"]) <= n <= _d(rule["to"]) for n in stay_nights) and nights < rule["min_nights"]:
        out.update(status="MIN_STAY_NOT_MET",
                   message=f"Stays including {rule['from']} to {rule['to']} require a minimum of {rule['min_nights']} nights.")
        return _evidence(ctx, "check_availability", out)

    plans = [rate_plan.upper()] if rate_plan else ["FLEX", "AP"]
    room_results, needs = [], []
    for i, room in enumerate(rooms or [], start=1):
        adults, children, unknown = _occupancy(room)
        matched, meta = _match_room(hotel, room.get("room_type"))
        r = {"room_no": i, "requested_room_type": room.get("room_type"), "room_type": matched,
             "adults_counted": adults, "children_under_12": children}
        if unknown:
            needs.append(f"room {i}: ages of {unknown} child(ren) are required (12 years and above count as adults)")
            r["status"] = "NEED_CHILD_AGES"
        elif room.get("room_type") and not matched:
            r["status"] = "ROOM_TYPE_NOT_IN_CATALOG"
            r["catalog"] = list(hotel["rooms"].keys())
        elif meta and not _fits(adults, children, meta["max"]):
            r["status"] = "EXCEEDS_MAX_OCCUPANCY"
            r["max_occupancy"] = meta["max"]
            r["advice"] = "Recommend an additional room or a larger room type."
        else:
            r["status"] = "AVAILABLE"
            if meta:
                r["max_occupancy"] = meta["max"]
            r["prices"] = {p: _price(hotel, p, nights, adults, foreign_passport) for p in plans}
        room_results.append(r)
    out.update(status="AVAILABLE" if all(r["status"] == "AVAILABLE" for r in room_results) else "ACTION_REQUIRED",
               rooms=room_results, missing_information=needs,
               cancellation_policy={p: POLICY["cancellation_policy"][p] for p in plans},
               payment={p: POLICY["payment_method"][p] for p in plans},
               airport_transfer=hotel["airport_transfer"], child_meal_policy=POLICY["child_meal_policy"],
               note="Rates are per room per night before tax (++); per_night_inclusive/total_inclusive include service charge, taxes and fees. Tourism tax (Malaysia) applies to foreign passport holders only.")
    return _evidence(ctx, "check_availability", out)


def _new_number():
    for _ in range(20):
        num = str(random.randint(10**10, 10**11 - 1))
        if not _load_reservation(num):
            return num
    raise ToolError("Could not allocate a confirmation number")


def _load_reservation(num):
    num = re.sub(r"\D", "", str(num or ""))
    item = store().get(f"RES#{num}")
    if item:
        return item
    for r in SEED["reservations"]:
        if r["confirmNum"] == num:
            return dict(r)
    return None


def _save_reservation(res):
    store().put(f"RES#{res['confirmNum']}", "META", {k: v for k, v in res.items() if k not in ("pk", "sk")})


def create_reservation(ctx, hotel_code, check_in, check_out, guest, rooms, rate_plan="FLEX",
                       special_requests=None, loyalty_membership_number=None, arrival_details=None,
                       points_to_redeem=0, foreign_passport=True, occasion=None):
    missing = []
    if not (guest or {}).get("last_name") and not (guest or {}).get("full_name"):
        missing.append("guest name")
    if not rooms:
        missing.append("number of rooms and occupancy")
    for i, room in enumerate(rooms or [], start=1):
        if not room.get("room_type"):
            missing.append(f"room {i}: preferred room type")
    if missing:
        return _evidence(ctx, "create_reservation", {"status": "MISSING_MANDATORY_INFO", "missing": missing})
    avail = check_availability(ctx, hotel_code, check_in, check_out, rooms, rate_plan, foreign_passport)
    if avail["status"] != "AVAILABLE":
        return _evidence(ctx, "create_reservation", {"status": "NOT_BOOKED", "reason": avail.get("status"), "availability": avail})
    plan = (rate_plan or "FLEX").upper()
    hotel = _hotel(hotel_code)
    redemption = None
    if points_to_redeem:
        redemption = _apply_points(ctx, loyalty_membership_number, int(points_to_redeem))
        if redemption["status"] != "OK":
            return _evidence(ctx, "create_reservation", {"status": "NOT_BOOKED", "reason": "POINTS_REDEMPTION_FAILED", "redemption": redemption})
    created = []
    for room, priced in zip(rooms, avail["rooms"]):
        num = _new_number()
        res = {
            "confirmNum": num, "hotelCode": hotel["hotel_code"], "status": "RESERVED",
            "guest": {"title": guest.get("title"), "firstName": guest.get("first_name"),
                      "lastName": guest.get("last_name") or guest.get("full_name"),
                      "email": guest.get("email") or ctx.get("customer_email"), "mobile": guest.get("mobile")},
            "arrivalDate": str(_d(check_in)), "departureDate": str(_d(check_out)),
            "rooms": [{"room_type": priced["room_type"] or room.get("room_type"), "adults": room.get("adults"),
                       "children_ages": room.get("children_ages") or []}],
            "ratePlan": plan, "channel": "Direct (email AI)", "packages": ["Room only"],
            "memberNo": loyalty_membership_number, "specials": [{"name": s} for s in (special_requests or [])],
            "comments": [{"commentText": f"Arrival details: {arrival_details}"}] if arrival_details else [],
            "occasion": occasion, "total_inclusive": priced["prices"][plan]["total_inclusive"],
            "currency": hotel["currency"], "created_by": "email-ai", "created_at": now_utc().isoformat(timespec="seconds"),
            "case_id": ctx.get("case_id"),
        }
        if redemption and not created:
            res["points_redeemed"] = redemption["points_redeemed"]
            res["points_value_usd"] = redemption["value_usd"]
        _save_reservation(res)  # General Logic #1: stored so later amend/cancel/reconfirm emails find it
        created.append({"confirmation_number": num, "room_type": res["rooms"][0]["room_type"],
                        "occupancy": f"{room.get('adults')} adult(s), children ages {room.get('children_ages') or []}",
                        "total_inclusive": f"{hotel['currency']} {res['total_inclusive']:,.2f}"})
    ctx.setdefault("actions", []).append({"type": "CREATE_RESERVATION", "numbers": [c["confirmation_number"] for c in created]})
    ctx.setdefault("case_state", {}).setdefault("reservations", []).extend(c["confirmation_number"] for c in created)
    out = {"status": "BOOKED", "hotel": hotel["name_en"], "check_in": str(_d(check_in)), "check_out": str(_d(check_out)),
           "rate_plan": {"FLEX": "Flexible Rate", "AP": "Advance Purchase Rate"}[plan], "reservations": created,
           "payment_next_step": ("A credit card guarantee link with the same confirmation number is sent in a separate email and is valid for 48 hours; complete it to avoid automatic cancellation."
                                 if plan == "FLEX" else "A prepayment link for the full stay is sent in a separate email; the booking is non-refundable and non-amendable."),
           "cancellation_policy": POLICY["cancellation_policy"][plan], "special_requests_note": "All special requests are noted and subject to availability.",
           "upsell_after_booking": POLICY["upsell"]}
    if redemption:
        out["points_redemption"] = redemption
    return _evidence(ctx, "create_reservation", out)


def _identity(ctx, res, guest_name, email=None, mobile=None):
    g = res.get("guest", {})
    names = {_norm(g.get("lastName")), _norm(g.get("firstName")), _norm(f"{g.get('firstName', '')}{g.get('lastName', '')}")} - {""}
    provided = _norm(guest_name)
    provided_tokens = {_norm(t) for t in re.split(r"[\s,]+", guest_name or "")} - {""}
    name_ok = bool(provided) and (provided in names or bool(provided_tokens & names))
    contacts = {_norm(email), _norm(ctx.get("customer_email")), re.sub(r"\D", "", mobile or "")[-8:]} - {""}
    res_contacts = {_norm(g.get("email")), re.sub(r"\D", "", g.get("mobile") or "")[-8:]} - {""}
    contact_ok = bool(contacts & res_contacts)
    return name_ok, contact_ok


def find_reservation(ctx, confirmation_number, guest_name, email=None, mobile=None):
    res = _load_reservation(confirmation_number)
    if not res:
        return _evidence(ctx, "find_reservation", {"status": "NOT_FOUND", "message": "No reservation matches this confirmation number. Ask the guest for the lead guest name, 11-digit confirmation number, hotel and travel dates."})
    name_ok, contact_ok = _identity(ctx, res, guest_name, email, mobile)
    if not name_ok:
        return _evidence(ctx, "find_reservation", {"status": "IDENTITY_NOT_MATCHED", "message": "The guest name does not match this booking. Do not disclose any booking detail; ask the guest to re-check the confirmation number and lead guest name."})
    if not contact_ok:
        return _evidence(ctx, "find_reservation", {"status": "NEED_CONTACT_VERIFICATION", "message": "Name matches but the sender email is not on the booking. Ask for the mobile number or email address registered on the booking before disclosing or changing anything."})
    ctx.setdefault("case_state", {}).setdefault("verified_reservations", [])
    if res["confirmNum"] not in ctx["case_state"]["verified_reservations"]:
        ctx["case_state"]["verified_reservations"].append(res["confirmNum"])
    hotel = _hotel(res["hotelCode"])
    detail = {"status": "FOUND_AND_VERIFIED", "hotel": hotel["name_en"], "reservation": _pms_view(res),
              "check_in_time": hotel["check_in"], "check_out_time": hotel["check_out"],
              "cancellation_policy": POLICY["cancellation_policy"].get(res.get("ratePlan"), ""),
              "amendable": res.get("ratePlan") != "AP" and res.get("channel", "").upper() not in NON_CANCELLABLE_CHANNELS}
    return _evidence(ctx, "find_reservation", detail)


def _pms_view(res):
    """Shape of Open API reservation/detail (subset)."""
    g = res.get("guest", {})
    rooms = res.get("rooms", [])
    return {"confirmNum": res["confirmNum"], "hotelCode": res["hotelCode"], "status": res["status"],
            "arrivalDate": res["arrivalDate"], "departureDate": res["departureDate"],
            "roomNum": len(rooms), "adultNum": sum(int(r.get("adults") or 0) for r in rooms),
            "childNum": sum(len(r.get("children_ages") or []) for r in rooms),
            "chargedRoomType": ", ".join(r.get("room_type", "") for r in rooms),
            "rateCodes": [res.get("ratePlan")], "packages": res.get("packages", []), "channel": res.get("channel"),
            "guestInfos": [{"title": g.get("title"), "firstName": g.get("firstName"), "lastName": g.get("lastName"), "memberNo": res.get("memberNo")}],
            "specials": res.get("specials", []), "comments": res.get("comments", []),
            "cancellationNum": res.get("cancellationNum")}


def _require_verified(ctx, num):
    if re.sub(r"\D", "", str(num)) not in ctx.get("case_state", {}).get("verified_reservations", []):
        raise ToolError("Booking identity not verified in this case. Call find_reservation with the confirmation number and guest name first.")


def update_reservation(ctx, confirmation_number, remarks=None, special_requests=None, arrival_time=None,
                       flight_details=None, new_check_in=None, new_check_out=None, new_room_type=None):
    _require_verified(ctx, confirmation_number)
    res = _load_reservation(confirmation_number)
    if res["status"] in ("CANCELLED", "CHECK_OUT", "NO_SHOW"):
        return _evidence(ctx, "update_reservation", {"status": "NOT_UPDATED", "reason": f"Reservation status is {res['status']}"})
    changes = []
    if new_check_in or new_check_out or new_room_type:
        if res.get("ratePlan") == "AP":
            return _evidence(ctx, "update_reservation", {"status": "NOT_AMENDABLE", "reason": "Advance Purchase Rate: amendment to reservations is not allowed."})
        if res.get("channel", "").upper() in NON_CANCELLABLE_CHANNELS:
            return _evidence(ctx, "update_reservation", {"status": "NOT_AMENDABLE", "reason": f"{res['channel']} bookings must be amended through the original booking channel."})
        ci, co = new_check_in or res["arrivalDate"], new_check_out or res["departureDate"]
        rooms = [{**r, "room_type": new_room_type or r.get("room_type")} for r in res["rooms"]]
        avail = check_availability(ctx, res["hotelCode"], ci, co, rooms, res.get("ratePlan"))
        if avail["status"] != "AVAILABLE":
            return _evidence(ctx, "update_reservation", {"status": "NOT_UPDATED", "reason": "New dates/room not available", "availability": avail})
        res["arrivalDate"], res["departureDate"], res["rooms"] = str(_d(ci)), str(_d(co)), rooms
        res["total_inclusive"] = sum(r["prices"][res["ratePlan"]]["total_inclusive"] for r in avail["rooms"])
        changes.append(f"dates/room changed to {ci} - {co} {new_room_type or ''}".strip())
    for s in special_requests or []:
        res.setdefault("specials", []).append({"name": s})
        changes.append(f"special request noted: {s}")
    for text in [remarks, f"Arrival time: {arrival_time}" if arrival_time else None, f"Flight: {flight_details}" if flight_details else None]:
        if text:
            res.setdefault("comments", []).append({"commentText": text, "createOperator": "email-ai", "createTime": now_utc().isoformat(timespec="seconds")})
            changes.append(f"remark added: {text}")
    if not changes:
        raise ToolError("Nothing to update")
    _save_reservation(res)
    ctx.setdefault("actions", []).append({"type": "UPDATE_RESERVATION", "number": res["confirmNum"], "changes": changes})
    return _evidence(ctx, "update_reservation", {"status": "UPDATED", "changes": changes, "reservation": _pms_view(res),
                     "note": "Special requests are subject to availability; the relevant hotel department has been informed via the booking remark."})


def cancel_reservation(ctx, confirmation_number, reason, guest_accepts_penalty=False, guest_requests_waiver=False):
    _require_verified(ctx, confirmation_number)
    res = _load_reservation(confirmation_number)
    status = res["status"]
    if status in ("IN_HOUSE", "CHECK_IN", "DUE_OUT"):
        return _evidence(ctx, "cancel_reservation", {"status": "GUEST_IN_HOUSE", "action": "Inform the guest to approach the Front Office of the hotel directly; do not cancel."})
    if status == "CANCELLED":
        return _evidence(ctx, "cancel_reservation", {"status": "ALREADY_CANCELLED", "cancellation_number": res.get("cancellationNum")})
    if status in ("CHECK_OUT", "NO_SHOW"):
        return _evidence(ctx, "cancel_reservation", {"status": "NOT_CANCELLABLE", "reason": f"Reservation status is {status}", "action": "Pass to a human agent."})
    if res.get("channel", "").upper() in NON_CANCELLABLE_CHANNELS:
        return _evidence(ctx, "cancel_reservation", {"status": "NOT_CANCELLABLE", "reason": f"{res['channel']} booking - must be cancelled with the original booking channel."})
    hotel = _hotel(res["hotelCode"])
    arrival = _d(res["arrivalDate"])
    deadline = datetime.combine(arrival - timedelta(days=1), datetime.min.time(), HOTEL_TZ).replace(hour=18)
    nights = (_d(res["departureDate"]) - arrival).days
    per_night = (res.get("total_inclusive") or _price(hotel, res.get("ratePlan", "FLEX"), nights,
                 sum(int(r.get("adults") or 0) for r in res["rooms"]), True)["total_inclusive"] * len(res["rooms"])) / max(1, nights)
    if res.get("ratePlan") == "AP":
        penalty = per_night * nights
        basis = "Advance Purchase Rate: prepayment is not refunded for cancellations."
    elif now_utc() > deadline.astimezone(timezone.utc):
        penalty = per_night
        basis = f"Cancelled after 18:00 hotel time on {arrival - timedelta(days=1)}: one-night room charge applies."
    else:
        penalty = 0
        basis = f"Free cancellation until 18:00 hotel time on {arrival - timedelta(days=1)}."
    penalty = _money(penalty)
    if penalty > 0 and guest_requests_waiver:
        return _evidence(ctx, "cancel_reservation", {"status": "WAIVER_NEEDS_APPROVAL", "penalty": f"{hotel['currency']} {penalty:,.2f}", "basis": basis,
                         "action": "Penalty waiver requires human approval - escalate with the supporting reason; do not cancel yet."})
    if penalty > 0 and not guest_accepts_penalty:
        return _evidence(ctx, "cancel_reservation", {"status": "REQUIRES_GUEST_CONFIRMATION", "penalty": f"{hotel['currency']} {penalty:,.2f}", "basis": basis,
                         "action": "Explain the penalty and ask the guest to confirm whether to proceed or keep the booking."})
    res["status"] = "CANCELLED"
    res["cancellationNum"] = _new_number()
    res.setdefault("comments", []).append({"commentText": f"Received email from guest to cancel booking due to {reason}. Penalty {penalty}. Cancelled by email-ai / {now_utc().date()}"})
    _save_reservation(res)
    ctx.setdefault("actions", []).append({"type": "CANCEL_RESERVATION", "number": res["confirmNum"], "cancellation_number": res["cancellationNum"]})
    g = res["guest"]
    return _evidence(ctx, "cancel_reservation", {"status": "CANCELLED", "confirmation_number": res["confirmNum"],
                     "cancellation_number": res["cancellationNum"], "guest_name": " ".join(filter(None, [g.get("title"), g.get("firstName"), g.get("lastName")])),
                     "penalty": f"{hotel['currency']} {penalty:,.2f}", "basis": basis, "cancellation_letter": "Cancellation letter will be emailed to the guest shortly."})


# ---------------------------------------------------------------- Example Rewards

def _profiles():
    out = []
    for p in SEED["loyalty_profiles"]:
        override = store().get(f"LOYALTY#{p['membership_number']}")
        out.append(override or dict(p))
    return out


def _find_profile(membership_number=None, email=None):
    num = re.sub(r"\D", "", str(membership_number or ""))
    for p in _profiles():
        if (num and p["membership_number"] == num) or (email and p.get("email") and p["email"].lower() == email.lower().strip()):
            return p
    return None


def _cmp(field, stored, given):
    if stored in (None, "") or given in (None, ""):
        return None
    if field == "date_of_birth":
        g = _parse_dob(given)
        return g is not None and g == stored
    if field == "mobile":
        a, b = re.sub(r"\D", "", stored), re.sub(r"\D", "", given)
        return len(b) >= 8 and a[-8:] == b[-8:]
    if field in ("email", "passport", "company"):
        return _norm(stored) == _norm(given)
    if field == "postal_address":  # SOP: must verify the address in full
        return _norm(stored) == _norm(given)
    if field == "source_of_enrolment":
        def kind(s):
            s = s.lower()
            return "mobile site" if "mobile site" in s or "mobile web" in s else "app" if "app" in s else "website" if "web" in s else _norm(s)
        return kind(stored) == kind(given)
    if field == "last_stay":
        st, gt = set(re.findall(r"[a-z]+", stored.lower())) - {"example", "hotel", "hotels", "on"}, set(re.findall(r"[a-z]+", given.lower())) - {"example", "hotel", "hotels", "on"}
        return bool(st) and len(st & gt) / len(st) >= 0.6
    return _norm(stored) == _norm(given)


def _parse_dob(text):
    text = str(text).strip()
    for fmt in ("%Y-%m-%d", "%d %B %Y", "%d %b %Y", "%B %d %Y", "%B %d, %Y", "%d/%m/%Y", "%Y/%m/%d", "%d-%m-%Y", "%Y年%m月%d日"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def verify_loyalty_member(ctx, provided, membership_number=None, email=None):
    state = ctx.setdefault("case_state", {})
    ver = state.setdefault("loyalty_verification", {"attempts": 0, "verified_member": None})
    if ver.get("verified_member"):
        return _evidence(ctx, "verify_loyalty_member", {"status": "ALREADY_VERIFIED", "membership_number": ver["verified_member"]})
    if ver["attempts"] >= 3:
        return _evidence(ctx, "verify_loyalty_member", {"status": "LOCKED", "action": "Verification failed 3 times - transfer to a human agent."})
    profile = _find_profile(membership_number, email or (provided or {}).get("email") or ctx.get("customer_email"))
    if not profile:
        ver["attempts"] += 1
        return _evidence(ctx, "verify_loyalty_member", {"status": "PROFILE_NOT_FOUND", "attempts_used": ver["attempts"], "attempts_left": 3 - ver["attempts"],
                         "action": "Ask the guest to double-check the membership number / registered email."})
    matched, not_matched = [], []
    for field in VERIFICATION_FIELDS:
        result = _cmp(field, profile.get(field), (provided or {}).get(field))
        if result is True:
            matched.append(field)
        elif result is False:
            not_matched.append(field)
    if len(matched) >= 3:
        ver["verified_member"] = profile["membership_number"]
        return _evidence(ctx, "verify_loyalty_member", {"status": "VERIFIED", "matched_fields": matched, "membership_number": profile["membership_number"]})
    ver["attempts"] += 1
    remaining = [f for f in VERIFICATION_FIELDS if f not in matched and f not in not_matched and profile.get(f)]
    status = "LOCKED" if ver["attempts"] >= 3 else "NOT_VERIFIED"
    return _evidence(ctx, "verify_loyalty_member", {
        "status": status, "matched_count": len(matched), "required": 3, "not_matched_fields": not_matched,
        "other_fields_guest_may_provide": remaining, "attempts_used": ver["attempts"], "attempts_left": max(0, 3 - ver["attempts"]),
        "action": "Transfer to a human agent." if status == "LOCKED" else "Politely ask the guest to try again or provide other items from the verification list. Never reveal stored values."})


def _verified_profile(ctx, membership_number=None):
    ver = ctx.get("case_state", {}).get("loyalty_verification", {})
    num = ver.get("verified_member")
    if not num or (membership_number and re.sub(r"\D", "", str(membership_number)) != num):
        raise ToolError("Member not verified in this case. Call verify_loyalty_member first (3 matching criteria required).")
    return _find_profile(num)


def get_loyalty_account(ctx, membership_number=None):
    p = _verified_profile(ctx, membership_number)
    return _evidence(ctx, "get_loyalty_account", {
        "membership_number": p["membership_number"], "name": p.get("name"), "salutation": p.get("salutation"), "tier": p["tier"],
        "points_balance": p["points_balance"], "points_expiry": p["points_expiry"], "registered_email": p.get("email"),
        "source_of_enrolment": p.get("source_of_enrolment"), "last_stay": p.get("last_stay"),
        "note": "Tier nights / tier points progress are not in the PoC dummy profile - say so rather than inventing numbers."})


def update_loyalty_profile(ctx, request_type, field, new_value, membership_number=None):
    p = _verified_profile(ctx, membership_number)
    request_type = (request_type or "").upper()
    change = {"membership_number": p["membership_number"], "request_type": request_type, "field": field, "new_value": new_value}
    if request_type in LOW_RISK_LOYALTY and request_type != "DUPLICATE_MERGE":
        if request_type == "UNSUBSCRIBE":
            p["marketing_opt_in"] = False
        elif field in ("name", "salutation"):
            p[field] = new_value
        store().put(f"LOYALTY#{p['membership_number']}", "META", {k: v for k, v in p.items() if k not in ("pk", "sk")})
        ctx.setdefault("actions", []).append({"type": "UPDATE_LOYALTY_PROFILE", **change})
        return _evidence(ctx, "update_loyalty_profile", {"status": "UPDATED", "risk_level": "LOW", **change})
    ctx.setdefault("actions", []).append({"type": "LOYALTY_CHANGE_PENDING_APPROVAL", **change})
    ctx.setdefault("case_state", {})["requires_human_validation"] = True
    return _evidence(ctx, "update_loyalty_profile", {"status": "PENDING_HUMAN_APPROVAL", "risk_level": "HIGH" if request_type not in LOW_RISK_LOYALTY else "LOW", **change,
                     "action": "Tell the guest verification is complete and the change request has been passed to the Member Services team for processing; set disposition HUMAN_REVIEW."})


def quote_cash_and_points(ctx, points=None, usd_amount=None):
    out = {"rule": f"Every {POINTS_PER_USD} points = USD 1. Minimum {MIN_REDEEM_POINTS} points. Members only. Available on Flexible Rate / Flexible Rate with Breakfast for selected room types; remaining balance settled upon arrival."}
    if points is not None:
        points = int(points)
        out.update(points=points, value_usd=_money(points / POINTS_PER_USD), eligible=points >= MIN_REDEEM_POINTS)
    if usd_amount is not None:
        out.update(usd_amount=usd_amount, points_needed=int(-(-float(usd_amount) * POINTS_PER_USD // 1)))
    return _evidence(ctx, "quote_cash_and_points", out)


def _apply_points(ctx, membership_number, points):
    p = _verified_profile(ctx, membership_number)
    if points < MIN_REDEEM_POINTS:
        return {"status": "BELOW_MINIMUM", "minimum": MIN_REDEEM_POINTS}
    if points > p["points_balance"]:
        return {"status": "INSUFFICIENT_POINTS", "balance": p["points_balance"]}
    p["points_balance"] -= points
    remaining, expiry = points, []
    for bucket in sorted(p["points_expiry"], key=lambda b: b["expires"]):  # burn earliest-expiring first
        used = min(bucket["points"], remaining)
        remaining -= used
        if bucket["points"] - used:
            expiry.append({**bucket, "points": bucket["points"] - used})
    p["points_expiry"] = expiry
    store().put(f"LOYALTY#{p['membership_number']}", "META", {k: v for k, v in p.items() if k not in ("pk", "sk")})
    ctx.setdefault("actions", []).append({"type": "REDEEM_POINTS", "membership_number": p["membership_number"], "points": points})
    return {"status": "OK", "points_redeemed": points, "value_usd": _money(points / POINTS_PER_USD),
            "new_points_balance": p["points_balance"], "points_expiry_after": expiry}


# ---------------------------------------------------------------- registry

REGISTRY = {
    "search_knowledge_base": search_knowledge_base,
    "get_hotel_policy": get_hotel_policy,
    "check_availability": check_availability,
    "create_reservation": create_reservation,
    "find_reservation": find_reservation,
    "update_reservation": update_reservation,
    "cancel_reservation": cancel_reservation,
    "verify_loyalty_member": verify_loyalty_member,
    "get_loyalty_account": get_loyalty_account,
    "update_loyalty_profile": update_loyalty_profile,
    "quote_cash_and_points": quote_cash_and_points,
}

_room_schema = {"type": "object", "properties": {
    "room_type": {"type": "string", "description": "Room type name as in the hotel catalog"},
    "adults": {"type": "integer"},
    "children_ages": {"type": "array", "items": {"type": ["integer", "null"]}, "description": "One entry per child; null if age unknown. Infants count as children (age 0/1)."}},
    "required": ["adults"]}

SPECS = [
    {"name": "search_knowledge_base", "description": "Search the approved hotel / Example Rewards knowledge base (hotel facilities, F&B, rooms, transport, LOYALTY T&C and FAQ, email templates). Call once per distinct question. Queries in English work best; Chinese also works.",
     "schema": {"type": "object", "properties": {"query": {"type": "string"}, "hotel_code": {"type": "string", "enum": ["HGH1", "SZX1", "PEN1", "PEN2", "LOYALTY"]}, "top_k": {"type": "integer"}}, "required": ["query"]}},
    {"name": "get_hotel_policy", "description": "POC dummy data for a hotel: availability window, rates (FLEX/AP), taxes, airport transfer prices, check-in/out, room catalog max occupancy, cancellation/payment/child-meal/upsell policies. This data PREVAILS over KB documents for rates and availability.",
     "schema": {"type": "object", "properties": {"hotel_code": {"type": "string", "enum": ["HGH1", "SZX1", "PEN1", "PEN2"]}}, "required": ["hotel_code"]}},
    {"name": "check_availability", "description": "Check availability and price per room (occupancy, min-stay, fully booked). Returns inclusive totals. Use before any quotation.",
     "schema": {"type": "object", "properties": {"hotel_code": {"type": "string"}, "check_in": {"type": "string", "description": "YYYY-MM-DD"}, "check_out": {"type": "string"},
                "rooms": {"type": "array", "items": _room_schema}, "rate_plan": {"type": "string", "enum": ["FLEX", "AP"]},
                "foreign_passport": {"type": "boolean", "description": "Malaysian tourism tax applies to foreign passport holders; default true"}},
                "required": ["hotel_code", "check_in", "check_out", "rooms"]}},
    {"name": "create_reservation", "description": "Create the reservation (one confirmation number per room) ONLY when the guest clearly asked to book and all mandatory info is known: hotel, dates, guest name, occupancy incl. child ages, number of rooms, room type. Never for quotation-only / pending-decision emails.",
     "schema": {"type": "object", "properties": {"hotel_code": {"type": "string"}, "check_in": {"type": "string"}, "check_out": {"type": "string"},
                "guest": {"type": "object", "properties": {"title": {"type": "string"}, "first_name": {"type": "string"}, "last_name": {"type": "string"}, "full_name": {"type": "string"}, "email": {"type": "string"}, "mobile": {"type": "string"}}},
                "rooms": {"type": "array", "items": _room_schema}, "rate_plan": {"type": "string", "enum": ["FLEX", "AP"]},
                "special_requests": {"type": "array", "items": {"type": "string"}}, "loyalty_membership_number": {"type": "string"},
                "arrival_details": {"type": "string"}, "points_to_redeem": {"type": "integer", "description": "Cash & Points - requires verified member"},
                "foreign_passport": {"type": "boolean"}, "occasion": {"type": "string"}},
                "required": ["hotel_code", "check_in", "check_out", "guest", "rooms"]}},
    {"name": "find_reservation", "description": "Validate an existing booking: confirmation number + guest name (+ email/mobile if sender email differs). Required before reconfirm / amend / cancel / special request on a booking.",
     "schema": {"type": "object", "properties": {"confirmation_number": {"type": "string"}, "guest_name": {"type": "string"}, "email": {"type": "string"}, "mobile": {"type": "string"}}, "required": ["confirmation_number", "guest_name"]}},
    {"name": "update_reservation", "description": "Add remarks / special requests / arrival time / flight to a verified booking, or amend dates / room type (not allowed for Advance Purchase).",
     "schema": {"type": "object", "properties": {"confirmation_number": {"type": "string"}, "remarks": {"type": "string"}, "special_requests": {"type": "array", "items": {"type": "string"}},
                "arrival_time": {"type": "string"}, "flight_details": {"type": "string"}, "new_check_in": {"type": "string"}, "new_check_out": {"type": "string"}, "new_room_type": {"type": "string"}},
                "required": ["confirmation_number"]}},
    {"name": "cancel_reservation", "description": "Cancel a verified booking following the Cancel Booking FIT flow. Returns REQUIRES_GUEST_CONFIRMATION when a penalty applies and the guest has not accepted it yet.",
     "schema": {"type": "object", "properties": {"confirmation_number": {"type": "string"}, "reason": {"type": "string"},
                "guest_accepts_penalty": {"type": "boolean"}, "guest_requests_waiver": {"type": "boolean", "description": "True if the guest asks to cancel without the applicable charge"}},
                "required": ["confirmation_number", "reason"]}},
    {"name": "verify_loyalty_member", "description": "LOYALTY identity verification: needs 3 matching criteria among date_of_birth, mobile, email, postal_address (in full), company, last_stay, source_of_enrolment, passport. Membership number alone is NOT a criterion. 3 failed attempts lock the case.",
     "schema": {"type": "object", "properties": {"membership_number": {"type": "string"}, "email": {"type": "string", "description": "Registered email used to locate the profile if no membership number"},
                "provided": {"type": "object", "properties": {f: {"type": "string"} for f in VERIFICATION_FIELDS}}}, "required": ["provided"]}},
    {"name": "get_loyalty_account", "description": "Tier, points balance and expiry, registered email of the VERIFIED member.",
     "schema": {"type": "object", "properties": {"membership_number": {"type": "string"}}}},
    {"name": "update_loyalty_profile", "description": "Profile change for the VERIFIED member. request_type: NAME_CORRECTION, UNSUBSCRIBE, DUPLICATE_MERGE (low risk) or DOB_CHANGE, EMAIL_CHANGE, MOBILE_CHANGE, ADDRESS_CHANGE, OTHER (high risk -> human approval).",
     "schema": {"type": "object", "properties": {"request_type": {"type": "string"}, "field": {"type": "string"}, "new_value": {"type": "string"}, "membership_number": {"type": "string"}}, "required": ["request_type", "field", "new_value"]}},
    {"name": "quote_cash_and_points", "description": "Cash & Points maths: 15 points = USD 1, minimum 500 points, members only.",
     "schema": {"type": "object", "properties": {"points": {"type": "integer"}, "usd_amount": {"type": "number"}}}},
]


TOOL_CAPABILITY = {
    "search_knowledge_base": "KB", "get_hotel_policy": "KB",
    "check_availability": "RSV_READ", "find_reservation": "RSV_READ", "update_reservation": "RSV_READ",
    "create_reservation": "RSV_WRITE", "cancel_reservation": "RSV_WRITE",
    "verify_loyalty_member": "LOYALTY", "get_loyalty_account": "LOYALTY", "update_loyalty_profile": "LOYALTY", "quote_cash_and_points": "LOYALTY",
}


def enabled(name):
    from common import ENABLED_CAPABILITIES
    return TOOL_CAPABILITY.get(name, "KB") in ENABLED_CAPABILITIES


def enabled_specs():
    return [s for s in SPECS if enabled(s["name"])]


def run(name, ctx, args):
    from common import ENABLED_CAPABILITIES
    fn = REGISTRY.get(name)
    if not fn:
        return {"error": f"Unknown tool {name}"}
    if not enabled(name):
        return {"error": f"{name} is not enabled in this PoC phase - mark the item ESCALATED for a human agent."}
    args = dict(args or {})
    if name == "update_reservation" and "RSV_WRITE" not in ENABLED_CAPABILITIES and any(args.get(k) for k in ("new_check_in", "new_check_out", "new_room_type")):
        return {"error": "Date / room-type amendments are not enabled in this PoC phase (remarks and special requests are). Mark the amendment ESCALATED."}
    if name == "create_reservation" and args.get("points_to_redeem") and "LOYALTY" not in ENABLED_CAPABILITIES:
        return {"error": "Cash & Points redemption is not enabled in this PoC phase. Mark it ESCALATED."}
    try:
        return fn(ctx, **(args or {}))
    except ToolError as exc:
        return {"error": str(exc)}
    except TypeError as exc:
        return {"error": f"Bad arguments for {name}: {exc}"}
