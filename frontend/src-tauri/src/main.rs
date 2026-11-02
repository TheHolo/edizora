#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod bridge;
mod window_state;

use bridge::Bridge;
use sha2::{Digest, Sha256};
use std::path::Path;
use tauri::{
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    Emitter, Manager, WebviewUrl, WebviewWindowBuilder, WindowEvent,
};

fn show_main(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        if window.show().is_ok() {
            let _ = window.unminimize();
            let visible =
                window.is_visible().unwrap_or(false) && !window.is_minimized().unwrap_or(true);
            let _ = window.emit("desktop-visibility", visible);
            let _ = window.set_focus();
        }
    }
}

fn start_visible(background: bool, configured: bool) -> bool {
    !background || !configured
}

fn profile_identifier(root: Option<&Path>) -> String {
    let Some(root) = root else {
        return "com.edizora.prototype.invalid".into();
    };
    // The canonical profile path is never exposed to another process; the
    // digest scopes the single-instance mutex to this installation.
    let normalized = root.to_string_lossy().to_lowercase();
    let digest = Sha256::digest(normalized.as_bytes());
    format!("com.edizora.prototype.p{}", hex::encode(&digest[..16]))
}

fn main() {
    let mut context = tauri::generate_context!();
    context.config_mut().identifier = profile_identifier(Bridge::prototype_root().ok().as_deref());
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, arguments, _| {
            if !arguments.iter().skip(1).any(|arg| arg == "--background") {
                show_main(app);
            }
        }))
        .manage(Bridge::new())
        .invoke_handler(tauri::generate_handler![
            bridge::desktop_status,
            bridge::desktop_retry,
            bridge::desktop_shutdown,
            bridge::desktop_shutdown_status,
            bridge::desktop_hide,
            bridge::desktop_is_visible,
            bridge::desktop_setup,
            bridge::desktop_request,
            bridge::desktop_pick_file,
            bridge::desktop_pick_files,
            bridge::desktop_release_files,
            bridge::desktop_upload,
            bridge::desktop_cancel,
            bridge::desktop_media,
            bridge::desktop_revoke_media,
        ])
        .register_asynchronous_uri_scheme_protocol(
            "edizora-media",
            |context, request, responder| {
                if context.webview_label() != "main" || request.method() != tauri::http::Method::GET
                {
                    responder.respond(
                        tauri::http::Response::builder()
                            .status(403)
                            .body(Vec::new())
                            .unwrap(),
                    );
                    return;
                }
                let bridge = context.app_handle().state::<Bridge>().inner().clone();
                let id = request.uri().path().trim_start_matches('/').to_string();
                tauri::async_runtime::spawn(async move {
                    responder.respond(bridge::media_response(bridge, &id).await);
                });
            },
        )
        .on_window_event(|window, event| {
            if window.label() == "main" {
                if let WindowEvent::CloseRequested { api, .. } = event {
                    api.prevent_close();
                    let _ = window.emit("desktop-close-request", ());
                } else if matches!(event, WindowEvent::Resized(_)) {
                    if let (Ok(visible), Ok(minimized)) =
                        (window.is_visible(), window.is_minimized())
                    {
                        let _ = window.emit("desktop-visibility", visible && !minimized);
                    }
                }
            }
        })
        .setup(|app| {
            let open = MenuItem::with_id(app, "open", "Открыть Edizora", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "Выйти и остановить", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&open, &quit])?;
            let icon = app
                .default_window_icon()
                .cloned()
                .ok_or_else(|| std::io::Error::other("The Edizora tray icon is unavailable"))?;
            TrayIconBuilder::with_id("edizora")
                .icon(icon)
                .tooltip("Edizora — тестовая сборка")
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(|app, event| match event.id().as_ref() {
                    "open" => show_main(app),
                    "quit" => {
                        show_main(app);
                        app.state::<Bridge>().request_shutdown(app.clone());
                    }
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if matches!(
                        event,
                        TrayIconEvent::Click {
                            button: MouseButton::Left,
                            button_state: MouseButtonState::Up,
                            ..
                        }
                    ) {
                        show_main(tray.app_handle());
                    }
                })
                .build(app)?;
            // Start the isolated backend while WebView2 creates its window.
            // Both are needed before the UI can work, but neither depends on
            // the other; serializing them adds the full WebView startup cost.
            app.state::<Bridge>().start(app.handle().clone());
            let root = Bridge::prototype_root().ok();
            let background = std::env::args_os().skip(1).any(|arg| arg == "--background");
            let configured = root
                .as_ref()
                .is_some_and(|path| path.join("control/node.json").is_file());
            let mut window = WebviewWindowBuilder::new(
                app,
                "main",
                WebviewUrl::App("desktop-prototype.html".into()),
            )
            .title("Edizora — prototype")
            .visible(start_visible(background, configured))
            .inner_size(1280.0, 800.0)
            .min_inner_size(840.0, 600.0)
            .on_navigation(|url| {
                url.scheme() == "http" && url.host_str() == Some("tauri.localhost")
            })
            .on_new_window(|_, _| tauri::webview::NewWindowResponse::Deny);
            if let Some(root) = &root {
                // Isolate the WebView's cache and preferences along with the
                // explicitly marked disposable backend profile.
                window = window.data_directory(root.join("webview"));
            }
            let window = window.build()?;
            if let Some(root) = root {
                window_state::install(window, root);
            }
            Ok(())
        })
        .run(context)
        .expect("failed to start Edizora desktop shell");
}

#[cfg(test)]
mod tests {
    use super::{profile_identifier, start_visible};
    use std::path::Path;

    #[test]
    fn background_start_shows_a_new_installation_but_hides_an_existing_one() {
        assert!(start_visible(true, false));
        assert!(!start_visible(true, true));
        assert!(start_visible(false, false));
        assert!(start_visible(false, true));
    }

    #[test]
    fn instance_names_are_stable_per_profile() {
        assert_eq!(
            profile_identifier(Some(Path::new("C:\\Fixture"))),
            profile_identifier(Some(Path::new("c:\\fixture")))
        );
        assert_ne!(
            profile_identifier(Some(Path::new("C:\\fixture-a"))),
            profile_identifier(Some(Path::new("C:\\fixture-b")))
        );
        assert_ne!(
            profile_identifier(None),
            profile_identifier(Some(Path::new("C:\\fixture")))
        );
    }
}
