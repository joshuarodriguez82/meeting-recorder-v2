//! macOS Calendar bridge.
//!
//! This module is the "right fix" for the EventKit / TCC subprocess
//! attribution bug. Background, since this trips everyone:
//!
//! Python lives at `~/Library/Application Support/MeetingRecorder/
//! .venv/bin/python3` — *outside* the .app bundle. macOS 14+ TCC keys
//! permission grants by the calling process's code-signing identity,
//! not by the parent process. So when Python calls EventKit from
//! pyobjc, TCC doesn't attribute the call to
//! `com.joshuarodriguez.meeting-recorder` (the .app's bundle ID); it
//! sees Python as a separate identity with no usage description, no
//! grant, no decision recorded. Result: `auth_status=0` (NotDetermined)
//! and zero events, even after the user has explicitly toggled
//! "Full" on for Meeting Recorder in System Settings.
//!
//! The Rust binary at `Meeting Recorder.app/Contents/MacOS/
//! meeting-recorder` *is* part of the bundle. EventKit calls from
//! here attribute correctly to `com.joshuarodriguez.meeting-recorder`
//! and the existing grant applies.
//!
//! The bridge is one-way: Rust pulls events on a timer and writes
//! them to a JSON sidecar that Python reads in
//! `_calendar_eventkit.py::_load_from_rust_cache()`. Python keeps
//! exposing `/calendar/upcoming` and the rest of the API surface
//! unchanged; only the data source moves.
//!
//! Threading: every EventKit call holds onto an `EKEventStore` whose
//! methods must be invoked from the same retain pool. We do all the
//! work inside one Tokio-free `std::thread` running an autoreleasepool
//! per iteration.

#![cfg(target_os = "macos")]

use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use block2::RcBlock;
use objc2::rc::Retained;
use objc2::runtime::Bool;
use objc2_event_kit::{
    EKAuthorizationStatus, EKEntityType, EKEvent, EKEventStore,
};
use objc2_foundation::{NSArray, NSDate, NSError};
use serde::Serialize;

const CACHE_FILENAME: &str = "calendar_cache.json";
const POLL_INTERVAL_SECS: u64 = 300; // 5 minutes
const FETCH_HOURS: i64 = 168; // 7 days, matches Python default

/// One serialized meeting. Field names match what
/// `_calendar_eventkit.py::_serialize_event()` returns so Python
/// doesn't need a translation layer.
#[derive(Serialize, Clone, Debug)]
pub struct CalendarEvent {
    pub subject: String,
    pub start: String,    // ISO-8601 local datetime, naive (no tz suffix)
    pub end: String,
    pub location: String,
    pub organizer: String,
    pub attendees: Vec<String>,
    pub duration: i64, // minutes
}

/// What we serialize to the JSON sidecar. The wrapper carries a
/// `generated_at` timestamp so Python can detect a stale cache and
/// flag it in the UI if needed.
#[derive(Serialize)]
struct CalendarCache {
    generated_at: String,
    auth_status: i64,
    fetch_horizon_hours: i64,
    events: Vec<CalendarEvent>,
}

/// Resolve the cache file path. Mirrors backend/config/settings.py's
/// `_user_data_dir()` for macOS.
fn cache_path() -> PathBuf {
    let mut p = dirs::home_dir().expect("no home dir");
    p.push("Library");
    p.push("Application Support");
    p.push("MeetingRecorder");
    p.push(CACHE_FILENAME);
    p
}

/// Public entrypoint: spawn the calendar polling thread. Called from
/// `lib.rs::run()` at app startup. No-op on platforms other than
/// macOS (the whole module is cfg-gated).
pub fn spawn_polling_thread() {
    thread::spawn(|| polling_loop());
}

fn polling_loop() {
    // Block briefly so the rest of app startup logs first; not
    // required, just keeps the launch trace readable.
    thread::sleep(Duration::from_secs(1));

    let store = EKEventStore::new();
    let store = Arc::new(store);

    // Fire the permission prompt. macOS shows it against the
    // .app bundle (which has the NSCalendarsFullAccessUsageDescription
    // string), the user clicks Allow, the grant is recorded for
    // com.joshuarodriguez.meeting-recorder, and every subsequent
    // EventKit call from this binary picks it up.
    if !request_access_blocking(&store) {
        log::warn!(
            "calendar_macos: full-access request returned false; \
             writing empty cache and giving up. User can re-enable \
             in System Settings → Privacy & Security → Calendars."
        );
        // EKAuthorizationStatus's inner `.0` is isize on this binding;
        // cast to i64 to match what we serialize into the JSON cache.
        write_empty_cache(EKAuthorizationStatus::Denied.0 as i64);
        return;
    }

    log::info!("calendar_macos: full access granted; polling every {}s",
               POLL_INTERVAL_SECS);

    // Steady-state poll loop. Each iteration reads events for the
    // next FETCH_HOURS, writes the cache, sleeps. If the user
    // revokes access mid-session, calendarsForEntityType will start
    // returning empty and we'll write empty caches — Python falls
    // back to whatever it had cached too.
    loop {
        let events = fetch_events(&store, FETCH_HOURS);
        let auth = unsafe {
            EKEventStore::authorizationStatusForEntityType(EKEntityType::Event)
        }.0 as i64;
        write_cache(events, auth);
        thread::sleep(Duration::from_secs(POLL_INTERVAL_SECS));
    }
}

/// Synchronous wrapper around `requestFullAccessToEventsWithCompletion:`.
/// EventKit's API is async-callback; we bridge it to a blocking call
/// using a Mutex<Option<bool>> the block fills in.
fn request_access_blocking(store: &EKEventStore) -> bool {
    let result: Arc<Mutex<Option<bool>>> = Arc::new(Mutex::new(None));
    let signal = Arc::new((std::sync::Condvar::new(), Mutex::new(false)));

    let result_cb = result.clone();
    let signal_cb = signal.clone();
    let block = RcBlock::new(move |granted: Bool, error: *mut NSError| {
        if !error.is_null() {
            // Best-effort: log the error description.
            let err: &NSError = unsafe { &*error };
            log::warn!(
                "calendar_macos: requestFullAccess error: {}",
                err.localizedDescription());
        }
        *result_cb.lock().unwrap() = Some(granted.as_bool());
        let (cv, m) = &*signal_cb;
        *m.lock().unwrap() = true;
        cv.notify_one();
    });

    unsafe {
        // The binding wants `*mut Block<...>` here; an `&RcBlock` won't
        // coerce, but `&*block` derefs to `&Block<...>` which Rust will
        // auto-coerce to the raw mut pointer the FFI expects.
        store.requestFullAccessToEventsWithCompletion(&*block);
    }

    // Wait up to 60s for the user to click Allow / Don't Allow.
    let (cv, m) = &*signal;
    let mut done = m.lock().unwrap();
    let timeout = Duration::from_secs(60);
    let (done_after, wait_result) = cv.wait_timeout(done, timeout).unwrap();
    done = done_after;
    if wait_result.timed_out() {
        log::warn!("calendar_macos: permission dialog timed out");
        return false;
    }
    drop(done);
    let r = result.lock().unwrap();
    r.unwrap_or(false)
}

/// Read events in `[now, now + hours)`. Returns an empty Vec on any
/// EventKit failure; the auth_status check in the polling loop
/// distinguishes "no events" from "lost access".
fn fetch_events(store: &EKEventStore, hours: i64) -> Vec<CalendarEvent> {
    use chrono::Local;

    let now = Local::now();
    let end = now + chrono::Duration::hours(hours);

    let ns_start = NSDate::dateWithTimeIntervalSince1970(
        now.timestamp() as f64);
    let ns_end = NSDate::dateWithTimeIntervalSince1970(
        end.timestamp() as f64);

    // Pass nil for calendars → "all calendars the app can see". On
    // macOS 14+ this also picks up subscribed/iCloud/Exchange
    // calendars the user has enabled in Calendar.app.
    let predicate = unsafe {
        store.predicateForEventsWithStartDate_endDate_calendars(
            &ns_start, &ns_end, None,
        )
    };
    let events: Retained<NSArray<EKEvent>> =
        unsafe { store.eventsMatchingPredicate(&predicate) };

    let mut out: Vec<CalendarEvent> = Vec::with_capacity(events.len());
    for ek in events.iter() {
        if let Some(serialized) = serialize(&ek) {
            out.push(serialized);
        }
    }
    out.sort_by(|a, b| a.start.cmp(&b.start));
    out
}

fn serialize(ek: &EKEvent) -> Option<CalendarEvent> {
    use chrono::{DateTime, Local, TimeZone};

    // objc2-event-kit 0.3 returns these as Retained<…> directly rather
    // than Option<Retained<…>>, so we use them without ? / .map(). If
    // the runtime ever returns nil here, Retained's Deref will trip —
    // hasn't been observed in practice for these methods.
    let title = unsafe { ek.title() }.to_string();
    let start_ns = unsafe { ek.startDate() };
    let end_ns = unsafe { ek.endDate() };
    let start_ts = unsafe { start_ns.timeIntervalSince1970() };
    let end_ts = unsafe { end_ns.timeIntervalSince1970() };

    let start_dt: DateTime<Local> = Local
        .timestamp_opt(start_ts as i64, 0)
        .single()?;
    let end_dt: DateTime<Local> = Local
        .timestamp_opt(end_ts as i64, 0)
        .single()?;
    // Format without tz suffix to match Python's naive-local format.
    let start = start_dt.format("%Y-%m-%dT%H:%M:%S").to_string();
    let end = end_dt.format("%Y-%m-%dT%H:%M:%S").to_string();
    let duration = ((end_ts - start_ts) / 60.0).max(1.0) as i64;

    // location/organizer ARE optional in this binding (Apple docs say
    // both can be nil for events without a location or implicit
    // organizer).
    let location = unsafe { ek.location() }
        .map(|s| s.to_string())
        .unwrap_or_default();
    let organizer = unsafe { ek.organizer() }
        .map(|p| unsafe { p.name() }
            .map(|s| s.to_string())
            .unwrap_or_default())
        .unwrap_or_default();

    let attendees: Vec<String> = unsafe { ek.attendees() }
        .map(|arr| {
            arr.iter()
                .filter_map(|p| {
                    // p.URL() is Retained<NSURL> non-optional in this
                    // binding. p.name() is Option. Prefer email
                    // (mailto:foo@bar) — Python does the same so
                    // client-matching code keys on the same value
                    // across platforms.
                    let url = unsafe { p.URL() };
                    let raw = unsafe { url.absoluteString() }.to_string();
                    if let Some(rest) = raw.strip_prefix("mailto:")
                        .or_else(|| raw.strip_prefix("MAILTO:")) {
                        return Some(rest.to_string());
                    }
                    unsafe { p.name() }.map(|s| s.to_string())
                })
                .collect()
        })
        .unwrap_or_default();

    let title = if title.is_empty() {
        "Untitled Meeting".to_string()
    } else {
        title
    };

    Some(CalendarEvent {
        subject: title,
        start,
        end,
        location,
        organizer,
        attendees,
        duration,
    })
}

fn write_cache(events: Vec<CalendarEvent>, auth_status: i64) {
    use chrono::Local;
    let cache = CalendarCache {
        generated_at: Local::now().format("%Y-%m-%dT%H:%M:%S").to_string(),
        auth_status,
        fetch_horizon_hours: FETCH_HOURS,
        events,
    };
    let path = cache_path();
    if let Some(parent) = path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let tmp = path.with_extension("json.tmp");
    match serde_json::to_string_pretty(&cache) {
        Ok(text) => {
            if let Err(e) = std::fs::write(&tmp, text) {
                log::warn!("calendar_macos: write tmp failed: {e}");
                return;
            }
            if let Err(e) = std::fs::rename(&tmp, &path) {
                log::warn!("calendar_macos: rename failed: {e}");
                let _ = std::fs::remove_file(&tmp);
                return;
            }
            log::info!(
                "calendar_macos: wrote {} events to {}",
                cache.events.len(),
                path.display());
        }
        Err(e) => log::warn!("calendar_macos: serialize failed: {e}"),
    }
}

fn write_empty_cache(auth_status: i64) {
    write_cache(Vec::new(), auth_status);
}
