use hmac::{Hmac, Mac};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::Sha256;
use std::{
    collections::HashMap,
    fs::File,
    io::{BufRead, BufReader, Read, Write},
    path::PathBuf,
    process::{ChildStdin, Command, Stdio},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex, OnceLock,
    },
    time::Duration,
};
use tauri::{Emitter, Manager};
use uuid::Uuid;

const MAX_JSON: usize = 4 * 1024 * 1024;
const MAX_FILE: u64 = 47 * 1024 * 1024;
const CONTRACT: &str = include_str!("../../../docs/openapi.json");
static PARSED_CONTRACT: OnceLock<Value> = OnceLock::new();

#[derive(Clone)]
struct Endpoint {
    origin: String,
    token: String,
}

enum Phase {
    Starting,
    Setup(Endpoint),
    Ready(Endpoint),
    Failed,
}

enum Shutdown {
    Idle,
    Stopping,
    Failed,
}

struct Session {
    client: reqwest::Client,
    csrf: String,
    generation: u64,
}

struct PickedFile {
    file: File,
    name: String,
    size: u64,
}

#[derive(Clone)]
pub struct Bridge {
    phase: Arc<Mutex<Phase>>,
    shutdown: Arc<Mutex<Shutdown>>,
    bootstrap_pipe: Arc<Mutex<Option<ChildStdin>>>,
    launch_settled: Arc<AtomicBool>,
    setup_lock: Arc<tokio::sync::Mutex<()>>,
    session: Arc<Mutex<Session>>,
    login_lock: Arc<tokio::sync::Mutex<()>>,
    files: Arc<Mutex<HashMap<String, PickedFile>>>,
    media: Arc<Mutex<HashMap<String, String>>>,
    pending: Arc<Mutex<HashMap<String, tokio::sync::oneshot::Sender<()>>>>,
}

#[derive(Serialize)]
pub struct Status {
    phase: &'static str,
    signed_in: bool,
}

#[derive(Serialize)]
pub struct ShutdownStatus {
    phase: &'static str,
}

#[derive(Serialize)]
pub struct ApiResult {
    status: u16,
    body: Value,
    request_id: Option<String>,
}

#[derive(Serialize)]
pub struct FileHandle {
    id: String,
    name: String,
    size: u64,
}

#[derive(Serialize, Clone)]
struct UploadProgress {
    request_id: String,
    uploaded: u64,
    total: u64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SetupInput {
    admin_id: u64,
    password: String,
    password_repeat: String,
    mode: String,
    checkout: String,
    database: String,
    storage: String,
    confirmed_stopped: bool,
    backup: String,
    recovery_password: String,
}

fn client() -> reqwest::Client {
    reqwest::Client::builder()
        .cookie_store(true)
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .connect_timeout(Duration::from_secs(5))
        .timeout(Duration::from_secs(120))
        .build()
        .expect("local HTTP client")
}

impl Bridge {
    pub fn new() -> Self {
        Self {
            phase: Arc::new(Mutex::new(Phase::Starting)),
            shutdown: Arc::new(Mutex::new(Shutdown::Idle)),
            bootstrap_pipe: Arc::new(Mutex::new(None)),
            launch_settled: Arc::new(AtomicBool::new(true)),
            setup_lock: Arc::new(tokio::sync::Mutex::new(())),
            session: Arc::new(Mutex::new(Session {
                client: client(),
                csrf: String::new(),
                generation: 0,
            })),
            login_lock: Arc::new(tokio::sync::Mutex::new(())),
            files: Arc::new(Mutex::new(HashMap::new())),
            media: Arc::new(Mutex::new(HashMap::new())),
            pending: Arc::new(Mutex::new(HashMap::new())),
        }
    }

    fn reset_session(&self, expected: Option<u64>) -> bool {
        {
            let mut session = self.session.lock().unwrap();
            if expected.is_some_and(|generation| generation != session.generation) {
                return false;
            }
            *session = Session {
                client: client(),
                csrf: String::new(),
                generation: session.generation.wrapping_add(1),
            };
        }
        self.files.lock().unwrap().clear();
        self.media.lock().unwrap().clear();
        let pending = std::mem::take(&mut *self.pending.lock().unwrap());
        for (_, cancel) in pending {
            let _ = cancel.send(());
        }
        true
    }

    fn endpoint(&self, setup: bool) -> Result<Endpoint, String> {
        if matches!(*self.shutdown.lock().unwrap(), Shutdown::Stopping) {
            return Err("Application is stopping".into());
        }
        match &*self.phase.lock().unwrap() {
            Phase::Setup(value) if setup => Ok(value.clone()),
            Phase::Ready(value) if !setup => Ok(value.clone()),
            _ => Err("Backend is not ready".into()),
        }
    }

    fn prepare_retry(&self) -> Result<(), &'static str> {
        let shutdown = self.shutdown.lock().unwrap();
        if matches!(*shutdown, Shutdown::Stopping) {
            return Err("stopping");
        }
        let mut phase = self.phase.lock().unwrap();
        if !matches!(*phase, Phase::Failed) {
            return Err("already_starting");
        }
        if !self.launch_settled.load(Ordering::SeqCst) {
            return Err("startup_unconfirmed");
        }
        *phase = Phase::Starting;
        Ok(())
    }

    pub fn start(&self, app: tauri::AppHandle) {
        let bridge = self.clone();
        std::thread::spawn(move || {
            let delays = [1, 2, 4, 8];
            let mut failures = 0;
            loop {
                let outcome = bridge.run_sidecar(failures > 0);
                if matches!(outcome, Ok(false)) {
                    return;
                }
                {
                    let shutdown = bridge.shutdown.lock().unwrap();
                    if matches!(*shutdown, Shutdown::Stopping) {
                        let mut phase = bridge.phase.lock().unwrap();
                        if matches!(*phase, Phase::Starting | Phase::Setup(_)) {
                            *phase = Phase::Failed;
                        }
                        return;
                    }
                    *bridge.phase.lock().unwrap() = Phase::Failed;
                }
                bridge.reset_session(None);
                // Hidden windows do not poll; remove signed-in content as soon
                // as the backend is lost, even before the next focus event.
                let _ = app.emit("desktop-disconnected", ());
                // Only retry a confirmed host exit, never a malformed or
                // incomplete bootstrap whose child might still be starting.
                if !matches!(outcome, Ok(true)) || failures >= delays.len() {
                    return;
                }
                std::thread::sleep(Duration::from_secs(delays[failures]));
                failures += 1;
                let shutdown = bridge.shutdown.lock().unwrap();
                if matches!(*shutdown, Shutdown::Stopping) {
                    return;
                }
                let mut phase = bridge.phase.lock().unwrap();
                if !matches!(*phase, Phase::Failed) {
                    return;
                }
                *phase = Phase::Starting;
            }
        });
    }

    pub fn prototype_root() -> Result<PathBuf, ()> {
        // The prototype is deliberately restricted to an explicitly marked
        // disposable profile until the installed build passes stage 1.
        let root = PathBuf::from(std::env::var_os("EDIZORA_PROTOTYPE_ROOT").ok_or(())?);
        if !root.is_absolute() || !root.join(".edizora-prototype").is_file() {
            return Err(());
        }
        // std::fs::canonicalize returns a verbatim \\?\ path on Windows;
        // SQLite's URI parser cannot use that spelling as a database URL.
        dunce::canonicalize(root).map_err(|_| ())
    }

    fn sidecar_command(stop: bool) -> Result<Command, ()> {
        let executable = std::env::current_exe().map_err(|_| ())?;
        let sidecar: PathBuf = executable
            .parent()
            .ok_or(())?
            .join("backend")
            .join("edizora-backend.exe");
        let root = Self::prototype_root()?;
        let mut command = Command::new(sidecar);
        command
            .args(["--publisher-module", "app.desktop.headless"])
            .env("PUBLISHER_DESKTOP_ROOT", root)
            .env("PUBLISHER_TAURI_SHELL", &executable)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x08000000); // CREATE_NO_WINDOW
        }
        if stop {
            command.arg("--stop");
        }
        Ok(command)
    }

    // Ok(true): a previously ready host was lost and may be retried.
    // Ok(false): normal supervision ended during shutdown. Err: bootstrap
    // was not proved complete; leave recovery to the user and diagnostics.
    fn run_sidecar(&self, recovering: bool) -> Result<bool, ()> {
        let nonce = random_secret();
        let token = random_secret();
        let mut command = Self::sidecar_command(false)?;
        self.launch_settled.store(false, Ordering::SeqCst);
        let mut child = match command.spawn() {
            Ok(child) => child,
            Err(_) => {
                self.launch_settled.store(true, Ordering::SeqCst);
                return Err(());
            }
        };
        let child_pid = child.id();
        let mut input = child.stdin.take().ok_or(())?;
        writeln!(
            input,
            "{}",
            json!({"protocol": 1, "nonce": nonce, "token": token})
        )
        .map_err(|_| ())?;
        input.flush().map_err(|_| ())?;
        *self.bootstrap_pipe.lock().unwrap() = Some(input);
        let output = child.stdout.take().ok_or(())?;
        let mut reader = BufReader::new(output);
        let mut line = String::new();
        let mut attached = false;
        let mut ready = false;
        let mut attached_identity = None;
        loop {
            line.clear();
            if (&mut reader)
                .take(8193)
                .read_line(&mut line)
                .map_err(|_| ())?
                == 0
            {
                break;
            }
            if line.len() > 8192 || !line.ends_with('\n') {
                return Err(());
            }
            let event: Value = serde_json::from_str(&line).map_err(|_| ())?;
            if event["protocol"] != 1 || event["nonce"] != nonce {
                return Err(());
            }
            let origin = event["origin"].as_str().ok_or(())?;
            if !valid_origin(origin) {
                return Err(());
            }
            let pid = event["pid"].as_u64().ok_or(())?;
            if pid == 0 || pid > u32::MAX as u64 {
                return Err(());
            }
            match event["phase"].as_str() {
                Some("setup") if pid == child_pid as u64 => {
                    *self.phase.lock().unwrap() = Phase::Setup(Endpoint {
                        origin: origin.into(),
                        token: token.clone(),
                    });
                }
                Some("ready") => {
                    attached = event["attached"].as_bool().ok_or(())?;
                    let received = event["token"].as_str().ok_or(())?;
                    if !valid_secret(received)
                        || (!attached && (received != token || pid != child_pid as u64))
                    {
                        return Err(());
                    }
                    for field in ["node_id", "system_id"] {
                        Uuid::parse_str(event[field].as_str().ok_or(())?).map_err(|_| ())?;
                    }
                    let endpoint = Endpoint {
                        origin: origin.into(),
                        token: received.into(),
                    };
                    if attached {
                        attached_identity = Some((
                            endpoint.clone(),
                            pid,
                            event["node_id"].as_str().ok_or(())?.to_owned(),
                            event["system_id"].as_str().ok_or(())?.to_owned(),
                        ));
                    }
                    ready = true;
                    *self.phase.lock().unwrap() = Phase::Ready(endpoint);
                }
                _ => return Err(()),
            }
        }
        self.bootstrap_pipe.lock().unwrap().take();
        let status = child.wait().map_err(|_| ())?;
        self.launch_settled.store(true, Ordering::SeqCst);
        if attached && status.success() {
            let (endpoint, pid, node_id, system_id) = attached_identity.ok_or(())?;
            self.watch_attached(endpoint, pid, node_id, system_id)
        } else if ready || recovering {
            Ok(true)
        } else {
            Err(())
        }
    }

    fn watch_attached(
        &self,
        endpoint: Endpoint,
        pid: u64,
        node_id: String,
        system_id: String,
    ) -> Result<bool, ()> {
        let http = reqwest::Client::builder()
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .timeout(Duration::from_secs(3))
            .build()
            .map_err(|_| ())?;
        let mut failures = 0;
        loop {
            std::thread::sleep(Duration::from_secs(5));
            if matches!(*self.shutdown.lock().unwrap(), Shutdown::Stopping) {
                return Ok(false);
            }
            if !matches!(&*self.phase.lock().unwrap(), Phase::Ready(current)
                if current.origin == endpoint.origin && current.token == endpoint.token)
            {
                return Ok(false);
            }
            let challenge = random_secret();
            let expected = json!({"protocol": 1, "challenge": challenge, "pid": pid,
                "origin": endpoint.origin, "node_id": node_id, "system_id": system_id});
            let verified = tauri::async_runtime::block_on(async {
                let mut response = http
                    .post(format!("{}/_host/desktop-identity", endpoint.origin))
                    .json(&json!({"challenge": challenge}))
                    .send()
                    .await
                    .map_err(|_| ())?;
                if !response.status().is_success()
                    || response.content_length().is_some_and(|n| n > 8192)
                {
                    return Err(());
                }
                let mut bytes = Vec::new();
                while let Some(chunk) = response.chunk().await.map_err(|_| ())? {
                    if bytes.len() + chunk.len() > 8192 {
                        return Err(());
                    }
                    bytes.extend_from_slice(&chunk);
                }
                let body: Value = serde_json::from_slice(&bytes).map_err(|_| ())?;
                verify_identity(body, &expected, &endpoint.token)
            })
            .is_ok();
            if verified {
                failures = 0;
            } else {
                failures += 1;
                if failures >= 3 {
                    return Ok(true);
                }
            }
        }
    }

    fn run_stopper(&self) -> Result<(), ()> {
        let nonce = random_secret();
        let token = random_secret();
        let mut child = Self::sidecar_command(true)?.spawn().map_err(|_| ())?;
        let mut input = child.stdin.take().ok_or(())?;
        writeln!(
            input,
            "{}",
            json!({"protocol": 1, "nonce": nonce, "token": token})
        )
        .map_err(|_| ())?;
        input.flush().map_err(|_| ())?;
        drop(input);
        let mut reader = BufReader::new(child.stdout.take().ok_or(())?);
        let mut line = String::new();
        let mut stopped = false;
        loop {
            line.clear();
            if (&mut reader)
                .take(8193)
                .read_line(&mut line)
                .map_err(|_| ())?
                == 0
            {
                break;
            }
            if line.len() > 8192 || !line.ends_with('\n') {
                return Err(());
            }
            let event: Value = serde_json::from_str(&line).map_err(|_| ())?;
            if event["protocol"] != 1 || event["nonce"] != nonce {
                return Err(());
            }
            match event["phase"].as_str() {
                Some("stopping") if !stopped => {}
                Some("stopped") if !stopped => stopped = true,
                _ => return Err(()),
            }
        }
        let status = child.wait().map_err(|_| ())?;
        if stopped && status.success() {
            Ok(())
        } else {
            Err(())
        }
    }

    fn quiesce_setup(endpoint: Endpoint) -> Result<bool, ()> {
        let response = tauri::async_runtime::block_on(async {
            let response = client()
                .post(format!("{}/setup-api/quiesce", endpoint.origin))
                .header("Origin", &endpoint.origin)
                .header("X-Setup-Token", &endpoint.token)
                .send()
                .await
                .map_err(|_| ())?;
            result(response).await.map_err(|_| ())
        })?;
        if response.status != 200 {
            return Err(());
        }
        response
            .body
            .get("completed")
            .and_then(Value::as_bool)
            .ok_or(())
    }

    pub fn request_shutdown(&self, app: tauri::AppHandle) {
        {
            let mut state = self.shutdown.lock().unwrap();
            if matches!(*state, Shutdown::Stopping) {
                return;
            }
            *state = Shutdown::Stopping;
        }
        let _ = app.emit("desktop-shutdown", "stopping");
        let bridge = self.clone();
        std::thread::spawn(move || {
            // Serialize with the renderer request, then ask the setup server
            // itself to finish any in-flight initialization and refuse late
            // requests. A lost HTTP response must never mean "not started".
            let _setup_guard = bridge.setup_lock.blocking_lock();
            let mut waiting_for_host = false;
            loop {
                let action = match &*bridge.phase.lock().unwrap() {
                    Phase::Starting => (0, None),
                    Phase::Setup(_) if waiting_for_host => (0, None),
                    Phase::Setup(endpoint) => (1, Some(endpoint.clone())),
                    Phase::Ready(_) => (2, None),
                    Phase::Failed if bridge.launch_settled.load(Ordering::SeqCst) => (2, None),
                    Phase::Failed => (3, None),
                };
                match action.0 {
                    0 => std::thread::sleep(Duration::from_millis(100)),
                    1 => {
                        match Self::quiesce_setup(action.1.unwrap()) {
                            Ok(true) => waiting_for_host = true,
                            Ok(false) => {
                                // The server has refused late setup requests;
                                // EOF now safely ends the unfinished wizard.
                                bridge.bootstrap_pipe.lock().unwrap().take();
                                break;
                            }
                            Err(()) => {
                                if matches!(*bridge.phase.lock().unwrap(), Phase::Ready(_)) {
                                    continue;
                                }
                                *bridge.shutdown.lock().unwrap() = Shutdown::Failed;
                                let _ = app.emit("desktop-shutdown", "failed");
                                return;
                            }
                        }
                    }
                    2 => break,
                    _ => {
                        *bridge.shutdown.lock().unwrap() = Shutdown::Failed;
                        let _ = app.emit("desktop-shutdown", "failed");
                        return;
                    }
                }
            }
            if bridge.run_stopper().is_ok() {
                bridge.reset_session(None);
                app.exit(0);
            } else {
                *bridge.shutdown.lock().unwrap() = Shutdown::Failed;
                let _ = app.emit("desktop-shutdown", "failed");
            }
        });
    }
}

fn random_secret() -> String {
    format!("{}{}", Uuid::new_v4().simple(), Uuid::new_v4().simple())
}

fn verify_identity(mut actual: Value, expected: &Value, token: &str) -> Result<(), ()> {
    let proof = actual
        .as_object_mut()
        .ok_or(())?
        .remove("proof")
        .ok_or(())?;
    let proof = hex::decode(proof.as_str().ok_or(())?).map_err(|_| ())?;
    if actual != *expected {
        return Err(());
    }
    let encoded = serde_json::to_vec(expected).map_err(|_| ())?;
    let mut mac = Hmac::<Sha256>::new_from_slice(token.as_bytes()).map_err(|_| ())?;
    mac.update(&encoded);
    mac.verify_slice(&proof).map_err(|_| ())
}

fn valid_secret(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
}

fn valid_origin(origin: &str) -> bool {
    let Ok(url) = url::Url::parse(origin) else {
        return false;
    };
    url.scheme() == "http"
        && url.host_str() == Some("127.0.0.1")
        && url.port().is_some()
        && url.username().is_empty()
        && url.password().is_none()
        && url.path() == "/"
        && url.query().is_none()
        && url.fragment().is_none()
        && origin == format!("http://127.0.0.1:{}", url.port().unwrap())
}

fn allowed_route(method: &str, value: &str) -> bool {
    if value.len() > 8192
        || value.contains(['#', '\\'])
        || value.bytes().any(|b| b.is_ascii_control())
    {
        return false;
    }
    let path = value.split('?').next().unwrap_or("");
    if !path.starts_with("/api/v1/")
        || path.contains("//")
        || !path
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"/-_".contains(&c))
        || path.split('/').any(|part| part == "." || part == "..")
    {
        return false;
    }
    // Binary files, setup and lifecycle use dedicated commands, not this proxy.
    if path == "/api/v1/assets/uploads"
        || path.starts_with("/api/v1/auth/telegram")
        || path.starts_with("/api/v1/auth/local-exchange")
        || path.ends_with("/content")
        || path.ends_with("/thumbnail")
    {
        return false;
    }
    let extra = [
        ("GET", "/api/v1/desktop/status"),
        ("GET", "/api/v1/desktop/report"),
        ("POST", "/api/v1/desktop/ai/test"),
        ("POST", "/api/v1/desktop/ai"),
        ("POST", "/api/v1/desktop/telegram"),
        ("POST", "/api/v1/desktop/autostart"),
    ];
    if extra.contains(&(method, path)) {
        return true;
    }
    let contract =
        PARSED_CONTRACT.get_or_init(|| serde_json::from_str(CONTRACT).unwrap_or(Value::Null));
    let Some(paths) = contract["paths"].as_object() else {
        return false;
    };
    paths.iter().any(|(template, operations)| {
        let actual: Vec<_> = path.split('/').collect();
        let expected: Vec<_> = template.split('/').collect();
        actual.len() == expected.len()
            && actual.iter().zip(expected.iter()).all(|(a, e)| {
                !a.is_empty() && ((e.starts_with('{') && e.ends_with('}')) || a == e)
                    || (a.is_empty() && e.is_empty())
            })
            && operations.get(method.to_ascii_lowercase()).is_some()
    })
}

fn valid_media_path(path: &str) -> bool {
    let parts: Vec<_> = path.split('/').collect();
    parts.len() == 6
        && parts[..4] == ["", "api", "v1", "assets"]
        && !parts[4].is_empty()
        && parts[4]
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"-_".contains(&c))
        && ["content", "thumbnail"].contains(&parts[5])
}

async fn result(mut response: reqwest::Response) -> Result<ApiResult, String> {
    let status = response.status().as_u16();
    let request_id = response
        .headers()
        .get("x-request-id")
        .and_then(|v| v.to_str().ok())
        .map(str::to_string);
    let mut bytes = Vec::new();
    while let Some(chunk) = response
        .chunk()
        .await
        .map_err(|_| "Backend response interrupted")?
    {
        if bytes.len() + chunk.len() > MAX_JSON {
            return Err("Backend response is too large".into());
        }
        bytes.extend_from_slice(&chunk);
    }
    let body = if bytes.is_empty() {
        Value::Null
    } else {
        serde_json::from_slice(&bytes).map_err(|_| "Invalid backend response")?
    };
    Ok(ApiResult {
        status,
        body,
        request_id,
    })
}

#[tauri::command]
pub fn desktop_status(bridge: tauri::State<'_, Bridge>) -> Status {
    let phase = match &*bridge.phase.lock().unwrap() {
        Phase::Starting => "starting",
        Phase::Setup(_) => "setup",
        Phase::Ready(_) => "ready",
        Phase::Failed => "failed",
    };
    let signed_in = !bridge.session.lock().unwrap().csrf.is_empty();
    Status { phase, signed_in }
}

#[tauri::command]
pub fn desktop_retry(
    window: tauri::WebviewWindow,
    bridge: tauri::State<'_, Bridge>,
) -> Result<(), String> {
    // Moving to Starting while holding the shutdown lock makes shutdown wait
    // for this attempt instead of racing a late process launch.
    bridge.prepare_retry().map_err(str::to_owned)?;
    bridge.start(window.app_handle().clone());
    Ok(())
}

#[tauri::command]
pub fn desktop_shutdown(app: tauri::AppHandle, bridge: tauri::State<'_, Bridge>) {
    bridge.request_shutdown(app);
}

#[tauri::command]
pub fn desktop_shutdown_status(bridge: tauri::State<'_, Bridge>) -> ShutdownStatus {
    let phase = match &*bridge.shutdown.lock().unwrap() {
        Shutdown::Idle => "idle",
        Shutdown::Stopping => "stopping",
        Shutdown::Failed => "failed",
    };
    ShutdownStatus { phase }
}

#[tauri::command]
pub fn desktop_hide(window: tauri::WebviewWindow) -> Result<(), String> {
    if window.label() != "main" {
        return Err("Window is not permitted".into());
    }
    window.hide().map_err(|_| "Could not hide the window")?;
    let _ = window.emit("desktop-visibility", false);
    Ok(())
}

#[tauri::command]
pub fn desktop_is_visible(window: tauri::WebviewWindow) -> Result<bool, String> {
    if window.label() != "main" {
        return Err("Window is not permitted".into());
    }
    let visible = window
        .is_visible()
        .map_err(|_| "Could not inspect the window")?;
    let minimized = window
        .is_minimized()
        .map_err(|_| "Could not inspect the window")?;
    Ok(visible && !minimized)
}

#[tauri::command]
pub async fn desktop_setup(
    bridge: tauri::State<'_, Bridge>,
    input: SetupInput,
) -> Result<ApiResult, String> {
    let _setup_guard = bridge.setup_lock.lock().await;
    if input.admin_id == 0
        || input.admin_id >= i64::MAX as u64
        || input.password.len() < 12
        || input.password.len() > 1024
        || input.password != input.password_repeat
        || !["new", "legacy", "restore"].contains(&input.mode.as_str())
        || [
            &input.checkout,
            &input.database,
            &input.storage,
            &input.backup,
        ]
        .iter()
        .any(|p| p.len() > 4096)
        || input.recovery_password.len() > 1024
    {
        return Err("Invalid setup fields".into());
    }
    let endpoint = bridge.endpoint(true)?;
    let body = json!({"admin_id":input.admin_id,"password":input.password,"password_repeat":input.password_repeat,
        "mode":input.mode,"checkout":input.checkout,"database":input.database,"storage":input.storage,
        "confirmed_stopped":input.confirmed_stopped,"backup":input.backup,"recovery_password":input.recovery_password});
    let response = client()
        .post(format!("{}/setup-api/initialize", endpoint.origin))
        .header("Origin", &endpoint.origin)
        .header("X-Setup-Token", &endpoint.token)
        .json(&body)
        .send()
        .await
        .map_err(|_| "Backend unavailable")?;
    result(response).await
}

#[tauri::command]
pub async fn desktop_request(
    window: tauri::WebviewWindow,
    bridge: tauri::State<'_, Bridge>,
    method: String,
    path: String,
    body: Option<Value>,
    key: Option<String>,
) -> Result<ApiResult, String> {
    if !allowed_route(&method, &path)
        || body
            .as_ref()
            .is_some_and(|v| v.to_string().len() > MAX_JSON)
        || key.as_ref().is_some_and(|k| Uuid::parse_str(k).is_err())
    {
        return Err("Request is not permitted".into());
    }
    let endpoint = bridge.endpoint(false)?;
    let login = path == "/api/v1/auth/password" && method == "POST";
    let logout = path == "/api/v1/auth/logout" && method == "POST";
    let _login_guard = if login {
        Some(bridge.login_lock.lock().await)
    } else {
        None
    };
    if login {
        if !bridge.session.lock().unwrap().csrf.is_empty() {
            return Err("Sign out before changing accounts".into());
        }
        bridge.reset_session(None);
    }
    let (http, csrf, generation) = {
        let session = bridge.session.lock().unwrap();
        (
            session.client.clone(),
            session.csrf.clone(),
            session.generation,
        )
    };
    let parsed_method = method
        .parse::<reqwest::Method>()
        .map_err(|_| "Invalid method")?;
    let mut request = http
        .request(parsed_method, format!("{}{}", endpoint.origin, path))
        .header("Accept", "application/json")
        .header("Origin", &endpoint.origin)
        .header("X-Edizora-Bridge", &endpoint.token);
    if !csrf.is_empty() {
        request = request.header("X-CSRF-Token", &csrf);
    }
    if let Some(key) = key {
        request = request.header("Idempotency-Key", key);
    }
    if let Some(body) = body {
        request = request.json(&body);
    }
    let sent = request.send().await;
    if logout && bridge.reset_session(Some(generation)) {
        reload_after_session_end(window.clone());
    }
    let mut response = result(sent.map_err(|_| "Backend unavailable")?).await?;
    if response.status == 401
        && !login
        && !logout
        && !csrf.is_empty()
        && bridge.reset_session(Some(generation))
    {
        reload_after_session_end(window);
    }
    if (login || path == "/api/v1/auth/session") && response.status < 300 {
        if let Some(csrf) = response.body.get("csrf_token").and_then(Value::as_str) {
            let mut session = bridge.session.lock().unwrap();
            if session.generation == generation {
                session.csrf = csrf.into();
            }
        }
    }
    if let Some(object) = response.body.as_object_mut() {
        object.remove("csrf_token");
        object.remove("token");
        object.remove("access_token");
    }
    Ok(response)
}

fn reload_after_session_end(window: tauri::WebviewWindow) {
    // WebView2 can retain decoded images in its document even with no-store.
    // Replacing the document releases that in-memory cache after the IPC reply;
    // persistent UI preferences are not deleted.
    tauri::async_runtime::spawn(async move {
        tokio::time::sleep(Duration::from_millis(150)).await;
        let _ = window.reload();
    });
}

#[tauri::command]
pub async fn desktop_pick_file(
    bridge: tauri::State<'_, Bridge>,
) -> Result<Option<FileHandle>, String> {
    Ok(select_files(bridge.inner(), false)
        .await?
        .into_iter()
        .next())
}

#[tauri::command]
pub async fn desktop_pick_files(
    bridge: tauri::State<'_, Bridge>,
) -> Result<Vec<FileHandle>, String> {
    select_files(bridge.inner(), true).await
}

async fn select_files(bridge: &Bridge, multiple: bool) -> Result<Vec<FileHandle>, String> {
    bridge.endpoint(false)?;
    let generation = {
        let session = bridge.session.lock().unwrap();
        if session.csrf.is_empty() {
            return Err("Sign in to select files".into());
        }
        session.generation
    };
    let paths = tauri::async_runtime::spawn_blocking(move || {
        if multiple {
            rfd::FileDialog::new().pick_files().unwrap_or_default()
        } else {
            rfd::FileDialog::new().pick_file().into_iter().collect()
        }
    })
    .await
    .map_err(|_| "File selection failed")?;
    if paths.len() > 32 {
        return Err("Too many selected files".into());
    }
    let mut selected = Vec::with_capacity(paths.len());
    for path in paths {
        let file = File::open(&path).map_err(|_| "Could not open file")?;
        let metadata = file.metadata().map_err(|_| "Could not read file")?;
        if !metadata.is_file() || metadata.len() == 0 || metadata.len() > MAX_FILE {
            return Err("Invalid file size".into());
        }
        let name = path
            .file_name()
            .and_then(|v| v.to_str())
            .ok_or("Invalid file name")?
            .to_string();
        if name.len() > 255 || name.chars().any(char::is_control) {
            return Err("Invalid file name".into());
        }
        selected.push((Uuid::new_v4().to_string(), file, name, metadata.len()));
    }
    let session = bridge.session.lock().unwrap();
    if session.generation != generation || session.csrf.is_empty() {
        return Err("Session changed during file selection".into());
    }
    let mut files = bridge.files.lock().unwrap();
    if files.len() + selected.len() > 32 {
        return Err("Too many selected files".into());
    }
    let mut handles = Vec::with_capacity(selected.len());
    for (id, file, name, size) in selected {
        files.insert(
            id.clone(),
            PickedFile {
                file,
                name: name.clone(),
                size,
            },
        );
        handles.push(FileHandle { id, name, size });
    }
    Ok(handles)
}

#[tauri::command]
pub fn desktop_release_files(
    bridge: tauri::State<'_, Bridge>,
    ids: Vec<String>,
) -> Result<(), String> {
    if ids.len() > 32 || ids.iter().any(|id| Uuid::parse_str(id).is_err()) {
        return Err("Invalid file handles".into());
    }
    let mut files = bridge.files.lock().unwrap();
    for id in ids {
        files.remove(&id);
    }
    Ok(())
}

#[tauri::command]
pub async fn desktop_upload(
    window: tauri::WebviewWindow,
    bridge: tauri::State<'_, Bridge>,
    id: String,
    request_id: String,
) -> Result<ApiResult, String> {
    use futures_util::StreamExt;
    Uuid::parse_str(&request_id).map_err(|_| "Invalid request identifier")?;
    let endpoint = bridge.endpoint(false)?;
    let picked = bridge
        .files
        .lock()
        .unwrap()
        .remove(&id)
        .ok_or("File handle expired")?;
    let (http, csrf) = {
        let session = bridge.session.lock().unwrap();
        (session.client.clone(), session.csrf.clone())
    };
    let total = picked.size;
    let mut uploaded = 0;
    let progress_id = request_id.clone();
    let stream = tokio_util::io::ReaderStream::with_capacity(
        tokio::fs::File::from_std(picked.file),
        256 * 1024,
    )
    .map(move |chunk| {
        if let Ok(bytes) = &chunk {
            uploaded += bytes.len() as u64;
            let _ = window.emit(
                "desktop-upload-progress",
                UploadProgress {
                    request_id: progress_id.clone(),
                    uploaded,
                    total,
                },
            );
        }
        chunk
    });
    let part =
        reqwest::multipart::Part::stream_with_length(reqwest::Body::wrap_stream(stream), total)
            .file_name(picked.name);
    let form = reqwest::multipart::Form::new().part("file", part);
    let (cancel, cancelled) = tokio::sync::oneshot::channel();
    {
        let mut pending = bridge.pending.lock().unwrap();
        if pending.contains_key(&request_id) {
            return Err("Request already exists".into());
        }
        pending.insert(request_id.clone(), cancel);
    }
    let operation = http
        .post(format!("{}/api/v1/assets/uploads", endpoint.origin))
        .header("Origin", &endpoint.origin)
        .header("X-Edizora-Bridge", &endpoint.token)
        .header("X-CSRF-Token", csrf)
        .header("Idempotency-Key", Uuid::new_v4().to_string())
        .multipart(form)
        .send();
    let response = tokio::select! {
        response = operation => response.map_err(|_| "Backend unavailable".to_string()),
        _ = cancelled => Err("Upload cancelled; the server may still finish processing it".to_string()),
    };
    bridge.pending.lock().unwrap().remove(&request_id);
    result(response?).await
}

#[tauri::command]
pub fn desktop_cancel(bridge: tauri::State<'_, Bridge>, request_id: String) {
    if let Some(cancel) = bridge.pending.lock().unwrap().remove(&request_id) {
        let _ = cancel.send(());
    }
}

#[tauri::command]
pub fn desktop_media(bridge: tauri::State<'_, Bridge>, path: String) -> Result<String, String> {
    bridge.endpoint(false)?;
    if bridge.session.lock().unwrap().csrf.is_empty() {
        return Err("Sign in to view media".into());
    }
    if !valid_media_path(&path) {
        return Err("Invalid media reference".into());
    }
    let mut media = bridge.media.lock().unwrap();
    if media.len() >= 512 {
        return Err("Too many media references".into());
    }
    let id = Uuid::new_v4().to_string();
    media.insert(id.clone(), path);
    Ok(format!("http://edizora-media.localhost/{id}"))
}

#[tauri::command]
pub fn desktop_revoke_media(bridge: tauri::State<'_, Bridge>, url: String) {
    if let Some(id) = url.strip_prefix("http://edizora-media.localhost/") {
        bridge.media.lock().unwrap().remove(id);
    }
}

pub async fn media_response(bridge: Bridge, id: &str) -> tauri::http::Response<Vec<u8>> {
    async fn fetch(bridge: &Bridge, id: &str) -> Result<(String, Vec<u8>), ()> {
        Uuid::parse_str(id).map_err(|_| ())?;
        let path = bridge.media.lock().unwrap().get(id).cloned().ok_or(())?;
        let endpoint = bridge.endpoint(false).map_err(|_| ())?;
        let http = bridge.session.lock().unwrap().client.clone();
        let mut response = http
            .get(format!("{}{}", endpoint.origin, path))
            .header("X-Edizora-Bridge", endpoint.token)
            .send()
            .await
            .map_err(|_| ())?;
        if !response.status().is_success() {
            return Err(());
        }
        let mime = response
            .headers()
            .get("content-type")
            .and_then(|v| v.to_str().ok())
            .unwrap_or("application/octet-stream")
            .to_string();
        let mut bytes = Vec::new();
        while let Some(chunk) = response.chunk().await.map_err(|_| ())? {
            if bytes.len() + chunk.len() > MAX_FILE as usize {
                return Err(());
            }
            bytes.extend_from_slice(&chunk);
        }
        // Logout can happen during transfer; do not release a revoked resource.
        if !bridge.media.lock().unwrap().contains_key(id) {
            return Err(());
        }
        Ok((mime, bytes))
    }
    match fetch(&bridge, id).await {
        Ok((mime, bytes)) => tauri::http::Response::builder()
            .status(200)
            .header("Content-Type", mime)
            .header(
                "Cache-Control",
                "private, no-store, no-cache, max-age=0, must-revalidate",
            )
            .header("Pragma", "no-cache")
            .header("Expires", "0")
            .header("Vary", "*")
            .header("X-Content-Type-Options", "nosniff")
            .body(bytes)
            .unwrap(),
        Err(()) => tauri::http::Response::builder()
            .status(404)
            .header("Cache-Control", "no-store")
            .body(Vec::new())
            .unwrap(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn restrict_routes_and_origins() {
        assert!(allowed_route("GET", "/api/v1/publications?search=abc"));
        assert!(allowed_route("POST", "/api/v1/auth/password"));
        for path in [
            "/_host/stop",
            "/api/v1/../_host/stop",
            "/api/v1/%2e%2e/x",
            "/api/v1/assets/uploads",
            "http://evil.test/api/v1/channels",
            "/api/v1/channels#x",
        ] {
            assert!(!allowed_route("GET", path), "{path}");
        }
        assert!(!allowed_route("POST", "/api/v1/channels/1/statistics"));
        assert!(valid_media_path("/api/v1/assets/123/thumbnail"));
        assert!(!valid_media_path("/api/v1/assets/../content"));
        assert!(valid_origin("http://127.0.0.1:12345"));
        assert!(!valid_origin("http://localhost:12345"));
    }

    #[test]
    fn stale_session_response_cannot_revoke_a_new_session() {
        let bridge = Bridge::new();
        bridge.session.lock().unwrap().csrf = "first".into();
        assert!(bridge.reset_session(Some(0)));
        bridge.session.lock().unwrap().csrf = "second".into();
        assert!(!bridge.reset_session(Some(0)));
        assert_eq!(bridge.session.lock().unwrap().csrf, "second");
        assert!(bridge.reset_session(Some(1)));
        assert!(bridge.session.lock().unwrap().csrf.is_empty());
    }

    #[test]
    fn retry_requires_a_settled_previous_launch() {
        let bridge = Bridge::new();
        assert_eq!(bridge.prepare_retry(), Err("already_starting"));
        *bridge.phase.lock().unwrap() = Phase::Failed;
        bridge.launch_settled.store(false, Ordering::SeqCst);
        assert_eq!(bridge.prepare_retry(), Err("startup_unconfirmed"));
        bridge.launch_settled.store(true, Ordering::SeqCst);
        assert_eq!(bridge.prepare_retry(), Ok(()));
        assert_eq!(bridge.prepare_retry(), Err("already_starting"));
        *bridge.shutdown.lock().unwrap() = Shutdown::Stopping;
        assert_eq!(bridge.prepare_retry(), Err("stopping"));
    }

    #[test]
    fn attached_host_proof_is_bound_to_challenge_and_installation() {
        let expected = json!({"protocol":1, "challenge":"b".repeat(64), "pid":123,
            "origin":"http://127.0.0.1:456", "node_id":"00000000-0000-0000-0000-000000000001",
            "system_id":"00000000-0000-0000-0000-000000000002"});
        let mut actual = expected.clone();
        actual["proof"] = json!("7b6d358c61c0a919b7a01e36f32163c519fec296715adf99d5bbf2f3e71a3a70");
        assert!(verify_identity(actual.clone(), &expected, &"a".repeat(64)).is_ok());
        assert!(verify_identity(actual.clone(), &expected, &"c".repeat(64)).is_err());
        actual["pid"] = json!(124);
        assert!(verify_identity(actual, &expected, &"a".repeat(64)).is_err());
    }
}
