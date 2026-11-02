//! Window geometry is a disposable profile preference, never application data.
use serde::{Deserialize, Serialize};
use std::{
    fs::{self, OpenOptions},
    io::Write,
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc, Arc,
    },
    time::Duration,
};
use tauri::{LogicalSize, PhysicalPosition, WebviewWindow, WindowEvent};
use uuid::Uuid;

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Geometry {
    version: u8,
    x: i32,
    y: i32,
    width: f64,
    height: f64,
}

#[derive(Clone, Copy)]
struct WorkArea {
    x: i32,
    y: i32,
    width: u32,
    height: u32,
    scale: f64,
}

impl Geometry {
    fn valid(&self) -> bool {
        self.version == 1
            && (-100_000..=100_000).contains(&self.x)
            && (-100_000..=100_000).contains(&self.y)
            && self.width.is_finite()
            && self.height.is_finite()
            && (840.0..=7680.0).contains(&self.width)
            && (600.0..=4320.0).contains(&self.height)
    }

    fn position_visible(&self, areas: &[WorkArea]) -> bool {
        areas.iter().any(|area| {
            let left = f64::from(area.x);
            let top = f64::from(area.y);
            let right = left + f64::from(area.width);
            let bottom = top + f64::from(area.height);
            let x = f64::from(self.x);
            let y = f64::from(self.y);
            // Keep a usable piece of the title bar in a work area. Size is
            // stored in logical pixels, position in desktop physical pixels.
            area.scale.is_finite()
                && area.scale > 0.0
                && x <= right - 100.0
                && x + self.width * area.scale >= left + 100.0
                && y >= top - 16.0
                && y <= bottom - 40.0
        })
    }

    fn fit(&self, area: WorkArea) -> Self {
        let mut value = self.clone();
        // Keep room for native borders and the title bar. The minimum window
        // size still applies if the display is smaller than that minimum.
        value.width = value
            .width
            .min(((f64::from(area.width) - 32.0) / area.scale).max(840.0));
        value.height = value
            .height
            .min(((f64::from(area.height) - 64.0) / area.scale).max(600.0));
        value
    }
}

fn load(path: &Path) -> Option<Geometry> {
    let metadata = fs::symlink_metadata(path).ok()?;
    if !metadata.is_file() || metadata.len() > 8192 {
        return None;
    }
    let value: Geometry = serde_json::from_slice(&fs::read(path).ok()?).ok()?;
    value.valid().then_some(value)
}

fn save(path: &Path, value: &Geometry) -> std::io::Result<()> {
    if !value.valid() || fs::symlink_metadata(path).is_ok_and(|info| info.file_type().is_symlink())
    {
        return Err(std::io::Error::other("Invalid window preference"));
    }
    let temporary = path.with_file_name(format!("window-state-{}.tmp", Uuid::new_v4().simple()));
    let result = (|| {
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temporary)?;
        serde_json::to_writer(&mut file, value)?;
        file.flush()?;
        file.sync_all()?;
        fs::rename(&temporary, path)
    })();
    if result.is_err() {
        let _ = fs::remove_file(temporary);
    }
    result
}

pub fn install(window: WebviewWindow, root: PathBuf) {
    let path = root.join("window-state.json");
    let (sender, receiver) = mpsc::channel::<Geometry>();
    let loaded = Arc::new(AtomicBool::new(false));
    let writer_path = path.clone();
    std::thread::spawn(move || {
        while let Ok(mut value) = receiver.recv() {
            // Coalesce drag/resize events before touching disk.
            while let Ok(next) = receiver.recv_timeout(Duration::from_millis(300)) {
                value = next;
            }
            let _ = save(&writer_path, &value);
        }
    });
    let capture_window = window.clone();
    let capture_loaded = loaded.clone();
    window.on_window_event(move |event| {
        if !matches!(event, WindowEvent::Moved(_) | WindowEvent::Resized(_))
            || !capture_loaded.load(Ordering::Acquire)
            || capture_window.is_minimized().unwrap_or(true)
            || capture_window.is_maximized().unwrap_or(true)
        {
            return;
        }
        let (Ok(position), Ok(size), Ok(scale)) = (
            capture_window.outer_position(),
            capture_window.inner_size(),
            capture_window.scale_factor(),
        ) else {
            return;
        };
        if !scale.is_finite() || scale <= 0.0 {
            return;
        }
        let geometry = Geometry {
            version: 1,
            x: position.x,
            y: position.y,
            width: f64::from(size.width) / scale,
            height: f64::from(size.height) / scale,
        };
        if geometry.valid() {
            let _ = sender.send(geometry);
        }
    });
    std::thread::spawn(move || {
        if let Some(value) = load(&path) {
            let work_area = |monitor: &tauri::Monitor| {
                let area = monitor.work_area();
                WorkArea {
                    x: area.position.x,
                    y: area.position.y,
                    width: area.size.width,
                    height: area.size.height,
                    scale: monitor.scale_factor(),
                }
            };
            let areas: Vec<_> = window
                .available_monitors()
                .unwrap_or_default()
                .iter()
                .map(work_area)
                .filter(|area| area.scale.is_finite() && area.scale > 0.0)
                .collect();
            let primary = window
                .primary_monitor()
                .ok()
                .flatten()
                .as_ref()
                .map(work_area)
                .filter(|area| area.scale.is_finite() && area.scale > 0.0);
            let selected = areas
                .iter()
                .copied()
                .find(|area| value.position_visible(&[*area]))
                .or(primary)
                .or_else(|| areas.first().copied());
            if let Some(area) = selected {
                let fitted = value.fit(area);
                let _ = window.set_size(LogicalSize::new(fitted.width, fitted.height));
                if fitted.position_visible(&[area]) {
                    let _ = window.set_position(PhysicalPosition::new(fitted.x, fitted.y));
                } else {
                    // The stored display may no longer be connected.
                    let _ = window.set_position(PhysicalPosition::new(area.x, area.y));
                }
            }
        }
        loaded.store(true, Ordering::Release);
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn disconnected_monitor_does_not_keep_title_bar_offscreen() {
        let area = WorkArea {
            x: 0,
            y: 0,
            width: 1920,
            height: 1040,
            scale: 1.0,
        };
        let mut state = Geometry {
            version: 1,
            x: -900,
            y: 40,
            width: 1200.0,
            height: 700.0,
        };
        assert!(state.valid() && state.position_visible(&[area]));
        state.x = -2000;
        assert!(!state.position_visible(&[area]));
        state.x = 0;
        state.y = 1100;
        assert!(!state.position_visible(&[area]));
        state.width = 7680.0;
        state.height = 4320.0;
        let fitted = state.fit(area);
        assert_eq!(fitted.width, 1888.0);
        assert_eq!(fitted.height, 976.0);
        state.width = f64::NAN;
        assert!(!state.valid());
    }
}
