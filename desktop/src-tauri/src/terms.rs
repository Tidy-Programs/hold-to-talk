//! Hold to talk's Terms of Use.
//!
//! The text is the repository's own `TERMS.md`, built into the app, so it
//! cannot go missing. Which version a person accepted is kept in one small
//! file, `terms-accepted.json`, in the app's data folder, in the same format
//! the Python app writes: `{"version", "accepted_at" (UTC), "how"}`. It holds
//! nothing personal and is never sent anywhere.
//!
//! Until the current version is accepted, nothing records: the shortcuts are
//! not registered (`commands::initialize_shortcuts`), and every way to start
//! a recording (hotkey, `--toggle-transcription`, Unix signals) passes through
//! `TranscribeAction::start`, which checks `is_accepted` first. A record for
//! another version, or one whose `how` is "unattended", counts as no record:
//! only a person choosing "I agree", in the installer or in the app, accepts.

use std::io;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use serde::{Deserialize, Serialize};
use specta::Type;
use tauri::AppHandle;

/// The version of the terms this build asks for. Must match the first line
/// of TERMS.md (a test checks it).
pub const TERMS_VERSION: &str = "1.0";

/// The terms, from the repository root.
pub const TERMS_TEXT: &str = include_str!("../../../TERMS.md");

const RECORD_NAME: &str = "terms-accepted.json";
const KNOWN_HOWS: [&str; 3] = ["installer", "app", "unattended"];
const ACCEPTING_HOWS: [&str; 2] = ["installer", "app"];
const TIME_FORMAT: &str = "%Y-%m-%dT%H:%M:%SZ";

/// Set once the current version is known to be accepted, so the hot path
/// (each press of the hotkey) does not read the file again.
static ACCEPTED: AtomicBool = AtomicBool::new(false);

/// The record of an acceptance, as stored in `terms-accepted.json`.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Type)]
pub struct TermsRecord {
    pub version: String,
    pub accepted_at: String,
    pub how: String,
}

/// The terms as the window shows them.
#[derive(Clone, Debug, Serialize, Type)]
pub struct TermsText {
    /// The version this build asks for, sent back by "I agree".
    pub version: String,
    /// The first line of TERMS.md, e.g. "Version 1.0 — 18 November 2026".
    pub version_line: String,
    /// The whole of TERMS.md.
    pub text: String,
}

/// Whether the current terms are accepted, and the record if there is one.
#[derive(Clone, Debug, Serialize, Type)]
pub struct TermsStatus {
    pub accepted: bool,
    pub record: Option<TermsRecord>,
}

fn record_path(dir: &Path) -> PathBuf {
    dir.join(RECORD_NAME)
}

/// The first line of the terms, without a byte-order mark.
pub fn version_line(text: &str) -> &str {
    text.trim_start_matches('\u{feff}')
        .lines()
        .next()
        .unwrap_or("")
        .trim()
}

/// The version named on the first line ("Version 1.0 — ..."), if any.
pub fn version_in(text: &str) -> Option<&str> {
    let rest = version_line(text).strip_prefix("Version ")?;
    let version = rest.split_whitespace().next()?;
    Some(version)
}

/// The record in `dir`, or None when there is none or it cannot be understood
/// (an unreadable record counts as no record).
pub fn read_record_in(dir: &Path) -> Option<TermsRecord> {
    let raw = std::fs::read_to_string(record_path(dir)).ok()?;
    let value: serde_json::Value = serde_json::from_str(raw.trim_start_matches('\u{feff}')).ok()?;
    let field = |name: &str| {
        value
            .get(name)
            .and_then(|v| v.as_str())
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(str::to_string)
    };
    let record = TermsRecord {
        version: field("version")?,
        accepted_at: field("accepted_at")?,
        how: field("how")?,
    };
    KNOWN_HOWS.contains(&record.how.as_str()).then_some(record)
}

/// True when `record` is a person accepting this build's version.
fn accepts_current(record: &TermsRecord) -> bool {
    record.version == TERMS_VERSION && ACCEPTING_HOWS.contains(&record.how.as_str())
}

/// True when `dir` holds a record of a person accepting this version.
pub fn is_accepted_in(dir: &Path) -> bool {
    read_record_in(dir).is_some_and(|r| accepts_current(&r))
}

/// Record, in `dir`, that this version was accepted at `now` (UTC). The file
/// is written whole and then moved into place, so it is never half-written.
pub fn accept_in(
    dir: &Path,
    how: &str,
    now: chrono::DateTime<chrono::Utc>,
) -> io::Result<TermsRecord> {
    if !ACCEPTING_HOWS.contains(&how) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "unknown way of accepting",
        ));
    }
    let record = TermsRecord {
        version: TERMS_VERSION.to_string(),
        accepted_at: now.format(TIME_FORMAT).to_string(),
        how: how.to_string(),
    };
    std::fs::create_dir_all(dir)?;
    let json = serde_json::to_string_pretty(&record).map_err(io::Error::other)? + "\n";
    let tmp = dir.join(format!(".terms-{}.tmp", std::process::id()));
    std::fs::write(&tmp, json)?;
    let target = record_path(dir);
    let mut attempt = 0;
    loop {
        match std::fs::rename(&tmp, &target) {
            Ok(()) => break,
            // A virus scanner may hold the file for a moment.
            Err(e) if e.kind() == io::ErrorKind::PermissionDenied && attempt < 19 => {
                attempt += 1;
                std::thread::sleep(Duration::from_millis(50));
            }
            Err(e) => {
                let _ = std::fs::remove_file(&tmp);
                return Err(e);
            }
        }
    }
    Ok(record)
}

fn data_dir(app: &AppHandle) -> Option<PathBuf> {
    crate::portable::app_data_dir(app)
        .map_err(|e| log::warn!("Terms of Use: no data folder: {}", e))
        .ok()
}

/// True when the current terms are accepted. Reads the record until it is,
/// then remembers (an installer may write the record while the app runs).
pub fn is_accepted(app: &AppHandle) -> bool {
    if ACCEPTED.load(Ordering::Relaxed) {
        return true;
    }
    let accepted = data_dir(app).is_some_and(|dir| is_accepted_in(&dir));
    if accepted {
        ACCEPTED.store(true, Ordering::Relaxed);
    }
    accepted
}

/// The terms, for the Terms of Use screen.
#[tauri::command]
#[specta::specta]
pub fn get_terms() -> TermsText {
    TermsText {
        version: TERMS_VERSION.to_string(),
        version_line: version_line(TERMS_TEXT).to_string(),
        text: TERMS_TEXT.to_string(),
    }
}

/// Whether the current terms are accepted, and the stored record.
#[tauri::command]
#[specta::specta]
pub fn get_terms_status(app: AppHandle) -> TermsStatus {
    let record = data_dir(&app).and_then(|dir| read_record_in(&dir));
    let accepted = record.as_ref().is_some_and(accepts_current);
    if accepted {
        ACCEPTED.store(true, Ordering::Relaxed);
    }
    TermsStatus { accepted, record }
}

/// "I agree": record the acceptance of `version`, which must be the version
/// the screen showed. Dictation can start as soon as this returns.
#[tauri::command]
#[specta::specta]
pub fn accept_terms(app: AppHandle, version: String) -> Result<TermsRecord, String> {
    if version != TERMS_VERSION {
        return Err("These are not the current Terms of Use. Close and open the app again.".into());
    }
    let dir = data_dir(&app).ok_or("The app's data folder could not be found.")?;
    let record = accept_in(&dir, "app", chrono::Utc::now())
        .map_err(|e| format!("Your agreement could not be saved: {}", e))?;
    ACCEPTED.store(true, Ordering::Relaxed);
    log::info!("Terms of Use version {} accepted (app)", record.version);
    Ok(record)
}

/// "Quit" on the Terms of Use screen.
#[tauri::command]
#[specta::specta]
pub fn quit_app(app: AppHandle) {
    app.exit(0);
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    fn write(dir: &Path, body: &str) {
        std::fs::write(record_path(dir), body).unwrap();
    }

    #[test]
    fn the_built_in_text_names_this_version() {
        assert_eq!(version_in(TERMS_TEXT), Some(TERMS_VERSION));
        assert!(version_line(TERMS_TEXT).starts_with("Version "));
        assert!(TERMS_TEXT.contains("## Summary"));
    }

    #[test]
    fn no_record_is_not_accepted() {
        let dir = tempfile::tempdir().unwrap();
        assert!(read_record_in(dir.path()).is_none());
        assert!(!is_accepted_in(dir.path()));
    }

    #[test]
    fn a_broken_record_is_not_accepted() {
        let dir = tempfile::tempdir().unwrap();
        for body in [
            "",
            "not json",
            "[]",
            r#"{"version": "1.0"}"#,
            r#"{"version": "1.0", "accepted_at": "", "how": "app"}"#,
            r#"{"version": 1.0, "accepted_at": "2026-11-23T10:00:00Z", "how": "app"}"#,
            r#"{"version": "1.0", "accepted_at": "2026-11-23T10:00:00Z", "how": "someone"}"#,
        ] {
            write(dir.path(), body);
            assert!(!is_accepted_in(dir.path()), "accepted: {body}");
        }
    }

    #[test]
    fn another_version_is_not_accepted() {
        let dir = tempfile::tempdir().unwrap();
        write(
            dir.path(),
            r#"{"version": "0.9", "accepted_at": "2026-11-23T10:00:00Z", "how": "app"}"#,
        );
        assert!(read_record_in(dir.path()).is_some());
        assert!(!is_accepted_in(dir.path()));
    }

    #[test]
    fn unattended_is_understood_but_not_accepted() {
        let dir = tempfile::tempdir().unwrap();
        write(
            dir.path(),
            r#"{"version": "1.0", "accepted_at": "2026-11-23T10:00:00Z", "how": "unattended"}"#,
        );
        assert_eq!(read_record_in(dir.path()).unwrap().how, "unattended");
        assert!(!is_accepted_in(dir.path()));
    }

    #[test]
    fn the_installers_record_is_accepted_with_a_byte_order_mark() {
        let dir = tempfile::tempdir().unwrap();
        write(
            dir.path(),
            "\u{feff}{\"version\": \"1.0\", \"accepted_at\": \"2026-11-23T10:00:00Z\", \"how\": \"installer\"}",
        );
        assert!(is_accepted_in(dir.path()));
    }

    #[test]
    fn accepting_writes_the_record_in_utc() {
        let dir = tempfile::tempdir().unwrap();
        let sub = dir.path().join("not-yet-made");
        let now = chrono::Utc.with_ymd_and_hms(2026, 11, 23, 9, 5, 7).unwrap();
        let record = accept_in(&sub, "app", now).unwrap();
        assert_eq!(
            record,
            TermsRecord {
                version: TERMS_VERSION.into(),
                accepted_at: "2026-11-23T09:05:07Z".into(),
                how: "app".into(),
            }
        );
        assert_eq!(read_record_in(&sub), Some(record));
        assert!(is_accepted_in(&sub));
        let leftovers: Vec<_> = std::fs::read_dir(&sub)
            .unwrap()
            .filter_map(|e| e.ok())
            .filter(|e| e.file_name().to_string_lossy().ends_with(".tmp"))
            .collect();
        assert!(leftovers.is_empty());
    }

    #[test]
    fn accepting_replaces_an_unattended_record() {
        let dir = tempfile::tempdir().unwrap();
        write(
            dir.path(),
            r#"{"version": "1.0", "accepted_at": "2026-11-23T10:00:00Z", "how": "unattended"}"#,
        );
        accept_in(dir.path(), "app", chrono::Utc::now()).unwrap();
        assert!(is_accepted_in(dir.path()));
    }

    #[test]
    fn only_a_person_can_accept() {
        let dir = tempfile::tempdir().unwrap();
        assert!(accept_in(dir.path(), "unattended", chrono::Utc::now()).is_err());
        assert!(accept_in(dir.path(), "", chrono::Utc::now()).is_err());
        assert!(!is_accepted_in(dir.path()));
    }
}
