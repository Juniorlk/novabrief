// A console window would flash on every launch of a tray application, and the
// app starts with Windows (EF-10). Release only: a console is where `cargo run`
// prints, and giving that up in development costs more than it saves.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    novabrief_desktop_lib::run();
}
