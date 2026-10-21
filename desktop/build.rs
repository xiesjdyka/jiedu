fn main() {
    let root = std::path::PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap());
    let release = std::fs::read_to_string(root.join("../VERSION")).expect("VERSION required");
    let release = release.trim();
    let config: serde_json::Value = serde_json::from_slice(
        &std::fs::read(root.join("tauri.conf.json")).expect("Tauri configuration required")
    ).expect("Invalid Tauri configuration");
    assert_eq!(std::env::var("CARGO_PKG_VERSION").unwrap(), release, "Cargo version differs from VERSION");
    assert_eq!(config["version"].as_str(), Some(release), "Tauri version differs from VERSION");
    let (base, revision) = release.split_once("-rc.").unwrap_or((release, "0"));
    let components: Vec<u16> = base.split('.').map(|v| v.parse().expect("Invalid VERSION component")).collect();
    assert_eq!(components.len(), 3, "VERSION requires three numeric components");
    let revision: u16 = revision.parse().expect("Invalid VERSION revision");
    println!("cargo:rerun-if-changed=../VERSION");
    println!("cargo:rerun-if-changed=icons/icon.ico");
    println!("cargo:rerun-if-changed=icons/tray.rgba");
    println!("cargo:rerun-if-changed=icons/window.rgba");
    println!("cargo:rerun-if-changed=ui");
    println!("cargo:rustc-env=JIEDU_RELEASE_VERSION={release}");
    println!("cargo:rustc-env=JIEDU_WINDOWS_FILE_VERSION={}.{}.{}.{revision}", components[0], components[1], components[2]);
    tauri_build::build();
}
