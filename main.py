"""
Apple iCloud CalDAV proxy for the n8n Calendar Tool.

Why this exists: n8n's built-in HTTP Request node (n8n Cloud and self-hosted alike,
as of the version tested on 2026-07-31) gets a stripped/empty CalDAV response from
Apple's servers even with valid credentials and correctly-formed requests — a plain
`curl` with the exact same headers/body works fine. This looks like server-side
filtering at a level n8n's HTTP Request node can't control (TLS fingerprint / client
stack), not a credentials or account problem. The `caldav` Python library (built on
`requests`) is confirmed working against iCloud by many real-world users, so this
tiny service does the actual CalDAV talking and exposes a plain JSON HTTP API that
n8n calls instead.

Endpoints (all POST except /health), all require header:  X-Proxy-Token: <PROXY_TOKEN>

  GET  /health
  POST /events/list    {date_from, date_to}                    -> {events: [...]}
  POST /events/create  {title, description?, start, end}       -> {id, title}
  POST /events/update  {id, new_start, new_end}                -> {ok: true}
  POST /events/delete  {id}                                    -> {ok: true}

date/time fields are ISO 8601 strings (e.g. "2026-08-01T14:00:00+03:00").
`id` is the event's iCalendar UID.

NOT YET LIVE-TESTED against a real iCloud account — this session never had access to
the actual Apple ID / app-specific password (by design, credentials never leave n8n).
Test against the real account as soon as this is deployed; see README.md.
"""

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import caldav
import icalendar
from caldav.lib.error import NotFoundError
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

# Bumped on every code change and echoed back by /health, purely so a redeploy can
# be confirmed to have actually picked up new code (Render's dashboard has caused
# real confusion about whether "Deploy latest commit" used the intended commit).
PROXY_VERSION = "2026-07-31-update-etag-reload"

APPLE_ID = os.environ["APPLE_ID"]
APPLE_APP_PASSWORD = os.environ["APPLE_APP_PASSWORD"]
CALENDAR_NAME = os.environ.get("APPLE_CALENDAR_NAME")  # optional; defaults to first calendar
PROXY_TOKEN = os.environ["PROXY_TOKEN"]
CALDAV_URL = os.environ.get("APPLE_CALDAV_URL", "https://caldav.icloud.com/")

app = FastAPI(title="Apple CalDAV Proxy")

_calendar_url: Optional[str] = None


def _new_client() -> caldav.DAVClient:
    # timeout=60: iCloud's CalDAV REPORT (date-search) queries are occasionally slow
    # to respond (observed hangs past the caldav library's 30s default), especially
    # right after a fresh connection. rate_limit_handle lets the library back off and
    # retry automatically if Apple responds with a rate-limit status instead of just
    # hanging.
    return caldav.DAVClient(
        url=CALDAV_URL,
        username=APPLE_ID,
        password=APPLE_APP_PASSWORD,
        timeout=60,
        rate_limit_handle=True,
    )


def get_calendar() -> caldav.Calendar:
    """Return a Calendar handle backed by a brand-new connection every call.

    Deliberately does NOT reuse a cached DAVClient/session across requests.
    Observed in production: a single long-lived DAVClient's pooled connection
    to caldav.icloud.com works for one request, then silently hangs forever
    (no error, no response, no TCP reset) on every request after — the classic
    stale-keepalive-connection failure mode. iCloud CalDAV apparently doesn't
    play well with connection reuse from this kind of always-on client, and
    the cost of a fresh TLS handshake per request (roughly 1-2s) is a fair
    trade for never getting stuck on a dead connection. Only the discovered
    calendar's URL is cached, to skip the extra principal()/calendars()
    discovery round-trip on every call.
    """
    global _calendar_url
    client = _new_client()
    if _calendar_url is not None:
        return caldav.Calendar(client=client, url=_calendar_url)
    principal = client.principal()
    calendars = principal.calendars()
    if not calendars:
        raise RuntimeError("No calendars found for this Apple ID")
    if CALENDAR_NAME:
        matches = [c for c in calendars if (c.name or "").lower() == CALENDAR_NAME.lower()]
        if not matches:
            available = ", ".join(c.name or "(unnamed)" for c in calendars)
            raise RuntimeError(f'Calendar "{CALENDAR_NAME}" not found. Available: {available}')
        chosen = matches[0]
    else:
        chosen = calendars[0]
    _calendar_url = str(chosen.url)
    return chosen


def check_token(x_proxy_token: str = Header(default="")):
    if x_proxy_token != PROXY_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Proxy-Token header")


class ListRequest(BaseModel):
    date_from: str
    date_to: str


class CreateRequest(BaseModel):
    title: str
    description: Optional[str] = ""
    start: str
    end: str


class UpdateRequest(BaseModel):
    id: str
    new_start: str
    new_end: str


class DeleteRequest(BaseModel):
    id: str


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _event_to_dict(event) -> dict:
    comp = event.icalendar_component
    uid = str(comp.get("uid", ""))
    summary = str(comp.get("summary", ""))
    description = str(comp.get("description", "") or "")
    dtstart = comp.get("dtstart")
    dtend = comp.get("dtend")
    return {
        "id": uid,
        "title": summary,
        "description": description,
        "start": dtstart.dt.isoformat() if dtstart else None,
        "end": dtend.dt.isoformat() if dtend else None,
    }


def _find_by_uid(calendar: caldav.Calendar, event_id: str):
    try:
        return calendar.event_by_uid(event_id)
    except NotFoundError:
        return None


def _to_utc(dt: datetime) -> datetime:
    """Normalize any offset-aware datetime to UTC before writing it into an ICS file.

    Why: icalendar only knows how to write an unambiguous, named timezone for
    tzinfo objects it recognizes (zoneinfo/pytz Olson zones). A plain fixed-offset
    tzinfo (what datetime.fromisoformat("...+03:00") produces) has no zone name,
    so icalendar silently drops the offset and writes a "floating" (timezone-less)
    DTSTART/DTEND — Apple then stores the raw wall-clock numbers with no timezone
    at all, which is why an event created for 15:00 MSK showed up as 18:00 when
    read back and displayed in Moscow time. Converting to UTC first (and always
    writing UTC, which icalendar encodes correctly as a trailing "Z") avoids this
    entirely — every downstream reader converts a "Z" instant correctly regardless
    of its own timezone.
    """
    if dt.tzinfo is None:
        # No offset given at all — assume the caller meant Europe/Moscow (the only
        # timezone this assistant is used from) rather than silently guessing UTC.
        dt = dt.replace(tzinfo=timezone(timedelta(hours=3)))
    return dt.astimezone(timezone.utc)


def _build_ics(uid: str, summary: str, description: str, dtstart: datetime, dtend: datetime) -> str:
    """Build a minimal VEVENT by hand instead of relying on caldav's kwargs-based
    save_event() shortcut — that shortcut's exact accepted parameter set has changed
    across caldav library versions, so building the iCalendar text explicitly (via the
    `icalendar` package, which has a stable well-documented API) is more predictable."""
    cal = icalendar.Calendar()
    cal.add("prodid", "-//apple-caldav-proxy//n8n//")
    cal.add("version", "2.0")
    event = icalendar.Event()
    event.add("uid", uid)
    event.add("summary", summary)
    if description:
        event.add("description", description)
    event.add("dtstart", _to_utc(dtstart))
    event.add("dtend", _to_utc(dtend))
    event.add("dtstamp", datetime.now(timezone.utc))
    cal.add_component(event)
    return cal.to_ical().decode("utf-8")


@app.get("/health")
def health():
    try:
        cal = get_calendar()
        return {"ok": True, "calendar": cal.name, "version": PROXY_VERSION}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/events/list")
def list_events(req: ListRequest, x_proxy_token: str = Header(default="")):
    check_token(x_proxy_token)
    try:
        cal = get_calendar()
        start = _parse_dt(req.date_from)
        end = _parse_dt(req.date_to)
        events = cal.date_search(start=start, end=end, expand=True)
        return {"events": [_event_to_dict(e) for e in events]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


@app.post("/events/create")
def create_event(req: CreateRequest, x_proxy_token: str = Header(default="")):
    check_token(x_proxy_token)
    try:
        cal = get_calendar()
        uid = str(uuid.uuid4())
        start = _parse_dt(req.start)
        end = _parse_dt(req.end)
        ics_text = _build_ics(uid, req.title, req.description or "", start, end)
        cal.save_event(ics_text)
        return {"id": uid, "title": req.title}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


@app.post("/events/update")
def update_event(req: UpdateRequest, x_proxy_token: str = Header(default="")):
    check_token(x_proxy_token)
    try:
        cal = get_calendar()
        event = _find_by_uid(cal, req.id)
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found")
        # _find_by_uid resolves the event via a REPORT/search query, not a plain
        # GET — the ETag it captures from that XML multistatus response is
        # apparently not always in the exact form iCloud expects back on a PUT's
        # If-Match header (we confirmed a totally unconditional PUT, with no
        # If-Match at all, still gets rejected with 412 — so Apple requires a
        # matching ETag, and what we had wasn't it). Forcing an explicit reload
        # (a plain GET) right before editing gets a freshly, correctly captured
        # ETag straight from the HTTP response header.
        event.load()
        comp = event.icalendar_component
        comp["dtstart"].dt = _to_utc(_parse_dt(req.new_start))
        comp["dtend"].dt = _to_utc(_parse_dt(req.new_end))
        event.save()
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


@app.post("/events/delete")
def delete_event(req: DeleteRequest, x_proxy_token: str = Header(default="")):
    check_token(x_proxy_token)
    try:
        cal = get_calendar()
        event = _find_by_uid(cal, req.id)
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found")
        event.delete()
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")
