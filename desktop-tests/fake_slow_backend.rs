//! Deliberately slow, non-publishing backend for the native startup test only.
//! It never opens data, networking, a database, or a Telegram connection.
use std::{env, fs::OpenOptions, io, io::BufRead, path::PathBuf, thread, time::Duration};

fn main() {
    std::hint::black_box("EDIZORA_FAKE_SLOW_BACKEND_V1");
    let mut line = String::new();
    if io::stdin().lock().read_line(&mut line).is_err() {
        return;
    }
    let Some(index) = line.find("\"nonce\":\"") else {
        return;
    };
    let start = index + "\"nonce\":\"".len();
    let Some(nonce) = line.get(start..start + 64) else {
        return;
    };
    if !nonce.bytes().all(|c| c.is_ascii_hexdigit()) {
        return;
    }
    if env::args().any(|arg| arg == "--stop") {
        println!("{{\"protocol\":1,\"nonce\":\"{nonce}\",\"phase\":\"stopped\"}}");
        return;
    }
    let Some(root) = env::var_os("EDIZORA_PROTOTYPE_ROOT") else {
        return;
    };
    let marker: PathBuf = PathBuf::from(root).join(".fake-slow-backend-seen");
    let first = OpenOptions::new().write(true).create_new(true).open(marker).is_ok();
    thread::sleep(Duration::from_secs(if first { 40 } else { 2 }));
}
