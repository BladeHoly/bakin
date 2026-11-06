//! The tray's and the notifications' texts in the interface's language
//! (PLAN 0.22.3). Spanish is the source, as in the page (desktop/ui/i18n.js):
//! `tr` gives the English text when the language is English. The choice is
//! the engine's `lang` setting (auto | es | en); "auto" follows Windows' display
//! language. The tray is built once, so a change shows there from the next start.

use serde_json::Value;

/// English or not: read from the settings file each time (the tray is built once and
/// notifications are rare, so there's nothing to cache).
pub fn english() -> bool {
    let file = crate::player::data_dir().join("ui-settings.json");
    let pref = std::fs::read_to_string(file)
        .ok()
        .and_then(|t| serde_json::from_str::<Value>(&t).ok())
        .and_then(|v| v["lang"].as_str().map(str::to_string))
        .unwrap_or_default();
    match pref.as_str() {
        "en" => true,
        "auto" => !system_spanish(),
        _ => false, // "es", or not chosen yet: Spanish, as Bakin always was
    }
}

#[cfg(windows)]
fn system_spanish() -> bool {
    #[link(name = "kernel32")]
    extern "system" {
        fn GetUserDefaultUILanguage() -> u16;
    }
    // the primary language is the low 10 bits; LANG_SPANISH = 0x0a
    // SAFETY: a plain query without arguments or pointers
    unsafe { GetUserDefaultUILanguage() & 0x3ff == 0x0a }
}

#[cfg(not(windows))]
fn system_spanish() -> bool {
    std::env::var("LANG").map(|l| l.starts_with("es")).unwrap_or(true)
}

/// `es` in English when the interface is in English (and the text is known).
pub fn tr(es: &'static str) -> &'static str {
    if english() { english_of(es) } else { es }
}

fn english_of(es: &'static str) -> &'static str {
    match es {
        "Abrir Bakin" => "Open Bakin",
        "Modo mínimo" => "Mini player",
        "Modo mínimo: usar el mouse" => "Mini player: use the mouse",
        "Pausar descargas" => "Pause downloads",
        "Reanudar descargas" => "Resume downloads",
        "Siguiente fondo" => "Next wallpaper",
        "Pausar la rotación de fondos" => "Pause wallpaper rotation",
        "Reanudar la rotación de fondos" => "Resume wallpaper rotation",
        "Salir de Bakin" => "Quit Bakin",
        "Bakin terminó" => "Bakin is done",
        "La cola quedó vacía: todo lo pendiente ya se procesó." => "The queue is empty: everything pending has been processed.",
        "Bakin pausó la cola" => "Bakin paused the queue",
        "Bakin: cookies vencidas" => "Bakin: cookies expired",
        "Exporta un cookies.txt nuevo para los temas que piden tu cuenta." => "Export a new cookies.txt for the songs that need your account.",
        _ => es,
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn unknown_texts_stay_spanish() {
        assert_eq!(super::english_of("Abrir Bakin"), "Open Bakin");
        assert_eq!(super::english_of("algo nuevo"), "algo nuevo");
    }
}
